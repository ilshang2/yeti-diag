"""Unattended drive logger: start it before setting off, drive, stop it when parked.

Reads only. No VCDS needed. The driver does not touch the laptop: it polls engine
OBD values often (stable while moving) and a few TP2.0 groups less often, and reads
the misfire counters and emission readiness once at the start and once at the end so
they can be compared. 09 is never contacted (it refuses diagnostics while moving, D7).

One serial connection is held for the whole drive (reopening Bluetooth repeatedly is
what kept dropping tonight). Each sample is one JSON line in reports/<stamp>_drive.jsonl.

Only the engine (01) is contacted by default. Do NOT pass --tp20-every while driving:
reading 03 ABS made the ABS/ESC lights flash with a chime (2026-11-06).

Stop early with Ctrl+C, or (from another window / remotely) create reports/STOP_DRIVE:
the logger then reads the after-drive health values before it exits.

usage: py -3.13 tools/drive_log.py --elm COM10 [--seconds 4200]
"""
import argparse, ctypes, datetime, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".deps"), str(ROOT)]
from yeti_diag import cli, measure, uds  # noqa: E402
from yeti_diag.vehicle import UDS_MODULES  # noqa: E402

# TP2.0 groups sampled less often: 03 ABS steering/accel/brake/wheels; 02 clutch & gear.
TP20_GROUPS = {"03": [1, 4, 5, 6], "02": [8, 70, 75, 98, 118]}
ES = 0x80000000 | 0x1 | 0x2
ENGINE_OFF_SAMPLES = 3  # consecutive 0 rpm / no-answer samples that end the drive


def keep_awake():
    ctypes.windll.kernel32.SetThreadExecutionState(ES)


def log(msg):
    print(f"{datetime.datetime.now():%H:%M:%S} {msg}", flush=True)


def read_obd(t, record):
    got = {}
    try:
        with t.client(UDS_MODULES["01"]) as c:
            for pid in uds.OBD_PIDS:
                try:
                    name, value, unit = uds.read_obd_pid(c, pid)
                    got[f"{pid:02X}"] = {"name": name, "value": value, "unit": unit}
                except (uds.NegativeResponse, uds.NoResponse):
                    pass
    except Exception as e:
        got["_error"] = str(e)
    record({"kind": "obd", "values": got})
    rpm = next((v["value"] for k, v in got.items() if "轉速" in v.get("name", "")), None)
    kmh = next((v["value"] for k, v in got.items() if "車速" in v.get("name", "")), None)
    if got.get("_error") is None and got:
        return f"轉速 {rpm} rpm、車速 {kmh} km/h", rpm
    return "引擎無回應", None


def read_tp20(base, t, record):
    for addr, groups in TP20_GROUPS.items():
        ns = argparse.Namespace(**vars(base)); ns.module = addr
        rows = []
        try:
            dest = cli.tp20_dest(ns)

            def readall(k):
                for g in groups:
                    try:
                        rows.append({"group": g, "values": [measure.value_text(v)
                                                             for v in measure.read_group(k, g)]})
                    except Exception as e:
                        rows.append({"group": g, "error": str(e)})
            cli.kwp_call(t, dest, readall, tries=2)
        except Exception as e:
            rows.append({"error": str(e)})
        record({"kind": "tp20", "module": addr, "groups": rows})


def health(t, record, label):
    try:
        with t.client(UDS_MODULES["01"]) as c:
            rec = {"when": label, "kind": "health", "misfire": uds.read_misfire(c, 4),
                   "readiness": uds.read_readiness(c)}
        rec["voltage"] = t.voltage()
        record(rec)
        mis = [m for m in rec["misfire"] if m.get("tid") == "0C" and m.get("value", 0) > 0]
        mon = rec["readiness"]["monitors"]
        log(f"{label}: 電壓 {rec['voltage']}，失火>0 的缸 {len(mis)}，"
            f"排放自檢完成 {sum(m['complete'] for m in mon)}/{len(mon)}")
    except Exception as e:
        record({"when": label, "kind": "health", "error": str(e)})
        log(f"{label}: 讀取失敗 {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--elm", default="COM10")
    ap.add_argument("--seconds", type=float, default=4200)
    ap.add_argument("--obd-every", type=float, default=2.0)
    # 0 = never. Polling 03 ABS while moving makes the cluster chime and flash ABS/ESC
    # (2026-11-06 drive): it may suspend ESC, so TP2.0 modules are off unless asked for.
    ap.add_argument("--tp20-every", type=float, default=0.0)
    ap.add_argument("--sim", action="store_true")
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # a cp950 console garbled the Chinese log
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    stop_file = ROOT / "reports" / "STOP_DRIVE"
    stop_file.unlink(missing_ok=True)
    base = cli.build_parser().parse_args((["--sim"] if a.sim else ["--elm", a.elm]) + ["voltage"])
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = open(ROOT / "reports" / f"{stamp}_drive.jsonl", "a", encoding="utf-8")

    def record(obj):
        obj.setdefault("t", time.time())
        out.write(json.dumps(obj, ensure_ascii=False) + "\n"); out.flush()

    keep_awake()
    log(f"行駛記錄開始 -> {out.name}")
    with cli.Transport(base) as t:
        health(t, record, "出發前")
        end = time.time() + a.seconds
        last_tp20 = 0.0
        stopped = 0
        try:
            while time.time() < end:
                if stop_file.exists():
                    stop_file.unlink(missing_ok=True)
                    log("收到停止檔，結束行駛記錄")
                    break
                keep_awake()
                text, rpm = read_obd(t, record)
                log("  " + text)
                # Engine off (0 rpm) or ignition off (no answer) for a few samples in a row:
                # the drive is over. Before this the logger hung after parking (2026-11-06).
                stopped = 0 if rpm else stopped + 1
                if stopped >= ENGINE_OFF_SAMPLES:
                    log("引擎已熄火，結束行駛記錄")
                    break
                if a.tp20_every > 0 and time.time() - last_tp20 >= a.tp20_every:
                    read_tp20(base, t, record)
                    last_tp20 = time.time()
                time.sleep(a.obd_every)
        except KeyboardInterrupt:
            log("手動停止")
        health(t, record, "行駛後")
    log("行駛記錄結束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
