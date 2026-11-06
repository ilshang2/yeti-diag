"""Read measuring-group names from plain-text VCDS-style label files (.lbl) the user has.

Nothing here ships label files: they are written by Ross-Tech and others. Users who own
some point the tool at their folder (`--labels DIR`, or `<data>/labels`). Encrypted .clb
files (what current VCDS installs mostly have) cannot be read and are reported as such.

Format handled (one entry per line, `;` starts a comment):
    GGG,F,Name[,Name2][,Comment]      measuring group GGG (decimal), field F (1-4)
    REDIRECT,<file>,<part number pattern>   `?` matches one character
Other line kinds (coding, adaptation…) are ignored.
"""
import re
from pathlib import Path

GROUP_LINE = re.compile(r"^(\d{3}),([1-9]),(.*)$")


def part_dashed(part):
    """'1K0937086AA' -> '1K0-937-086-AA' (how label files and redirects name parts)."""
    p = re.sub(r"[\s-]", "", part or "").upper()
    if len(p) < 9:
        return p
    out = f"{p[:3]}-{p[3:6]}-{p[6:9]}"
    return out + (f"-{p[9:]}" if len(p) > 9 else "")


def _read(path):
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def parse(text):
    """{group (int): {field (str): {"name": ..., "detail": ...}}} from one label file."""
    groups = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        m = GROUP_LINE.match(line)
        if not m:
            continue
        parts = [p.strip() for p in m.group(3).split(",")]
        name = " ".join(p for p in parts[:2] if p)  # "Transmission,Speed (G38)" -> one name
        if not name:
            continue
        entry = {"name": name}
        detail = ",".join(parts[2:]).replace("\\n", "\n").strip()
        if detail:
            entry["detail"] = detail
        groups.setdefault(int(m.group(1)), {})[m.group(2)] = entry
    return groups


def _pattern(pat):
    return re.compile("^" + re.escape(part_dashed(pat).upper()).replace(r"\?", "."))


def find_file(folder, part):
    """The label file for a part number: a REDIRECT naming it, else a file named after it.
    Returns (path or None, note)."""
    folder = Path(folder)
    if not folder.is_dir():
        return None, f"標籤資料夾不存在: {folder}"
    dashed = part_dashed(part)
    files = {p.name.upper(): p for p in folder.iterdir() if p.is_file()}
    for p in sorted(files.values()):
        if p.suffix.upper() != ".LBL":
            continue
        for raw in _read(p).splitlines():
            fields = [f.strip() for f in raw.split(";", 1)[0].split(",")]
            if len(fields) >= 3 and fields[0].upper() == "REDIRECT" and _pattern(fields[2]).match(dashed):
                target = files.get(fields[1].upper())
                if target is None:
                    return None, f"{p.name} 指到 {fields[1]}，但資料夾裡沒有這個檔"
                return target, f"{p.name} 轉址到 {target.name}"
    for cut in (len(dashed), 11):  # 1K0-937-086-AA.lbl, then 1K0-937-086.lbl
        for ext in (".LBL", ".CLB"):
            hit = files.get(dashed[:cut] + ext)
            if hit:
                return hit, f"檔名對應 {hit.name}"
    return None, f"找不到 {dashed} 的標籤檔"


def load(folder, part):
    """(labels dict for parse(), note). Empty dict when no readable file."""
    path, note = find_file(folder, part)
    if path is None:
        return {}, note
    if path.suffix.upper() == ".CLB":
        return {}, f"{path.name} 是加密標籤檔 (.clb)，只有 VCDS 能讀，本程式不支援"
    return parse(_read(path)), note


def merge(profile_labels, file_labels):
    """Field names for one group: names checked on a car (profile) win; the label file fills
    the rest and is marked so nobody mistakes it for a verified name."""
    out = {k: dict(v, source="profile") for k, v in (profile_labels or {}).items() if not k.startswith("_")}
    for field, entry in (file_labels or {}).items():
        out.setdefault(field, dict(entry, source="label file"))
    return out
