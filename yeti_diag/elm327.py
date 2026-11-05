"""ELM327 adapters (USB, or Bluetooth SPP which Windows shows as a COM port).

The ELM's own ISO-TP handling cannot send multi-frame requests, so it runs
with CAN auto-formatting off (ATCAF0) and this module does ISO-TP itself.
"""
import time

PAD = 0x55


def segment(payload):
    """Split a payload into 8-byte ISO-TP frames."""
    if len(payload) <= 7:
        frames = [bytes([len(payload)]) + payload]
    else:
        n = len(payload)
        frames = [bytes([0x10 | (n >> 8), n & 0xFF]) + payload[:6]]
        rest, seq = payload[6:], 1
        while rest:
            frames.append(bytes([0x20 | (seq & 0xF)]) + rest[:7])
            rest, seq = rest[7:], seq + 1
    return [f.ljust(8, bytes([PAD])) for f in frames]


def probe_port(port, wait=2.5, keep=False):
    """True when an ELM327 answers ATZ on `port`. A paired Bluetooth adapter that is not
    in range makes the open itself fail after a few seconds (OSError).
    keep=True returns the still-open port instead of True: closing and reopening a
    Bluetooth port right away failed with "device not connected" (COM9, 2026-11-04)."""
    import serial
    try:
        ser = serial.Serial(port, baudrate=38400, timeout=0.1, write_timeout=2)
    except (OSError, ValueError):
        return False
    ok = False
    try:
        ser.write(b"ATZ\r")
        buf, deadline = b"", time.monotonic() + wait
        while time.monotonic() < deadline and b">" not in buf:
            buf += ser.read(256)
        ok = b"ELM327" in buf.upper()
        if ok and keep:
            ser.timeout = 0.05
            return ser
        return ok
    except OSError:
        ok = False
        return False
    finally:
        if not (ok and keep):
            ser.close()


def find_port(cache=None, say=print, keep_open=False):
    """COM port of the first answering ELM327 (for --elm auto). The port that worked last
    time (saved in `cache`) is tried first, then Bluetooth serial ports, then the rest.
    keep_open=True returns (port, open serial object) so the caller does not reopen it."""
    from serial.tools import list_ports
    # Skip Windows' incoming Bluetooth ports (device address all zeros): they are not
    # adapters, and opening one waited forever (COM11, 2026-11-04).
    ports = [p for p in list_ports.comports()
             if "Intel" not in (p.description or "") and "000000000000" not in (p.hwid or "")]
    last = cache.read_text(encoding="utf-8").strip() if cache and cache.exists() else None
    ports.sort(key=lambda p: (p.device != last, "BTHENUM" not in (p.hwid or "").upper(), p.device))
    for p in ports:
        say(f"找接頭: 試 {p.device} ({p.description}) ...")
        ser = probe_port(p.device, keep=True) if keep_open else probe_port(p.device)
        if ser:
            if cache:
                cache.parent.mkdir(exist_ok=True)
                cache.write_text(p.device, encoding="utf-8")
            say(f"找到 ELM327: {p.device}")
            return (p.device, ser) if keep_open else p.device
    raise ElmError("找不到 ELM327 接頭：沒有任何 COM 埠回應 ATZ。接頭有插在車上、藍牙有配對嗎？")


class ElmError(Exception):
    pass


