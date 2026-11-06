"""Screenshots of the four GUI tabs on the simulator, for the README and the web page.

usage: py -3.13 tools/make_screenshots.py <output folder>

Run it from the PUBLIC copy (Desktop\\yeti-diag-public): the scan screen prints the profile's
VIN, and only the public copy has the sample one. The script refuses any other VIN.
Opens a window for about a minute; do not cover it (the screenshots are taken from the screen).
"""
import ctypes
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_VIN = "TMBJF65L0E6000000"
os.environ["YETI_DIAG_HOME"] = tempfile.mkdtemp(prefix="yeti_shots_")  # no real reports shown
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / ".deps"), str(ROOT)]

from PIL import ImageGrab  # noqa: E402

import gui  # noqa: E402
from yeti_diag import vehicle  # noqa: E402

SHOTS = ["1_live.png", "2_health.png", "3_scan.png", "4_features.png"]


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    if vehicle.VIN != SAMPLE_VIN:
        print(f"設定檔的 VIN 不是範例值 ({SAMPLE_VIN})：截圖會露出車身號碼。請在公開版資料夾執行。")
        return 1
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    app = gui.App()
    app.conn.set("SIM · 模擬器")
    app.geometry("760x680+40+40"); app.attributes("-topmost", True)
    nb = next(w for w in app.winfo_children() if w.winfo_class() == "TNotebook")

    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            app.update(); time.sleep(0.03)

    def idle(limit=180):
        end = time.time() + limit
        while time.time() < end and app._busy:
            app.update(); time.sleep(0.05)
        pump(0.3)

    def shot(name):
        pump(0.6)
        x, y, w, h = app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()
        ImageGrab.grab((x, y, x + w, y + h)).save(out / name)
        print(out / name)

    pump(1)
    app.log.configure(state="normal"); app.log.delete("1.0", "end"); app.log.configure(state="disabled")
    app._log("就緒。行駛中只用「即時儀表」。")  # the default first line shows the data folder path
    app.do_connect(); idle()
    nb.select(0); app.toggle_live(); pump(5); shot(SHOTS[0]); app.toggle_live(); idle(20)
    nb.select(1); app.simple_cmd(("misfire",)); idle(); shot(SHOTS[1])
    nb.select(2); app.simple_cmd(("autoscan",), target="fault"); idle()
    app.fault_out.see("end"); app.fault_out.yview(app.fault_out.search("全車掃描 20", "1.0") or "1.0")
    shot(SHOTS[2])
    nb.select(3); app.load_features(); idle(); shot(SHOTS[3])
    app.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
