"""Listen to another tester (VCDS through a Y-cable) and learn from its traffic.

The ELM327 only monitors (ATMA): it sends nothing on the bus, so it cannot disturb
VCDS. Three steps:

1. `record`: monitor and save every frame with the PC clock (one capture file).
2. `exchanges`: rebuild UDS (ISO-TP) and TP2.0 (KWP2000) messages from the frames
   and pair each request with its answer.
3. `match_vcds`: VCDS logs measured values to CSV with its own names and units.
   Each logged column is fitted against the raw bytes read at the same moment, so
   names and formulas come out without screenshots.

What VCDS writes (coding, adaptation) shows up in `actions`: every request that is
not a plain read, in order, with the answer.
"""
import bisect
import csv
import datetime
import io
import math
import re
import time
from dataclasses import dataclass, field

from . import measure, tp20, uds
from .elm327 import ElmError

SETUP_IDS = range(0x200, 0x300)  # TP2.0 channel setup: 0x200 request, 0x200 + dest answer
READ_SERVICES = set(uds.READ_ONLY_SERVICES) | tp20.KWP_READ_SERVICES - {0x10}


@dataclass
class Frame:
    t: float      # PC clock (epoch seconds)
    can_id: int
    data: bytes


@dataclass
class Message:
    t: float      # time of the last frame
    can_id: int
    payload: bytes
    kind: str     # "uds" or "tp20"
    chan: tuple = None  # TP2.0: (address, "request" | "answer") valid when the message ended


@dataclass
class Exchange:
    t: float
    module: str   # VCDS address ("01") when known, else the CAN ID
    kind: str
    request: bytes
    response: bytes = None
    pending: int = 0  # 7F xx 78 answers before the real one


@dataclass
class Capture:
    frames: list = field(default_factory=list)
    gaps: list = field(default_factory=list)   # times of BUFFER FULL (frames lost there)
    header: dict = field(default_factory=dict)


# ---------------------------------------------------------------- recording

def id_filter(ids):
    """ATCF / ATCM pair that passes all `ids` (and possibly a few neighbours)."""
    mask = 0x7FF
    for i in ids[1:]:
        mask &= ~(i ^ ids[0]) & 0x7FF
    return ids[0] & mask, mask


def record(elm, out, seconds=None, ids=None, stop=None, say=print):
    """Monitor the bus and write `epoch_time ID HEX` lines to the open text file `out`.

    The clone stops by itself with BUFFER FULL when frames come faster than Bluetooth
    carries them; that is noted in the file (frames were lost there) and monitoring
    restarts at once. A filter (`ids`) keeps the traffic down to what is being studied.
    Returns counts.
    """
    elm.use_protocol("tp20")  # ATSPB: 500 kbit/s, frames shown with their real length
    try:
        elm.command("ATCSM1")  # silent monitoring: do not acknowledge frames
    except ElmError:
        pass  # older clones do not know it; they are silent in ATMA anyway
    if ids:
        cf, cm = id_filter(ids)
        elm.command(f"ATCF{cf:03X}")
        elm.command(f"ATCM{cm:03X}")
    else:
        elm.command("ATAR")  # no filter
    elm.filter = None  # the next normal request sets its own receive filter again
    out.write(f"# yeti_diag sniff start {datetime.datetime.now().isoformat(timespec='seconds')}"
              f" filter={','.join(f'{i:03X}' for i in ids) if ids else 'none'}\n")
    out.flush()
    counts = {"frames": 0, "buffer_full": 0}
    deadline = time.monotonic() + seconds if seconds else None
    elm._drain()
    elm.ser.write(b"ATMA\r")
    buf = b""
    try:
        while not (deadline and time.monotonic() >= deadline) and not (stop and stop()):
            chunk = elm.ser.read(256)
            if not chunk:
                continue
            now = time.time()
            buf += chunk
            *lines, buf = buf.replace(b">", b"\r>\r").split(b"\r")
            for raw in lines:
                line = raw.decode("ascii", "replace").strip().replace(" ", "")
                if not line or line == "ATMA":
                    continue
                if line == ">":  # the ELM stopped monitoring (after BUFFER FULL): restart
                    elm.ser.write(b"ATMA\r")
                    continue
                if line == "BUFFERFULL":
                    counts["buffer_full"] += 1
                    out.write(f"# {now:.3f} BUFFER FULL\n")
                    say("接頭緩衝區滿了，這段有漏掉的封包 (可用 --ids 縮小範圍)")
                    continue
                if len(line) >= 3 and len(line) % 2 == 1 and all(c in "0123456789ABCDEF" for c in line):
                    out.write(f"{now:.3f} {line[:3]} {line[3:]}\n")
                    counts["frames"] += 1
            # Straight to disk: a sniffer that is stopped (terminate) or loses Bluetooth must not
            # lose its last seconds. On 2026-11-06 the VCDS adaptation save was in the unwritten
            # buffer when the capture was stopped.
            out.flush()
    except KeyboardInterrupt:
        say("停止監聽")
    finally:
        elm.ser.write(b" ")  # any character stops ATMA
        end = time.monotonic() + 1.5
        while time.monotonic() < end and b">" not in elm.ser.read(256):
            pass
        elm._drain()
        if ids:
            elm.command("ATAR")
        out.write(f"# yeti_diag sniff end {datetime.datetime.now().isoformat(timespec='seconds')}\n")
    return counts


