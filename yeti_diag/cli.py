"""Command line: python -m yeti_diag [transport options] [--json] <command> [options]

Transports:
  --elm COM5                 ELM327 (USB, or Bluetooth paired as a COM port)
  --interface slcan --channel COM3   USB-CAN adapter through python-can
  --sim / --sim-elm          built-in simulator (CAN / fake ELM327)

--json: stdout carries exactly one JSON document (see `envelope`); progress
text goes to stderr. Exit codes are listed in EXIT_CODES. `commands` prints the
command catalog as JSON so an AI agent can discover what it may call.

Reading commands never change the car. Writing commands (write-did, restore,
clear-dtc, feature) go through writer.py: backup, checks, confirmation,
verification. Without a terminal to confirm in (--json, or --plan), a write
stops at a plan; `apply <id>` then sends it once.
"""
import argparse
import contextlib
import datetime
import json
import pathlib
import sys
import time
import traceback

from . import battery, discover, features, labels, measure, plans, sniff, tp20, uds, vehicle, writer
from .elm327 import Elm327, ElmChannel, ElmError, find_port
from .sim import FakeElmSerial, SimCar
from .vehicle import (DIAG_BITRATE, ID_DIDS, REAR_DOOR_DEST, TP20_MODULES, TP20_PART_NUMBERS,
                      UDS_MODULES, UDS_VALUES, VCDS_TRUTH, VIN, Module)
from .paths import DATA_ROOT

TOOL_VERSION = "0.5.0"
REPORT_DIR = DATA_ROOT / "reports"

EXIT_CODES = {
    "ok": 0,
    "check_failed": 1,   # ran fine, but the result is not all good (verify fails, module errors)
    "usage": 2,          # bad arguments, unknown module, missing TP2.0 map
    "connection": 3,     # adapter / module did not answer
    "refused": 4,        # write refused by a safety check, or plan mismatch
    "verify_failed": 5,  # write sent but read-back differs
    "internal": 70,      # bug in this tool
}
WRITE_COMMANDS = {"write-did", "restore", "clear-dtc", "feature", "apply", "coding-rewrite", "adapt-write"}


MOVING_KMH = 3        # above this OBD speed the car counts as moving
PARKED_RECHECK = 10.0  # seconds a "parked" check stays valid


class CliError(Exception):
    def __init__(self, message, kind="usage"):
        super().__init__(message)
        self.kind = kind


def say(*parts):
    """Human-readable progress. In --json mode it is redirected to stderr."""
    print(*parts, flush=True)


def map_file(args):
    """Simulator and real car keep separate TP2.0 address maps; other profiles (cars) too."""
    car = "" if vehicle.PROFILE_NAME == vehicle.DEFAULT_PROFILE else f"_{vehicle.PROFILE_NAME}"
    return REPORT_DIR / (f"tp20_map{car}_sim.json" if is_sim(args) else f"tp20_map{car}.json")


class Transport:
    """Opens the chosen adapter and hands out one UdsClient per module."""

    def __init__(self, args):
        self.args = args
        self.sim = self.elm = self.bus = None

    def __enter__(self):
        a = self.args
        if a.sim:
            self.sim = SimCar()
            self.bus = self.sim.__enter__()
        elif a.sim_elm:
            self.elm = Elm327(serial_obj=FakeElmSerial())
        elif a.elm and a.elm.upper().startswith("BLE:"):
            from .ble import BleSerial
            try:
                self.elm = Elm327(serial_obj=BleSerial(a.elm[4:]))
            except Exception as e:  # noqa: BLE001 - bleak raises its own types; all mean "no link"
                raise CliError(f"BLE 接頭 {a.elm[4:]} 連不上: {e}。先用 ble-scan 看名稱或位址。", "connection")
        elif a.elm:
            port, ser = a.elm, None
            if port.lower() == "auto":
                try:  # keep the port the probe opened: reopening a Bluetooth port at once fails
                    port, ser = find_port(REPORT_DIR / "elm_port.txt", say, keep_open=True)
                except ElmError as e:
                    raise CliError(str(e), "connection")
            try:
                self.elm = Elm327(port=port, baudrate=a.baud, serial_obj=ser)
            except OSError as e:
                raise CliError(f"打不開 {port}: {e}。藍牙接頭是否已配對、插在車上、沒有被其他程式佔用？",
                               "connection")
        else:
            import can
            self.bus = can.Bus(interface=a.interface, channel=a.channel, bitrate=DIAG_BITRATE)
        return self

    def __exit__(self, *exc):
        if self.sim:
            self.sim.__exit__(*exc)
        elif self.bus:
            self.bus.shutdown()
        if self.elm:
            self.elm.close()

    def client(self, mod, timeout=None):
        if getattr(self.args, "cmd", None) in WRITE_COMMANDS:
            self.ensure_parked("寫入")
        return self._client(mod, timeout)

    def _client(self, mod, timeout=None):
        if self.elm:
            channel = ElmChannel(self.elm, mod.tx_id, mod.rx_id)
        else:
            channel = uds.can_channel(self.bus, mod.tx_id, mod.rx_id)
        return uds.UdsClient(channel, timeout=timeout or self.args.timeout)

    def vehicle_speed(self):
        """OBD vehicle speed (km/h) from the engine, or None when it does not answer."""
        try:
            with self._client(UDS_MODULES["01"]) as c:
                return uds.read_obd_pid(c, 0x0D)[1]
        except (uds.NoResponse, uds.NegativeResponse, ElmError):
            return None

    def ensure_parked(self, what):
        """Refuse TP2.0 modules and every write while the car moves. Polling 03 ABS at
        speed made the cluster chime and flash the ABS/ESC lights (2026-11-06): ABS may
        suspend ESC while a diagnostic channel is open. 09 answers D7 when moving anyway.
        Checked at most every PARKED_RECHECK seconds; --allow-moving skips it."""
        if getattr(self.args, "allow_moving", False):
            return
        now = time.monotonic()
        if now - getattr(self, "_parked_at", -1e9) < PARKED_RECHECK:
            return
        speed = self.vehicle_speed()
        if speed is not None and speed > MOVING_KMH:
            raise CliError(f"車速 {speed} km/h：行駛中不能{what} (讀 ABS 會讓儀表叮聲、ABS / 車身穩定燈閃，"
                           f"可能暫停車身穩定功能)。請停車後再做。", "refused")
        self._parked_at = now

    def link(self):
        """Raw-frame access for TP2.0."""
        return tp20.ElmLink(self.elm) if self.elm else tp20.CanLink(self.bus)

    def kwp(self, dest):
        """Open a TP2.0 channel; use as `with t.kwp(dest) as k:`."""
        self.ensure_parked("連舊模組 (TP2.0)")
        return KwpSession(tp20.Tp20Channel(self.link(), dest, timeout=self.args.timeout,
                                           fast=self.tp20_fast()))

    def tp20_fast(self):
        """--tp20-fast, unless the adapter is known not to manage it. The Vgate (ELM327
        v2.1) returned nothing without reply counts: every read failed (2026-11-04)."""
        if not getattr(self.args, "tp20_fast", False):
            return False
        if self.elm and "v2." in (self.elm.version or ""):
            if not getattr(self, "_fast_warned", False):
                say(f"這顆接頭 ({self.elm.version.strip()}) 實測不支援 --tp20-fast，改用一般模式")
                self._fast_warned = True
            return False
        return True

    def ecu_voltage(self):
        """The engine's own supply voltage (OBD PID 0x42), or None when it does not answer."""
        try:
            with self.client(UDS_MODULES["01"]) as c:
                return uds.read_obd_pid(c, 0x42)[1]
        except (uds.NoResponse, uds.NegativeResponse, ElmError):
            return None

    def voltage(self):
        """Battery voltage. ELM: ATRV, but the engine's reading wins when the two differ by
        more than 0.5 V: the Vgate's ATRV read about 1 V low (11.4 V while the engine said
        12.42 V, 2026-11-04), which would wrongly block writes. No ELM: the engine's reading."""
        if not self.elm:
            return self.ecu_voltage()
        try:
            atrv = self.elm.battery_voltage()
        except (ValueError, OSError, ElmError):
            atrv = None
        ecu = self.ecu_voltage()
        if ecu is not None and (atrv is None or abs(ecu - atrv) > 0.5):
            if atrv is not None and not getattr(self, "_volt_warned", False):
                say(f"接頭量到 {atrv:.2f} V，引擎模組量到 {ecu:.2f} V，差太多：採用引擎模組的值")
                self._volt_warned = True
            return ecu
        return atrv


class KwpSession:
    def __init__(self, channel):
        self.channel = channel

    def __enter__(self):
        # __exit__ does not run when __enter__ fails, so close here: a channel left
        # open keeps the gateway answering "busy" (D8) to every address.
        try:
            self.channel.open()
            client = tp20.KwpClient(self.channel)
            client.start_session()
        except BaseException:
            self.channel.close()
            raise
        return client

    def __exit__(self, *exc):
        self.channel.close()


TP20_RETRY_ERRORS = (uds.NoResponse, tp20.Tp20Error, ElmError)
TP20_BUSY_WAIT = 1.0  # seconds; the gateway stays busy ~2 s after a bad channel end
TP20_PROBE_TIMEOUT = 0.3  # quick probe pass: one short setup try per address (tests use less)


def kwp_call(t, dest, fn, tries=3):
    """Run fn(kwp_client) on a fresh TP2.0 channel, retrying timing failures.

    A failed read can leave the channel unusable (seen on the car with the
    ELM327 clone), so every attempt opens its own channel. Read-only callers only:
    writes must go out once.
    """
    for attempt in range(tries):
        try:
            with t.kwp(dest) as k:
                return fn(k)
        except TP20_RETRY_ERRORS as e:
            if attempt == tries - 1:
                raise
            time.sleep(TP20_BUSY_WAIT if isinstance(e, tp20.Tp20Busy) else 0.3)


