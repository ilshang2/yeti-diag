"""VW TP2.0 transport and a read-only KWP2000 client.

TP2.0 in short:
  setup     tester -> 0x200:        [dest, C0, 00, 10, rxLo, rxHi, 01]
            ECU    -> 0x200+dest:   [00, D0, rxLo, rxHi, txLo, txHi, 01]
            (rx = where the tester listens, tx = where the tester sends)
  params    tester -> tx: A0 0F CA FF 32 FF      ECU -> rx: A1 ...
  data      first byte = opcode | sequence:
              0x0n more follows, ACK wanted   0x1n last, ACK wanted
              0x2n more follows, no ACK       0x3n last, no ACK
            payload starts with a 2-byte KWP message length
  ACK       0xB0 | next expected sequence
  close     A8
"""
import time

from .uds import NegativeResponse, NoResponse, ReadOnlyViolation

APP_KWP = 0x01
SETUP_ID = 0x200
TESTER_RX = 0x300


class Tp20Error(Exception):
    pass


class Tp20Busy(Tp20Error):
    """Setup refused with D6-D8. On the car the gateway answers D8 for every
    address for ~2 s after a channel ended badly; wait longer before retrying."""


class CanLink:
    """Raw CAN frames through python-can (USB-CAN adapters, simulator)."""

    stepwise = False  # sees every frame as it arrives

    def __init__(self, bus):
        self.bus = bus

    def exchange(self, tx_id, data, rx_id, wait=0.3, expect=None, no_reply=False):
        import can
        while self.bus.recv(timeout=0):  # drop stale frames
            pass
        self.bus.send(can.Message(arbitration_id=tx_id, data=data, is_extended_id=False))
        return [] if no_reply else self.listen(rx_id, wait)

    def listen(self, rx_id, wait):
        frames, deadline = [], time.monotonic() + wait
        while (left := deadline - time.monotonic()) > 0:
            msg = self.bus.recv(timeout=left)
            if msg is not None and msg.arbitration_id == rx_id:
                frames.append(bytes(msg.data))
                deadline = time.monotonic() + 0.05  # keep going while frames arrive
        return frames


class ElmLink:
    """Raw CAN frames through an ELM327 (ATCAF0 mode).

    The ELM only reports frames after it sends one, and only once its wait
    ends, which is too late for TP2.0 ACKs. So the channel runs stepwise:
    block size 1, and each send names how many replies to return after.
    """

    stepwise = True

    def __init__(self, elm):
        self.elm = elm

    def exchange(self, tx_id, data, rx_id, wait=0.3, expect=None, no_reply=False):
        self.elm.use_protocol("tp20")
        # Door modules take >200 ms to answer; wait long only when a reply is expected and
        # the ELM can be told how many (it returns as soon as they are in). An 8-byte frame
        # cannot carry the count: a long wait there outlasts the module's answer, the module
        # repeats it unacknowledged every 150-200 ms and drops the channel (09 adaptation
        # 31 B9 01 03 xx, Vgate, 2026-11-05). So ~100 ms there; a slower answer is picked up by
        # recv() (listen), which ACKs the repeat.
        if expect is not None and len(data) >= 8:
            self.elm.set_st(0x19)
        else:
            self.elm.set_long_wait(expect is not None)
        self.elm.select(tx_id, rx_id)
        frames = self.elm.send_frame(bytes(data), wait=max(wait, 1.5 if expect else 0.3), expect=expect,
                                     no_reply=no_reply)
        return [d for i, d in frames if i == rx_id]

    def listen(self, rx_id, wait, expect=2):
        # The ELM cannot just listen (ATMA overflows on clones), so send a TP2.0
        # channel test (A3); the module answers A1 and resends anything un-ACKed.
        # expect=2 (A1 + one data packet) returns at once so that packet can be ACKed
        # in time; listening longer let the door modules give up and close (A8).
        self.elm.use_protocol("tp20")
        self.elm.set_long_wait(False)
        return [d for i, d in self.elm.send_frame(b"\xA3", wait=max(wait, 0.3), expect=expect) if i == rx_id]