def read_capture(text):
    cap = Capture()
    for line in text.splitlines():
        if line.startswith("#"):
            m = re.match(r"# ([\d.]+) BUFFER FULL", line)
            if m:
                cap.gaps.append(float(m.group(1)))
            m = re.match(r"# yeti_diag sniff start (\S+) filter=(\S+)", line)
            if m:
                cap.header = {"start": m.group(1), "filter": m.group(2)}
            continue
        parts = line.split()
        if len(parts) == 3:
            cap.frames.append(Frame(float(parts[0]), int(parts[1], 16), bytes.fromhex(parts[2])))
        elif len(parts) == 2:  # frame without data bytes
            cap.frames.append(Frame(float(parts[0]), int(parts[1], 16), b""))
    return cap


# ---------------------------------------------------------------- messages

def tp20_channels(frames):
    """CAN IDs of TP2.0 channels -> (TP2.0 address, "request" | "answer"), from the
    setup answers seen (bytes 2-3: the ID the tester listens on, 4-5: it sends on)."""
    channels = {}
    for f in frames:
        d = f.data
        if f.can_id in SETUP_IDS and f.can_id != 0x200 and len(d) >= 6 and d[1] == 0xD0:
            dest = f.can_id - 0x200
            channels[d[2] | (d[3] & 0x0F) << 8] = (dest, "answer")
            channels[d[4] | (d[5] & 0x0F) << 8] = (dest, "request")
    return channels


def setup_ids(f):
    """A TP2.0 setup answer -> {CAN ID: (address, direction)} it opens, else {}."""
    d = f.data
    if f.can_id in SETUP_IDS and f.can_id != 0x200 and len(d) >= 6 and d[1] == 0xD0:
        dest = f.can_id - 0x200
        return {d[2] | (d[3] & 0x0F) << 8: (dest, "answer"), d[4] | (d[5] & 0x0F) << 8: (dest, "request")}
    return {}


