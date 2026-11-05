"""Unattended VCDS measuring-block survey with sniffing (read only).

Drives the VCDS window (Select -> module -> Meas. Blocks - 08), steps through
groups, and every sample stores every visible text of the screen with its
position and the PC time. The ELM327 on the Y cable records the bus at the same
time (`yeti sniff`). Between modules the battery voltage is checked; below the
limit the run stops (exit code 2) so nobody has to watch it.

Safety: only buttons from ALLOWED are ever clicked, by exact text. Anything
that could write or clear (Coding, Adaptation, Basic Settings, Output Tests,
Clear DTCs, Do It!) is not in the list. Unknown popups are answered only with
an OK button, and their text is logged.

usage: py -3.13 tools/vcds_night.py --elm COM10 [--rounds 8] [--groups 1-64]
       [--modules 09,19,...] [--min-volt 12.2] [--pause-min 30]
"""
import argparse, ctypes, datetime, json, os, re, subprocess, sys, time
from pathlib import Path

import win32con, win32gui

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
VCDS_EXE = r"C:\Ross-Tech\VCDS\VCDS.EXE"

ALLOWED = {"&Select", "Go!", "Meas. Blocks - 08", "&Meas. Blocks - 08", "Done, Go &Back",
           "Close Controller, Go Back - 06", "Go Back", "OK", "確定", "&OK"}
MODULE_BTN = re.compile(r"^\s+([0-9A-F]{2})-")

ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2


def keep_awake():
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)


def log(msg):
    line = f"{datetime.datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)


# ------------------------------------------------------------------ windows

def text(h):
    n = win32gui.SendMessage(h, win32con.WM_GETTEXTLENGTH, 0, 0)
    if not n:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    ctypes.windll.user32.SendMessageW(h, win32con.WM_GETTEXT, n + 1, buf)
    return buf.value


def tops():
    out = []

    def cb(h, _):
        try:
            if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h):
                out.append(h)
        except win32gui.error:
            pass
    win32gui.EnumWindows(cb, None)
    return out


def kids(h):
    out = []
    try:
        win32gui.EnumChildWindows(h, lambda c, _: out.append(c), None)
    except win32gui.error:
        pass
    return [c for c in out if win32gui.IsWindowVisible(c)]


def find_top(pattern, timeout=0.0):
    end = time.time() + timeout
    while True:
        for h in tops():
            if re.search(pattern, win32gui.GetWindowText(h)):
                return h
        if time.time() >= end:
            return None
        time.sleep(0.3)


def click(h, label):
    if label not in ALLOWED and not MODULE_BTN.match(label):
        raise RuntimeError(f"refusing to click {label!r}")
    for c in kids(h):
        if win32gui.GetClassName(c) == "Button" and text(c) == label:
            win32gui.PostMessage(c, win32con.BM_CLICK, 0, 0)
            return c
    return None


def vcds_pids():
    import win32process
    pids = set()
    for h in tops():
        try:
            if win32gui.GetWindowText(h).startswith("VCDS"):
                pids.add(win32process.GetWindowThreadProcessId(h)[1])
        except win32gui.error:
            pass
    return pids


def popups(record):
    """Answer VCDS's own message boxes with OK only; return their texts.
    Windows of other programs are never touched; windows that vanish mid-scan are skipped."""
    import win32process
    seen = []
    pids = vcds_pids()
    for h in tops():
        try:
            if win32process.GetWindowThreadProcessId(h)[1] not in pids:
                continue
            title = win32gui.GetWindowText(h)
            if win32gui.GetClassName(h) != "#32770" or title.startswith("VCDS Release")                     or title.startswith("VCDS:  Main"):
                continue
            body = " | ".join(text(c) for c in kids(h) if win32gui.GetClassName(c) == "Static" and text(c))
            btns = [text(c) for c in kids(h) if win32gui.GetClassName(c) == "Button"]
        except win32gui.error:
            continue
        ok = next((b for b in btns if b in ("OK", "確定", "&OK")), None)
        record({"popup": title, "text": body, "buttons": btns, "answered": ok})
        seen.append(body)
        if ok:
            click(h, ok)
        time.sleep(0.5)
    return seen


def snapshot(h):
    """All visible texts of a window with their screen rectangles."""
    out = []
    for c in kids(h):
        try:
            t = text(c)
            if t:
                r = win32gui.GetWindowRect(c)
                out.append([win32gui.GetClassName(c), r[0], r[1], r[2] - r[0], r[3] - r[1], t])
        except win32gui.error:
            pass
    return out