class Tp20Channel:
    """fast (experimental, ELM only): block size 15 and no reply counts, so a module
    sends a whole answer at once and the ELM returns when the bus goes quiet (ATST).
    This is how Car Scanner reads the DSG (Bluetooth log 2026-11-03); the default
    stepwise mode ACKs every packet, which was proven on all modules of this car."""

    def __init__(self, link, dest, timeout=1.0, fast=False):
        self.link, self.dest, self.timeout = link, dest, timeout
        self.tx = self.rx = None
        self.seq = 0      # next sequence number we send
        self.rx_seq = 0   # next sequence number we expect from the module
        self.buf = bytearray()
        self.messages = []
        self.closed_by_module = False
        self.stepwise = getattr(link, "stepwise", False) and not fast
        self.fast_elm = fast and getattr(link, "stepwise", False)
        self.block_size = 0x01 if self.stepwise else 0x0F
        self.params = bytes([self.block_size, 0xCA, 0xFF, 0x32, 0xFF])

    def open(self):
        req = bytes([self.dest, 0xC0, 0x00, 0x10, TESTER_RX & 0xFF, TESTER_RX >> 8, APP_KWP])
        # The module repeats its setup reply; expect=1 lets the ELM return after the first.
        frames = self.link.exchange(SETUP_ID, req, SETUP_ID + self.dest, self.timeout, expect=1)
        resp = next((f for f in frames if len(f) >= 7 and f[1] == 0xD0), None)
        refused = next((f for f in frames if len(f) >= 2 and f[1] in (0xD6, 0xD7, 0xD8)), None)
        if resp is None and refused is not None:
            raise Tp20Busy(f"TP2.0 位址 0x{self.dest:02X} 暫時拒絕開通道 ({refused[1]:02X})")
        if resp is None:
            raise NoResponse(f"TP2.0 位址 0x{self.dest:02X} 沒有回應")
        self.rx = resp[2] | (resp[3] & 0x0F) << 8
        self.tx = resp[4] | (resp[5] & 0x0F) << 8
        self.seq = self.rx_seq = 0
        # A0 + params; T1 0xCA = 1 s. The module still answers its own T1 (8A = 100 ms),
        # so ACKs must stay fast whatever we send here.
        frames = self.link.exchange(self.tx, b"\xA0" + self.params, self.rx, self.timeout,
                                    expect=1 if self.stepwise else None)
        if not any(f[0] == 0xA1 for f in frames):
            raise Tp20Error(f"TP2.0 位址 0x{self.dest:02X} 沒有回應通道參數")
        return self

    def close(self):
        if self.tx is not None:
            try:
                self.link.exchange(self.tx, bytes([0xA8]), self.rx, 0.05)
            except Exception:
                pass
            self.tx = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def send(self, payload):
        msg = len(payload).to_bytes(2, "big") + bytes(payload)
        chunks = [msg[i:i + 7] for i in range(0, len(msg), 7)]
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            op = (0x10 if last else 0x20) | self.seq
            self.seq = (self.seq + 1) & 0x0F
            # Stepwise: return after the module's ACK plus its first data packet.
            # Packets before the last need no reply, so they go out without waiting.
            expect = 2 if self.stepwise and last else None
            self._process(self.link.exchange(self.tx, bytes([op]) + chunk, self.rx,
                                             self.timeout if last else 0.05, expect=expect,
                                             no_reply=not last))

    def _ack(self, seq, more):
        ack = bytes([0xB0 | ((seq + 1) & 0x0F)])
        expect = 1 if self.stepwise and more else None
        return self.link.exchange(self.tx, ack, self.rx, self.timeout if more else 0.05, expect=expect)

    def _answer_test(self):
        # A1 + our parameters; expect=1 picks up a data packet that follows at once.
        return self.link.exchange(self.tx, b"\xA1" + self.params, self.rx, 0.05,
                                  expect=1 if self.stepwise else None)

    def _process(self, frames, answer_tests=True):
        for f in frames:
            kind = f[0] >> 4
            if f[0] == 0xA8:
                self.closed_by_module = True
                return  # recv() keeps any reply that was already complete
            if f[0] == 0xA3 and answer_tests:
                # The module's channel test while it works on a slow request (door
                # DTC reads). Without our A1 it closes the channel (A8). One answer
                # per batch, and none for tests that come back with it, so a module
                # repeating A3 cannot keep us answering forever.
                answer_tests = False
                self._process(self._answer_test(), answer_tests=False)
                continue
            if kind == 0xB or f[0] in (0xA1, 0xA3) or kind > 3:
                continue  # ACK / parameter / other frames
            seq = f[0] & 0x0F
            if seq != self.rx_seq:
                # A retransmission of a packet we already have: ACK it again, skip the data.
                # The ACK exchange can bring the next new packet, so process its frames too.
                if kind in (0x0, 0x1):
                    self._process(self._ack(seq, more=False))
                continue
            self.rx_seq = (seq + 1) & 0x0F
            self.buf += f[1:]
            pending = False
            if kind in (0x1, 0x3):
                # Finish this message before ACKing: the ACK exchange can bring the
                # next message's first packet, which must not land in this buffer.
                # The length's top bit is a flag (seen as 80 03 on the ABS).
                length = int.from_bytes(self.buf[:2], "big") & 0x7FFF
                msg = bytes(self.buf[2:2 + length])
                if msg and len(msg) == length:  # drop broken reassemblies
                    self.messages.append(msg)
                    pending = len(msg) >= 3 and msg[0] == 0x7F and msg[2] == 0x78
                self.buf = bytearray()
            if kind in (0x0, 0x1):
                # After "response pending" the real answer follows at once: like an ACK
                # inside a message, return on its first packet so that one is ACKed in
                # time. The Vgate (ELM327 v2.1) otherwise listened 380 ms after our ACK
                # and the ABS closed the channel (2026-11-04).
                self._process(self._ack(seq, more=kind == 0x0 or pending))

    def recv(self, timeout):
        deadline = time.monotonic() + timeout
        while not self.messages:
            if self.closed_by_module:
                raise Tp20Error("模組關閉了 TP2.0 通道 (可能是 ACK 太慢)")
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            # Fast mode: no reply count, so the whole answer after our A3 comes in.
            extra = {"expect": None} if self.fast_elm else {}
            self._process(self.link.listen(self.rx, min(left, 1.0), **extra))
        return self.messages.pop(0)