def tp20_answers(t, dest, tries=3, timeout=0.3):
    """Quick setup check for probing; a busy gateway is waited out, not skipped."""
    for attempt in range(tries):
        try:
            tp20.Tp20Channel(t.link(), dest, timeout=timeout).open().close()
            return True
        except tp20.Tp20Busy:
            time.sleep(TP20_BUSY_WAIT)
        except (uds.NoResponse, tp20.Tp20Error, ElmError):
            return False
    return False


def known_dests(args):
    """TP2.0 addresses that answered before, for probe_tp20's second look."""
    dests = {int(d, 16) for d in (load_map(args) or {}).values()}
    if not is_sim(args):
        dests |= set(REAR_DOOR_DEST.values())
    return dests


def load_map(args):
    path = map_file(args)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["modules"]
    return None


def tp20_dest(args, module=None):
    """'0x20' -> 0x20; a module address like '09' -> its probed TP2.0 address."""
    arg = module or args.module
    if arg.lower().startswith("0x"):
        return int(arg, 16)
    mapping = load_map(args) or {}
    if arg in mapping:
        return int(mapping[arg], 16)
    raise CliError(f"不知道模組 {arg} 的 TP2.0 位址。請先執行 tp20-probe，或直接給位址，例如 0x20。")


def module_arg(addr):
    if addr not in UDS_MODULES:
        known = ", ".join(UDS_MODULES)
        raise CliError(f"模組 {addr} 不在 UDS 清單 ({known})。TP2.0 模組請用 kwp-* 指令。")
    return UDS_MODULES[addr]


def is_sim(args):
    return bool(args.sim or args.sim_elm)


def sim_tag(args):
    return "  [模擬器]" if is_sim(args) else ""


def uds_dtc(d):
    return {"code": uds.format_dtc(d["dtc"]), "raw": f"{d['dtc']:06X}", "status": f"{d['status']:02X}",
            "status_text": uds.describe_status(d["status"])}


def kwp_dtc(d):
    if d.get("uds"):
        return {"code": uds.format_dtc(d["code"]), "raw": f"{d['code']:06X}", "status": f"{d['status']:02X}",
                "status_text": uds.describe_status(d["status"])}
    return labels.vag_dtc(d["code"], d["status"])


def dtc_line(d):
    if "symptom" in d:
        return labels.vag_dtc_line(d)
    return f"{d['code']} ({d['raw']}) 狀態 {d['status']}: {d['status_text']}"


def retry_elm(fn, tries=2):
    """One more try after an adapter error (the clone reports BUFFERFULL now and then)."""
    for attempt in range(tries):
        try:
            return fn()
        except ElmError:
            if attempt == tries - 1:
                raise
            time.sleep(0.5)


def scan_module(t, mod):
    """One UDS module; any failure is recorded here so a scan always finishes."""
    result = {"address": mod.address, "name": mod.name, "protocol": "UDS", "ids": {}, "dtcs": None,
              "error": None}
    with t.client(mod) as c:
        try:
            for did, label in ID_DIDS.items():
                try:
                    result["ids"][label] = uds.decode_did(did, retry_elm(lambda: c.read_did(did)))
                except uds.NegativeResponse:
                    pass
            result["dtcs"] = [uds_dtc(d) for d in retry_elm(c.read_dtcs)]
        except (uds.NoResponse, uds.NegativeResponse, ElmError) as e:
            result["error"] = str(e)
        if mod.address in UDS_VALUES:
            result["values"] = read_values(c, mod.address)
    return result


def read_tp20_part(t, dest):
    """Part number of a TP2.0 module. Older modules (03 ABS on this car) may stay
    silent on 22 F187 instead of refusing it, which leaves that channel dead, so
    1A 9B is asked on a fresh channel."""
    try:
        return kwp_call(t, dest, lambda k: k.read_part(), tries=2)
    except (*TP20_RETRY_ERRORS, uds.NegativeResponse):
        return kwp_call(t, dest, lambda k: tp20.ascii_part(k.read_ident(0x9B)))


def scan_tp20_module(t, addr, dest):
    result = {"address": addr, "name": TP20_MODULES.get(addr, ""), "protocol": "TP2.0",
              "tp20": f"0x{dest:02X}", "ids": {}, "coding": None, "dtcs": None, "error": None}
    try:
        result["ids"]["零件號碼"] = read_tp20_part(t, dest)
    except (*TP20_RETRY_ERRORS, uds.NegativeResponse) as e:
        result["error"] = f"零件號碼: {e}"
    try:
        # Only the newer modules keep their coding at 22 0600; one try is enough.
        result["coding"] = kwp_call(t, dest, lambda k: k.read_raw([0x22, 0x06, 0x00]), tries=1).hex().upper()
    except (*TP20_RETRY_ERRORS, uds.NegativeResponse):
        pass
    try:
        result["dtcs"] = [kwp_dtc(d) for d in kwp_call(t, dest, lambda k: k.read_dtcs())]
    except (*TP20_RETRY_ERRORS, uds.NegativeResponse) as e:
        result["error"] = (result["error"] + "; " if result["error"] else "") + f"故障碼: {e}"
    return result


def module_lines(r):
    where = f" [{r['tp20']}]" if r.get("tp20") else ""
    if r["dtcs"] is None:
        return [f"{r['address']} {r['name']}{where}  無法讀取: {r['error']}"]
    part = r["ids"].get("零件號碼", "?")
    lines = [f"{r['address']} {r['name']}{where}  零件 {part}  故障碼 {len(r['dtcs'])} 個"]
    if r["error"]:
        lines.append(f"     (部分讀取失敗: {r['error']})")
    lines += [f"     {dtc_line(d)}" for d in r["dtcs"]]
    lines += [f"     {v['name']}: {v['value']} {v['unit']}" for v in r.get("values", []) if "error" not in v]
    return lines


def save_report(args, kind, started, text, data):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = REPORT_DIR / f"{started:%Y%m%d-%H%M%S}_{kind}{'_sim' if is_sim(args) else ''}"
    stem.with_suffix(".txt").write_text(text, encoding="utf-8")
    stem.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return stem


def cmd_scan(args):
    started = datetime.datetime.now()
    with Transport(args) as t:
        results = [scan_module(t, mod) for mod in UDS_MODULES.values()]
    lines = [f"Skoda Yeti {VIN}  UDS 掃描 {started:%Y-%m-%d %H:%M:%S}{sim_tag(args)}", ""]
    for r in results:
        lines += module_lines(r)
    total = sum(len(r["dtcs"] or []) for r in results)
    lines += ["", f"合計故障碼: {total}", "舊模組 (TP2.0) 請用 autoscan"]
    report = "\n".join(lines)
    say(report)
    data = {"vin": VIN, "time": started.isoformat(), "sim": is_sim(args), "modules": results,
            "dtc_total": total}
    stem = save_report(args, "scan", started, report, data)
    say(f"\n已存檔: {stem}.txt / .json")
    data["report"] = str(stem.with_suffix(".json"))
    data["ok"] = not any(r["error"] for r in results)
    return data


def cmd_autoscan(args):
    """Every module this tool knows, UDS and TP2.0: ids, coding, decoded fault codes."""
    started = datetime.datetime.now()
    with Transport(args) as t:
        voltage = t.voltage()
        say(f"電壓 {voltage:.2f} V" if voltage is not None else "讀不到電壓")
        results = []
        for mod in UDS_MODULES.values():
            say(f"讀取 {mod.address} {mod.name} ...")
            results.append(scan_module(t, mod))
        mapping = None if args.probe else load_map(args)
        if not mapping:
            say("找舊模組 (TP2.0) 位址 ...")
            found, mapping = probe_tp20(t, 0x01, 0x7F, known_dests(args))
            save_map(args, found, mapping)
        for addr, dest in sorted(mapping.items()):
            say(f"讀取 {addr} {TP20_MODULES.get(addr, '')} ...")
            results.append(scan_tp20_module(t, addr, int(dest, 16)))
    missing = [a for a in TP20_MODULES if a not in mapping]
    total = sum(len(r["dtcs"] or []) for r in results)
    errors = [r["address"] for r in results if r["dtcs"] is None]
    lines = [f"Skoda Yeti {VIN}  全車掃描 {started:%Y-%m-%d %H:%M:%S}{sim_tag(args)}",
             f"電壓 {voltage:.2f} V" if voltage is not None else "電壓 讀不到", ""]
    for r in sorted(results, key=lambda r: r["address"]):
        lines += module_lines(r)
    lines += ["", f"模組 {len(results)} 個，合計故障碼 {total} 個"]
    if errors:
        lines.append("無法讀取: " + "、".join(errors))
    if missing:
        lines.append("沒找到 (TP2.0 掃描沒有回應): " + "、".join(f"{a} {TP20_MODULES[a]}" for a in missing))
    report = "\n".join(lines)
    say("\n" + report)
    data = {"vin": VIN, "time": started.isoformat(), "sim": is_sim(args), "voltage": voltage,
            "modules": sorted(results, key=lambda r: r["address"]), "dtc_total": total,
            "unreadable": errors, "not_found": missing}
    stem = save_report(args, "autoscan", started, report, data)
    say(f"\n已存檔: {stem}.txt / .json")
    data["report"] = str(stem.with_suffix(".json"))
    data["ok"] = not errors
    return data


def cmd_dtc(args):
    mod = module_arg(args.module)
    with Transport(args) as t, t.client(mod) as c:
        raw = c.read_dtcs()
        dtcs = [uds_dtc(d) for d in raw]
        if args.details:
            for d, r in zip(dtcs, raw):
                d["details"] = c.read_dtc_details(r["dtc"])
    say(f"{mod.address} {mod.name}: 故障碼 {len(dtcs)} 個")
    for d in dtcs:
        say(f"  {dtc_line(d)}")
        for key, label in (("snapshot", "凍結資料"), ("extended", "延伸資料")):
            if d.get("details", {}).get(key):
                say(f"     {label} (原始): {d['details'][key]}")
    return {"address": mod.address, "name": mod.name, "dtcs": dtcs}


