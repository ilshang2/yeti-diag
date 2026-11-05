"""Read-only VCDS extras while the ELM327 sniffs on the Y cable:
1. adaptation channels (documented list, then typed channels) - only 'Read' is pressed
2. fault code screens of KWP modules (for freeze-frame format)
Writes: none. Test / Save / Clear buttons are not in the allow list.
usage: py -3.13 tools/vcds_more.py --elm COM10
"""
import argparse, json, re, sys, time
from pathlib import Path
import win32con, win32gui
sys.path.insert(0, str(Path(__file__).parent))
import vcds_night as v  # noqa: E402
import vcds_adapt as ad  # noqa: E402

v.ALLOWED |= {"Adaptation - 10", "&Adaptation - 10", "&Read", "&Fault Codes - 02", "Fault Codes - 02"}
SKIP_CHANNELS = {0, 1}  # 00 = clear learned values, 01 = key count / key learning on the 09


def open_module(addr, button):
    sel = v.back_to_select(print)
    v.click(sel, v.module_buttons(sel)[addr])
    o = v.find_top(r"Open Controller", 25)
    if not o:
        return None
    time.sleep(2.5)
    o = v.find_top(r"Open Controller")
    if f": {addr}-" not in win32gui.GetWindowText(o):
        print("wrong module", win32gui.GetWindowText(o)); return None
    for b in button:
        if v.click(o, b):
            return o
    return None


def typed_channels(addr, chans, record):
    a = v.find_top(r"Adaptation$", 15)
    if not a:
        return
    time.sleep(1.5)
    ed = next(c for c in v.kids(a) if win32gui.GetClassName(c) == "Edit"
              and win32gui.GetWindowRect(c)[1] - win32gui.GetWindowRect(a)[1] < 280)
    for ch in chans:
        if ch in SKIP_CHANNELS:
            continue
        win32gui.SendMessage(ed, win32con.WM_SETTEXT, 0, f"{ch:02d}")
        time.sleep(0.2)
        v.click(a, "&Read")
        time.sleep(1.6)
        pops = v.popups(record)
        texts = [x[5] for x in v.snapshot(a) if x[0] == "Static" and x[5].strip()]
        ok = not any(t == "ERROR:" for t in texts)
        record({"module": addr, "kind": "adaptation", "channel": ch, "texts": texts, "popups": pops})
        if ok:
            print(f"  ch {ch:3d}: {' | '.join(texts)[:110]}")
    v.click(a, "Done, Go &Back"); time.sleep(2)


def fault_screen(addr, record):
    f = v.find_top(r"Fault Codes", 30)
    if not f:
        print(addr, "no fault screen"); return
    time.sleep(6)
    texts = [v.text(c) for c in v.kids(f) if v.text(c)]
    record({"module": addr, "kind": "faults", "texts": texts})
    body = [t for t in texts if re.search(r"\d{5}|Freeze|Status|Priority|Mileage|Fault", t)]
    print(addr, "faults:", " / ".join(body)[:300].replace("\n", " "))
    for b in ("Done, Go &Back", "&Done, Go Back", "Done,Go Back", "Close"):
        v.ALLOWED.add(b)
        if v.click(f, b):
            break
    time.sleep(2)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--elm", default="COM10"); a = ap.parse_args()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = open(Path(v.REPORTS) / f"{stamp}_vcds_more.jsonl", "a", encoding="utf-8")

    def record(o):
        o.setdefault("t", time.time()); out.write(json.dumps(o, ensure_ascii=False) + "\n"); out.flush()
    v.keep_awake()
    volt = v.voltage(a); print("voltage", volt)
    if volt is None or volt < 12.2:
        print("STOP: voltage"); return 2
    sn = v.run_yeti(a, "sniff", "--seconds", "2400", background=True)
    time.sleep(6)
    try:
        # 1 documented adaptation channels, with sniffing this time
        for m in ("09", "03"):
            if open_module(m, ("Adaptation - 10", "&Adaptation - 10")):
                v.find_top(r"Adaptation$", 15); time.sleep(1.5)
                print("== adaptation documented", m)
                ad.main(m, str(Path(v.REPORTS) / f"{stamp}_adapt_{m}.json"))
                aw = v.find_top(r"Adaptation$")
                if aw:
                    v.click(aw, "Done, Go &Back"); time.sleep(2)
        # 2 all channels 2-99 on the 09
        if open_module("09", ("Adaptation - 10", "&Adaptation - 10")):
            print("== adaptation 09 channels 2-99")
            typed_channels("09", range(2, 100), record)
        # 3 fault code screens
        for m in ("09", "19", "62", "72"):
            if open_module(m, ("&Fault Codes - 02", "Fault Codes - 02")):
                fault_screen(m, record)
        v.back_to_select(print)
    finally:
        sn.terminate()
        try:
            sn.communicate(timeout=20)
        except Exception:
            sn.kill()
    print("ALL DONE", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