# --- KWP2000 -----------------------------------------------------------------

# Reading services only. 0x10 is allowed solely for the diagnostic session 0x89.
# Newer PQ35 modules (gateway, 09, doors) also answer UDS-style 0x22 / 0x19 over TP2.0.
# 0x12 readFreezeFrameData and 0x17 readStatusOfDTC are ISO 14230 reading services.
KWP_READ_SERVICES = {0x10, 0x12, 0x17, 0x18, 0x19, 0x1A, 0x21, 0x22, 0x3E}

# Adaptation *reading* on PQ35 modules runs as routine 01 03 (recorded from VCDS on the car,
# 2026-11-05): 31 B8 = start, 31 B9 <ch> = select channel, 31 BA = results, 32 B8 = stop.
# Only these exact requests are let through; any other 0x31 / 0x32 stays refused.
ADAPT_READ = (bytes.fromhex("31B80000"), bytes.fromhex("31B80103"), bytes.fromhex("31BA0103"),
              bytes.fromhex("32B80103"))
ADAPT_SELECT = bytes.fromhex("31B90103")  # + channel number (1 byte)


def adapt_read_ok(payload):
    return payload in ADAPT_READ or (len(payload) == 5 and payload[:4] == ADAPT_SELECT)


# 0x1A identification options worth trying; which one holds the long coding
# differs between modules, so `verify` searches all of them.
IDENT_OPTIONS = (0x9B, 0x9A, 0x9C, 0x91, 0x86, 0x80)

# Places a module may keep its long coding, tried in order by `verify`.
CODING_READS = ([0x22, 0x06, 0x00],) + tuple([0x1A, o] for o in IDENT_OPTIONS)

# Answers meaning "ask another way". 0x80 (not supported in this session) is what
# the 02 DSG (0AM, DQ200) gives for 22 F187 on this car.
UNSUPPORTED = (0x11, 0x12, 0x31, 0x80)