def cmd_did(args):
    mod = module_arg(args.module)
    values = []
    with Transport(args) as t, t.client(mod) as c:
        for did in args.dids:
            try:
                data = c.read_did(did)
                values.append({"did": f"{did:04X}", "hex": data.hex().upper(), "text": uds.decode_did(did, data)})
                say(f"{mod.address} DID {did:04X}: {uds.decode_did(did, data)}  [{data.hex(' ').upper()}]")
            except (uds.NegativeResponse, uds.NoResponse) as e:
                values.append({"did": f"{did:04X}", "error": str(e)})
                say(f"{mod.address} DID {did:04X}: {e}")
    return {"address": mod.address, "values": values, "ok": all("error" not in v for v in values)}


def read_values(c, addr):
    """The confirmed readable values (vehicle.UDS_VALUES) of one UDS module."""
    out = []
    for did, (label, unit, decode, checked) in UDS_VALUES.get(addr, {}).items():
        try:
            data = retry_elm(lambda: c.read_did(did))
            out.append({"did": f"{did:04X}", "name": label, "value": decode(data), "unit": unit,
                        "raw": data.hex().upper(), "checked": checked})
        except (uds.NegativeResponse, uds.NoResponse, ElmError) as e:
            out.append({"did": f"{did:04X}", "name": label, "error": str(e)})
    return out


def cmd_values(args):
    """Confirmed readable values (odometer, ...) of all UDS modules that have some."""
    rows = []
    with Transport(args) as t:
        for addr in UDS_VALUES:
            with t.client(UDS_MODULES[addr]) as c:
                for v in read_values(c, addr):
                    rows.append({"module": addr, **v})
                    text = f"{v['value']} {v['unit']}" if "error" not in v else f"讀不到 ({v['error']})"
                    say(f"{addr} {v['name']}: {text}")
    return {"values": rows, "ok": all("error" not in r for r in rows)}


def sniff_ids(args):
    """CAN IDs to pass through the ELM's filter: --ids, or a UDS module's pair."""
    if args.ids:
        try:
            return [int(x, 16) for x in args.ids.split(",")]
        except ValueError:
            raise CliError(f"--ids 要用逗號分隔的十六進位 CAN ID，例如 714,77E (收到 {args.ids})")
    if args.module:
        mod = module_arg(args.module)
        return [mod.tx_id, mod.rx_id]
    return None


def cmd_sniff(args):
    """Record another tester's traffic (VCDS through a Y-cable). Sends nothing on the bus."""
    if not (args.elm or args.sim_elm):
        raise CliError("sniff 要用 ELM327 監聽 (--elm COMx)")
    ids = sniff_ids(args)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{datetime.datetime.now():%Y%m%d-%H%M%S}_sniff{'_sim' if is_sim(args) else ''}.txt"
    with Transport(args) as t:
        v = t.voltage()
        say(f"電壓 {v:.2f} V" if v else "電壓讀不到")
        say(f"開始監聽{f' ({args.seconds:g} 秒)' if args.seconds else '，按 Ctrl+C 結束'}，"
            f"過濾 {','.join(f'{i:03X}' for i in ids) if ids else '無'}。現在可以在 VCDS 操作。")
        with open(path, "w", encoding="utf-8") as out:
            counts = sniff.record(t.elm, out, seconds=args.seconds, ids=ids, say=say)
    say(f"收到 {counts['frames']} 個封包，緩衝區滿 {counts['buffer_full']} 次。存到 {path}")
    say(f"分析: sniff-analyze {path.name} --vcds <VCDS 記錄的 CSV>")
    return {"file": str(path), **counts, "voltage": v, "ok": counts["frames"] > 0}


