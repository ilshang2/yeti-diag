"""Pair VCDS screen values (tools/vcds_night.py logs) with the sniffed raw answers.

For every module / group / field: VCDS name, VCDS text, raw 3 bytes (formula, a, b),
our decoded value, and whether they agree. Numbers are paired by time (nearest
reading of the same group within 1.5 s); across samples a linear fit tells the
scaling when our formula is unknown or disagrees.

usage: py -3.13 tools/vcds_pair.py reports/<a>_vcds_night.jsonl [more.jsonl ...] [--json out.json]
"""
import bisect, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".deps"), str(ROOT)]
from yeti_diag import sniff  # noqa: E402
from yeti_diag.cli import REAR_DOOR_DEST, UDS_MODULES  # noqa: E402

NUM = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(.*)$")


def screens(paths):
    for p in paths:
        for line in open(p, encoding="utf-8"):
            o = json.loads(line)
            if "screen" in o:
                yield o


def vcds_points(paths):
    """{(module, group, field): {"label", "points": [(t, text)]}} from the screen logs."""
    out = {}
    for o in screens(paths):
        rows = [x for x in o["screen"] if x[0] == "Edit" and x[5].isdigit()]
        for g, row in zip(o["groups"], rows):
            y = row[2]
            cells = sorted((x for x in o["screen"] if x[0] == "Static" and x[1] > 150
                            and abs(x[2] - y) <= 3 and x[4] >= 20), key=lambda x: x[1])
            if not any(c[5].strip() for c in cells) or any(c[5] == "ERROR:" for c in cells):
                continue
            for i, c in enumerate(cells[:4], 1):
                lab = " ".join(x[5].strip() for x in sorted(
                    (x for x in o["screen"] if x[0] == "Static" and abs(x[1] - c[1]) <= 2
                     and y + 26 <= x[2] <= y + 60 and x[4] < 20), key=lambda x: x[2]) if x[5].strip())
                d = out.setdefault((o["module"], g, i), {"label": lab, "points": []})
                d["label"] = d["label"] or lab
                d["points"].append((o["t"], c[5].strip()))
    return out


def sniff_files_for(t0, t1):
    """Capture files whose start lies before t1 and that have data after t0."""
    files = []
    for f in sorted((ROOT / "reports").glob("2026*_sniff.txt")):
        if f.stat().st_size == 0:
            continue
        first = None
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                if line[:1].isdigit():
                    first = float(line.split()[0]); break
        if first is None or first > t1 or f.stat().st_mtime < t0:
            continue
        files.append(f)
    return files


def observations(files, module):
    obs = {}
    mods = {a: (m.tx_id, m.rx_id) for a, m in UDS_MODULES.items()}
    names = {d: a for a, d in REAR_DOOR_DEST.items()}
    for f in files:
        cap = sniff.read_capture(f.read_text(encoding="utf-8"))
        for k, v in sniff.observations(sniff.exchanges(cap.frames, mods, names)).items():
            m = re.match(r".* 群組 (\d+)\.(\d+)$", k)
            if m:  # one module per session: whatever TP2.0 name the channel got, it is `module`
                obs.setdefault((module, int(m.group(1)), int(m.group(2))), []).extend(v)
    return obs


def main(argv):
    out_json = None
    if "--json" in argv:
        i = argv.index("--json"); out_json = argv[i + 1]; argv = argv[:i] + argv[i + 2:]
    vc = vcds_points(argv)
    by_mod = {}
    for (m, g, i), d in vc.items():
        ts = [p[0] for p in d["points"]]
        lo, hi = by_mod.get(m, (min(ts), max(ts)))
        by_mod[m] = (min(lo, min(ts)), max(hi, max(ts)))
    obs = {}
    for m, (t0, t1) in by_mod.items():
        obs.update(observations(sniff_files_for(t0 - 30, t1 + 30), m))
    rows = []
    for key in sorted(vc):
        d = vc[key]
        reads = sorted(obs.get(key, []), key=lambda r: r[0])
        times = [r[0] for r in reads]
        pairs = []
        for t, txt in d["points"]:
            if not times:
                break
            j = bisect.bisect_left(times, t)
            j = min((x for x in (j - 1, j) if 0 <= x < len(times)), key=lambda x: abs(times[x] - t))
            if abs(times[j] - t) <= 1.5:
                pairs.append((txt, reads[j][1], reads[j][2]))
        row = {"module": key[0], "group": key[1], "field": key[2], "label": d["label"],
               "vcds": d["points"][-1][1], "samples": len(d["points"]), "paired": len(pairs)}
        if pairs:
            txt, raw, dec = pairs[-1]
            row["raw"] = raw.hex().upper()
            row["formula"] = f"{raw[0]:02X}" if raw else None
            row["ours"] = dec
            nums = [(NUM.match(t), r, dd) for t, r, dd in pairs]
            if all(n for n, _, _ in nums) and dec is not None:
                ok = all(abs(float(n.group(1)) - dd) <= max(0.06, abs(dd) * 0.01)
                         for n, _, dd in nums if dd is not None)
                row["agree"] = ok
                row["unit"] = nums[-1][0].group(2)
            elif all(n for n, _, _ in nums):
                row["agree"] = None  # number on VCDS, our formula has no number
                row["unit"] = nums[-1][0].group(2)
            else:
                # text value: raw -> text table
                row["texts"] = sorted({f"{r.hex().upper()}={t}" for t, r, _ in pairs})
        rows.append(row)
    for r in rows:
        mark = {True: "✔", False: "✘", None: "?"}.get(r.get("agree"), " ")
        extra = r.get("texts") or ""
        print(f"{r['module']} {r['group']:3d}.{r['field']} {mark} {r['label'][:38]:<38} "
              f"VCDS={r['vcds']:<16} raw={r.get('raw', '-'):<8} ours={r.get('ours')} {extra if extra else ''}")
    s = {"✔": sum(r.get("agree") is True for r in rows), "✘": sum(r.get("agree") is False for r in rows),
         "?": sum(r.get("agree") is None and "raw" in r and "texts" not in r for r in rows),
         "text": sum("texts" in r for r in rows), "no raw": sum("raw" not in r for r in rows)}
    print("summary:", s)
    if out_json:
        Path(out_json).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
