"""讀兩個後門模組的測量群組 (KWP 0x21)。只讀取。

用法:
  py -3.13 tools\\door_blocks.py COM9 save      車門全關時讀取並存檔
  py -3.13 tools\\door_blocks.py COM9 compare   打開一扇後門後再讀，列出有變化的地方
  py -3.13 tools\\door_blocks.py COM9 watch [秒數] [62|72|all] [群組，例如 1 或 1,3]
        連續讀群組 1-8，有變化就印出 (開關訊號逐位元)，同時存 reports\\door_watch_*.txt。
        用來查 02115 門鎖單元訊號不合理：一邊鎖門 / 開鎖 / 拉門把 / 開關門，一邊看哪個訊號沒跟著變。
"""
import datetime
import json
import pathlib
import sys
import time

from yeti_diag import measure, tp20, uds
from yeti_diag.elm327 import Elm327, ElmError

DOORS = (0x24, 0x25)
DOOR_NAMES = {0x24: "62 左後門", 0x25: "72 右後門"}
GROUPS = range(1, 11)
WATCH_GROUPS = range(1, 9)  # 9 and 10 are not supported (7F 21 31) on these modules
REPORTS = pathlib.Path(__file__).resolve().parent.parent / "reports"
SNAPSHOT = REPORTS / "door_blocks_closed.json"


def read_all(link):
    result = {}
    for dest in DOORS:
        for group in GROUPS:
            key = f"0x{dest:02X}-{group:03d}"
            for attempt in range(3):
                ch = tp20.Tp20Channel(link, dest, timeout=1.5)
                try:
                    ch.open()
                    k = tp20.KwpClient(ch)
                    k.start_session()
                    result[key] = k.read_raw([0x21, group]).hex(" ").upper()
                    break
                except uds.NegativeResponse as e:
                    result[key] = f"不支援 ({e.nrc:02X})"
                    break
                except (uds.NoResponse, tp20.Tp20Error) as e:
                    result[key] = f"讀取失敗: {e}"
                finally:
                    ch.close()
            print(f"{key}: {result[key]}", flush=True)
    return result


def describe(raw):
    """One group's raw bytes -> list of value texts (switch triples as 8 bits)."""
    return [str(v["value"]) + (f" {v['unit']}" if v["unit"] and v["unit"] != "bits" else "")
            if v["value"] is not None else v["raw"] for v in measure.parse_group(raw)]


def watch(link, seconds, doors, out, groups=WATCH_GROUPS):
    """Read groups 1-8 of each door over and over; print what changed (one channel per round)."""
    last, end = {}, time.monotonic() + seconds
    print("開始監看。現在可以：遙控鎖門 / 開鎖、從車內拉門把、開關車門、按中控鎖鍵。Ctrl+C 結束。", flush=True)
    while time.monotonic() < end:
        for dest in doors:
            ch = tp20.Tp20Channel(link, dest, timeout=1.5)
            try:
                ch.open()
                k = tp20.KwpClient(ch)
                k.start_session()
                for g in groups:
                    raw = k.read_raw([0x21, g])
                    now = describe(raw)
                    key = (dest, g)
                    before = last.get(key)
                    last[key] = now
                    if before is None:
                        line = f"{DOOR_NAMES[dest]} 群組 {g}: " + " | ".join(now)
                    elif before != now:
                        diffs = [f"第 {i} 值 {b} → {n}" for i, (b, n) in enumerate(zip(before, now), 1) if b != n]
                        line = f"{DOOR_NAMES[dest]} 群組 {g} 變化: " + "；".join(diffs)
                    else:
                        continue
                    stamp = datetime.datetime.now().strftime("%H:%M:%S")
                    print(f"{stamp} {line}", flush=True)
                    out.write(f"{stamp} {line}\n")
                    out.flush()
            except (uds.NoResponse, uds.NegativeResponse, tp20.Tp20Error, ElmError) as e:
                print(f"{DOOR_NAMES[dest]}: 讀取失敗 ({e})，下一輪再試", flush=True)
                time.sleep(1.0)  # the gateway stays busy a moment after a bad channel end
            finally:
                ch.close()


def main():
    elm = Elm327(port=sys.argv[1])
    mode = sys.argv[2]
    try:
        if mode == "watch":
            seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 120
            pick = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "all" else None
            groups = [int(g) for g in sys.argv[5].split(",")] if len(sys.argv) > 5 else WATCH_GROUPS
            doors = [d for d in DOORS if pick is None or DOOR_NAMES[d].startswith(pick)]
            print(f"電壓 {elm.battery_voltage():.2f} V", flush=True)
            REPORTS.mkdir(exist_ok=True)
            path = REPORTS / f"door_watch_{datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
            with open(path, "w", encoding="utf-8") as out:
                try:
                    watch(tp20.ElmLink(elm), seconds, doors, out, groups)
                except KeyboardInterrupt:
                    print("停止")
            print(f"已存檔: {path}")
            return
        data = read_all(tp20.ElmLink(elm))
    finally:
        elm.close()
    if mode == "save":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已存檔 (車門全關): {SNAPSHOT}")
    else:
        closed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        print("\n=== 與車門全關時比較，有變化的群組 ===")
        changed = [k for k in data if data[k] != closed.get(k)]
        for k in changed:
            print(f"{k}\n  關門: {closed.get(k)}\n  現在: {data[k]}")
        if not changed:
            print("沒有任何變化")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
