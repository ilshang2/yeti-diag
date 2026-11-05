"""Repeat TP2.0 reads and print the raw frames of every failed attempt (read-only).

Usage: py -3.13 tools\\tp20_stress.py COM9 [rounds] [-v] [--only=72] [--fast]
(-v prints the frames of retried attempts too; --only picks jobs by name prefix)
Each round reads 09 (22 0600 then 1A 9B), 62 and 72 fault codes, the same
back-to-back pattern as `verify`. Only failures print their frame log.
"""
import sys
import time

from yeti_diag import tp20, uds
from yeti_diag.elm327 import Elm327, ElmError

T0 = time.monotonic()
ERRORS = (uds.NoResponse, uds.NegativeResponse, tp20.Tp20Error, ElmError)


class LoggingLink(tp20.ElmLink):
    def __init__(self, elm, log):
        super().__init__(elm)
        self.log = log

    def exchange(self, tx_id, data, rx_id, wait=0.3, expect=None, no_reply=False):
        t = time.monotonic() - T0
        frames = super().exchange(tx_id, data, rx_id, wait, expect, no_reply)
        self.log.append(f"{t:7.2f}s {tx_id:03X} > {bytes(data).hex(' ').upper():<24} expect={expect} "
                        f"({(time.monotonic() - T0 - t) * 1000:.0f} ms) < "
                        f"{[f.hex(' ').upper() for f in frames]}")
        return frames

    def listen(self, rx_id, wait):
        frames = super().listen(rx_id, wait)
        self.log.append(f"{time.monotonic() - T0:7.2f}s listen(A3) < {[f.hex(' ').upper() for f in frames]}")
        return frames


def attempt(elm, dest, fn, tries=3):
    """Like cli.kwp_call: fresh channel per try, longer wait when the gateway is busy."""
    logs = []
    for i in range(tries):
        ok, log = one_try(elm, dest, fn)
        if ok:
            return True, i, logs
        logs += log
        time.sleep(1.0 if "Tp20Busy" in log[-1] else 0.3)
    return False, tries, logs


def one_try(elm, dest, fn):
    log = []
    ch = tp20.Tp20Channel(LoggingLink(elm, log), dest, fast="--fast" in sys.argv)
    try:
        ch.open()
        k = tp20.KwpClient(ch)
        k.start_session()
        fn(k)
        return True, log
    except ERRORS as e:
        log.append(f"         錯誤: {type(e).__name__}: {e}")
        return False, log
    finally:
        ch.close()


JOBS = [
    ("09 22 0600", 0x20, lambda k: k.read_raw([0x22, 0x06, 0x00])),
    ("09 22 F187", 0x20, lambda k: k.read_part()),
    ("62 故障碼", 0x24, lambda k: k.read_dtcs()),
    ("72 故障碼", 0x25, lambda k: k.read_dtcs()),
    ("03 22 F187", 0x03, lambda k: k.read_part()),  # ABS: long answer, closed by the module on the Vgate
]

args = [a for a in sys.argv[1:] if not a.startswith("-")]
only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None)
if only:
    JOBS = [j for j in JOBS if j[0].startswith(only)]
elm = Elm327(port=args[0])
rounds = int(args[1]) if len(args) > 1 else 5
fails = {name: 0 for name, _, _ in JOBS}
retried = {name: 0 for name, _, _ in JOBS}
try:
    print(f"電壓 {elm.battery_voltage():.2f} V")
    for r in range(1, rounds + 1):
        for name, dest, fn in JOBS:
            ok, retries, log = attempt(elm, dest, fn)
            print(f"第 {r} 輪 {name}: {'成功' if ok else '失敗'} (重試 {retries} 次)", flush=True)
            retried[name] += retries
            if not ok:
                fails[name] += 1
            if log and (not ok or "-v" in sys.argv):
                print("\n".join(log), flush=True)
    print("\n最終失敗:", ", ".join(f"{n} {c}/{rounds}" for n, c in fails.items()))
    print("重試總數:", ", ".join(f"{n} {c}" for n, c in retried.items()))
finally:
    elm.close()