def messages(frames, uds_ids):
    """Rebuild whole messages. `uds_ids`: CAN IDs that carry ISO-TP.

    TP2.0 channel IDs are taken from the setup answers *as they happen*: VCDS answers
    every channel on the same tester ID (0x300), so a later channel reuses an ID that
    meant another module before (seen on the car 2026-11-05)."""
    live = {}
    state, out = {}, []
    for f in frames:
        d = f.data
        if not d:
            continue
        opened = setup_ids(f)
        if opened:
            live.update(opened)
            for cid in opened:
                state.pop(cid, None)
            continue
        if f.can_id in live:
            op = d[0] >> 4
            if op > 3:  # ACK (B), channel parameters / test / close (A)
                continue
            buf = state.setdefault(f.can_id, bytearray())
            buf += d[1:]
            if op in (1, 3):  # last packet of the message
                if len(buf) >= 2:
                    n = buf[0] << 8 | buf[1]
                    out.append(Message(f.t, f.can_id, bytes(buf[2:2 + n]), "tp20", live[f.can_id]))
                state[f.can_id] = bytearray()
        elif f.can_id in uds_ids:
            pci = d[0] >> 4
            if pci == 0:
                out.append(Message(f.t, f.can_id, bytes(d[1:1 + (d[0] & 0x0F)]), "uds"))
            elif pci == 1 and len(d) >= 2:
                state[f.can_id] = ((d[0] & 0x0F) << 8 | d[1], bytearray(d[2:]))
            elif pci == 2 and isinstance(state.get(f.can_id), tuple):
                total, buf = state[f.can_id]
                buf += d[1:]
                if len(buf) >= total:
                    out.append(Message(f.t, f.can_id, bytes(buf[:total]), "uds"))
                    del state[f.can_id]
    return out, tp20_channels(frames)


def exchanges(frames, uds_modules, tp20_names=None):
    """Pair requests with answers.

    uds_modules: {addr: (tx_id, rx_id)}; tp20_names: {tp20 address: VCDS address}.
    """
    uds_tx = {tx: (a, rx) for a, (tx, rx) in uds_modules.items()}
    uds_rx = {rx: a for a, (tx, rx) in uds_modules.items()}
    msgs, channels = messages(frames, set(uds_tx) | set(uds_rx) | {0x7DF})
    names = tp20_names or {}
    open_req, out = {}, []
    for m in msgs:
        if m.kind == "uds":
            if m.can_id in uds_tx or m.can_id == 0x7DF:
                addr = uds_tx[m.can_id][0] if m.can_id in uds_tx else "7DF"
                ex = Exchange(m.t, addr, "uds", m.payload)
                open_req[addr] = ex
                out.append(ex)
            else:
                ex = open_req.get(uds_rx[m.can_id])
                if ex is None or ex.response is not None:
                    ex = open_req.get("7DF")  # answer to a functional (OBD) request
                _answer(ex, m)
        else:
            dest, direction = m.chan or channels[m.can_id]
            addr = names.get(dest, f"TP2.0 0x{dest:02X}")
            if direction == "answer":
                _answer(open_req.get(addr), m)
            else:
                ex = Exchange(m.t, addr, "tp20", m.payload)
                open_req[addr] = ex
                out.append(ex)
    return out


def _answer(ex, m):
    if ex is None:
        return
    p = m.payload
    if len(p) >= 3 and p[0] == 0x7F and p[2] == 0x78:
        ex.pending += 1  # response pending: the real answer follows
        return
    if ex.response is None:
        ex.response, ex.t = p, m.t


def actions(exs):
    """Everything that is not a plain read (session changes, logins, writes, routines)."""
    return [e for e in exs if e.request and e.request[0] not in READ_SERVICES]


# ---------------------------------------------------------------- observations