class KwpClient:
    CLEAR_DTCS = bytes([0x14, 0xFF, 0x00])  # clearDiagnosticInformation, all groups

    def __init__(self, channel):
        self.ch = channel

    def request(self, payload):
        payload = bytes(payload)
        sid = payload[0]
        if adapt_read_ok(payload):  # the adaptation-reading routine, nothing else of 0x31 / 0x32
            return self._transact(payload)
        if sid not in KWP_READ_SERVICES or (sid == 0x10 and payload[1:] != b"\x89"):
            raise ReadOnlyViolation(f"KWP 服務 {payload[:2].hex().upper()} 不是唯讀服務，已拒絕送出")
        return self._transact(payload)

    # --- used only by writer.py, which backs up, checks and verifies around it ---

    def _transact(self, payload):
        payload = bytes(payload)
        sid = payload[0]
        self.ch.messages.clear()
        self.ch.send(payload)
        deadline = time.monotonic() + self.ch.timeout
        while True:
            resp = self.ch.recv(max(0.05, deadline - time.monotonic()))
            if resp is None:
                raise NoResponse(f"模組沒有回應 (KWP 0x{sid:02X})")
            if resp[0] == 0x7F and len(resp) >= 3 and resp[1] == sid:
                if resp[2] == 0x78:
                    deadline = time.monotonic() + 5.0
                    continue
                raise NegativeResponse(sid, resp[2])
            if resp[0] == sid + 0x40:
                return resp

    def start_session(self):
        for _ in range(3):
            try:
                self.request([0x10, 0x89])
                return
            except NegativeResponse as e:
                if e.nrc != 0x21:  # 0x21 = busy, repeat request
                    raise
                time.sleep(0.5)
        raise NoResponse("模組一直回覆忙碌 (0x21)")

    def read_adaptation(self, channel, polls=30):
        """Adaptation channel value and its display fields (read only, VCDS's routine 01 03).

        Answer of 31 BA: 82, then blocks <length incl. itself><data>, FF at the end.
        First block = stored value, the others = formula/a/b like a measuring value.
        A channel the module does not have is refused with 7F 31 31."""
        from . import measure
        if not getattr(self, "_routines_listed", False):
            self.request([0x31, 0xB8, 0x00, 0x00])  # routine list: VCDS asks once per connection
            self._routines_listed = True
        self.request([0x31, 0xB8, 0x01, 0x03])
        try:
            for _ in range(polls):
                if self.request([0x31, 0xBA, 0x01, 0x03])[4:5] == bytes([0x81]):  # ready
                    break
                time.sleep(0.1)
            self.request([0x31, 0xB9, 0x01, 0x03, channel])
            for _ in range(polls):
                r = self.request([0x31, 0xBA, 0x01, 0x03])
                if r[4:5] == bytes([0x82]):  # results
                    break
                time.sleep(0.1)
            else:
                raise NoResponse(f"調整通道 {channel} 沒有回結果")
        finally:
            try:
                self.request([0x32, 0xB8, 0x01, 0x03])
            except (NegativeResponse, NoResponse):
                pass
        blocks, i, body = [], 0, r[5:]
        while i < len(body) and body[i] != 0xFF and body[i] > 0:
            blocks.append(body[i + 1:i + body[i]])
            i += body[i]
        value = int.from_bytes(blocks[0], "big") if blocks else None
        fields = [measure.decode_value(b[0], b[1], b[2]) if len(b) == 3 else {"raw": b.hex().upper()}
                  for b in blocks[1:]]
        return {"channel": channel, "value": value, "raw": r.hex().upper(), "fields": fields}

    # --- used only by writer.py (backup, checks, confirmation, read-back around it) ---

    def _write_adaptation(self, channel, value, wsc, polls=30):
        """Store an adaptation value the way VCDS did on the 09 (sniffed 2026-11-06):
        routine 01 03 on the channel, 31 B9 01 03 <value 2 bytes> (test value),
        31 BB 01 03 <value 2 bytes> <workshop code 6 bytes> (save), 32 B8 01 03 (stop).
        Only the refusal was seen on the car (7F 31 31 for an out-of-range value), so the
        caller judges success by reading the channel back."""
        if channel == 0:
            raise ValueError("通道 00 是清除學習值，不寫")
        val = value.to_bytes(2, "big")
        self.request([0x31, 0xB8, 0x01, 0x03])
        try:
            for _ in range(polls):
                if self.request([0x31, 0xBA, 0x01, 0x03])[4:5] == bytes([0x81]):
                    break
                time.sleep(0.1)
            self.request([0x31, 0xB9, 0x01, 0x03, channel])
            for _ in range(polls):
                if self.request([0x31, 0xBA, 0x01, 0x03])[4:5] == bytes([0x82]):
                    break
                time.sleep(0.1)
            self._transact(bytes([0x31, 0xB9, 0x01, 0x03]) + val)        # test value
            self.request([0x31, 0xBA, 0x01, 0x03])
            self._transact(bytes([0x31, 0xBB, 0x01, 0x03]) + val + wsc)  # save, sent once
        finally:
            try:
                self.request([0x32, 0xB8, 0x01, 0x03])
            except (NegativeResponse, NoResponse):
                pass

    def read_ident(self, option):
        return self.request([0x1A, option])[2:]

    def read_did(self, did):
        """UDS-style 22 xxxx, answered by the newer PQ35 modules (09, 19, doors)."""
        return self.request([0x22, did >> 8, did & 0xFF])[3:]

    # Where the last workshop code can be read back. The 09 refuses 22 F198 (0x31) but
    # answers 22 F1A5 with exactly what VCDS wrote to F198 (car, 2026-11-06).
    WSC_READS = (0xF198, 0xF1A5)

    def workshop_code(self):
        for did in self.WSC_READS:
            try:
                wsc = self.read_did(did)
            except NegativeResponse as e:
                if e.nrc not in UNSUPPORTED + (0x22,):
                    raise
                continue
            if wsc:
                return wsc
        return None

    def enter_write_session(self):
        """Same order as VCDS (recorded on the car 2026-11-05 for 09 coding):
        10 89, then the workshop code 2E F198 written back unchanged, then the caller's 2E.
        Without the F198 write the 09 refuses the coding with 7F 2E 22 (car, 2026-11-06).
        Modules with no workshop code to read just stay in session 0x89."""
        self.start_session()
        wsc = self.workshop_code()
        if wsc:
            self._transact(bytes([0x2E, 0xF1, 0x98]) + wsc)

    def exit_write_session(self):
        pass  # closing the TP2.0 channel ends the session

    def read_part(self):
        """Part number: newer modules answer 22 F187, older ones 1A 9B."""
        try:
            return ascii_part(self.request([0x22, 0xF1, 0x87])[3:])
        except NegativeResponse as e:
            if e.nrc not in UNSUPPORTED:
                raise
        return ascii_part(self.read_ident(0x9B))

    def read_raw(self, req):
        """Answer to a read request, without the echoed service/identifier bytes."""
        return self.request(req)[len(req):]

    def read_dtcs(self):
        """VAG 5-digit codes via KWP 18 02 (or 18 00); falls back to UDS-style 19 02."""
        for status_group in (0x02, 0x00):
            try:
                body = self.request([0x18, status_group, 0xFF, 0x00])[2:]
                return [{"code": body[i] << 8 | body[i + 1], "status": body[i + 2]}
                        for i in range(0, len(body) - 2, 3)
                        if body[i] << 8 | body[i + 1] != 0xFFFF]
            except NegativeResponse as e:
                if e.nrc not in UNSUPPORTED:
                    raise
        body = self.request([0x19, 0x02, 0x0D])[3:]  # stored faults, like uds.FAULT_STATUS_MASK
        return [{"code": int.from_bytes(body[i:i + 3], "big"), "status": body[i + 3], "uds": True}
                for i in range(0, len(body) - 3, 4)]

    def read_freeze_frame(self, code):
        """Freeze frame of a 5-digit fault code: KWP 12 00 04 <code> (as VCDS asks it)."""
        r = self.request([0x12, 0x00, 0x04, code >> 8, code & 0xFF])
        return parse_freeze_frame(r[1:])


