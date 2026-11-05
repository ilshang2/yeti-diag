"""Measuring groups another app read over TP2.0 (KWP 21 gg), from tools/btsnoop_elm.py output.

Per group: how often it was read, and each distinct answer (first time seen) as raw
triples with our decoding, to match with the app's screen. Answers 7F 21 12
(group not supported) are skipped. Optional start time (phone clock) skips older reads.

    py -3.13 tools\\elm_log_groups.py reports\\phone2\\elm_log.txt [07:04:00]
"""
import collections
import re
import sys

from yeti_diag.measure import decode_value

sys.stdout.reconfigure(encoding="utf-8")
log = open(sys.argv[1], encoding="utf-8").read().splitlines()
start = sys.argv[2] if len(sys.argv) > 2 else "00:00:00"
seen = collections.defaultdict(list)
i = 0
while i < len(log):
    m = re.match(r"\s*[\d.]+ (\S+) >> [0-9A-F]{2}0002(21[0-9A-F]{2})\\r", log[i])
    if m and m.group(1) >= start:
        clock = m.group(1)
        payload = b""
        i += 1
        while i < len(log) and "<<" in log[i]:
            for fr in re.findall(r"300([0-9A-F]+)\\r", log[i]):
                d = bytes.fromhex(fr if len(fr) % 2 == 0 else fr[:-1])
                if len(d) > 1:
                    payload += d[1:]
            if log[i].endswith("\\r>"):
                break
            i += 1
        if len(payload) > 4 and payload[2] == 0x61:
            n = payload[0] << 8 | payload[1]
            body = payload[2:2 + n]
            seen[body[1]].append((clock, body[2:]))
    i += 1
for g in sorted(seen):
    rows = seen[g]
    print(f"== 群組 {g} (0x{g:02X}) {len(rows)} 次  {rows[0][0]}..{rows[-1][0]}")
    distinct = list(dict.fromkeys(r[1] for r in rows))
    print(f"   不同內容 {len(distinct)} 種")
    for clock, body in [next(r for r in rows if r[1] == d) for d in distinct][:8]:
        vals = [decode_value(*body[k:k + 3]) for k in range(0, len(body) - 2, 3)]
        print(f"  {clock} " + " | ".join(f"{v['raw']}={v['value']}{v['unit'] or ''}" for v in vals))
