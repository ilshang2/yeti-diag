"""Save an adaptation channel with the value it already has, in VCDS, while sniffing.

Purpose: record VCDS's adaptation write so this program can learn the format.
Nothing changes: the stored value is read first and the save is refused unless the
new value equals it. Save is pressed once. Only for a write the user approved.

usage: py -3.13 tools/vcds_adapt_save_same.py --elm COM10 09 31 5
"""
import argparse, json, re, sys, time
from pathlib import Path
import win32con, win32gui
sys.path.insert(0, str(Path(__file__).parent))
import vcds_night as v  # noqa: E402


def stored_value(a):
    """Number shown under 'Stored value' (the Static just below that label)."""
    lab = next(c for c in v.kids(a) if v.text(c) == "Stored value")
    ly = win32gui.GetWindowRect(lab)[1]
    cands = [c for c in v.kids(a) if win32gui.GetClassName(c) == "Static"
             and 0 < win32gui.GetWindowRect(c)[1] - ly < 30 and v.text(c).strip()]
    return v.text(cands[0]).strip() if cands else None


def main():
    # DISABLED 2026-11-06: VCDS ignored the value put into its 'New value' box with WM_SETTEXT
    # and saved 0 instead of 5 (31 BB 01 03 0000 ...). The 09 refused it (7F 31 31, out of
    # range) so nothing changed, but this tool could write a wrong value. Adaptation writes go
    # through yeti_diag itself, which builds the bytes it sends.
    print("已停用：VCDS 沒有收到填入的新值 (送出 0 而不是 5)，可能寫入錯誤的值。")
    return 3
    ap = argparse.ArgumentParser()
    ap.add_argument("--elm", default="COM10")
    ap.add_argument("module"); ap.add_argument("channel", type=int); ap.add_argument("value")
    a = ap.parse_args()
    v.ALLOWED |= {"Adaptation - 10", "&Adaptation - 10", "&Read"}
    volt = v.voltage(a); print("電壓", volt)
    if volt is None or volt < 12.2:
        print("電壓不足，停止"); return 2
    # A sniffer left from an earlier run keeps COM10 busy (seen 2026-11-06): stop it first.
    import subprocess
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name like 'py%'\" | "
                    "Where-Object { $_.CommandLine -match 'yeti_diag.* sniff' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"], capture_output=True)
    sn = None
    for attempt in range(3):  # the Bluetooth port often needs a few seconds after the voltage check
        time.sleep(8)
        t0 = time.time()
        sn = v.run_yeti(a, "sniff", "--seconds", "300", background=True)
        time.sleep(8)
        fresh = [p for p in Path(v.REPORTS).glob("*_sniff.txt") if p.stat().st_mtime >= t0 - 1]
        if sn.poll() is None and fresh:
            print("錄封包已啟動:", fresh[0].name)
            break
        print("錄封包沒啟動，重試")
        if sn.poll() is None:
            sn.kill()
    else:
        print("錄封包啟動失敗，停止 (沒有按 Save)"); return 1
    try:
        for _ in range(3):  # leftovers of an earlier run
            aw = v.find_top(r"Adaptation$")
            if aw:
                v.click(aw, "Done, Go &Back"); time.sleep(2)
        sel = v.back_to_select(print)
        v.click(sel, v.module_buttons(sel)[a.module])
        o = v.find_top(r"Open Controller", 25); time.sleep(3); o = v.find_top(r"Open Controller")
        if f": {a.module}-" not in win32gui.GetWindowText(o):
            print("模組不對，停止"); return 1
        if not (v.click(o, "Adaptation - 10") or v.click(o, "&Adaptation - 10")):
            print("沒有 Adaptation 按鈕"); return 1
        ad = v.find_top(r"Adaptation$", 15); time.sleep(1.5)
        ch_edit = next(c for c in v.kids(ad) if win32gui.GetClassName(c) == "Edit"
                       and win32gui.GetWindowRect(c)[1] - win32gui.GetWindowRect(ad)[1] < 280)
        win32gui.SendMessage(ch_edit, win32con.WM_SETTEXT, 0, f"{a.channel:02d}")
        v.click(ad, "&Read"); time.sleep(2.5)
        cur = stored_value(ad)
        print("通道", a.channel, "目前值", repr(cur))
        if cur != a.value:
            print("目前值和要存的值不同，停止 (只做原值儲存)"); return 1
        new_edit = next(c for c in v.kids(ad) if win32gui.GetClassName(c) == "Edit" and c != ch_edit)
        win32gui.SendMessage(new_edit, win32con.WM_SETTEXT, 0, a.value)
        time.sleep(0.5)
        if v.text(new_edit) != a.value:
            print("新值欄位不對，停止"); return 1
        # The capture must be alive right now: on 2026-11-06 the Bluetooth link dropped
        # just before Save and the write was not recorded.
        cap = max(Path(v.REPORTS).glob("*_sniff.txt"), key=lambda p: p.stat().st_mtime)
        s1 = cap.stat().st_size
        v.click(ad, "&Read"); time.sleep(2.0)  # makes traffic the sniffer must see
        s2, age = cap.stat().st_size, time.time() - cap.stat().st_mtime
        print(f"錄封包檢查: {cap.name} {s1} -> {s2} bytes，{age:.1f} 秒前更新")
        if s2 <= s1 or age > 2.0 or sn.poll() is not None:
            print("錄封包沒有在動，不按 Save"); return 1
        print("按 Save (一次)", time.strftime("%H:%M:%S"))
        v.ALLOWED.add("&Save")
        v.click(ad, "&Save")
        v.ALLOWED.discard("&Save")
        # VCDS may ask to confirm; answer Yes once, record every dialog text
        seen = []
        t_end = time.time() + 15
        answered = False
        while time.time() < t_end:
            for h in v.tops():
                try:
                    if win32gui.GetClassName(h) != "#32770" or win32gui.GetWindowText(h).startswith("VCDS Release"):
                        continue
                    body = " ".join(v.text(c) for c in v.kids(h) if win32gui.GetClassName(c) == "Static")
                    if not body.strip() or body in seen:
                        continue
                    seen.append(body); print("對話框:", body.replace("\n", " ")[:200])
                    for c in v.kids(h):
                        t = v.text(c) if win32gui.GetClassName(c) == "Button" else ""
                        if not answered and re.search(r"\(&Y\)|^&?Yes$", t):
                            win32gui.PostMessage(c, win32con.BM_CLICK, 0, 0); answered = True; print("回答：是")
                            break
                        if re.search(r"^(OK|&OK|確定)$", t):
                            win32gui.PostMessage(c, win32con.BM_CLICK, 0, 0); print("回答：確定")
                            break
                except win32gui.error:
                    pass
            time.sleep(0.4)
        texts = [x[5] for x in v.snapshot(ad) if x[0] == "Static" and x[5].strip()]
        print("儲存後畫面:", " | ".join(texts)[:250])
        v.click(ad, "&Read"); time.sleep(2.5)
        print("重讀目前值:", repr(stored_value(ad)))
        v.click(ad, "Done, Go &Back"); time.sleep(2)
        v.back_to_select(print)
        return 0
    finally:
        sn.terminate()
        try:
            sn.communicate(timeout=20)
        except Exception:
            sn.kill()


if __name__ == "__main__":
    sys.exit(main())