def cmd_sniff_analyze(args):
    """Rebuild requests / answers from a capture; with --vcds, name the values."""
    path = pathlib.Path(args.file)
    if not path.exists() and (REPORT_DIR / args.file).exists():
        path = REPORT_DIR / args.file
    if not path.exists():
        raise CliError(f"找不到監聽檔 {args.file}")
    cap = sniff.read_capture(path.read_text(encoding="utf-8"))
    mods = {a: (m.tx_id, m.rx_id) for a, m in UDS_MODULES.items()}
    names = {int(d, 16): a for a, d in (load_map(args) or {}).items()}
    names.update({d: a for a, d in REAR_DOOR_DEST.items()})
    exs = sniff.exchanges(cap.frames, mods, names)
    obs = sniff.observations(exs)
    acts = sniff.actions(exs)
    say(f"{path.name}: {len(cap.frames)} 個封包，{len(exs)} 組請求/回覆，緩衝區滿 {len(cap.gaps)} 次")
    per_module = {}
    for e in exs:
        per_module[e.module] = per_module.get(e.module, 0) + 1
    for m, n in sorted(per_module.items()):
        say(f"  {m}: {n} 次")
    readings = [{"key": k, "count": len(v), "last_raw": v[-1][1].hex().upper(),
                 "last_value": v[-1][2]} for k, v in sorted(obs.items())]
    if acts:
        say("讀取以外的動作 (依時間順序):")
    action_rows = []
    for e in acts:
        row = {"time": datetime.datetime.fromtimestamp(e.t).strftime("%H:%M:%S.%f")[:-3], "module": e.module,
               "kind": e.kind, "request": e.request.hex().upper(),
               "response": e.response.hex().upper() if e.response is not None else None, "pending": e.pending}
        action_rows.append(row)
        say(f"  {row['time']} {e.module} > {row['request']}  < {row['response'] or '(沒有回覆)'}")
    result = {"file": str(path), "frames": len(cap.frames), "buffer_full": len(cap.gaps),
              "exchanges": per_module, "readings": readings, "actions": action_rows}
    if args.vcds:
        start = None
        if args.vcds_start:
            try:
                start = datetime.datetime.fromisoformat(args.vcds_start).timestamp()
            except ValueError:
                raise CliError(f"--vcds-start 格式要像 2026-11-05 07:30:15 (收到 {args.vcds_start})")
        try:
            text = pathlib.Path(args.vcds).read_text(encoding="utf-8-sig", errors="replace")
            vcds = sniff.read_vcds_csv(text, start)
        except (OSError, ValueError) as e:
            raise CliError(f"讀不了 VCDS CSV: {e}")
        matches = sniff.match_vcds(obs, vcds, tolerance=args.tolerance)
        say("VCDS 數值對照:")
        for m in matches:
            best = m["matches"][0] if m["matches"] else None
            if best:
                off = f" {best['offset']:+g}" if best["offset"] else ""
                say(f"  {m['vcds']} [{m['unit']}] = {best['key']} {best['how']} × {best['factor']}{off}"
                    f"  ({best['points']} 點，例 {best['example']})")
            elif m["constant_candidates"]:
                say(f"  {m['vcds']}: 數值沒變，候選 {', '.join(c['key'] for c in m['constant_candidates'][:3])}")
            else:
                say(f"  {m['vcds']}: 對不上")
        result["vcds_matches"] = matches
    out = path.with_name(path.stem + "_analysis.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    say(f"分析存到 {out}")
    result["saved"] = str(out)
    return result


def cmd_candidates(args):
    """Read the profile's unconfirmed DIDs / measuring groups raw, for matching with
    another tool's screen (Car Scanner, VCDS). Read-only; saved to reports/."""
    cand = vehicle.PROFILE.get("candidates", {})
    started = datetime.datetime.now()
    out = {"uds": {}, "tp20": {}}
    with Transport(args) as t:
        for addr, dids in cand.get("uds_dids", {}).items():
            rows = out["uds"][addr] = []
            with t.client(module_arg(addr)) as c:
                for did in dids:
                    try:
                        data = retry_elm(lambda: c.read_did(int(did, 16)))
                        rows.append({"did": did, "hex": data.hex().upper(), "int": int.from_bytes(data, "big")})
                    except (uds.NegativeResponse, uds.NoResponse, ElmError) as e:
                        rows.append({"did": did, "error": str(e)})
                    r = rows[-1]
                    say(f"{addr} DID {did}: {r.get('hex', '')} {('= ' + str(r['int'])) if 'int' in r else r.get('error')}")
        for addr, groups in cand.get("tp20_groups", {}).items():
            rows = out["tp20"][addr] = []
            dest = tp20_dest(args, addr)
            for g in groups:
                try:
                    values = measure.label(kwp_call(t, dest, lambda k: measure.read_group(k, int(g, 16))),
                                           group_labels(addr, int(g, 16)))
                    rows.append({"group": g, "values": values})
                    say(f"{addr} 群組 {g}: " + " | ".join(measure.value_text(v) for v in values))
                except (*TP20_RETRY_ERRORS, uds.NegativeResponse) as e:
                    rows.append({"group": g, "error": str(e)})
                    say(f"{addr} 群組 {g}: {e}")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{started:%Y%m%d-%H%M%S}_candidates{'_sim' if is_sim(args) else ''}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    say(f"已存檔: {path}")
    return {**out, "report": str(path)}


def cmd_profile(args):
    """Which vehicle profile is active, and which ones exist (YETI_DIAG_PROFILE picks one)."""
    path = vehicle.profile_path()
    problems = vehicle.check_profile(vehicle.PROFILE)
    available = sorted(p.stem for p in vehicle.PROFILE_DIR.glob("*.json"))
    say(f"車型設定檔: {vehicle.PROFILE_NAME} ({vehicle.PROFILE['name']})  {path}")
    say(f"UDS 模組 {len(UDS_MODULES)} 個，TP2.0 模組 {len(TP20_MODULES)} 個；可用: {', '.join(available)}")
    say("換車型: 設定環境變數 YETI_DIAG_PROFILE=<名稱或 .json 路徑>")
    for msg in problems:
        say(f"  問題: {msg}")
    return {"profile": vehicle.PROFILE_NAME, "name": vehicle.PROFILE["name"], "path": str(path), "vin": VIN,
            "uds_modules": list(UDS_MODULES), "tp20_modules": list(TP20_MODULES), "available": available,
            "problems": problems, "ok": not problems}


def cmd_discover(args):
    """Scan an unknown car for UDS and TP2.0 modules and write a profile for it (read-only)."""
    key = args.name
    if not key.replace("_", "").isalnum():
        raise CliError(f"設定檔名稱只能用英數字和底線 (收到 {key})")
    out = vehicle.PROFILE_DIR / f"{key}{'_sim' if is_sim(args) else ''}.json"
    if out.exists():
        raise CliError(f"{out} 已經存在，換一個 --name，或先把舊檔改名")
    with Transport(args) as t:
        v = t.voltage()
        say(f"電壓 {v:.2f} V" if v else "電壓讀不到")
        say("掃描 UDS 模組 (CAN ID 700-7E7，約 1-2 分鐘) ...")
        rows = discover.scan_uds(lambda mod: t.client(mod, timeout=0.3), say=say)
        say("掃描 TP2.0 模組 (位址 00-7F，約 1-2 分鐘) ...")
        found, _ = probe_tp20(t, 0x00, 0x7F)
        for dest, part in sorted(found.items()):
            say(f"TP2.0 {dest}: {part}")
        adapter = t.elm.version.strip() if t.elm else args.interface
    profile = discover.build_profile(key, args.title or key, rows, found, adapter=adapter)
    out.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    problems = vehicle.check_profile(profile)
    say(f"\nUDS 模組 {len(profile['uds_modules'])} 個，TP2.0 模組 {len(profile['tp20_modules'])} 個，"
        f"車身號碼 {profile['vin'] or '讀不到'}")
    say(f"已寫入 {out}")
    say(f"使用: 設定 YETI_DIAG_PROFILE={out.stem}，再跑 profile、tp20-probe、autoscan")
    for msg in problems:
        say(f"  問題: {msg}")
    return {"profile_file": str(out), "profile": profile, "problems": problems, "ok": not problems}


def cmd_scan_dids(args):
    """Walk a DID range and list which ones the module answers (for finding measurements)."""
    mod = module_arg(args.module)
    found = []
    stopped = None
    with Transport(args) as t, t.client(mod) as c:
        for did in range(args.start, args.end + 1):
            try:
                data = retry_elm(lambda: c.read_did(did))
            except uds.NegativeResponse:
                continue
            except (uds.NoResponse, ElmError):
                stopped = f"{did:04X}"
                say(f"DID {did:04X}: 沒有回應，停止")
                break
            found.append({"did": f"{did:04X}", "hex": data.hex().upper()})
            say(f"DID {did:04X}: {data.hex(' ').upper()}")
    say(f"{mod.address} 在 {args.start:04X}-{args.end:04X} 找到 {len(found)} 個 DID")
    return {"address": mod.address, "range": [f"{args.start:04X}", f"{args.end:04X}"], "found": found,
            "stopped_at": stopped}


def cmd_obd(args):
    values = []
    with Transport(args) as t, t.client(UDS_MODULES["01"]) as c:
        for pid in uds.OBD_PIDS:
            try:
                name, value, unit = uds.read_obd_pid(c, pid)
                values.append({"pid": f"{pid:02X}", "name": name, "value": value, "unit": unit})
                say(f"{name}: {value:g} {unit}")
            except (uds.NegativeResponse, uds.NoResponse) as e:
                values.append({"pid": f"{pid:02X}", "error": str(e)})
                say(f"PID {pid:02X}: {e}")
        codes = uds.read_obd_dtcs(c)
    say("OBD 故障碼: " + (", ".join(codes) if codes else "無"))
    return {"values": values, "obd_dtcs": codes}


def cmd_readiness(args):
    """OBD-II readiness (VCDS 'Readiness'): which emission self-tests have completed."""
    with Transport(args) as t, t.client(UDS_MODULES["01"]) as c:
        r = uds.read_readiness(c)
    say(f"故障燈 (MIL): {'亮' if r['mil'] else '不亮'}，OBD 故障碼 {r['dtc_count']} 個 ({'柴油' if r['engine'] == 'diesel' else '汽油'}引擎)")
    for m in r["monitors"]:
        say(f"  {m['name']}: {'完成' if m['complete'] else '未完成'}")
    say("全部完成 (可通過驗車排氣自我檢測)" if r["all_complete"] else
        "有未完成的項目：清過故障碼或拔過電瓶後要正常行駛一段時間 (含冷車啟動、怠速、定速) 才會完成")
    return r


def cmd_misfire(args):
    """Misfire counters per cylinder from OBD-II Mode $06 (SAE J1979, works on any petrol car)."""
    with Transport(args) as t, t.client(UDS_MODULES["01"]) as c:
        rows = uds.read_misfire(c, args.cylinders)
    for r in rows:
        if "error" in r:
            say(f"  {r['name']} (MID {r['mid']}): {r['error']}")
        elif r["scaled"]:
            say(f"  {r['name']}: {r['value']} {r['unit']}  ({r['test']})")
        else:
            say(f"  {r['name']}: 原始值 {r['value']} (單位代碼 {r['uasid']}，{r['test']})")
    counts = [r for r in rows if r.get("tid") == "0C" and r["mid"] != "A1" and r["scaled"]]
    worst = max(counts, key=lambda r: r["value"], default=None)
    if worst and worst["value"] > 0:
        say(f"失火最多: {worst['name']} ({worst['value']} 次，這次 / 上次行車循環)")
    elif counts:
        say("各缸沒有失火紀錄")
    return {"misfire": rows, "worst": worst["name"] if worst and worst["value"] > 0 else None}


def cmd_probe(args):
    """Check which UDS module IDs answer, to confirm the CAN ID table."""
    rows = []
    with Transport(args) as t:
        for mod in UDS_MODULES.values():
            row = {"address": mod.address, "name": mod.name, "tx": f"{mod.tx_id:03X}", "rx": f"{mod.rx_id:03X}"}
            with t.client(mod) as c:
                try:
                    part = uds.decode_did(0xF187, c.read_did(0xF187))
                    row.update(part=part, match=part.replace(" ", "") == mod.part_no.replace(" ", ""))
                    ok = "符合" if row["match"] else f"不符 (預期 {mod.part_no})"
                    say(f"{mod.address} {mod.name}: {row['tx']}/{row['rx']} 回應 {part} -> {ok}")
                except (uds.NoResponse, uds.NegativeResponse) as e:
                    row.update(error=str(e), match=False)
                    say(f"{mod.address} {mod.name}: {row['tx']}/{row['rx']} {e}")
            rows.append(row)
    return {"modules": rows, "ok": all(r["match"] for r in rows)}


def cmd_ble_scan(args):
    """List nearby BLE devices, OBD-looking ones first (for --elm BLE:...)."""
    import asyncio

    from . import ble
    try:
        rows = asyncio.run(ble.scan(args.seconds))
    except Exception as e:  # noqa: BLE001 - no Bluetooth adapter, radio off, ...
        raise CliError(f"BLE 掃描失敗: {e}", "connection")
    devices = [{"address": a, "name": n, "rssi": r, "looks_like_obd": obd} for a, n, r, obd in rows]
    for d in devices:
        mark = "OBD? " if d["looks_like_obd"] else "     "
        say(f"{mark}{d['address']}  {d['name'] or '(沒有名稱)'}  {d['rssi']} dBm")
    say("使用: --elm BLE:<名稱或位址>，例如 --elm BLE:OBDII" if devices else "沒有找到 BLE 裝置")
    return {"devices": devices}


CRANK_MAX_SECONDS = 120
GATEWAY_VOLT_GROUP = 73  # 19 gateway group 73 values 6-7: supply voltage (formula 0x06, 2026-11-04)


def battery_snapshot(t, args, gateway=True):
    """Every voltage the car offers: adapter (ATRV), engine (PID 42) and gateway (group 73),
    plus engine speed to tell resting from charging."""
    snap = {"atrv": None, "engine": None, "gateway": None, "rpm": None}
    if t.elm:
        try:
            snap["atrv"] = t.elm.battery_voltage()
        except (ValueError, OSError, ElmError):
            pass
    snap["engine"] = t.ecu_voltage()
    try:
        with t.client(UDS_MODULES["01"]) as c:
            snap["rpm"] = uds.read_obd_pid(c, 0x0C)[1]
    except (uds.NoResponse, uds.NegativeResponse, ElmError):
        pass
    if gateway:
        try:
            vals = kwp_call(t, tp20_dest(args, "19"), lambda k: measure.read_group(k, GATEWAY_VOLT_GROUP))
            volts = [v["value"] for v in vals if v.get("formula") in ("06", 0x06) and v.get("value")]
            snap["gateway"] = max(volts) if volts else None
        except (*TP20_RETRY_ERRORS, uds.NegativeResponse, CliError):
            pass
    ref = next((snap[k] for k in ("gateway", "engine", "atrv") if snap[k] is not None), None)
    snap["reference"] = ref
    snap["reference_source"] = next((k for k in ("gateway", "engine", "atrv") if snap[k] is not None), None)
    return snap


def cmd_battery(args):
    """Battery check from voltages: resting charge, charging voltage, and (--crank) the
    voltage dip while the engine starts."""
    started = datetime.datetime.now()
    with Transport(args) as t:
        snap = battery_snapshot(t, args, gateway=not args.crank)
        names = {"atrv": "接頭", "engine": "引擎模組", "gateway": "閘道器"}
        say("電壓: " + "、".join(f"{names[k]} {snap[k]:.2f} V" for k in names if snap[k] is not None))
        out = {"snapshot": snap}
        if snap["reference"] is None:
            raise CliError("讀不到任何電壓。點火開關有打開嗎？", "connection")
        running = (snap["rpm"] or 0) > 300
        if not args.crank:
            if running:
                out["charging"] = battery.charging_verdict(snap["reference"])
                say(f"引擎運轉中 ({snap['rpm']:.0f} rpm)：{out['charging']['text']}")
            else:
                out["rest"] = battery.rest_verdict(snap["reference"])
                say(f"引擎沒發動：估計電量 {out['rest']['soc']}%，{out['rest']['text']}")
                say("  (要熄火放幾小時才準；剛開完車或點火開關 ON 很久，數字會偏高或偏低)")
            say("要看啟動壓降：battery --crank 30，開始後轉鑰匙發動")
            return out
        if not t.elm:
            raise CliError("--crank 需要 ELM327 接頭 (ATRV 才能在啟動時連續量)。", "usage")
        if running:
            raise CliError("引擎已經在運轉。先熄火 (點火開關 ON 就好)，再跑 battery --crank。", "usage")
        seconds = min(max(args.crank, 5), CRANK_MAX_SECONDS)
        # Correct the adapter's ATRV with the engine's reading (the Vgate read ~1 V low).
        offset = 0.0
        if snap["atrv"] is not None and snap["engine"] is not None and abs(snap["engine"] - snap["atrv"]) > 0.3:
            offset = snap["engine"] - snap["atrv"]
            say(f"接頭電壓比引擎模組差 {offset:+.2f} V，記錄時會自動修正")
        say(f"開始記錄 {seconds:.0f} 秒：現在轉鑰匙發動引擎")
        samples, errors, t0 = [], 0, time.monotonic()
        while time.monotonic() - t0 < seconds:
            try:
                v = t.elm.battery_voltage() + offset
                samples.append((round(time.monotonic() - t0, 3), round(v, 2)))
            except (ValueError, ElmError):
                errors += 1  # adapter browned out / reset during the dip
            except OSError:
                errors += 1
                say("接頭斷線，用目前錄到的資料分析")
                break
        result = battery.analyze_crank(samples)
        result["read_errors"] = errors
        if errors:
            result["note"] = ((result.get("note") or "") +
                              f" 有 {errors} 次讀取失敗 (可能是電壓太低讓接頭重開機)").strip()
        say(result["text"])
        if result.get("detected"):
            say(f"  啟動前 {result['before']:.2f} V，啟動時最低 {result['min']:.2f} V "
                f"(起動馬達接上瞬間 {result['inrush_min']:.2f} V，第 {result['min_at']:.1f} 秒，不列入評等)，"
                f"壓降約 {result['crank_seconds']:.1f} 秒，"
                + (f"發動後 {result['after']:.2f} V ({'有充電' if result['charging'] else '沒看到充電'})"
                   if result["after"] else "發動後沒有資料"))
        if result.get("note"):
            say("  注意: " + result["note"])
        out["crank"] = result
        out["samples"] = samples
        text = "\n".join(f"{s:.3f}\t{v:.2f}" for s, v in samples)
        out["report"] = str(save_report(args, "battery_crank", started, text, out))
        say(f"已存檔: {out['report']}.json")
    return out


def cmd_voltage(args):
    with Transport(args) as t:
        v = t.voltage()
    say(f"電瓶電壓: {v:.2f} V" if v is not None else "讀不到電瓶電壓")
    return {"voltage": v, "unit": "V", "ok": v is not None}


def probe_tp20(t, start, end, known=()):
    """Try every TP2.0 address; map answering ones to modules by part number.

    `known`: addresses that answered before (saved map, door test). The quick pass
    gives each address one short try, which missed a door once in 10 runs on the
    car, so known addresses that stayed silent get a second, patient look.
    """
    found, modules, rear_doors = {}, {}, []
    first = [(d, False) for d in range(start, end + 1)]
    second = [(d, True) for d in sorted(set(known)) if start <= d <= end]
    second_dests = {d for d, _ in second}
    for dest, patient in first + second:
        if f"0x{dest:02X}" in found:
            continue
        if patient:
            time.sleep(TP20_BUSY_WAIT)
        # Quick pass: one short setup try, so silent addresses cost little.
        if not tp20_answers(t, dest, timeout=1.0 if patient else TP20_PROBE_TIMEOUT):
            continue
        try:
            part = read_tp20_part(t, dest)
        except (*TP20_RETRY_ERRORS, uds.NegativeResponse):
            # A known address gets its second, patient look instead (03 ABS was
            # missed once in 3 verify runs on the car, 2026-11-03 evening).
            if not patient and dest in second_dests:
                continue
            found[f"0x{dest:02X}"] = "(讀不到零件號碼)"
            continue
        found[f"0x{dest:02X}"] = part
        candidates = [a for a, expected in TP20_PART_NUMBERS.items() if expected == part]
        if candidates == ["62", "72"]:
            rear_doors.append(dest)
        elif candidates:
            modules[candidates[0]] = f"0x{dest:02X}"
    assign_rear_doors(rear_doors, found, modules)
    return found, modules


def assign_rear_doors(dests, found, modules):
    """62/72 share part number and (on this car) coding, so left/right cannot be
    read from the modules. Use the door-open test result from vehicle.py when the
    addresses match it; otherwise assign by address order and mark it unconfirmed."""
    confirmed = {d: a for a, d in REAR_DOOR_DEST.items()}
    for dest in dests:
        if dest in confirmed:
            modules[confirmed[dest]] = f"0x{dest:02X}"
            found[f"0x{dest:02X}"] += " (已實測開門確認)"
    leftover = [d for d in sorted(dests) if f"0x{d:02X}" not in modules.values()]
    for addr, dest in zip([a for a in ("62", "72") if a not in modules], leftover):
        modules[addr] = f"0x{dest:02X}"
        found[f"0x{dest:02X}"] += " (左右未確認，依位址順序)"


def save_map(args, found, modules):
    """Merge into the saved map, so probing part of the range keeps earlier results."""
    path = map_file(args)
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"found": {}, "modules": {}}
    found = {**old["found"], **found}
    # Drop old entries that point at an address this probe re-assigned.
    kept = {a: d for a, d in old["modules"].items() if d not in modules.values()}
    modules = {**kept, **modules}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sim": is_sim(args), "found": found, "modules": modules},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def cmd_tp20_probe(args):
    with Transport(args) as t:
        found, modules = probe_tp20(t, args.start, args.end, known_dests(args))
    for dest, part in found.items():
        addr = next((a for a, d in modules.items() if d == dest), "?")
        say(f"TP2.0 {dest}: 零件 {part:<12} -> 模組 {addr} {TP20_MODULES.get(addr, '')}")
    say(f"\n這次在 0x{args.start:02X}-0x{args.end:02X} 找到 {len(found)} 個位址，對應 {len(modules)} 個模組")
    path = save_map(args, found, modules)
    say(f"已合併存檔: {path}")
    return {"found": found, "modules": modules, "map_file": str(path)}


