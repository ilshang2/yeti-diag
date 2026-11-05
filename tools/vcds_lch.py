"""Read every byte page of the VCDS Long Coding Helper for one module (read only).

Opens module -> Coding - 07 -> Long Coding Helper, steps the byte number box,
screenshots the bit labels of each byte, and checks before every page that the
coding string in the helper is still the original. Closes the helper without
transfer and cancels the coding screen. Nothing is written.

usage: py -3.13 tools/vcds_lch.py 09 [out_dir]
"""
import ctypes, sys, time
from pathlib import Path
import win32con, win32gui
from PIL import Image, ImageGrab

sys.path.insert(0, str(Path(__file__).parent))
import vcds_night as v  # noqa: E402

ctypes.windll.user32.SetProcessDPIAware()
v.ALLOWED |= {"&Coding - 07", "Coding - 07", "Long Coding Helper", "Cancel"}


def main(addr, out_dir, attach=False):
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    if attach:
        r = v.find_top(r"Recode Module")
        edits = [c for c in v.kids(r) if win32gui.GetClassName(c) == "Edit"]
        orig = v.text(edits[0]).strip()
        h = v.find_top(r"Long Coding - \d+ Bytes")
        return page(addr, out_dir, r, edits, orig, h)
    for pat in (r"Long Coding - \d+ Bytes", r"Recode Module"):  # leftovers: close, never transfer
        h = v.find_top(pat)
        if h:
            win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0); time.sleep(1.5)
    sel = v.back_to_select(print)
    v.click(sel, v.module_buttons(sel)[addr])
    o = v.find_top(r"Open Controller", 25)
    if not o:
        print("open failed"); return 1
    title = win32gui.GetWindowText(o)
    print("opened:", title)
    if f": {addr}-" not in title:
        print("WRONG MODULE, stopping"); v.back_to_select(print); return 1
    if not (v.click(o, "&Coding - 07") or v.click(o, "Coding - 07")):
        print("no coding button"); return 1
    r = v.find_top(rf": {addr}-.*Recode Module", 15)  # only this module's coding window
    if not r:
        print("no coding window for", addr); v.back_to_select(print); return 1
    for _ in range(20):  # the coding is read from the module after the window opens
        edits = [c for c in v.kids(r) if win32gui.GetClassName(c) == "Edit"]
        if edits and v.text(edits[0]).strip():
            break
        time.sleep(0.5)
    else:
        print("coding not loaded", [v.text(c) for c in v.kids(r) if v.text(c)][:6])
        win32gui.PostMessage(r, win32con.WM_CLOSE, 0, 0); time.sleep(1); v.back_to_select(print)
        return 1
    orig = v.text(edits[0]).strip()
    print("coding", orig, len(orig) // 2, "bytes")
    v.click(r, "Long Coding Helper")
    h = v.find_top(r"Long Coding - \d+ Bytes", 20)
    if not h:
        print("no helper"); return 1
    time.sleep(1.5)
    return page(addr, out_dir, r, edits, orig, h)


def page(addr, out_dir, r, edits, orig, h):
    eds = [c for c in v.kids(h) if win32gui.GetClassName(c) == "Edit"]
    code = next(c for c in eds if v.text(c) == orig)
    idx = next(c for c in eds if v.text(c).isdigit() and c != code and win32gui.GetWindowRect(c)[0] -
               win32gui.GetWindowRect(h)[0] < 60)  # byte number box sits at the left edge
    win32gui.SetForegroundWindow(h)
    n = len(orig) // 2
    for b in range(n):
        win32gui.SendMessage(idx, win32con.WM_SETTEXT, 0, str(b))
        win32gui.PostMessage(idx, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
        win32gui.PostMessage(idx, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
        time.sleep(0.7)
        if v.text(code) != orig:
            print("CODING CHANGED in helper, stopping:", v.text(code)); break
        x0, y0, x1, y1 = win32gui.GetWindowRect(h)
        ImageGrab.grab(bbox=(x0, y0 + 82, x0 + 780, y1), all_screens=True).save(out_dir / f"{addr}_byte{b:02d}.png")
    # sheets of 6 pages for reading
    pages = sorted(out_dir.glob(f"{addr}_byte*.png"))
    for s in range(0, len(pages), 6):
        ims = [Image.open(p) for p in pages[s:s + 6]]
        w, hh = ims[0].size
        sheet = Image.new("RGB", (w * 2, hh * ((len(ims) + 1) // 2)), "white")
        for i, im in enumerate(ims):
            sheet.paste(im, ((i % 2) * w, (i // 2) * hh))
        sheet.save(out_dir / f"{addr}_sheet{s // 6}.png")
    win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)  # close without transfer
    time.sleep(2)
    new_box = edits[1] if len(edits) > 1 else None
    print("new coding box after helper:", repr(v.text(new_box)) if new_box else None)
    v.click(r, "Cancel")
    time.sleep(1)
    v.back_to_select(print)
    print("done", len(pages), "pages")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--attach"]
    sys.exit(main(args[0], args[1] if len(args) > 1 else "reports/lch", "--attach" in sys.argv))
