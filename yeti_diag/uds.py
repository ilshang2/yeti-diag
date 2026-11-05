"""Minimal UDS / OBD client over ISO-TP.

`request()` only sends reading services. Anything that writes, codes,
resets or clears is refused before it reaches the bus; those go through
writer.py, which backs up, checks and verifies around every change.
"""
import time

import isotp

# Services request() may send. Everything else raises ReadOnlyViolation.
READ_ONLY_SERVICES = {
    0x01: "OBD 即時數據",
    0x03: "OBD 已儲存故障碼",
    0x06: "OBD 車載監測測試結果 (Mode $06)",
    0x19: "讀取故障碼",
    0x22: "讀取資料 (DID)",
    0x3E: "保持連線",
}

NRC_TEXT = {
    0x10: "一般拒絕",
    0x11: "不支援此服務",
    0x12: "不支援此子功能",
    0x13: "訊息長度或格式錯誤",
    0x22: "條件不符",
    0x31: "超出範圍 (不支援此 DID)",
    0x33: "需要安全存取",
    0x7E: "目前階段不支援子功能",
    0x7F: "目前階段不支援此服務",
}


class ReadOnlyViolation(Exception):
    pass


class NoResponse(Exception):
    pass


class NegativeResponse(Exception):
    def __init__(self, service, nrc):
        self.service, self.nrc = service, nrc
        super().__init__(f"0x{service:02X} 被拒絕: 0x{nrc:02X} {NRC_TEXT.get(nrc, '未知')}")


def can_channel(bus, tx_id, rx_id):
    """ISO-TP channel on a python-can bus (USB-CAN adapters, simulator)."""
    address = isotp.Address(isotp.AddressingMode.Normal_11bits, txid=tx_id, rxid=rx_id)
    # VAG modules expect padded frames.
    return isotp.CanStack(bus, address=address, params={"tx_padding": 0x55, "blocking_send": True})


class UdsClient:
    """One ISO-TP channel to one module.

    `channel` is anything with start/stop/send/recv/clear_rx_queue:
    an isotp.CanStack (see can_channel) or an elm327.ElmChannel.
    """

    def __init__(self, channel, timeout=1.0):
        self.timeout = timeout
        self.stack = channel

    def __enter__(self):
        self.stack.start()
        return self

    def __exit__(self, *exc):
        self.stack.stop()

    def request(self, payload):
        payload = bytes(payload)
        service = payload[0]
        if service not in READ_ONLY_SERVICES:
            raise ReadOnlyViolation(f"服務 0x{service:02X} 不是唯讀服務，已拒絕送出")
        return self._transact(payload)

    def _transact(self, payload):
        service = payload[0]
        self.stack.clear_rx_queue()
        self.stack.send(payload)
        deadline = time.monotonic() + self.timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise NoResponse(f"模組沒有回應 (服務 0x{service:02X})")
            resp = self.stack.recv(block=True, timeout=left)
            if resp is None:
                continue
            if resp[0] == 0x7F and len(resp) >= 3 and resp[1] == service:
                if resp[2] == 0x78:  # response pending: module needs more time
                    deadline = time.monotonic() + 5.0
                    continue
                raise NegativeResponse(service, resp[2])
            if resp[0] == service + 0x40:
                return bytes(resp)

    def read_did(self, did):
        resp = self.request([0x22, did >> 8, did & 0xFF])
        return resp[3:]

    def enter_write_session(self):
        self._transact(bytes([0x10, 0x03]))

    def exit_write_session(self):
        self._transact(bytes([0x10, 0x01]))

    def read_dtcs(self, status_mask=None):
        """Stored faults only by default (FAULT_STATUS_MASK). 0xFF also lists codes whose
        test simply has not run yet (status 0x10): 41 on 08, 93 on 16 on this car, none of
        them faults, and on 01 the long answer overflowed the ELM clone (BUFFERFULL)."""
        status_mask = FAULT_STATUS_MASK if status_mask is None else status_mask
        resp = self.request([0x19, 0x02, status_mask])
        records = resp[3:]
        return [
            {"dtc": int.from_bytes(records[i:i + 3], "big"), "status": records[i + 3]}
            for i in range(0, len(records) - 3, 4)
        ]

    def read_dtc_details(self, dtc):
        """Freeze frame (19 04, snapshot) and extended data (19 06, e.g. counters) of one
        DTC, all records (FF). The layout of VW records is not public, so the raw bytes
        after the DTC and status are returned; None when the module has none."""
        out = {}
        for key, sub in (("snapshot", 0x04), ("extended", 0x06)):
            try:
                resp = self.request([0x19, sub, dtc >> 16 & 0xFF, dtc >> 8 & 0xFF, dtc & 0xFF, 0xFF])
                out[key] = resp[6:].hex().upper() or None
            except NegativeResponse as e:
                out[key] = None if e.nrc == 0x31 else f"負面回應 {e.nrc:02X}"
        return out