def cmd_kwp_dtc(args):
    dest = tp20_dest(args)
    with Transport(args) as t:
        raw = kwp_call(t, dest, lambda k: k.read_dtcs())
        dtcs = [kwp_dtc(d) for d in raw]
        if getattr(args, "details", False):
            for d, r in zip(dtcs, raw):
                if r.get("uds"):
                    continue
                try:
                    d["freeze_frame"] = kwp_call(t, dest, lambda k: k.read_freeze_frame(r["code"]))
                except (*TP20_RETRY_ERRORS, uds.NegativeResponse) as e:
                    d["freeze_frame"] = {"error": str(e)}
    say(f"{args.module} (TP2.0 0x{dest:02X}): 故障碼 {len(dtcs)} 個")
    for d in dtcs:
        say(f"  {dtc_line(d)}")
        ff = d.get("freeze_frame")
        if ff and "mileage_km" in ff:
            date = ff["date"] if ff["date_valid"] else f"{ff['date']} (無效)"
            say(f"    凍結資料: 里程 {ff['mileage_km']} km、發生 {ff['frequency']} 次、優先度 {ff['priority']}、"
                f"重開機計數 {ff['reset_counter']}、日期 {date} {ff['time']}")
        elif ff:
            say(f"    凍結資料: {ff}")
    return {"module": args.module, "tp20": f"0x{dest:02X}", "dtcs": dtcs}


def cmd_kwp_ident(args):
    dest = tp20_dest(args)
    reads = ([0x22, 0xF1, 0x87],) + tp20.CODING_READS
    rows = []
    with Transport(args) as t:
        for req in reads:
            name = bytes(req).hex(" ").upper()
            try:
                data = kwp_call(t, dest, lambda k: k.read_raw(req))
            except (*TP20_RETRY_ERRORS, uds.NegativeResponse) as e:
                rows.append({"request": name, "error": str(e)})
                say(f"{name}: {e}")
                continue
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
            rows.append({"request": name, "hex": data.hex().upper(), "text": text})
            say(f"{name}: {data.hex(' ').upper()}\n       {text}")
    return {"module": args.module, "tp20": f"0x{dest:02X}", "reads": rows}


def group_labels(module, group):
    """Value names for one measuring group from the vehicle profile (decimal group numbers)."""
    return vehicle.PROFILE.get("group_labels", {}).get(module, {}).get(str(group), {})