def observations(exs):
    """Readings in the traffic: {key: [(t, raw bytes, decoded or None)]}.

    Keys: "01 DID 155A" (UDS 22; several DIDs in one request are split where the next
    asked DID starts), "02 群組 118.4" (KWP 21: one key per value position).
    """
    obs = {}
    for e in exs:
        r = e.response
        if not r or r[0] == 0x7F:
            continue
        q = e.request
        if q[0] == 0x22 and r[0] == 0x62:
            dids = [q[i:i + 2] for i in range(1, len(q) - 1, 2)]
            body, pos = r[1:], 0
            for i, did in enumerate(dids):
                if body[pos:pos + 2] != did:
                    break
                start = pos + 2
                nxt = body.find(dids[i + 1], start) if i + 1 < len(dids) else len(body)
                nxt = len(body) if nxt < 0 else nxt
                obs.setdefault(f"{e.module} DID {did.hex().upper()}", []).append((e.t, body[start:nxt], None))
                pos = nxt
        elif q[0] == 0x21 and r[0] == 0x61 and len(r) >= 2:
            for k, v in enumerate(measure.parse_group(r[2:]), 1):
                raw = r[2 + (k - 1) * 3:2 + k * 3]
                value = v["value"] if isinstance(v["value"], (int, float)) else None
                obs.setdefault(f"{e.module} 群組 {r[1]}.{k}", []).append((e.t, raw, value))
    return obs


# ---------------------------------------------------------------- VCDS CSV

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


def vcds_start(first_line):
    """Start time from the first CSV line, e.g. 'Tuesday,04,November,2026,07:30:15:40345,...'."""
    fields = [x.strip() for x in first_line.split(",")]
    day = month = year = clock = None
    for i, x in enumerate(fields):
        if x.lower() in MONTHS and i >= 1:
            month = MONTHS[x.lower()]
            day = int(fields[i - 1]) if fields[i - 1].isdigit() else None
            year = int(fields[i + 1]) if i + 1 < len(fields) and fields[i + 1].isdigit() else None
        m = re.match(r"(\d{1,2}):(\d{2}):(\d{2})(?::(\d+))?$", x)
        if m:
            clock = m
    if not (day and month and year and clock):
        return None
    h, mi, s, frac = clock.groups()
    t = datetime.datetime(year, month, day, int(h), int(mi), int(s))
    return t.timestamp() + (float("0." + frac) if frac else 0.0)


def _number(text):
    text = text.strip().replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None, 0
    decimals = len(text.split(".")[1]) if "." in text else 0
    return value, decimals


def read_vcds_csv(text, start=None):
    """VCDS log -> {name: {"unit": str, "points": [(epoch t, value, decimals)]}}.

    Columns after a TIME column belong to that TIME (VCDS writes one per group or per
    value). `start` (epoch seconds) overrides the start time in the file's first line.
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {}
    t0 = start if start is not None else vcds_start(",".join(rows[0]))
    if t0 is None:
        raise ValueError("VCDS CSV 第一行找不到開始時間，請用 --vcds-start 指定")
    head = next((i for i, r in enumerate(rows) if any(c.strip().upper() == "TIME" for c in r)), None)
    if head is None:
        raise ValueError("VCDS CSV 找不到 TIME 欄")
    names = rows[head]
    units = [""] * len(names)
    data_rows = rows[head + 1:]
    # Rows above the header (group numbers, value names) and below it (units) help name a column.
    above = rows[head - 1] if head >= 1 else []
    if data_rows and not any(_number(c)[0] is not None for c in data_rows[0][1:]):
        units, data_rows = data_rows[0], data_rows[1:]
    series, time_col = {}, None
    columns = []
    for c, name in enumerate(names):
        if name.strip().upper() == "TIME":
            time_col = c
        elif time_col is not None and name.strip() and name.strip().lower() != "marker":
            label = name.strip()
            if c < len(above) and above[c].strip() and above[c].strip() not in label:
                label = f"{above[c].strip()} {label}"
            while label in series:
                label += "'"
            series[label] = {"unit": units[c].strip() if c < len(units) else "", "points": []}
            columns.append((c, time_col, label))
    for r in data_rows:
        for c, tc, label in columns:
            if c >= len(r) or tc >= len(r):
                continue
            t, _ = _number(r[tc])
            v, dec = _number(r[c])
            if t is not None and v is not None:
                series[label]["points"].append((t0 + t, v, dec))
    return {k: v for k, v in series.items() if v["points"]}


# ---------------------------------------------------------------- matching

def interpretations(raw, decoded):
    """Ways to read raw bytes as one number: name -> value."""
    out = {}
    if decoded is not None:
        out["公式解碼值"] = decoded
    if raw:
        out["無號整數"] = int.from_bytes(raw, "big")
        if len(raw) in (1, 2, 4):
            out["有號整數"] = int.from_bytes(raw, "big", signed=True)
        if 1 < len(raw) <= 4:
            for i, b in enumerate(raw):
                out[f"第 {i + 1} 位元組"] = b
    return out


def nice(k):
    """Readable form of a factor: 0.08, 1/32, ..."""
    if k == 0:
        return "0"
    for n in (1, 2, 4, 8, 10, 16, 20, 32, 40, 50, 64, 100, 128, 200, 256, 1000, 1024, 10000):
        if abs(k * n - round(k * n)) < 1e-6 * max(1, abs(k * n)):
            m = round(k * n)
            return f"{m}" if n == 1 else (f"1/{n}" if m == 1 else f"{m}/{n}")
    return f"{k:.6g}"


def fit(xs, ys, tols):
    """Least squares y = k x + c; None unless every point lies within its tolerance."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    k = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    # Prefer a round factor / offset when it still fits every point (VCDS rounds what it shows).
    ks = [m / n for n in (1, 2, 4, 8, 10, 16, 20, 32, 40, 50, 64, 100, 128, 200, 256, 1000, 1024, 10000)
          for m in (round(k * n),) if m and abs(m / n - k) <= abs(k) * 0.02] + [k]
    for kk in ks:
        c = my - kk * mx
        for cc in (round(c), round(c, 1), round(c, 3), c):
            if all(abs(kk * x + cc - y) <= t for x, y, t in zip(xs, ys, tols)):
                return kk, cc
    return None


