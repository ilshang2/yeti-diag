"""Read-only raw look at one UDS module: prints every ELM line while the normal
client reads the part number and the stored faults.

    py -3.13 tools\\uds_raw.py COM9 01 [rounds]
"""
import sys
import time

from yeti_diag import uds
from yeti_diag.elm327 import Elm327, ElmChannel
from yeti_diag.vehicle import UDS_MODULES

port, addr = sys.argv[1], sys.argv[2]
rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 1
mod = UDS_MODULES[addr]
elm = Elm327(port=port)
orig = elm._raw
t0 = time.monotonic()


def raw(text, wait):
    lines = orig(text, wait)
    print(f"{time.monotonic() - t0:6.2f} >> {text}  << {lines}")
    return lines


elm._raw = raw
print("ELM", elm.version, "volt", elm.command("ATRV"))
c = uds.UdsClient(ElmChannel(elm, mod.tx_id, mod.rx_id))
for i in range(rounds):
    for name, fn in (("F187", lambda: c.read_did(0xF187)), ("DTC", c.read_dtcs)):
        try:
            print(f"== {name}: {fn()}")
        except Exception as e:
            print(f"== {name}: {type(e).__name__} {e}")
elm.close()
