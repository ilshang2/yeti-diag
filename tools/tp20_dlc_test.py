"""Compare 7-byte and 8-byte TP2.0 setup frames on a few addresses.

Read-only: setup requests only (and A8 close if a channel opens).
Usage: py -3.13 tools\\tp20_dlc_test.py COM9
"""
import sys

from yeti_diag.elm327 import Elm327

elm = Elm327(port=sys.argv[1])


def raw(cmd, wait=0.8):
    elm.ser.write((cmd + "\r").encode())
    return elm._read_until_prompt(wait)


for dest in (0x01, 0x02, 0x03, 0x09, 0x1F, 0x20, 0x42):
    for label, frame in (("7B", f"{dest:02X}C00010000301"), ("8B-00", f"{dest:02X}C0001000030100"),
                         ("8B-55", f"{dest:02X}C0001000030155")):
        raw("ATSH200")
        raw(f"ATCRA{0x200 + dest:03X}")
        lines = raw(frame)
        print(f"0x{dest:02X} {label:<6} {frame:<18} -> {lines}", flush=True)
        for line in lines:
            if len(line) >= 17 and line[5:7] == "D0":
                tx = int(line[13:15], 16) | (int(line[15:17], 16) & 0x0F) << 8
                raw(f"ATSH{tx:03X}")
                raw("A8")
elm.close()
