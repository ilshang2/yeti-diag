"""追蹤 09 中央電子進入寫入工作階段 (10 03) 的過程。不送任何寫入指令。

只做: 建立通道 -> 10 89 -> 10 03 -> 22 0600 (讀編碼) -> 關閉通道
用法: py -3.13 tools\\tp20_session_trace.py COM9
"""
import sys
import time

from yeti_diag import tp20
from yeti_diag.elm327 import Elm327

T0 = time.monotonic()


class LoggingLink(tp20.ElmLink):
    def exchange(self, tx_id, data, rx_id, wait=0.3, expect=None, no_reply=False):
        t = time.monotonic() - T0
        frames = super().exchange(tx_id, data, rx_id, wait, expect, no_reply)
        print(f"{t:6.2f}s  {tx_id:03X} > {bytes(data).hex(' ').upper():<24} expect={expect} "
              f"no_reply={no_reply} < {[f.hex(' ').upper() for f in frames]}", flush=True)
        return frames

    def listen(self, rx_id, wait):
        frames = super().listen(rx_id, wait)
        print(f"{time.monotonic() - T0:6.2f}s  listen(A3) < {[f.hex(' ').upper() for f in frames]}", flush=True)
        return frames


elm = Elm327(port=sys.argv[1])
ch = tp20.Tp20Channel(LoggingLink(elm), 0x20, timeout=1.5)
try:
    ch.open()
    k = tp20.KwpClient(ch)
    k.start_session()
    print("--- 10 03 ---")
    try:
        k._transact(bytes([0x10, 0x03]))
        print("結果: 10 03 被接受")
    except Exception as e:
        print(f"結果: {type(e).__name__}: {e}")
    print("--- 22 0600 ---")
    try:
        print("編碼:", k.read_did(0x0600).hex().upper())
    except Exception as e:
        print(f"結果: {type(e).__name__}: {e}")
finally:
    ch.close()
    elm.close()
