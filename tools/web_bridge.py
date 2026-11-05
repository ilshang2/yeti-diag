"""測試用橋接伺服器：讓手機網頁的程式碼在電腦上透過序列埠 ELM327 (例如藍牙 COM9) 跟真車對話。

網頁 (web/yeti-diag.html) 在 http://localhost:8765/ 提供；網頁的「電腦橋接 (測試用)」按鈕
會把每一行接頭指令 POST 到 /cmd，這裡寫進序列埠，讀到 '>' 提示字元為止再傳回。
網頁本身不分讀寫；它只送讀取用的指令。

用法: py -3.13 tools\\web_bridge.py COM9
"""
import http.server
import json
import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".deps"))
import serial  # noqa: E402

PORT = 8765
HEAD = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<style>body{margin:0}[hidden]{display:none!important}</style></head><body>\n')

ser = serial.Serial(sys.argv[1] if len(sys.argv) > 1 else "COM9", 38400, timeout=0.05)
lock = threading.Lock()


def exchange(text, wait_ms):
    with lock:
        ser.reset_input_buffer()
        ser.write((text + "\r").encode("ascii"))
        buf, end = b"", time.monotonic() + wait_ms / 1000
        while time.monotonic() < end:
            chunk = ser.read(256)
            if chunk:
                buf += chunk
                if b">" in buf:
                    break
        return buf.decode("ascii", errors="replace")


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/info":
            self._send(ser.port)
        elif self.path.split("?")[0] in ("/", "/index.html"):
            page = (ROOT / "web" / "yeti-diag.html").read_text(encoding="utf-8")
            self._send(HEAD + page + "\n</body></html>", "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/cmd":
            self.send_error(404)
            return
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._send(exchange(req["text"], int(req.get("wait", 1500))))

    def log_message(self, *args):
        pass


print(f"橋接啟動: http://localhost:{PORT}/  序列埠 {ser.port}", flush=True)
http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
