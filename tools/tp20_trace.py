"""Trace TP2.0 + KWP reads frame by frame with timing (read-only).

Usage: py -3.13 tools\\tp20_trace.py COM9 [dest_hex]
Reads 22 F187 (part number) and 18 02 FF 00 (fault codes), logging every frame.
"""
import sys
import time

from yeti_diag import tp20
from yeti_diag.elm327 import Elm327

T0 = time.monotonic()


class LoggingLink(tp20.ElmLink):
    def exchange(self, tx_id, data, rx_id, wait=0.3, expect=None):
        t = time.monotonic() - T0
        frames = super().exchange(tx_id, data, rx_id, wait, expect)
        dt = time.monotonic() - T0 - t
        print(f"{t:6.2f}s  {tx_id:03X} > {bytes(data).hex(' ').upper():<24} expect={expect}  "
              f"({dt * 1000:.0f} ms) < {[f.hex(' ').upper() for f in frames]}", flush=True)
        return frames

    def listen(self, rx_id, wait):
        frames = super().listen(rx_id, wait)
        print(f"{time.monotonic() - T0:6.2f}s  listen(A3) < {[f.hex(' ').upper() for f in frames]}", flush=True)
        return frames


elm = Elm327(port=sys.argv[1])
dest = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0x24
for name, req in (("1A 9B", [0x1A, 0x9B]), ("22 F187", [0x22, 0xF1, 0x87]),
                  ("18 02 FF 00", [0x18, 0x02, 0xFF, 0x00])):
    print(f"\n=== 0x{dest:02X} {name} ===")
    ch = tp20.Tp20Channel(LoggingLink(elm), dest, timeout=1.5)
    try:
        ch.open()
        k = tp20.KwpClient(ch)
        k.start_session()
        data = k.read_raw(req)
        print("結果:", data.hex(" ").upper())
    except Exception as e:
        print(f"失敗: {type(e).__name__}: {e}")
    finally:
        ch.close()
elm.close()
