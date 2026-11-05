"""Values of the UDS DIDs another app read, from tools/btsnoop_elm.py output.

Apps like Car Scanner ask several DIDs per request (22 155A 15AA 2206); the
answer is 62 + each DID followed by its data. Data lengths are not sent, so a
DID's data runs until the next requested DID. Prints, per DID, the raw values
seen over the log (first / last / distinct count), to match with the app's screen.

    py -3.13 tools\\elm_log_dids.py reports\\phone\\elm_log.txt [rx_id]
    py -3.13 tools\\elm_log_dids.py reports\\phone\\elm_log.txt 7E8 155A 2029   (each reading with its time)
"""
import collections
import re
import sys


def frames(text):
    return re.findall(r"([0-9A-F]{3})([0-9A-F]{2,16})(?=\\r|$)", text)


def reassemble(lines, rx):
    """ISO-TP payload from the frames of one answer (CAF1 lines, headers on)."""
    out, total = b"", None
    for can_id, data in lines:
        if can_id != rx:
            continue
        d = bytes.fromhex(data)
        pci = d[0] >> 4
        if pci == 0:
            return d[1:1 + (d[0] & 0x0F)]
        if pci == 1:
            total, out = (d[0] & 0x0F) << 8 | d[1], d[2:]
        elif pci == 2 and total:
            out += d[1:]
    return out[:total] if total and len(out) >= total else None


def split(resp, dids):
    """62 DID data DID data ... -> {DID: data}, cut at the next requested DID."""
    if not resp or resp[0] != 0x62:
        return {}
    body, found = resp[1:], {}
    pos = 0
    for i, did in enumerate(dids):
        key = bytes.fromhex(did)
        if body[pos:pos + 2] != key:
            break
        start = pos + 2
        nxt = len(body)
        if i + 1 < len(dids):
            j = body.find(bytes.fromhex(dids[i + 1]), start)
            nxt = j if j >= 0 else len(body)
        found[did] = body[start:nxt]
        pos = nxt
    return found


def main(path, rx="7E8", series=None):
    log = open(path, encoding="utf-8").read().splitlines()
    seen = collections.defaultdict(list)
    times = collections.defaultdict(list)
    i = 0
    while i < len(log):
        m = re.search(r">> 22((?:[0-9A-F]{4})+)[0-9A-F]?\\r", log[i])
        if m:
            clock = log[i].split()[1] if ":" in log[i].split()[1] else ""
            dids = re.findall("....", m.group(1))
            text = ""
            i += 1
            while i < len(log) and "<<" in log[i]:
                text += log[i].split("<< ", 1)[1]
                if text.rstrip().endswith(">"):
                    break
                i += 1
            for did, data in split(reassemble(frames(text), rx), dids).items():
                seen[did].append(data.hex().upper())
                times[did].append((clock, data.hex().upper()))
        i += 1
    if series:
        for did in series:
            print(did + ": " + "  ".join(f"{c} {v}({int(v, 16) if v else ''})" for c, v in times[did]))
        return
    for did in sorted(seen):
        vals = seen[did]
        distinct = list(dict.fromkeys(vals))
        ints = [int(v, 16) for v in distinct if v]
        print(f"{did}: {len(vals):3d} 次  首 {vals[0]:<10} 末 {vals[-1]:<10} 不同值 {len(distinct):3d}"
              + (f"  範圍 {min(ints)}..{max(ints)}" if ints else ""))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1], *sys.argv[2:3], series=sys.argv[3:] or None)