def match_vcds(obs, vcds, tolerance=0.6, min_points=3):
    """For each VCDS column, the raw readings that explain it.

    A VCDS point is paired with the reading closest in time (within `tolerance` s).
    A match needs `min_points` pairs, varying values, and every pair on one line.
    Constant columns are reported as `constant` candidates (equal values only).
    """
    results = []
    for name, col in vcds.items():
        found, constant = [], []
        for key, readings in obs.items():
            readings = sorted(readings, key=lambda r: r[0])
            times = [r[0] for r in readings]
            pairs = []
            for t, v, dec in col["points"]:
                i = bisect.bisect_left(times, t)
                j = min((x for x in (i - 1, i) if 0 <= x < len(times)), key=lambda x: abs(times[x] - t))
                if abs(times[j] - t) <= tolerance:
                    pairs.append((readings[j], v, dec))
            if len(pairs) < min_points:
                continue
            ys = [p[1] for p in pairs]
            tols = [0.5 * 10 ** -p[2] + 1e-9 + abs(p[1]) * 1e-6 for p in pairs]
            for how in interpretations(pairs[0][0][1], pairs[0][0][2]):
                xs = []
                for (t, raw, dec), _, _ in pairs:
                    x = interpretations(raw, dec).get(how)
                    if x is None:
                        break
                    xs.append(x)
                if len(xs) != len(pairs):
                    continue
                if len(set(ys)) == 1 and len(set(xs)) == 1:
                    if math.isclose(xs[0], ys[0], abs_tol=tols[0]):
                        constant.append({"key": key, "how": how})
                    continue
                f = fit(xs, ys, tols)
                if f:
                    k, c = f
                    found.append({"key": key, "how": how, "factor": nice(k), "offset": c,
                                  "points": len(pairs), "example": f"{xs[-1]} → {ys[-1]}"})
        found.sort(key=lambda m: (-m["points"], m["how"] != "公式解碼值", m["how"] != "無號整數"))
        results.append({"vcds": name, "unit": col["unit"], "points": len(col["points"]),
                        "matches": found[:5], "constant_candidates": constant[:10]})
    return results
