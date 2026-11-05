"""Yeti 診斷工具 — 單視窗圖形介面 (tkinter)。

背景呼叫 yeti_diag 的 --json 介面，不卡畫面；結果顯示程式本身印的中文說明。所有寫入
沿用程式本身的備份 / 電壓 / 確認 / 讀回驗證與行駛中保護，GUI 只多一層勾選與確認對話框。

分頁：即時儀表 (只讀引擎，行駛中可用)、健康檢查、故障碼、隱藏功能。

執行 (開發)：  py -3.13 tools/gui.py
打包成 exe ：  見 交接說明.md「獨立 exe」的重建指令
"""
import contextlib
import datetime
import io
import json
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

# Works both as `py tools/gui.py` and bundled into an exe (PyInstaller sets _MEIPASS).
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
for p in (str(ROOT / ".deps"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
from yeti_diag import cli, uds  # noqa: E402
from yeti_diag.paths import DATA_ROOT  # noqa: E402
from yeti_diag.vehicle import UDS_MODULES  # noqa: E402

SETTINGS = DATA_ROOT / "reports" / "gui_settings.json"
LIVE_PIDS = ((0x0D, "車速", "km/h"), (0x0C, "轉速", "rpm"), (0x05, "水溫", "°C"),
             (0x42, "電壓", "V"), (0x0F, "進氣溫度", "°C"), (0x46, "外氣溫度", "°C"))


def transport_argv(conn):
    return ["--sim"] if conn == "SIM" else ["--elm", conn]


def run_json(conn, *cmd):
    """Run one yeti_diag command in JSON mode in-process (no subprocess, so it bundles into
    a standalone exe). Returns (ok, data, doc, text): text is the tool's own Chinese
    progress output, which is what a person wants to read."""
    argv = transport_argv(conn) + ["--json", *cmd]
    buf, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            cli.main(argv)
    except SystemExit:
        pass
    except Exception as e:  # noqa: BLE001
        return False, None, str(e), err.getvalue()
    out, text = buf.getvalue(), err.getvalue().strip()
    i = out.find("{")
    if i < 0:
        return False, None, out.strip() or "無輸出", text
    try:
        doc = json.loads(out[i:])
    except json.JSONDecodeError:
        return False, None, out.strip(), text
    return bool(doc.get("ok")), doc.get("data"), doc, text


def error_text(doc):
    if isinstance(doc, dict):
        return (doc.get("error") or {}).get("message") or "失敗"
    return str(doc)


def list_ports():
    try:
        from serial.tools import list_ports as lp
        ports = [(p.device, p.description) for p in lp.comports()]
    except Exception:  # noqa: BLE001
        ports = []
    return sorted(ports, key=lambda p: int("".join(c for c in p[0] if c.isdigit()) or 0)) + [("SIM", "模擬器")]


def load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Yeti 診斷工具")
        self.geometry("720x640")
        self.minsize(600, 520)
        self.q = queue.Queue()
        self.settings = load_settings()
        self.conn = tk.StringVar(value=self.settings.get("port", "COM10"))
        self.status = tk.StringVar(value="未連線")
        self.feature_rows = []
        self._busy = False
        self._busy_buttons = []
        self.live_stop = threading.Event()
        self.live_thread = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(120, self._poll)

    # ---- layout ----
    def _build(self):
        style = ttk.Style(self)
        style.configure("Big.TLabel", font=("Microsoft JhengHei UI", 26, "bold"))
        style.configure("Cap.TLabel", font=("Microsoft JhengHei UI", 10), foreground="#555")

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="接頭").pack(side="left")
        self.port_box = ttk.Combobox(top, textvariable=self.conn, width=26)
        self.port_box.pack(side="left", padx=4)
        ttk.Button(top, text="↻", width=3, command=self.refresh_ports).pack(side="left")
        self.btn_conn = self._button(top, "連線 / 讀電壓", self.do_connect)
        self.btn_conn.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(top, mode="indeterminate", length=80)
        self.progress.pack(side="right")
        ttk.Label(self, textvariable=self.status, foreground="#036", padding=(10, 0)).pack(anchor="w")
        self.refresh_ports()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=4)
        self._tab_live(nb)
        self._tab_health(nb)
        self._tab_faults(nb)
        self._tab_features(nb)

        self.log = tk.Text(self, height=6, wrap="word", state="disabled", bg="#111", fg="#9f9")
        self.log.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self._log(f"就緒。資料存在 {DATA_ROOT}")
        self._log("行駛中只用「即時儀表」：程式會拒絕在行駛中連 ABS 等舊模組與寫入。")

    def _button(self, parent, text, command):
        b = ttk.Button(parent, text=text, command=command)
        self._busy_buttons.append(b)
        return b

    def _text_box(self, parent, height):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, pady=6)
        box = tk.Text(frame, height=height, wrap="word", state="disabled", font=("Consolas", 10))
        sb = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=sb.set)
        box.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return box

    def _tab_live(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="即時儀表")
        bar = ttk.Frame(f); bar.pack(fill="x")
        self.btn_live = ttk.Button(bar, text="開始", command=self.toggle_live)
        self.btn_live.pack(side="left")
        self.live_record = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="同時存檔 (reports\\*_live.jsonl)", variable=self.live_record).pack(side="left", padx=10)
        ttk.Label(f, text="只讀引擎模組的 OBD 數值，行駛中也安全；不連 ABS / 變速箱 / 09。",
                  style="Cap.TLabel").pack(anchor="w", pady=(6, 4))
        grid = ttk.Frame(f); grid.pack(fill="both", expand=True)
        self.live_vars = {}
        for i, (pid, name, unit) in enumerate(LIVE_PIDS):
            cell = ttk.Frame(grid, padding=8, relief="groove")
            cell.grid(row=i // 3, column=i % 3, sticky="nsew", padx=4, pady=4)
            ttk.Label(cell, text=f"{name} ({unit})", style="Cap.TLabel").pack(anchor="w")
            var = tk.StringVar(value="—")
            ttk.Label(cell, textvariable=var, style="Big.TLabel").pack(anchor="w")
            self.live_vars[pid] = var
        for c in range(3):
            grid.columnconfigure(c, weight=1)
        self.live_info = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.live_info, style="Cap.TLabel").pack(anchor="w", pady=4)

    def _tab_health(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text="健康檢查")
        bar = ttk.Frame(f); bar.pack(fill="x")
        for text, cmd in (("各缸失火", ("misfire",)), ("電瓶電壓", ("voltage",)),
                          ("排放自我檢測", ("readiness",)), ("引擎 OBD 數據", ("obd",))):
            self._button(bar, text, lambda c=cmd: self.simple_cmd(c)).pack(side="left", padx=(0, 6))
        self.health_out = self._text_box(f, 14)

    def _tab_faults(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text="故障碼")
        bar = ttk.Frame(f); bar.pack(fill="x")
        self._button(bar, "全車掃描 (約 1-2 分鐘，要停車)",
                     lambda: self.simple_cmd(("autoscan",), target="fault")).pack(side="left")
        self._button(bar, "只看引擎故障碼",
                     lambda: self.simple_cmd(("obd",), target="fault")).pack(side="left", padx=6)
        ttk.Label(f, text="清除故障碼請用指令列 clear-dtc (會先備份、清後驗證)。", style="Cap.TLabel").pack(anchor="w", pady=(4, 0))
        self.fault_out = self._text_box(f, 16)

    def _tab_features(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text="隱藏功能")
        bar = ttk.Frame(f); bar.pack(fill="x")
        self._button(bar, "讀取目前狀態", self.load_features).pack(side="left")
        self._button(bar, "套用勾選的變更", self.apply_features).pack(side="left", padx=6)
        ttk.Label(f, text="要停車、建議引擎發動。寫入前會列出變更讓你確認，每項自動備份並讀回驗證。",
                  style="Cap.TLabel").pack(anchor="w", pady=(4, 0))
        holder = ttk.Frame(f); holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, highlightthickness=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        self.feat_frame = ttk.Frame(canvas)
        self.feat_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.feat_frame, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True, pady=6)
        sb.pack(side="right", fill="y")

    # ---- helpers ----
    def port(self):
        """'COM10 · 透過藍牙…' in the box -> 'COM10'."""
        return self.conn.get().split(" ")[0].strip() or "COM10"

    def refresh_ports(self):
        ports = list_ports()
        self.port_box["values"] = [f"{d} · {desc}" for d, desc in ports]
        if not self.conn.get():
            self.conn.set(ports[0][0])

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.datetime.now():%H:%M:%S} {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _write_box(self, box, text):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", text)
        box.configure(state="disabled")

    def _set_busy(self, busy):
        self._busy = busy
        for b in self._busy_buttons:
            b.state(["disabled"] if busy else ["!disabled"])
        self.btn_live.state(["disabled"] if busy and not self.live_running() else ["!disabled"])
        self.progress.start(12) if busy else self.progress.stop()

    def _async(self, fn, done):
        if self._busy:
            self._log("忙碌中，請稍候…")
            return
        save_settings({**self.settings, "port": self.port()})
        self._set_busy(True)

        def work():
            try:
                self.q.put((done, fn()))
            except Exception as e:  # noqa: BLE001
                self.q.put((done, (False, None, str(e), "")))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                done, result = self.q.get_nowait()
                if done is None:      # live dashboard update
                    result()
                elif done is False:   # live dashboard thread ended
                    self._set_busy(False)
                    self._live_done(result)
                else:
                    self._set_busy(False)
                    done(result)
        except queue.Empty:
            pass
        self.after(120, self._poll)

    # ---- actions ----
    def do_connect(self):
        self.status.set("連線中…")
        port = self.port()
        self._async(lambda: run_json(port, "voltage"), self._connected)

    def _connected(self, result):
        ok, data, doc, _ = result
        if ok and data:
            self.status.set(f"已連線 {self.port()} · 電壓 {data.get('voltage'):.2f} V")
            self._log(f"連線成功，電壓 {data.get('voltage'):.2f} V")
        else:
            self.status.set("連線失敗")
            self._log(f"連線失敗：{error_text(doc)}")

    def simple_cmd(self, cmd, target="health"):
        self._log(f"執行 {' '.join(cmd)} …")
        box = self.fault_out if target == "fault" else self.health_out
        self._write_box(box, "讀取中…")
        port = self.port()
        self._async(lambda: run_json(port, *cmd), lambda r: self._show_simple(r, box))

    def _show_simple(self, result, box):
        ok, data, doc, text = result
        if not ok and not text:
            text = f"失敗：{error_text(doc)}"
        elif not ok:
            text += f"\n\n失敗：{error_text(doc)}"
        self._write_box(box, text or json.dumps(data, ensure_ascii=False, indent=2))
        self._log("完成。" if ok else f"失敗：{error_text(doc)}")

    # ---- live dashboard (engine OBD only) ----
    def live_running(self):
        return self.live_thread is not None and self.live_thread.is_alive()

    def toggle_live(self):
        if self.live_running():
            self.live_stop.set()
            self.btn_live.configure(text="停止中…")
            return
        if self._busy:
            self._log("忙碌中，請稍候…")
            return
        save_settings({**self.settings, "port": self.port()})
        self.live_stop.clear()
        self.live_thread = threading.Thread(target=self._live_loop, args=(self.port(), self.live_record.get()),
                                            daemon=True)
        self.live_thread.start()  # before _set_busy, which keeps the stop button usable while live
        self._set_busy(True)
        self.btn_live.configure(text="停止")
        self._log("即時儀表開始 (只讀引擎)。")

    def _live_loop(self, port, record):
        out = None
        try:
            args = cli.build_parser().parse_args(transport_argv(port) + ["obd"])
            if record:
                (DATA_ROOT / "reports").mkdir(parents=True, exist_ok=True)
                out = open(DATA_ROOT / "reports" / f"{datetime.datetime.now():%Y%m%d-%H%M%S}_live.jsonl",
                           "a", encoding="utf-8")
            with cli.Transport(args) as t:
                misses = 0
                while not self.live_stop.is_set():
                    values = {}
                    try:
                        with t.client(UDS_MODULES["01"]) as c:
                            for pid, _, _ in LIVE_PIDS:
                                try:
                                    values[pid] = uds.read_obd_pid(c, pid)[1]
                                except (uds.NegativeResponse, uds.NoResponse):
                                    pass
                    except Exception as e:  # noqa: BLE001
                        values = {"_error": str(e)}
                    misses = 0 if values and "_error" not in values else misses + 1
                    if out and values and "_error" not in values:
                        out.write(json.dumps({"t": time.time(), **{f"{k:02X}": v for k, v in values.items()}}) + "\n")
                        out.flush()
                    self.q.put((None, lambda v=dict(values), m=misses: self._live_show(v, m)))
                    self.live_stop.wait(1.5)
        except Exception as e:  # noqa: BLE001
            self.q.put((None, lambda e=e: self._log(f"即時儀表錯誤：{e}")))
        finally:
            if out:
                out.close()
            self.q.put((False, None))

    def _live_show(self, values, misses):
        if "_error" in values or not values:
            self.live_info.set(f"引擎沒有回應 ({misses} 次)：熄火了，或接頭斷線")
            return
        for pid, _, unit in LIVE_PIDS:
            v = values.get(pid)
            self.live_vars[pid].set("—" if v is None else (f"{v:.1f}" if unit == "V" else f"{v:.0f}"))
        self.live_info.set(f"更新 {datetime.datetime.now():%H:%M:%S}")

    def _live_done(self, _):
        self.btn_live.configure(text="開始")
        self._log("即時儀表已停止。")

    # ---- hidden features (09) ----
    def load_features(self):
        self._log("讀取隱藏功能…")
        port = self.port()
        self._async(lambda: self._read_features(port), self._show_features)

    @staticmethod
    def _read_features(port):
        """Worker thread. First run on a new computer/car: no TP2.0 address map yet, so
        find the modules once (tp20-probe, read-only, ~1 min) and try again."""
        result = run_json(port, "features")
        ok, _, doc, _ = result
        if not ok and "tp20-probe" in error_text(doc):
            probed = run_json(port, "tp20-probe")
            if not probed[0]:
                return probed
            result = run_json(port, "features")
        return result

    def _show_features(self, result):
        ok, data, doc, _ = result
        for w in self.feat_frame.winfo_children():
            w.destroy()
        self.feature_rows = []
        if not ok or not data:
            self._log(f"讀取失敗：{error_text(doc)}")
            return
        for feat in data.get("features", []):
            var = tk.BooleanVar(value=bool(feat["on"]))
            row = ttk.Frame(self.feat_frame); row.pack(fill="x", anchor="w")
            ttk.Checkbutton(row, variable=var).pack(side="left")
            ttk.Label(row, text=f"{feat['name']}  ", width=18).pack(side="left")
            ttk.Label(row, text=feat.get("note", ""), foreground="#666").pack(side="left")
            self.feature_rows.append((feat["key"], feat["name"], bool(feat["on"]), var))
        self._log(f"隱藏功能 {len(self.feature_rows)} 項已載入。勾選想變更的，再按「套用勾選的變更」。")

    def apply_features(self):
        changes = [(k, n, v.get()) for k, n, orig, v in self.feature_rows if v.get() != orig]
        if not changes:
            self._log("沒有變更。")
            return
        lines = "\n".join(f"· {n}：{'關閉 → 開啟' if on else '開啟 → 關閉'}" for k, n, on in changes)
        if not messagebox.askyesno("確認寫入", f"要寫入以下變更嗎？（每項只送一次、自動備份、寫後讀回驗證）\n\n{lines}"):
            self._log("已取消。")
            return
        self._log(f"開始寫入 {len(changes)} 項變更…")
        port = self.port()
        self._async(lambda: self._write_features(port, changes), lambda r: self._log(r[1]))

    def _write_features(self, port, changes):
        """Worker thread: must not touch tkinter, so the port comes in as an argument."""
        msgs = []
        for key, name, on in changes:
            ok, data, doc, _ = run_json(port, "--yes", "feature", key, "on" if on else "off")
            msgs.append(f"✓ {name}：{(data or {}).get('status', 'ok')}" if ok else f"✗ {name}：{error_text(doc)}")
        return ("done", "\n".join(msgs) + "\n(完成後請重新「讀取目前狀態」確認)")

    def _close(self):
        self.live_stop.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