def cmd_measure(args):
    """KWP measuring groups (VCDS 'Measuring Blocks') of a TP2.0 module."""
    dest = tp20_dest(args)
    groups = []
    with Transport(args) as t:
        for _ in range(args.repeat):
            for g in args.groups:
                row = {"group": g, "time": datetime.datetime.now().isoformat(timespec="milliseconds")}
                try:
                    row["values"] = measure.label(kwp_call(t, dest, lambda k: measure.read_group(k, g)),
                                                  group_labels(args.module, g))
                    say(f"{args.module} 群組 {g:03d}: " + " | ".join(measure.value_text(v) for v in row["values"]))
                except uds.NegativeResponse as e:
                    row["error"] = f"不支援 ({e.nrc:02X})"
                    say(f"{args.module} 群組 {g:03d}: {row['error']}")
                except TP20_RETRY_ERRORS as e:
                    row["error"] = str(e)
                    say(f"{args.module} 群組 {g:03d}: 讀取失敗: {e}")
                groups.append(row)
    return {"module": args.module, "tp20": f"0x{dest:02X}", "groups": groups,
            "ok": any("values" in g for g in groups)}


def cmd_adapt(args):
    """Adaptation channels of a TP2.0 module (VCDS 'Adaptation - 10'), read only."""
    if any(not 1 <= ch <= 255 for ch in args.channels):
        raise CliError("通道要 1-255；00 是清除所有學習值，本工具不提供")
    dest = tp20_dest(args)
    done = {}

    def read_all(k):
        # One TP2.0 channel for all of them, like VCDS: the 09 drops or refuses (D8) channels
        # that are reopened every second (car, 2026-11-05). A retry continues where it stopped.
        for ch in args.channels:
            if ch in done:
                continue
            try:
                r = k.read_adaptation(ch)
                done[ch] = dict(r)
                shown = " | ".join(measure.value_text(f) for f in r["fields"] if "formula" in f)
                say(f"{args.module} 通道 {ch:03d}: 目前值 {r['value']}   ({shown})")
            except uds.NegativeResponse as e:
                done[ch] = {"channel": ch, "error": "模組沒有這個通道" if e.nrc == 0x31 else f"拒絕 ({e.nrc:02X})"}
                say(f"{args.module} 通道 {ch:03d}: {done[ch]['error']}")
            time.sleep(0.5)

    with Transport(args) as t:
        try:
            kwp_call(t, dest, read_all, tries=5)  # read only; channels already read are kept
        except TP20_RETRY_ERRORS as e:
            say(f"讀取中斷: {e}")
    rows = [done.get(ch, {"channel": ch, "error": "沒讀到"}) for ch in args.channels]
    return {"module": args.module, "tp20": f"0x{dest:02X}", "channels": rows,
            "ok": any("value" in r for r in rows)}


# Not contacted while driving: 03 ABS, 15 airbag, 44 steering assist (safety systems),
# and 09, which refuses diagnostics (D7) while the car moves (2026-11-04).
DRIVING_SKIP = {"03", "09", "15", "44"}


def cmd_verify(args):
    """Compare this tool's readings with the VCDS results of 2026-11-03."""
    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        say(f"[{'通過' if ok else '失敗'}] {name}: {detail}")

    with Transport(args) as t:
        v = t.voltage()
        check("電瓶電壓", v is not None, f"{v:.2f} V" if v is not None else "讀不到")

        driving = getattr(args, "driving", False)
        skipped = []
        if driving:
            skipped = sorted(DRIVING_SKIP)
            say(f"行駛中模式：跳過 {', '.join(skipped)} (安全系統不在行駛中連線；09 行駛中會拒絕)")
        for mod in UDS_MODULES.values():
            if driving and mod.address in DRIVING_SKIP:
                continue
            try:
                with t.client(mod) as c:
                    part = uds.decode_did(0xF187, retry_elm(lambda: c.read_did(0xF187))).replace(" ", "")
                check(f"UDS {mod.address} {mod.name}", part == mod.part_no.replace(" ", ""),
                      f"讀到 {part}，VCDS {mod.part_no}")
            except (uds.NoResponse, uds.NegativeResponse, ElmError) as e:
                check(f"UDS {mod.address} {mod.name}", False, str(e))

        if driving:
            # No address scan (it would knock on 03 / 44 too): read the part number at
            # each saved address instead.
            found, modules = {}, dict(load_map(args) or {})
            if not modules:
                raise CliError("行駛中模式需要先前存好的 TP2.0 位址表 (停車時跑過 tp20-probe / verify)。", "usage")
            for addr, expected in TP20_PART_NUMBERS.items():
                if addr in DRIVING_SKIP:
                    continue
                if addr not in modules:
                    check(f"TP2.0 {addr} {TP20_MODULES[addr]}", False, "位址表裡沒有")
                    continue
                try:
                    part = read_tp20_part(t, int(modules[addr], 16))
                    check(f"TP2.0 {addr} {TP20_MODULES[addr]}", part == expected,
                          f"位址 {modules[addr]} 讀到 {part}" + ("" if part == expected else f"，預期 {expected}"))
                except (*TP20_RETRY_ERRORS, uds.NegativeResponse) as e:
                    check(f"TP2.0 {addr} {TP20_MODULES[addr]}", False, f"位址 {modules[addr]}: {e}")
        else:
            found, modules = probe_tp20(t, args.start, args.end, known_dests(args))
            for addr in TP20_PART_NUMBERS:
                check(f"TP2.0 {addr} {TP20_MODULES[addr]}", addr in modules,
                      f"位址 {modules[addr]}" if addr in modules else "沒找到")

        if "09" in modules and not driving:
            hit = None
            for req in tp20.CODING_READS:
                try:
                    data = kwp_call(t, int(modules["09"], 16), lambda k: k.read_raw(req))
                except (*TP20_RETRY_ERRORS, uds.NegativeResponse):
                    continue
                if VCDS_TRUTH["09_coding"] in data:
                    hit = bytes(req).hex(" ").upper()
                    break
            check("09 長編碼與 VCDS 相同", hit is not None,
                  f"在 {hit} 找到" if hit else "各讀取方式中都沒找到")

        for addr in ("62", "72"):
            if addr not in modules:
                continue
            try:
                codes = [d["code"] for d in kwp_call(t, int(modules[addr], 16), lambda k: k.read_dtcs())]
            except (*TP20_RETRY_ERRORS, uds.NegativeResponse) as e:
                check(f"{addr} 故障碼 {VCDS_TRUTH['door_dtc']:05d}", False, str(e))
                continue
            # Faults come and go (72's 02115 was cleared on 2026-11-03 22:28), so this
            # checks that the door's fault memory reads, and reports what is in it.
            check(f"{addr} 故障碼讀取", True, f"讀到 {', '.join(f'{c:05d}' for c in codes) or '無'}"
                  f" (VCDS 當時: {VCDS_TRUTH['door_dtc']:05d})")

    passed = sum(c["ok"] for c in checks)
    say(f"\n結果: {passed}/{len(checks)} 項通過")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{datetime.datetime.now():%Y%m%d-%H%M%S}_verify{'_sim' if is_sim(args) else ''}.txt"
    path.write_text("\n".join(f"[{'通過' if c['ok'] else '失敗'}] {c['name']}: {c['detail']}" for c in checks)
                    + f"\n\n結果: {passed}/{len(checks)} 項通過\n", encoding="utf-8")
    if not driving:
        save_map(args, found, modules)
    say(f"已存檔: {path}")
    return {"checks": checks, "passed": passed, "total": len(checks), "report": str(path),
            "skipped": skipped, "ok": passed == len(checks)}


def cmd_features(args):
    dest = tp20_dest(args, "09")
    with Transport(args) as t:
        coding = kwp_call(t, dest, features.read_coding)
    say(f"09 中央電子長編碼: {coding.hex().upper()}\n")
    rows = []
    for f in features.FEATURES:
        on = features.is_on(coding, f)
        rows.append({"key": f.key, "name": f.name, "on": on, "byte": f.byte, "bit": f.bit,
                     "verified": f.verified, "note": f.note})
        say(f"[{'開' if on else '關'}] {f.key:<20} {f.name:<14} Byte {f.byte:>2} Bit {f.bit}  ({f.verified})  {f.note}")
    return {"coding": coding.hex().upper(), "features": rows}


def cmd_feature(args):
    f = features.BY_KEY.get(args.key)
    if f is None:
        raise CliError(f"沒有這個功能: {args.key}。可用的功能請執行 features 查看。")
    dest = tp20_dest(args, "09")
    on = args.state == "on"
    say(f"{f.name} ({f.key}) -> {'開' if on else '關'}")
    with Transport(args) as t:
        voltage = t.voltage()
        # Writes are sent once only (tries=1): never repeat a write the user approved once.
        return run_write(args, lambda confirm: kwp_call(t, dest, lambda k: features.set_feature(
            k, f, on, voltage=voltage, confirm=confirm), tries=1),
            summary=f"09 {f.name} ({f.key}) -> {'開' if on else '關'}")


def cmd_adapt_write(args):
    """Store one adaptation value of a TP2.0 module (format sniffed from VCDS, 2026-11-06)."""
    dest = tp20_dest(args)
    mod = Module(args.module, TP20_MODULES.get(args.module, args.module),
                 TP20_PART_NUMBERS.get(args.module, ""), 0, 0)
    with Transport(args) as t:
        voltage = t.voltage()
        return run_write(args, lambda confirm: kwp_call(t, dest, lambda k: writer.write_adaptation(
            k, mod, args.channel, args.value, voltage=voltage, confirm=confirm), tries=1),
            summary=f"{args.module} 調整通道 {args.channel} -> {args.value}")


def cmd_coding_rewrite(args):
    dest = tp20_dest(args, "09")
    with Transport(args) as t:
        voltage = t.voltage()
        return run_write(args, lambda confirm: kwp_call(t, dest, lambda k: features.rewrite_coding(
            k, voltage=voltage, confirm=confirm), tries=1),
            summary="09 長編碼原值寫回 (驗證寫入流程，不改任何設定)")