class Elm327:
    """One ELM327 on a serial port, shared by all module channels."""

    def __init__(self, port=None, baudrate=38400, serial_obj=None, timeout_ms=200, open_retries=4):
        if serial_obj is None:
            import time as _t

            import serial
            last = None
            for attempt in range(open_retries):  # Bluetooth COM often "signal timeout"s; retry
                try:
                    serial_obj = serial.Serial(port, baudrate=baudrate, timeout=0.05)
                    break
                except serial.SerialException as e:
                    last = e
                    _t.sleep(1.5)
            if serial_obj is None:
                raise last
        self.ser = serial_obj
        self.header = self.filter = None
        self.version = self.command("ATZ", wait=1.5)
        self.protocol = None
        self.short_st = max(1, min(255, timeout_ms // 4))
        self.st = self.short_st
        # ATPB C001 defines user CAN protocol B: 11-bit, variable data length, 500 kbit/s.
        for cmd in ("ATE0", "ATL0", "ATS0", "ATH1", "ATPBC001", "ATCAF0", "ATAL",
                    f"ATST{max(1, min(255, timeout_ms // 4)):02X}"):
            self.command(cmd)
        self.use_protocol("uds")

    def set_long_wait(self, long):
        """ATST: ~1 s of silence when waiting for slow module data, else the short default.
        With a reply count (expect) the ELM still returns as soon as the frames arrive."""
        self.set_st(0xFF if long else self.short_st)

    def set_st(self, st):
        """ATST in 4 ms steps: how long the ELM waits after the last frame before returning."""
        if self.st != st:
            self.command(f"ATST{st:02X}")
            self.st = st

    def use_protocol(self, kind):
        """'uds' -> ATSP6 (ISO 15765, what the engine answers on this car);
        'tp20' -> ATSPB, because TP2.0 modules ignore frames padded to 8 bytes."""
        if self.protocol != kind:
            self.command("ATSP6" if kind == "uds" else "ATSPB")
            self.command("ATCAF0")
            self.header = self.filter = None
            self.protocol = kind

    def close(self):
        self.ser.close()

    def _read_until_prompt(self, wait):
        buf = b""
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                buf += chunk
                if b">" in buf:
                    break
        text = buf.decode("ascii", errors="replace").replace(">", "")
        return [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]

    def _drain(self):
        """Drop leftovers (e.g. repeated TP2.0 setup replies) before the next command."""
        if hasattr(self.ser, "reset_input_buffer"):
            self.ser.reset_input_buffer()

    def command(self, cmd, wait=1.0):
        # Clones sometimes answer '?' while still busy with late frames; retry once.
        for attempt in (1, 2):
            self._drain()
            self.ser.write((cmd + "\r").encode("ascii"))
            lines = self._read_until_prompt(wait)
            if not (lines and lines[-1] == "?"):
                return " ".join(lines)
            time.sleep(0.3)
        raise ElmError(f"ELM 不接受指令 {cmd}")

    def battery_voltage(self):
        text = self.command("ATRV").upper().replace("V", "")
        return float(text)

    def select(self, tx_id, rx_id):
        if self.header != tx_id:
            self.command(f"ATSH{tx_id:03X}")
            self.header = tx_id
        if self.filter != rx_id:
            self.command(f"ATCRA{rx_id:03X}")
            self.filter = rx_id

    def send_frame(self, frame, wait=1.5, expect=None, no_reply=False):
        """Send one frame; return frames received until the ELM times out.

        `expect` (1-15) makes the ELM return after that many replies; needed
        for the TP2.0 setup, which the module keeps repeating.
        `no_reply` turns ELM responses off (ATR0) so back-to-back packets of a
        long TP2.0 message go out without the ELM's listening pause in between.
        """
        self._set_responses(not no_reply)
        # The clones answer '?' to an 8-byte frame followed by a reply count (v1.5 on the car
        # 2026-11-03: every UDS read failed; Vgate v2.1 the same, 2026-11-05), so the count
        # only goes on shorter frames.
        if len(frame) >= 8:
            expect = None
        text = frame.hex().upper() + (f"{expect:X}" if expect and not no_reply else "")
        lines = self._raw(text, 0.3 if no_reply else wait)
        return [] if no_reply else self._checked_parse(lines)

    def _checked_parse(self, lines):
        try:
            return self._parse(lines)
        except ElmError:
            self.recover()
            raise

    def recover(self):
        """After CAN ERROR / BUFFER FULL: close the protocol and forget the cached
        settings, so the next request sets protocol, header and filter again. On the
        car a failed 01 read left the clone answering CAN ERROR to the next requests."""
        try:
            self.command("ATPC")
        except ElmError:
            pass
        self.protocol = self.header = self.filter = None
        self.responses_on = None  # unknown: _set_responses sends ATR0/ATR1 again

    def _set_responses(self, on):
        if getattr(self, "responses_on", True) != on:
            self.command("ATR1" if on else "ATR0")
            self.responses_on = on

    def _raw(self, text, wait):
        self._drain()
        self.ser.write((text + "\r").encode("ascii"))
        return self._read_until_prompt(wait)

    def listen(self, seconds, until_id=None):
        """Monitor the bus (ATMA) without sending, e.g. after 'response pending'.

        Lines are read while they arrive (reading only at the end overflowed the
        clone: BUFFER FULL), and monitoring stops at the first complete frame from
        `until_id`, so a first frame can still get its flow control in time.
        """
        self._drain()
        self.ser.write(b"ATMA\r")
        buf, deadline = b"", time.monotonic() + seconds
        tag = f"{until_id:03X}".encode() if until_id is not None else None
        while time.monotonic() < deadline:
            buf += self.ser.read(256)
            if tag and any(line.strip().replace(b" ", b"").startswith(tag) for line in buf.split(b"\r")[:-1]):
                break
        # Any character stops ATMA, but a lone CR repeats the last command: if the ELM
        # had already stopped itself (BUFFER FULL), CR restarted monitoring on the car.
        # A space is ignored by the ELM when it is not monitoring.
        self.ser.write(b" ")
        # Read until the ELM goes quiet, not just to the first prompt: after an overflow
        # it prints BUFFER FULL > and then STOPPED > for our stop character, and a late
        # STOPPED would otherwise land in the next command's answer.
        end, quiet_since = time.monotonic() + 1.5, time.monotonic()
        while time.monotonic() < end and not (b">" in buf and time.monotonic() - quiet_since > 0.15):
            chunk = self.ser.read(256)
            if chunk:
                buf, quiet_since = buf + chunk, time.monotonic()
        text = buf.decode("ascii", errors="replace").replace(">", "")
        lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
        lines = [line for line in lines if line != "ATMA"]
        if tag and any(line.replace(" ", "").encode().startswith(tag) for line in lines):
            # The engine repeats its late answer until the clone overflows (seen on the
            # car: 15 copies of 59 02 FF, then BUFFER FULL). The answer is in; keep it.
            lines = [line for line in lines if line.replace(" ", "") != "BUFFERFULL"]
        return self._checked_parse(lines)

    @staticmethod
    def _parse(lines):
        frames = []
        for line in lines:
            line = line.replace(" ", "")
            if line in ("NODATA", "STOPPED", "OK", "") or not all(c in "0123456789ABCDEF" for c in line):
                if line in ("CANERROR", "BUFFERFULL", "BUSERROR"):
                    raise ElmError(f"ELM 回報 {line}")
                continue
            # A line cut short by the clone (odd hex length, seen on the car) is noise.
            if len(line) >= 5 and len(line) % 2 == 1:
                frames.append((int(line[:3], 16), bytes.fromhex(line[3:])))
        return frames


class ElmChannel:
    """ISO-TP channel to one module through a shared Elm327.

    Long answers are pulled in blocks of BLOCK_SIZE frames: with block size 0 the
    module sends everything at once and the clone overflows (BUFFERFULL on 01 and
    15 fault-code reads, 2026-11-03).
    """

    BLOCK_SIZE = 8

    def __init__(self, elm, tx_id, rx_id):
        self.elm, self.tx_id, self.rx_id = elm, tx_id, rx_id
        self.queue = []
        self._partial = None  # (expected_len, bytearray) while receiving multi-frame
        self._cf_seq = 0      # sequence number the next consecutive frame must carry
        self._block_left = 0  # consecutive frames still due before the next flow control

    def start(self):
        pass

    def stop(self):
        pass

    def clear_rx_queue(self):
        self.queue.clear()
        self._partial = None

    def send(self, payload):
        self.elm.use_protocol("uds")
        self.elm.select(self.tx_id, self.rx_id)
        frames = segment(bytes(payload))
        received = self.elm.send_frame(frames[0])
        if len(frames) > 1:
            fc = next((d for i, d in received if i == self.rx_id and d[0] >> 4 == 3), None)
            if fc is None or fc[0] & 0x0F != 0:
                raise ElmError("模組沒有回應流量控制 (Flow Control)")
            for cf in frames[1:]:
                received = self.elm.send_frame(cf, wait=0.3 if cf is not frames[-1] else 1.5)
        self._feed(received)

    FC = bytes([0x30, BLOCK_SIZE, 0x00]).ljust(8, bytes([PAD]))

    def _flow_control(self):
        """Ask for the next block; the ELM returns once the module goes quiet (ATST)."""
        total, buf = self._partial
        remaining = -(-(total - len(buf)) // 7)
        self._block_left = min(self.BLOCK_SIZE, remaining)
        self._feed(self.elm.send_frame(self.FC))

    def _feed(self, frames):
        for can_id, data in frames:
            if can_id != self.rx_id:
                continue
            pci = data[0] >> 4
            if pci == 0:
                self.queue.append(bytes(data[1:1 + (data[0] & 0x0F)]))
            elif pci == 1:
                total = (data[0] & 0x0F) << 8 | data[1]
                self._partial = (total, bytearray(data[2:8]))
                self._cf_seq, self._block_left = 1, 0
            elif pci == 2 and self._partial:
                if data[0] & 0x0F != self._cf_seq:
                    # A frame the clone missed would shift every later byte (wrong
                    # fault codes, no error). Drop the message; the read is retried.
                    self._partial = None
                    raise ElmError(f"ISO-TP 封包序號跳號 (要 {self._cf_seq:X}，收到 {data[0] & 0x0F:X})")
                self._cf_seq = (self._cf_seq + 1) & 0x0F
                total, buf = self._partial
                buf += data[1:8]
                self._block_left -= 1
                if len(buf) >= total:
                    self.queue.append(bytes(buf[:total]))
                    self._partial = None
        # The module waits for flow control after the first frame and after each block.
        if self._partial and self._block_left <= 0:
            self._flow_control()

    def recv(self, block=True, timeout=None):
        if not self.queue and block:
            # Waiting after "response pending" (7F xx 78, the engine on 19 02). Stay
            # silent: a frame sent to the module meanwhile (we tried flow control as a
            # poll) made the engine drop its answer. Monitor until the first frame.
            self._feed(self.elm.listen(min(timeout or 1.0, 5.0), until_id=self.rx_id))
        return self.queue.pop(0) if self.queue else None
