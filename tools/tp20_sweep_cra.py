"""Sweep TP2.0 setup requests 0x00-0x7F with an exact ATCRA filter per address.

Read-only: channel-setup requests (C0) only, plus a close (A8) on any channel
that opens. Usage: py -3.13 tools\\tp20_sweep_cra.py COM9
"""
import sys

from yeti_diag.elm327 import Elm327

elm = Elm327(port=sys.argv[1])


def raw(cmd, wait=0.6):
    elm.ser.write((cmd + "\r").encode())
    return elm._read_until_prompt(wait)


hits = []
for dest in range(0x00, 0x80):
    raw("ATSH200")
    raw(f"ATCRA{0x200 + dest:03X}")
    lines = [l for l in raw(f"{dest:02X}C00010000301") if l not in ("NO DATA", "OK")]
    if lines:
        print(f"dest 0x{dest:02X}: {lines}", flush=True)
        hits.append(dest)
        for line in lines:
            if len(line) >= 17 and line[5:7] == "D0":
                tx = int(line[13:15], 16) | (int(line[15:17], 16) & 0x0F) << 8
                raw(f"ATSH{tx:03X}")
                raw("A8")
print(f"\n回應的位址: {', '.join(f'0x{d:02X}' for d in hits) or '無'}")
elm.close()