def confirmer(args, summary):
    """How a write gets its go-ahead: an applied plan, a new plan, --yes or YES typed."""
    if getattr(args, "plan_obj", None):
        return plans.applier(args.plan_obj)
    if args.plan or (args.json and not args.yes):
        return plans.planner(args.cmd_argv, summary, write_target(args))

    def confirm(old, new):
        if new is None:
            say(f"即將清除 {len(old)} 個故障碼")
        else:
            say(f"舊值: {old.hex(' ').upper()}\n新值: {new.hex(' ').upper()}")
        if args.yes:
            say("(--yes 已確認)")
            return True
        return input("確定寫入嗎? 輸入 YES 繼續: ").strip() == "YES"
    return confirm


def run_write(args, fn, summary):
    try:
        result = fn(confirmer(args, summary))
    except plans.PlanCreated as p:
        plan = p.plan
        say(f"已建立寫入計畫 {plan['id']} (尚未寫入): {plan['summary']}")
        say(f"  舊值: {plan['old']}\n  新值: {plan['new']}")
        say(f"  {plan['expires']} 前執行 `apply {plan['id']}` 才會寫入，只送一次")
        return {"status": "planned", "plan": plan}
    except plans.PlanMismatch as e:
        raise CliError(str(e), "refused")
    except writer.WriteRefused as e:
        raise CliError(f"已拒絕: {e}", "refused")
    except writer.VerifyFailed as e:
        raise CliError(f"驗證失敗: {e}", "verify_failed")
    except (*TP20_RETRY_ERRORS, uds.NegativeResponse) as e:
        raise CliError(f"寫入失敗 (只送一次，未重試): {e}。請重新讀取確認車上的值。", "connection")
    say(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return plans.describe(result)


def cmd_write_did(args):
    mod = module_arg(args.module)
    new = bytes.fromhex(args.value)
    with Transport(args) as t:
        voltage = t.voltage()
        with t.client(mod) as c:
            return run_write(args, lambda confirm: writer.write_did(c, mod, args.did, new, voltage=voltage,
                                                                    confirm=confirm),
                             summary=f"{mod.address} {mod.name} DID {args.did:04X} -> {new.hex().upper()}")


def cmd_restore(args):
    mod = module_arg(args.module)
    with Transport(args) as t:
        voltage = t.voltage()
        with t.client(mod) as c:
            return run_write(args, lambda confirm: writer.restore(c, mod, args.backup, voltage=voltage,
                                                                  confirm=confirm),
                             summary=f"{mod.address} {mod.name} 用備份 {args.backup} 還原")


def cmd_clear_dtc(args):
    if args.module in UDS_MODULES:
        mod = UDS_MODULES[args.module]
        with Transport(args) as t, t.client(mod) as c:
            return run_write(args, lambda confirm: writer.clear_dtcs(c, mod, confirm=confirm),
                             summary=f"{mod.address} {mod.name} 清除故障碼")
    if args.module not in TP20_MODULES:
        raise CliError(f"模組 {args.module} 不在 UDS 或 TP2.0 清單")
    dest = tp20_dest(args)
    mod = argparse.Namespace(address=args.module, name=TP20_MODULES[args.module])
    with Transport(args) as t:
        # tries=1: the clear goes out once, like every write.
        return run_write(args, lambda confirm: kwp_call(t, dest, lambda k: writer.clear_dtcs(
            k, mod, confirm=confirm), tries=1), summary=f"{mod.address} {mod.name} 清除故障碼")


def write_target(args):
    """Where a write goes, so a plan made on the simulator cannot be applied to the car."""
    if args.sim:
        return "sim"
    if args.sim_elm:
        return "sim-elm"
    if args.elm:
        return f"elm:{args.elm.upper()}"
    return f"can:{args.interface}:{args.channel}"


def cmd_apply(args):
    try:
        plan = plans.load(args.plan_id, write_target(args))
    except plans.PlanMismatch as e:
        raise CliError(str(e), "refused")
    say(f"執行寫入計畫 {plan['id']}: {plan['summary']}")
    inner = build_parser().parse_args(transport_argv(args) + plan["argv"])
    if inner.cmd not in WRITE_COMMANDS or inner.cmd == "apply":
        raise CliError(f"計畫 {plan['id']} 的指令 {inner.cmd} 不是寫入指令", "refused")
    inner.plan_obj, inner.cmd_argv, inner.json = plan, plan["argv"], args.json
    result = inner.fn(inner)
    plans.mark_applied(plan, result)
    return {"plan": plan["id"], "result": result}


def cmd_commands(args):
    """The command catalog as data, for AI agents and scripts."""
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {c.dest: c.help for c in sub._choices_actions}
    catalog = []
    for name, p in sub.choices.items():
        params = [{"name": a.dest, "required": a.required or not a.option_strings,
                   "option": a.option_strings[0] if a.option_strings else None,
                   "help": a.help, "choices": list(a.choices) if a.choices else None,
                   "default": a.default if a.option_strings else None}
                  for a in p._actions if a.dest != "help"]
        catalog.append({"name": name, "help": helps.get(name), "writes_to_car": name in WRITE_COMMANDS,
                        "params": params})
    globals_ = [{"option": a.option_strings[0], "help": a.help} for a in parser._actions
                if a.option_strings and a.dest != "help"]
    for c in catalog:
        say(f"{c['name']:<12} {'[寫入] ' if c['writes_to_car'] else ''}{c['help']}")
    return {"tool": "yeti_diag", "version": TOOL_VERSION, "global_options": globals_, "commands": catalog,
            "exit_codes": EXIT_CODES,
            "write_flow": "寫入指令加 --json 或 --plan 只會產生計畫 (含舊值/新值)，"
                          "人確認後執行 `apply <id>` 才寫入，只送一次；計畫 15 分鐘後失效"}


def hex_int(text):
    return int(text, 16)


class Parser(argparse.ArgumentParser):
    """Argument errors become CliError, so --json callers get them as JSON too.
    Subcommand parsers are created with the same class."""

    def error(self, message):
        raise CliError(f"{self.prog}: {message}", "usage")


def build_parser():
    p = Parser(prog="yeti_diag", description="Skoda Yeti 診斷工具")
    p.add_argument("--sim", action="store_true", help="使用內建 CAN 模擬器，不接車")
    p.add_argument("--sim-elm", action="store_true", help="使用模擬的 ELM327，不接車")
    p.add_argument("--elm", metavar="COM", help="ELM327 的 COM 埠 (USB 或藍牙配對後的序列埠)；auto = 自動找；只有 BLE 的接頭用 BLE:名稱或位址")
    p.add_argument("--baud", type=int, default=38400, help="ELM327 鮑率 (預設 38400)")
    p.add_argument("--interface", default="slcan", help="python-can 介面 (slcan / gs_usb / pcan ...)")
    p.add_argument("--channel", default="COM3", help="python-can 通道，例如 COM3 或 PCAN_USBBUS1")
    p.add_argument("--timeout", type=float, default=1.0, help="每個請求的逾時秒數")
    p.add_argument("--tp20-fast", action="store_true",
                   help="實驗：舊模組 (TP2.0) 一次收完整個回覆，比較快；尚未在實車驗證")
    p.add_argument("--json", action="store_true",
                   help="stdout 只輸出一份 JSON (給 AI/程式讀)，過程訊息改到 stderr")
    p.add_argument("--plan", action="store_true", help="寫入指令只產生計畫，不寫入 (之後用 apply 執行)")
    p.add_argument("--yes", action="store_true", help="寫入時不再詢問 (其他安全檢查仍會執行)")
    p.add_argument("--allow-moving", action="store_true",
                   help="不檢查車速 (預設行駛中拒絕連舊模組與寫入；只給台架 / 頂車測試用)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("autoscan", help="全車掃描：所有模組的零件號碼、編碼、故障碼 (含說明) 並存檔") \
        .add_argument("--probe", action="store_true", help="重新找舊模組位址，不用存檔的位址表")
    sub.choices["autoscan"].set_defaults(fn=cmd_autoscan)
    sub.add_parser("scan", help="只掃 UDS 模組的識別資料與故障碼並存檔").set_defaults(fn=cmd_scan)
    sub.add_parser("probe", help="確認各模組的 CAN ID 是否正確").set_defaults(fn=cmd_probe)
    sub.add_parser("obd", help="讀引擎 OBD 即時數據與故障碼").set_defaults(fn=cmd_obd)
    sub.add_parser("readiness", help="排放自我檢測完成狀態 (VCDS Readiness)，清碼或拔電瓶後看是否已跑完").set_defaults(fn=cmd_readiness)
    s = sub.add_parser("misfire", help="各缸失火次數 (OBD-II Mode $06，公開標準)")
    s.add_argument("--cylinders", type=int, default=4, help="汽缸數 (1.2 TSI = 4)")
    s.set_defaults(fn=cmd_misfire)
    sub.add_parser("voltage", help="讀電瓶電壓").set_defaults(fn=cmd_voltage)
    s = sub.add_parser("battery", help="電瓶檢查：靜置電量 / 充電電壓；--crank 秒數 = 記錄啟動壓降 (開始後轉鑰匙發動)")
    s.add_argument("--crank", type=float, metavar="SECONDS",
                   help=f"記錄啟動時的電壓 (秒，最多 {CRANK_MAX_SECONDS})；引擎要先熄火、點火開關 ON")
    s.set_defaults(fn=cmd_battery)
    s = sub.add_parser("ble-scan", help="列出附近的 BLE 裝置 (給只有 BLE、沒有 COM 埠的接頭用)")
    s.add_argument("--seconds", type=float, default=6.0, help="掃描秒數 (預設 6)")
    s.set_defaults(fn=cmd_ble_scan)

    s = sub.add_parser("dtc", help="讀單一 UDS 模組故障碼")
    s.add_argument("module", help="模組位址，例如 17")
    s.add_argument("--details", action="store_true", help="每個故障碼加讀凍結資料 (19 04) 與延伸資料 (19 06)，原始位元組")
    s.set_defaults(fn=cmd_dtc)

    s = sub.add_parser("did", help="讀單一 UDS 模組的 DID (十六進位)")
    s.add_argument("module", help="模組位址，例如 17")
    s.add_argument("dids", nargs="+", type=hex_int, help="DID，十六進位，例如 F187")
    s.set_defaults(fn=cmd_did)

    s = sub.add_parser("scan-dids", help="掃描一段 DID 範圍，找出模組支援的數值")
    s.add_argument("module", help="模組位址，例如 17")
    s.add_argument("start", type=hex_int, help="起始 DID (十六進位)")
    s.add_argument("end", type=hex_int, help="結束 DID (十六進位)")
    s.set_defaults(fn=cmd_scan_dids)

    s = sub.add_parser("write-did", help="寫入 UDS DID (先備份、檢查、確認，寫後驗證)")
    s.add_argument("module", help="模組位址，例如 17")
    s.add_argument("did", type=hex_int, help="DID (十六進位)")
    s.add_argument("value", help="新值，十六進位，例如 004B")
    s.set_defaults(fn=cmd_write_did)

    s = sub.add_parser("restore", help="用備份檔把 DID 還原")
    s.add_argument("module", help="模組位址，例如 17")
    s.add_argument("backup", help="backups 資料夾裡的備份檔")
    s.set_defaults(fn=cmd_restore)

    sub.add_parser("candidates", help="讀設定檔裡還沒確認的 DID / 群組原始值，對照其他軟體畫面用").set_defaults(
        fn=cmd_candidates)
    sub.add_parser("profile", help="目前使用的車型設定檔 (換車型用 YETI_DIAG_PROFILE)").set_defaults(fn=cmd_profile)
    s = sub.add_parser("discover", help="別台車：掃描車上所有模組 (只讀)，自動產生車型設定檔")
    s.add_argument("--name", required=True, help="設定檔名稱 (英數字、底線)，例如 golf6_2010")
    s.add_argument("--title", help="顯示用的車名，例如 'VW Golf 6 1.4 TSI 2010'")
    s.set_defaults(fn=cmd_discover)
    sub.add_parser("values", help="讀已在實車確認的數值 (總里程等)").set_defaults(fn=cmd_values)
    s = sub.add_parser("sniff", help="只聽不發：錄下其他診斷軟體 (VCDS + Y 接線) 的封包並存檔")
    s.add_argument("--seconds", type=float, help="錄幾秒 (不給就錄到按 Ctrl+C)")
    s.add_argument("--module", help="只收這個 UDS 模組的封包，例如 17")
    s.add_argument("--ids", help="只收這些 CAN ID (十六進位，逗號分隔)，例如 714,77E")
    s.set_defaults(fn=cmd_sniff)
    s = sub.add_parser("sniff-analyze", help="分析監聽檔：還原請求/回覆、列出寫入動作；加 --vcds 自動對照數值名稱")
    s.add_argument("file", help="sniff 存的檔案 (reports 裡的檔名或完整路徑)")
    s.add_argument("--vcds", help="同時間 VCDS 記錄的 CSV")
    s.add_argument("--vcds-start", help="VCDS 記錄開始時間 (CSV 第一行沒有時才需要)，例如 2026-11-05 07:30:15")
    s.add_argument("--tolerance", type=float, default=0.6, help="時間配對容許秒數 (預設 0.6)")
    s.set_defaults(fn=cmd_sniff_analyze)

    s = sub.add_parser("clear-dtc", help="清除模組故障碼，UDS 與 TP2.0 都可 (先備份，清除後驗證)")
    s.add_argument("module", help="模組位址，例如 17")
    s.set_defaults(fn=cmd_clear_dtc)

    for name, fn, text in (("tp20-probe", cmd_tp20_probe, "找出舊模組 (TP2.0) 的位址並存檔"),
                           ("verify", cmd_verify, "與 VCDS 結果比對，確認本工具讀得正確")):
        s = sub.add_parser(name, help=text)
        s.add_argument("--start", type=hex_int, default=0x01, help="TP2.0 起始位址 (十六進位)")
        s.add_argument("--end", type=hex_int, default=0x7F, help="TP2.0 結束位址 (十六進位)")
        if name == "verify":
            s.add_argument("--driving", action="store_true",
                           help="行駛中：跳過 03 / 09 / 15 / 44，不掃位址，改讀存好位址的零件號碼")
        s.set_defaults(fn=fn)

    s = sub.add_parser("kwp-dtc", help="讀舊模組 (TP2.0) 故障碼 (含說明)，例如 kwp-dtc 62")
    s.add_argument("module", help="模組位址 (需先 tp20-probe) 或 TP2.0 位址，例如 0x20")
    s.add_argument("--details", action="store_true", help="每個故障碼加讀凍結資料 (里程、次數、日期時間)")
    s.set_defaults(fn=cmd_kwp_dtc)

    s = sub.add_parser("kwp-ident", help="讀舊模組 (TP2.0) 的識別資料與編碼")
    s.add_argument("module", help="模組位址 (需先 tp20-probe) 或 TP2.0 位址，例如 0x20")
    s.set_defaults(fn=cmd_kwp_ident)

    s = sub.add_parser("measure", help="讀舊模組 (TP2.0) 測量群組，例如 measure 19 1 2")
    s.add_argument("module", help="模組位址 (需先 tp20-probe) 或 TP2.0 位址，例如 0x1F")
    s.add_argument("groups", nargs="+", type=int, help="群組編號 (十進位，1-255)")
    s.add_argument("--repeat", type=int, default=1, help="重複讀取次數 (看數值變化)")
    s.set_defaults(fn=cmd_measure)

    s = sub.add_parser("adapt", help="讀舊模組 (TP2.0) 調整通道 (唯讀)，例如 adapt 09 31 25")
    s.add_argument("module", help="模組位址，例如 09")
    s.add_argument("channels", nargs="+", type=int, help="通道編號 (十進位)；00 清除學習值不提供")
    s.set_defaults(fn=cmd_adapt)

    sub.add_parser("features", help="列出 09 中央電子隱藏功能目前的開關狀態").set_defaults(fn=cmd_features)

    s = sub.add_parser("adapt-write", help="寫入舊模組調整值 (先備份、檢查、確認，寫後讀回)，例如 adapt-write 09 31 4")
    s.add_argument("module", help="模組位址，例如 09")
    s.add_argument("channel", type=int, help="通道 (1-255)")
    s.add_argument("value", type=int, help="新值 (十進位)")
    s.set_defaults(fn=cmd_adapt_write)
    s = sub.add_parser("coding-rewrite", help="09 長編碼原值寫回：在實車驗證寫入流程，不改任何設定")
    s.set_defaults(fn=cmd_coding_rewrite)
    s = sub.add_parser("feature", help="開關一個隱藏功能 (先備份、檢查、確認，寫後驗證)")
    s.add_argument("key", help="功能代號，例如 rear-wiper-drip")
    s.add_argument("state", choices=["on", "off"], help="on 開 / off 關")
    s.set_defaults(fn=cmd_feature)

    s = sub.add_parser("apply", help="執行先前產生的寫入計畫 (只送一次)")
    s.add_argument("plan_id", help="計畫編號 (寫入指令加 --plan 或 --json 時產生)")
    s.set_defaults(fn=cmd_apply)

    sub.add_parser("commands", help="列出所有指令與參數 (加 --json 給 AI 讀)").set_defaults(fn=cmd_commands)
    return p


TRANSPORT_OPTIONS = ("sim", "sim_elm", "elm", "baud", "interface", "channel", "timeout", "tp20_fast")


def transport_argv(args):
    """The transport options of this run, to re-parse a plan's command with."""
    out = []
    for name in TRANSPORT_OPTIONS:
        value = getattr(args, name)
        flag = "--" + name.replace("_", "-")
        if value is True:
            out.append(flag)
        elif value not in (None, False):
            out += [flag, str(value)]
    return out


def envelope(args, data, error=None):
    return {"ok": error is None and (not isinstance(data, dict) or data.get("ok", True)),
            "command": getattr(args, "cmd", None), "tool": "yeti_diag", "version": TOOL_VERSION,
            "time": datetime.datetime.now().isoformat(timespec="seconds"), "vin": VIN,
            "sim": is_sim(args) if args else None, "data": data, "error": error}


def run(args):
    """Run one command; returns (exit kind, result dict, error dict or None)."""
    try:
        data = args.fn(args)
        ok = not isinstance(data, dict) or data.get("ok", True)
        return ("ok" if ok else "check_failed"), data, None
    except CliError as e:
        return e.kind, None, {"kind": e.kind, "message": str(e)}
    except (uds.NoResponse, tp20.Tp20Error, ElmError, OSError) as e:
        return "connection", None, {"kind": "connection", "message": f"{type(e).__name__}: {e}"}
    except uds.NegativeResponse as e:
        return "check_failed", None, {"kind": "negative_response", "message": str(e)}
    except Exception as e:  # noqa: BLE001 - report, never hide, an internal bug
        return "internal", None, {"kind": "internal", "message": f"{type(e).__name__}: {e}",
                                  "traceback": traceback.format_exc()}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(argv)
    except CliError as e:
        error = {"kind": "usage", "message": str(e)}
        if "--json" in argv:
            print(json.dumps(envelope(None, None, error), ensure_ascii=False, indent=2))
        else:
            print(error["message"], file=sys.stderr)
        sys.exit(EXIT_CODES["usage"])
    args.cmd_argv = argv[argv.index(args.cmd):] if args.cmd in argv else [args.cmd]
    if args.json:
        with contextlib.redirect_stdout(sys.stderr):
            kind, data, error = run(args)
        print(json.dumps(envelope(args, data, error), ensure_ascii=False, indent=2, default=str))
    else:
        kind, data, error = run(args)
        if error:
            print(error["message"], file=sys.stderr)
            if error.get("traceback"):
                print(error["traceback"], file=sys.stderr)
    sys.exit(EXIT_CODES[kind])