# testFailed | pendingDTC | confirmedDTC: what VCDS lists as a fault.
FAULT_STATUS_MASK = 0x0D

DTC_STATUS_BITS = {
    0: "目前失效",
    2: "待確認",
    3: "已確認",
    5: "清除後曾失效",
    7: "警示燈要求",
}


def describe_status(status):
    return "、".join(text for bit, text in DTC_STATUS_BITS.items() if status & (1 << bit)) or "無"


def format_dtc(dtc):
    """3-byte UDS DTC -> 'U0100-00' style (SAE letter + 4 hex digits + failure type)."""
    high = dtc >> 8
    letter = "PCBU"[(high >> 14) & 0x3]
    return f"{letter}{(high >> 12) & 0x3}{high & 0xFFF:03X}-{dtc & 0xFF:02X}"


def decode_did(did, data):
    """Readable text for identification DIDs, hex otherwise."""
    if did in (0xF187, 0xF189, 0xF18C, 0xF190, 0xF197):
        text = data.decode("ascii", errors="replace").strip(" \x00")
        if text.isprintable():
            return text
    return data.hex(" ").upper()


# --- OBD-II service 01 (engine only) ---------------------------------------
OBD_PIDS = {
    0x05: ("冷卻水溫", "°C", lambda d: d[0] - 40),
    0x0C: ("引擎轉速", "rpm", lambda d: (d[0] * 256 + d[1]) / 4),
    0x0D: ("車速", "km/h", lambda d: d[0]),
    0x0F: ("進氣溫度", "°C", lambda d: d[0] - 40),
    0x42: ("模組電壓", "V", lambda d: (d[0] * 256 + d[1]) / 1000),
    0x46: ("外氣溫度", "°C", lambda d: d[0] - 40),
}


def read_obd_pid(client, pid):
    resp = client.request([0x01, pid])
    name, unit, fn = OBD_PIDS[pid]
    return name, fn(resp[2:]), unit


# --- OBD-II Mode $06: on-board monitoring test results (SAE J1979, public standard) ---------
# Answer on CAN: 46 then records of 9 bytes: MID TID UASID value(2) min(2) max(2).
MISFIRE_MIDS = {0xA1: "全部汽缸"} | {0xA1 + n: f"第 {n} 缸" for n in range(1, 13)}
MISFIRE_TIDS = {0x0B: "最近 10 個行車循環的平均失火次數 (EWMA)",
                0x0C: "這次 / 上次行車循環的失火次數"}
# Unit and scaling IDs (J1979 appendix): only the ones misfire monitors use; others stay raw.
UASID = {0x01: ("", 1), 0x24: ("次", 1)}


def mode06_supported(client):
    """MIDs the engine reports, from the 00 / 20 / 40 ... bit masks."""
    mids, base = [], 0x00
    while base <= 0xE0:
        try:
            r = client.request([0x06, base])
        except NegativeResponse:
            break
        mask = int.from_bytes(r[2:6], "big")
        mids += [base + i + 1 for i in range(32) if mask & (1 << (31 - i)) and (base + i + 1) % 0x20]
        if not mask & 1:  # last bit: next block of 32 is supported
            break
        base += 0x20
    return mids


