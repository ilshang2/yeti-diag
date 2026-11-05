"""Readable table from a *_vcds_night.jsonl: module, group, field, label, value (last sample)."""
import json, sys

def table(path, module=None):
    last = {}
    for line in open(path, encoding="utf-8"):
        o = json.loads(line)
        if "screen" not in o or (module and o["module"] != module):
            continue
        rows = [x for x in o["screen"] if x[0] == "Edit" and x[5].isdigit()]
        for g, row in zip(o["groups"], rows):
            y = row[2]
            cells = sorted((x for x in o["screen"] if x[0] == "Static" and x[1] > 150
                            and abs(x[2] - y) <= 3 and x[4] >= 20), key=lambda x: x[1])
            if not any(c[5].strip() for c in cells) or any(c[5] == "ERROR:" for c in cells):
                continue
            for i, c in enumerate(cells[:4]):
                lab = " ".join(x[5].strip() for x in sorted(
                    (x for x in o["screen"] if x[0] == "Static" and abs(x[1] - c[1]) <= 2
                     and y + 26 <= x[2] <= y + 60 and x[4] < 20), key=lambda x: x[2]) if x[5].strip())
                last[(o["module"], g, i + 1)] = (lab, c[5].strip())
    return last

if __name__ == "__main__":
    for (m, g, i), (lab, v) in sorted(table(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None).items()):
        print(f"{m} {g:3d}.{i}  {lab:<45} {v}")