# ------------------------------------------------------------------ car side

ENV = dict(os.environ, PYTHONPATH=f"{ROOT / '.deps'};{ROOT}", PYTHONIOENCODING="utf-8")


def run_yeti(args, *cmd, background=False):
    full = [sys.executable, "-m", "yeti_diag", "--elm", args.elm, *cmd]
    if background:
        return subprocess.Popen(full, cwd=ROOT, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
    p = subprocess.run(full, cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    return p.returncode, p.stdout + p.stderr


def voltage(args):
    for _ in range(3):
        rc, out = run_yeti(args, "voltage")
        m = re.search(r"電瓶電壓:\s*([\d.]+)", out)
        if m:
            return float(m.group(1))
        time.sleep(5)
    return None


# ------------------------------------------------------------------ survey

def ensure_main():
    h = find_top(r"^VCDS:  Main Screen")
    if not h:
        subprocess.Popen([VCDS_EXE], cwd=os.path.dirname(VCDS_EXE))
        h = find_top(r"^VCDS:  Main Screen", 20)
    return h


def back_to_select(record):
    for _ in range(4):
        popups(record)
        m = find_top(r"Measuring Blocks")
        if m:
            click(m, "Done, Go &Back"); time.sleep(2); continue
        o = find_top(r"Open Controller")
        if o:
            click(o, "Close Controller, Go Back - 06"); time.sleep(3); continue
        break
    s = find_top(r"Select Control Module")
    if not s:
        main = ensure_main()
        click(main, "&Select")
        s = find_top(r"Select Control Module", 10)
    return s


def module_buttons(sel):
    return {MODULE_BTN.match(text(c)).group(1): text(c) for c in kids(sel)
            if win32gui.GetClassName(c) == "Button" and MODULE_BTN.match(text(c))}


def survey_module(addr, groups, record):
    sel = back_to_select(record)
    if not sel:
        record({"error": "no select screen"}); return False
    btns = module_buttons(sel)
    if addr not in btns:
        record({"module": addr, "error": "no button"}); return False
    click(sel, btns[addr])
    o = find_top(r"Open Controller", 25)
    if not o:
        record({"module": addr, "error": "open failed", "popups": popups(record)})
        back_to_select(record)
        return False
    record({"module": addr, "controller": snapshot(o)})
    if not click(o, "&Meas. Blocks - 08") and not click(o, "Meas. Blocks - 08"):
        record({"module": addr, "error": "no Meas. Blocks button (UDS module?)"})
        back_to_select(record)
        return False
    m = find_top(r"Measuring Blocks", 15)
    if not m:
        record({"module": addr, "error": "meas screen missing", "popups": popups(record)})
        back_to_select(record)
        return False
    edits = [c for c in kids(m) if win32gui.GetClassName(c) == "Edit"][:3]
    gos = [c for c in kids(m) if win32gui.GetClassName(c) == "Button" and text(c) == "Go!"][:3]
    has_data = False
    prev = None
    for i in range(0, len(groups), 3):
        batch = groups[i:i + 3]
        for e, g, b in zip(edits, batch, gos):
            win32gui.SendMessage(e, win32con.WM_SETTEXT, 0, f"{g:03d}")
            win32gui.PostMessage(b, win32con.BM_CLICK, 0, 0)
            time.sleep(0.25)
        # Wait until every row shows the new group (values/labels changed, or the
        # 'ERROR: Group NNN' of the requested group), at most 3.5 s.
        t_end = time.time() + 3.5
        while True:
            shot = snapshot(m)
            if fresh(shot, batch, prev) or time.time() >= t_end:
                break
            time.sleep(0.25)
        for k in range(2):  # two samples per batch
            if k:
                time.sleep(0.7)
                shot = snapshot(m)
            p = popups(record)
            if not find_top(r"Measuring Blocks"):
                record({"module": addr, "error": "meas screen closed", "popups": p})
                back_to_select(record)
                return False
            record({"module": addr, "groups": batch, "sample": k, "screen": shot})
            if row_has_data(shot):
                has_data = True
        prev = shot
        if i >= 3 and not has_data and groups[0] == 1:  # only judge a module by its first groups
            record({"module": addr, "error": "no data in first groups, skipped"})
            back_to_select(record)
            return False
    back_to_select(record)
    return True


def row_cells(screen, row):
    return [x[5].strip() for x in screen if x[0] == "Static" and x[1] > 150
            and abs(x[2] - row[2]) <= 3 and x[4] >= 20]


def row_sig(screen, row):
    """Everything right of the group box that belongs to this row: values + labels."""
    return tuple(x[5] for x in screen if x[0] == "Static" and x[1] > 150
                 and row[2] - 3 <= x[2] <= row[2] + 60)


def fresh(screen, batch, prev):
    rows = [x for x in screen if x[0] == "Edit" and x[5].isdigit()]
    prow = [x for x in prev if x[0] == "Edit" and x[5].isdigit()] if prev else []
    for n, (g, row) in enumerate(zip(batch, rows)):
        sig = row_sig(screen, row)
        if f"Group {g:03d}" in sig:
            continue
        if not any(t.strip() for t in sig[:4]):
            return False
        if prev and n < len(prow) and sig == row_sig(prev, prow[n]):
            return False
    return True


def row_has_data(screen):
    rows = [x for x in screen if x[0] == "Edit" and x[5].isdigit()]
    return any(any(c) and "ERROR:" not in c for c in (row_cells(screen, r) for r in rows))


def answered_groups(paths):
    """{module: [groups that showed data]} from an earlier run.

    Each group row sits at the y of its number box (Edit); the value cells are the
    26 px high Statics right of it. A row with only blanks or 'ERROR:' had no data
    (09/19 print ERROR, 46 leaves the row empty)."""
    seen, good = {}, {}
    lines = [l for p in paths.split(",") for l in open(p, encoding="utf-8")]
    for line in lines:
        o = json.loads(line)
        if "screen" not in o:
            continue
        m = o["module"]
        seen.setdefault(m, set())
        good.setdefault(m, set())
        rows = [x for x in o["screen"] if x[0] == "Edit" and x[5].isdigit()]
        for g, row in zip(o["groups"], rows):
            seen[m].add(g)
            cells = row_cells(o["screen"], row)
            if any(cells) and "ERROR:" not in cells:
                good[m].add(g)
    return {m: sorted(good[m]) for m in seen}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--elm", default="COM10")
    ap.add_argument("--modules", default="09,19,46,17,08,03,15,16,25,42,52,62,72,44,02,01")
    ap.add_argument("--groups", default="1-64")
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--pause-min", type=float, default=30)
    ap.add_argument("--min-volt", type=float, default=12.2)
    ap.add_argument("--groups-from", help="earlier *_vcds_night.jsonl: only modules/groups that answered there")
    ap.add_argument("--first-round", type=int, default=1, help="number of the first round (for the log)")
    a = ap.parse_args()
    lo, hi = (int(x) for x in a.groups.split("-"))
    groups = list(range(lo, hi + 1))
    modules = a.modules.split(",")
    per_module = {m: groups for m in modules}
    if a.groups_from:
        per_module = answered_groups(a.groups_from)
        modules = [m for m in modules if per_module.get(m)]
        log("from " + a.groups_from + ": " + ", ".join(f"{m}:{len(per_module[m])}" for m in modules))
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = open(REPORTS / f"{stamp}_vcds_night.jsonl", "a", encoding="utf-8")

    def record(obj):
        obj.setdefault("t", time.time())
        out.write(json.dumps(obj, ensure_ascii=False) + "\n"); out.flush()

    keep_awake()
    log(f"log -> {out.name}")
    for rnd in range(a.first_round, a.first_round + a.rounds):
        for addr in modules:
            groups = per_module[addr]
            keep_awake()
            v = voltage(a)
            record({"round": rnd, "module": addr, "voltage": v})
            log(f"round {rnd} module {addr} voltage {v}")
            if v is None or v < a.min_volt:
                log(f"STOP: voltage {v} below {a.min_volt}")
                back_to_select(record)
                return 2
            sn = run_yeti(a, "sniff", "--seconds", str(len(groups) // 3 * 9 + 90), background=True)
            time.sleep(6)  # sniffer opening the port and starting ATMA
            t0 = time.time()
            ok = survey_module(addr, groups, record)
            log(f"  module {addr}: {'ok' if ok else 'skipped'} in {time.time() - t0:.0f}s")
            sn.terminate()
            try:
                tail = sn.communicate(timeout=20)[0][-300:]
            except subprocess.TimeoutExpired:
                sn.kill(); tail = ""
            record({"round": rnd, "module": addr, "sniff_tail": tail})
            time.sleep(3)
        if rnd < a.first_round + a.rounds - 1:
            log(f"round {rnd} done, pause {a.pause_min} min")
            end = time.time() + a.pause_min * 60
            while time.time() < end:
                keep_awake(); time.sleep(60)
    log("all rounds done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