def mode06_records(resp):
    """Records of one Mode $06 answer (without the 46)."""
    out = []
    for i in range(0, len(resp) - 8, 9):
        mid, tid, ua = resp[i], resp[i + 1], resp[i + 2]
        unit, k = UASID.get(ua, ("", 1))
        val, lo, hi = (int.from_bytes(resp[i + j:i + j + 2], "big") * k for j in (3, 5, 7))
        out.append({"mid": f"{mid:02X}", "tid": f"{tid:02X}", "uasid": f"{ua:02X}", "value": val,
                    "min": lo, "max": hi, "unit": unit, "scaled": ua in UASID})
    return out


def read_misfire(client, cylinders=4):
    """Misfire counters per cylinder from Mode $06 MIDs A1 (all) and A2.. (cylinder n)."""
    rows = []
    wanted = [0xA1] + [0xA1 + n for n in range(1, cylinders + 1)]
    # Ask only what the engine lists: the 1.2 TSI supports A2-A5 but not A1, and simply does
    # not answer an unsupported MID (no 7F) (car, 2026-11-06).
    supported = set(mode06_supported(client))
    for mid in [m for m in wanted if m in supported] if supported else wanted:
        try:
            r = client.request([0x06, mid])
        except NegativeResponse as e:
            rows.append({"mid": f"{mid:02X}", "name": MISFIRE_MIDS[mid], "error": f"不支援 ({e.nrc:02X})"})
            continue
        for rec in mode06_records(r[1:]):
            rec["name"] = MISFIRE_MIDS.get(int(rec["mid"], 16), rec["mid"])
            rec["test"] = MISFIRE_TIDS.get(int(rec["tid"], 16), f"TID {rec['tid']}")
            rows.append(rec)
    return rows


# OBD-II PID 01 "monitor status since DTCs cleared" (SAE J1979), what VCDS shows as Readiness.
READINESS_CONTINUOUS = ["失火", "燃油系統", "綜合元件"]  # byte B bits 0-2 supported, 4-6 incomplete
READINESS_SPARK = ["觸媒轉換器", "加熱式觸媒", "油氣蒸發系統", "二次空氣", "冷氣冷媒",
                   "含氧感知器", "含氧感知器加熱", "EGR / VVT"]  # bytes C (supported) / D (incomplete)
READINESS_DIESEL = ["NMHC 觸媒", "NOx / SCR", None, "增壓壓力", None,
                    "排氣感知器", "PM 濾網", "EGR / VVT"]


def decode_readiness(d):
    """4 data bytes of mode 01 PID 01 -> MIL, DTC count and every monitor's state.
    A set bit in byte B high nibble / byte D means the test has NOT completed yet."""
    a, b, c, dd = d[:4]
    diesel = bool(b & 0x08)
    monitors = []
    for i, name in enumerate(READINESS_CONTINUOUS):
        if b & (1 << i):
            monitors.append({"name": name, "kind": "continuous", "complete": not b & (1 << (i + 4))})
    for i, name in enumerate(READINESS_DIESEL if diesel else READINESS_SPARK):
        if name and c & (1 << i):
            monitors.append({"name": name, "kind": "non-continuous", "complete": not dd & (1 << i)})
    return {"mil": bool(a & 0x80), "dtc_count": a & 0x7F, "engine": "diesel" if diesel else "spark",
            "monitors": monitors, "all_complete": all(m["complete"] for m in monitors),
            "raw": bytes(d[:4]).hex(" ").upper()}


def read_readiness(client):
    resp = client.request([0x01, 0x01])
    return decode_readiness(resp[2:])


def read_obd_dtcs(client):
    resp = client.request([0x03])
    codes = []
    data = resp[2:]  # 0x43, DTC count, then 2 bytes per DTC
    for i in range(0, len(data) - 1, 2):
        raw = data[i] << 8 | data[i + 1]
        if raw:
            codes.append(f"{'PCBU'[raw >> 14]}{(raw >> 12) & 0x3}{raw & 0xFFF:03X}")
    return codes
