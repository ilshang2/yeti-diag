"""Sweep TP2.0 setup requests 0x00-0x7F with a 0x2xx mask filter.

Read-only: sends only channel-setup requests (C0) and a close (A8) on any
channel that opens. Usage: py -3.13 tools\\tp20_sweep.py COM9
"""
import sys

from yeti_diag.elm327 import Elm327

elm = Elm327(port=sys.argv[1])


def raw(cmd, wait=0.6):
    elm.ser.write((cmd + "\r").encode())
    return elm._read_until_prompt(wait)


print("ATCF200 ->", raw("ATCF200"))   # filter value
print("ATCM700 ->", raw("ATCM700"))   # mask: accept 0x200-0x2FF
print("ATSH200 ->", raw("ATSH200"))
hits = []
for dest in range(0x00, 0x80):
    lines = [l for l in raw(f"{dest:02X}C00010000301") if l not in ("NO DATA", "OK")]
    if lines:
        print(f"dest 0x{dest:02X}: {lines}")
        hits.append((dest, lines))
        for line in lines:  # close the channel we just opened
            if len(line) >= 17 and line[5:7] == "D0":
                tx = int(line[13:15], 16) | (int(line[15:17], 16) & 0x0F) << 8
                raw(f"ATSH{tx:03X}")
                raw("A8")
                raw("ATSH200")
print(f"\n回應的位址: {', '.join(f'0x{d:02X}' for d, _ in hits) or '無'}")
elm.close()
