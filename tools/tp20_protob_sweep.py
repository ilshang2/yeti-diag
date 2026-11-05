"""TP2.0 setup sweep using the ELM's user CAN protocol B (variable DLC).

ATPB C001 = 11-bit IDs, variable data length, 500 kbit/s; ATSP B selects it.
Read-only: setup requests (C0) only, plus a close (A8) on any channel that opens.
Usage: py -3.13 tools\\tp20_protob_sweep.py COM9
"""
import sys

from yeti_diag.elm327 import Elm327

elm = Elm327(port=sys.argv[1])


def raw(cmd, wait=0.8):
    elm.ser.write((cmd + "\r").encode())
    return elm._read_until_prompt(wait)


for cmd in ("ATPBC001", "ATSPB", "ATCAF0", "ATH1", "ATS0"):
    print(f"{cmd} -> {raw(cmd)}")

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
                tx = int(line[11:13], 16) | (int(line[13:15], 16) & 0x0F) << 8
                raw(f"ATSH{tx:03X}")
                raw("A8")
                break
print(f"\n回應的位址: {', '.join(f'0x{d:02X}' for d in hits) or '無'}")
elm.close()
