"""Send one TP2.0 setup request, then monitor all bus traffic (ATMA).

Read-only. Usage: py -3.13 tools\\tp20_monitor.py COM9 [dest_hex]
"""
import sys
import time

from yeti_diag.elm327 import Elm327

elm = Elm327(port=sys.argv[1])
dest = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0x09


def raw(cmd, wait=0.6):
    elm.ser.write((cmd + "\r").encode())
    return elm._read_until_prompt(wait)


print("ATAR ->", raw("ATAR"))
print("ATSH200 ->", raw("ATSH200"))
print(f"setup 0x{dest:02X} ->", raw(f"{dest:02X}C00010000301", 1.0))
elm.ser.write(b"ATMA\r")
time.sleep(2.0)
elm.ser.write(b"\r")
lines = elm._read_until_prompt(2.0)
print(f"ATMA 2 秒內看到 {len(lines)} 行:")
for line in lines[:60]:
    print("  ", line)
elm.close()
