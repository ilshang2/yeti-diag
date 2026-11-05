"""Try several identification reads on each known TP2.0 address (read-only).

Usage: py -3.13 tools\\tp20_ident_diag.py COM9 [dest_hex ...]
"""
import sys
import time

from yeti_diag import tp20
from yeti_diag.elm327 import Elm327

READS = {
    "1A 9B": [0x1A, 0x9B],
    "22 F187": [0x22, 0xF1, 0x87],
    "22 F18C": [0x22, 0xF1, 0x8C],
    "1A 9A": [0x1A, 0x9A],
    "1A 91": [0x1A, 0x91],
}

elm = Elm327(port=sys.argv[1])
link = tp20.ElmLink(elm)
dests = [int(a, 16) for a in sys.argv[2:]] or [0x03, 0x09, 0x1F, 0x20, 0x22, 0x23, 0x24, 0x25]
for dest in dests:
    ch = tp20.Tp20Channel(link, dest, timeout=1.5)
    try:
        ch.open()
        k = tp20.KwpClient(ch)
        for attempt in range(3):
            try:
                k.start_session()
                break
            except tp20.NegativeResponse as e:
                if e.nrc != 0x21:
                    raise
                time.sleep(0.5)  # 0x21 busy, repeat request
        for name, req in READS.items():
            try:
                data = k.request(req)[len(req):]
                text = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
                print(f"0x{dest:02X} {name:<8} OK   {data.hex(' ').upper()[:90]}\n{'':15}{text}", flush=True)
            except Exception as e:
                print(f"0x{dest:02X} {name:<8} {type(e).__name__}: {e}", flush=True)
    except Exception as e:
        print(f"0x{dest:02X} 連線失敗 {type(e).__name__}: {e}", flush=True)
    finally:
        ch.close()
elm.close()
