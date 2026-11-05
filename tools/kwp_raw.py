"""Read-only raw KWP/UDS-style requests to one TP2.0 module (KwpClient refuses writes).

    py -3.13 tools\\kwp_raw.py COM9 19 "19 02 0D" "18 02 FF 00"
"""
import argparse
import sys

from yeti_diag import cli, tp20, uds
from yeti_diag.elm327 import ElmError

port, module, requests = sys.argv[1], sys.argv[2], sys.argv[3:]
args = cli.build_parser().parse_args(["--elm", port, "kwp-dtc", module])
dest = cli.tp20_dest(args)
with cli.Transport(args) as t:
    for text in requests:
        req = bytes.fromhex(text)
        try:
            resp = cli.kwp_call(t, dest, lambda k: k.request(req))
            print(f"{text:<16} -> {resp.hex(' ').upper()}")
        except uds.NegativeResponse as e:
            print(f"{text:<16} -> 負面回應 {e.nrc:02X}")
        except (*cli.TP20_RETRY_ERRORS, ElmError) as e:
            print(f"{text:<16} -> 錯誤 {e}")
