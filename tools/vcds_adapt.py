"""Read the documented adaptation channels of the open VCDS Adaptation screen (read only).

Only 'Read' is pressed. Test / Save are never touched (not in the allow list).
Channel 00 (learned value clearing) and key-count adaptation are skipped.
usage: py -3.13 tools/vcds_adapt.py <module> [out.json]
"""
import ctypes, json, sys, time
from pathlib import Path
import win32con, win32gui
sys.path.insert(0, str(Path(__file__).parent))
import vcds_night as v  # noqa: E402

v.ALLOWED |= {"&Read"}
SKIP = ("Learned Value clearing", "Remote Control Adaptation", "Key")


def items(cb):
    out = []
    for i in range(win32gui.SendMessage(cb, win32con.CB_GETCOUNT, 0, 0)):
        n = win32gui.SendMessage(cb, win32con.CB_GETLBTEXTLEN, i, 0)
        buf = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.SendMessageW(cb, win32con.CB_GETLBTEXT, i, buf)
        out.append(buf.value)
    return out


def main(addr, out):
    a = v.find_top(r"Adaptation$")
    cb = next(c for c in v.kids(a) if win32gui.GetClassName(c) == "ComboBox")
    ch_edit = next(c for c in v.kids(a) if win32gui.GetClassName(c) == "Edit"
                   and win32gui.GetWindowRect(c)[1] - win32gui.GetWindowRect(a)[1] < 280)
    cid = win32gui.GetDlgCtrlID(cb)
    res = []
    for i, name in enumerate(items(cb)):
        if i == 0 or any(s in name for s in SKIP):
            continue
        win32gui.SendMessage(cb, win32con.CB_SETCURSEL, i, 0)
        win32gui.SendMessage(a, win32con.WM_COMMAND, (win32con.CBN_SELCHANGE << 16) | cid, cb)
        time.sleep(0.8)
        ch = v.text(ch_edit)
        if not ch.isdigit() or int(ch) == 0:
            print("skip, channel box shows", repr(ch), name); continue
        v.click(a, "&Read")
        time.sleep(2.0)
        v.popups(lambda o: res.append({"popup": o}))
        shot = v.snapshot(a)
        texts = [x[5] for x in shot if x[0] == "Static" and x[5].strip()]
        res.append({"module": addr, "item": name, "channel": int(ch), "texts": texts, "t": time.time()})
        print(f"{int(ch):3d} {name[:45]:<45} {' | '.join(texts)[:120]}")
    Path(out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else f"reports/adapt_{sys.argv[1]}.json")
