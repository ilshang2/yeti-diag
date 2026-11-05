"""Raw TP2.0 setup test through an ELM327. Read-only: channel setup frames only.

Usage: py -3.13 tools\\tp20_raw.py COM9
"""
import sys
import time

from yeti_diag.elm327 import Elm327

port = sys.argv[1]
elm = Elm327(port=port)
print("ATZ ->", elm.version)
print("ATI ->", elm.command("ATI"))
print("AT@1 ->", elm.command("AT@1"))


def raw(cmd, wait=1.5):
    elm.ser.write((cmd + "\r").encode())
    lines = elm._read_until_prompt(wait)
    print(f"{cmd:<20} -> {lines}")
    return lines


# Setup request to 0x200 for a few likely destinations, listening on everything.
raw("ATSH200")
raw("ATAR")          # auto receive (clear CRA filter)
for dest in (0x01, 0x20, 0x1F, 0x03):
    raw(f"{dest:02X}C00010000301", 2.0)
    time.sleep(0.2)
raw("ATCRA201")
raw("01C00010000301", 2.0)
elm.close()