def parse_freeze_frame(body):
    """KWP 12 00 04 <dtc> answer without its 52, checked against VCDS on the car (62, 02115,
    2026-11-05): 00 ?? ?? status priority frequency reset_counter mileage(3) time_ind ?? then
    32 bits year-2000(6) month(4) day(5) hour(5) minute(6) second(6), then the DTC echoed.
    VCDS showed the seconds one higher (08 for 07 here); one sample only."""
    if len(body) < 16:
        return {"raw": body.hex().upper()}
    bits = int.from_bytes(body[12:16], "big")
    y, mo, d = bits >> 26 & 0x3F, bits >> 22 & 0x0F, bits >> 17 & 0x1F
    h, mi, s = bits >> 12 & 0x1F, bits >> 6 & 0x3F, bits & 0x3F
    return {"status": body[3], "priority": body[4], "frequency": body[5], "reset_counter": body[6],
            "mileage_km": int.from_bytes(body[7:10], "big"), "time_indication": body[10],
            "date": f"{2000 + y:04d}.{mo:02d}.{d:02d}", "time": f"{h:02d}:{mi:02d}:{s:02d}",
            "date_valid": 1 <= mo <= 12 and 1 <= d <= 31, "raw": body.hex().upper()}


def ascii_part(data):
    """Part number at the start of a 0x1A 0x9B / 0x22 F187 answer, spaces removed."""
    text = data[:12].decode("ascii", errors="replace")
    return "".join(ch for ch in text if ch.isalnum())
