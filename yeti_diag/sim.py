"""Fake ECUs for testing without the car.

Two ways in: a python-can virtual bus (SimCar) or a fake ELM327 serial port
(FakeElmSerial). Data mirrors what VCDS read on 2026-11-03.
"""
import threading

import can
import isotp

from .elm327 import segment
from .vehicle import TP20_PART_NUMBERS, UDS_MODULES, VCDS_TRUTH, VIN

SIM_CHANNEL = "yeti-sim"

# 08 HVAC: an example measuring DID for the refrigerant pressure (10.0 bar).
# The real DID on the car is not known yet; find it with `scan-dids`.
SIM_REFRIGERANT_DID = 0x2600
# 17 instruments: an example writable DID (oil service distance, km / 100).
SIM_OIL_SERVICE_DID = 0x2700


def _ascii(text):
    return text.encode("ascii")


def default_ecus():
    ecus = {}
    for addr, mod in UDS_MODULES.items():
        ecus[addr] = {
            "dids": {
                0xF187: _ascii(mod.part_no.replace(" ", "")),
                0xF189: _ascii("0001"),
                0xF190: _ascii(VIN),
            },
            "writable": set(),
            "dtcs": [],
        }
    ecus["08"]["dids"][SIM_REFRIGERANT_DID] = bytes([0x00, 100])  # 0.1 bar units
    ecus["17"]["dids"][SIM_OIL_SERVICE_DID] = bytes([0x00, 150])  # 15000 km
    ecus["17"]["writable"].add(SIM_OIL_SERVICE_DID)
    ecus["17"]["dids"][0x2203] = bytes.fromhex("77B7")  # odometer / 10 km, as read on the car
    ecus["17"]["dids"][0x0600] = bytes.fromhex("520800")  # short coding; staging (byte1 bit0) off
    ecus["17"]["writable"].add(0x0600)
    # Engine values from the Car Scanner Bluetooth log, 2026-11-04 (ignition on).
    for did, raw in {0x155A: "0D05", 0x2006: "0576", 0x2029: "337F", 0x202A: "2F6B", 0x20A1: "42",
                     0x2206: "06", 0x15AA: "00"}.items():
        ecus["01"]["dids"][did] = bytes.fromhex(raw)
    # Mode $06 misfire monitors (SAE J1979 layout; made-up counts, not from this car):
    # MIDs A1-A5 supported, cylinder 3 has misfires.
    def _rec(mid, tid, val):
        return bytes([mid, tid, 0x24]) + val.to_bytes(2, "big") + bytes(2) + (0xFFFF).to_bytes(2, "big")
    ecus["01"]["mode06"] = {
        # Support masks as the car's engine answers them (2026-11-06): 00 / 20 / 40 / 60 / 80 chain
        # on, A0 = 78 00 00 00 (A2-A5, no A1).
        0x00: bytes.fromhex("00" "C0000001"), 0x20: bytes.fromhex("20" "80000009"),
        0x40: bytes.fromhex("40" "C0000001"), 0x60: bytes.fromhex("60" "00000001"),
        0x80: bytes.fromhex("80" "00000001"),
        0xA0: bytes.fromhex("A0" "78000000"),
        # cylinder 3 = MID A4 has the misfires
        **{mid: _rec(mid, 0x0B, n) + _rec(mid, 0x0C, m)
           for mid, n, m in ((0xA2, 0, 0), (0xA3, 0, 0), (0xA4, 12, 30), (0xA5, 0, 0))},
    }
    # Engine also answers generic OBD-II.
    ecus["01"]["obd"] = {0x05: bytes([90 + 40]), 0x0C: bytes([0x12, 0xC0]),
                         0x0D: bytes([0]), 0x46: bytes([31 + 40]),
                         0x0F: bytes([35 + 40]), 0x42: bytes([0x36, 0x6C]),
                         # PID 01: MIL off, 0 DTCs; continuous all supported and complete;
                         # catalyst / evap / O2 / O2 heater supported, evap not complete yet
                         0x01: bytes([0x00, 0x07, 0x65, 0x04])}
    return ecus


class EcuModel:
    """UDS behaviour of one module. Options in `data`:
    sticky_dtcs  - 0x14 answers OK but keeps the DTCs (seen on the rear doors)
    ignore_writes - 0x2E answers OK but does not store the value
    """

    def __init__(self, data):
        self.data = data
        self.session = 0x01

    def respond(self, req):
        sid = req[0]
        d = self.data
        if sid == 0x10 and len(req) == 2 and req[1] in (0x01, 0x03):
            self.session = req[1]
            return bytes([0x50, req[1], 0x00, 0x32, 0x01, 0xF4])
        if sid == 0x22 and len(req) == 3:
            did = req[1] << 8 | req[2]
            if did in d["dids"]:
                return bytes([0x62, req[1], req[2]]) + d["dids"][did]
            return bytes([0x7F, sid, 0x31])
        if sid == 0x2E and len(req) > 3:
            did = req[1] << 8 | req[2]
            if self.session != 0x03:
                return bytes([0x7F, sid, 0x7F])
            if did not in d.get("writable", ()):
                return bytes([0x7F, sid, 0x31])
            if not d.get("ignore_writes"):
                d["dids"][did] = bytes(req[3:])
            return bytes([0x6E, req[1], req[2]])
        if sid == 0x19 and len(req) == 3 and req[1] == 0x02:
            # Like the real modules: only codes whose status matches the mask.
            body = b"".join(dtc.to_bytes(3, "big") + bytes([s]) for dtc, s in d["dtcs"] if s & req[2])
            return bytes([0x59, 0x02, 0xFF]) + body
        if sid == 0x19 and len(req) == 6 and req[1] in (0x04, 0x06):
            dtc = int.from_bytes(req[2:5], "big")
            status = next((st for code, st in d["dtcs"] if code == dtc), None)
            if status is None:
                return bytes([0x7F, sid, 0x31])
            record = d.get("snapshots" if req[1] == 0x04 else "extended", {}).get(dtc, b"")
            return bytes([0x59, req[1]]) + req[2:5] + bytes([status]) + record
        if sid == 0x14 and len(req) == 4:
            if not d.get("sticky_dtcs"):
                d["dtcs"] = []
            return bytes([0x54])
        if sid == 0x01 and "obd" in d and len(req) == 2 and req[1] in d["obd"]:
            return bytes([0x41, req[1]]) + d["obd"][req[1]]
        if sid == 0x03 and "obd" in d:
            return bytes([0x43, 0x00])
        if sid == 0x06 and len(req) == 2 and "mode06" in d:
            if req[1] in d["mode06"]:
                return bytes([0x46]) + d["mode06"][req[1]]
            return bytes([0x7F, sid, 0x31])
        return bytes([0x7F, sid, 0x11])


# TP2.0 logical addresses used by the simulator. The real ones are unknown;
# `tp20-probe` discovers them on the car by part number.
SIM_TP20_DEST = {"02": 0x02, "03": 0x03, "09": 0x20, "19": 0x1F, "42": 0x60,
                 "44": 0x44, "52": 0x61, "62": 0x62, "72": 0x63}


def default_tp20_ecus():
    ecus = {}
    for i, (addr, part) in enumerate(TP20_PART_NUMBERS.items()):
        ecus[addr] = {
            "dest": SIM_TP20_DEST[addr],
            "ecu_rx": 0x760 + i,  # clear of the UDS IDs (0x746 is the HVAC)
            "ident": {0x9B: f"{part:<12}".encode("ascii") + b"SIM COMPONENT"},
            "dtcs": [],
        }
    ecus["09"]["ident"][0x9A] = VCDS_TRUTH["09_coding"]
    ecus["09"]["ident"]["coding"] = VCDS_TRUTH["09_coding"]
    ecus["09"]["writable_coding"] = True
    ecus["09"]["ident"]["wsc"] = bytes.fromhex("000212345678")  # F198 as VCDS wrote it on the car
    # Adaptation answers of the car's 09 (after 82, before FF), sniffed 2026-11-05:
    ecus["09"]["adaptation"] = {
        3: bytes.fromhex("030001042501CE042503B1043600010425006B"),   # single door unlock = 1
        25: bytes.fromhex("03000F0425026F04250270043602580425006B"),    # rear window heater 15 (600 s)
        31: bytes.fromhex("030005042503B40425019C043600050425006B"),    # comfort blink 5 times
    }
    ecus["09"]["adaptation_range"] = {
        31: (2, 5),  # simulator's choice; the car refused 0 for channel 31 (2026-11-06)
    }
    ecus["72"]["ident"]["coding"] = VCDS_TRUTH["72_coding"]
    ecus["62"]["ident"]["coding"] = bytes.fromhex("010001200000521003")  # differs from 72
    ecus["62"]["dtcs"] = [(VCDS_TRUTH["door_dtc"], 0x28)]
    # Freeze frame of 02115 on the car's 62 (12 00 04 08 43), VCDS: 300521 km, 105 times, 2026-11-05
    ecus["62"]["freeze_frames"] = {0x0843: bytes.fromhex("006C0128036943 0495E9 0001FFBEB987 040843".replace(" ", ""))}
    ecus["72"]["dtcs"] = [(VCDS_TRUTH["door_dtc"], 0x28)]
    # Made-up measuring group for tests: 12.60 V (formula 06), 306430 km (formula 24),
    # an unknown formula (FE) and switch bits (10).
    ecus["19"]["groups"] = {1: bytes([0x06, 0x64, 0x7E, 0x24, 0x77, 0xB3, 0xFE, 0x01, 0x02, 0x10, 0xFF, 0x05])}
    # Real DSG answers from the Car Scanner Bluetooth logs, 2026-11-03 / 11-04.
    ecus["02"]["groups"] = {11: bytes.fromhex("9F04809F0480250000250000"),
                            98: bytes.fromhex("1F14820D027F2500009F7A8C"),
                            99: bytes.fromhex("5919115911472C54312C001F"),
                            109: bytes.fromhex("24785B7728DF77280A772812115220"),
                            118: bytes.fromhex("1F14820D027F2500009F9E90"),
                            119: bytes.fromhex("5918735911E22C58212C0020"),
                            122: bytes.fromhex("37C8CE37C88237C85A250000")}
    return ecus


class Tp20EcuModel:
    """Frame-level TP2.0 + KWP2000 behaviour of one module."""

    def __init__(self, data):
        self.data = data
        self.dest, self.ecu_rx = data["dest"], data["ecu_rx"]
        self.tester_rx = None
        self.buf = bytearray()
        self.seq = 0
        self.block_size = 0x0F
        self.outbox = []  # packets waiting for the tester's ACK (block size)

    def on_frame(self, can_id, f):
        if can_id == 0x200 and len(f) >= 7 and f[0] == self.dest and f[1] == 0xC0:
            self.tester_rx = f[4] | (f[5] & 0x0F) << 8
            self.seq = 0
            self.outbox = []
            return [(0x200 + self.dest,
                     bytes([0x00, 0xD0, f[4], f[5], self.ecu_rx & 0xFF, self.ecu_rx >> 8, 0x01]))]
        if can_id != self.ecu_rx or self.tester_rx is None:
            return []
        op = f[0]
        if op == 0xA0:
            self.block_size = f[1] or 0x0F
            return [(self.tester_rx, bytes([0xA1, 0x0F, 0x8A, 0xFF, 0x4A, 0xFF]))]
        if op == 0xA3:
            return [(self.tester_rx, bytes([0xA1, 0x0F, 0x8A, 0xFF, 0x4A, 0xFF]))]
        if op == 0xA8:
            self.tester_rx = None
            return []
        if op >> 4 == 0xB:
            return self._next_block()  # ACK from the tester releases the next block
        if op >> 4 > 3:
            return []
        self.buf += f[1:]
        out = []
        if op >> 4 in (0, 1):
            out.append((self.tester_rx, bytes([0xB0 | ((op + 1) & 0x0F)])))
        if op >> 4 in (1, 3):
            length = int.from_bytes(self.buf[:2], "big")
            req = bytes(self.buf[2:2 + length])
            self.buf = bytearray()
            self.outbox = self._packets(self._kwp(req))
            out += self._next_block()
        return out

    def _next_block(self):
        """Send packets up to and including the next one that waits for an ACK."""
        out = []
        while self.outbox:
            p = self.outbox.pop(0)
            out.append((self.tester_rx, p))
            if p[0] >> 4 in (0, 1):
                break
        return out

    def _packets(self, resp):
        msg = len(resp).to_bytes(2, "big") + resp
        chunks = [msg[i:i + 7] for i in range(0, len(msg), 7)]
        frames = []
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                kind = 0x10                       # last, ACK wanted
            elif (i + 1) % self.block_size == 0:
                kind = 0x00                       # block end, ACK wanted
            else:
                kind = 0x20                       # more follows, no ACK
            frames.append(bytes([kind | self.seq]) + chunk)
            self.seq = (self.seq + 1) & 0x0F
        return frames

    def _kwp(self, req):
        sid = req[0]
        ident = self.data["ident"]
        if req in (b"\x10\x89", b"\x10\x03"):
            self.session = req[1]
            return bytes([0x50, req[1]])
        # Adaptation reading, routine 01 03, answers as recorded from the 09 on 2026-11-05.
        adapt = self.data.get("adaptation", {})
        if req == bytes.fromhex("31B80000") and adapt:
            return bytes.fromhex("71B80102010301060107010801140FFF")  # routine list of the car's 09
        if req == bytes.fromhex("31B80103"):
            self.adapt_ch = None
            return bytes.fromhex("71B80103")
        if req[:4] == bytes.fromhex("31B90103") and len(req) == 5:
            if req[4] not in adapt:
                return bytes([0x7F, 0x31, 0x31])
            self.adapt_ch = req[4]
            return bytes.fromhex("71B90103")
        # Adaptation writing as VCDS sent it (2026-11-06): 31 B9 01 03 <value 2> = test value,
        # 31 BB 01 03 <value 2> <workshop code 6> = save. Out of range -> 7F 31 31 like the car.
        # Allowed ranges here are the simulator's own (the car's are unknown, 0 was refused on 31).
        limits = self.data.get("adaptation_range", {})
        ch = getattr(self, "adapt_ch", None)
        if req[:4] == bytes.fromhex("31B90103") and len(req) == 6:
            lo, hi = limits.get(ch, (0, 0xFFFF))
            if ch is None or not lo <= int.from_bytes(req[4:6], "big") <= hi:
                return bytes([0x7F, 0x31, 0x31])
            return bytes.fromhex("71B90103")
        if req[:4] == bytes.fromhex("31BB0103") and len(req) == 12:
            value = int.from_bytes(req[4:6], "big")
            lo, hi = limits.get(ch, (0, 0xFFFF))
            if ch is None or not lo <= value <= hi or req[6:12] != ident.get("wsc"):
                return bytes([0x7F, 0x31, 0x31])
            blk = bytearray(adapt[ch])
            blk[1:3] = value.to_bytes(2, "big")  # first block <03> <value 2 bytes>
            adapt[ch] = bytes(blk)
            return bytes.fromhex("71BB0103")
        if req == bytes.fromhex("31BA0103"):
            ch = getattr(self, "adapt_ch", None)
            if ch is None:
                return bytes.fromhex("71BA010381")
            return bytes.fromhex("71BA010382") + adapt[ch] + b"\xFF"
        if req == bytes.fromhex("32B80103"):
            self.adapt_ch = None
            return bytes.fromhex("72B8010362")
        if req == b"\x22\xF1\x87":
            return b"\x62\xF1\x87" + ident[0x9B][:12].rstrip()
        if req == b"\x22\x06\x00" and "coding" in ident:
            return b"\x62\x06\x00" + ident["coding"]
        # Workshop code: VCDS writes it back before the coding (car, 2026-11-05). Like the
        # car's 09: 22 F198 is refused (31), the code reads back as 22 F1A5 (2026-11-06).
        if req == b"\x22\xF1\x98" and "wsc" in ident:
            return bytes([0x7F, 0x22, 0x31])
        if req == b"\x22\xF1\xA5" and "wsc" in ident:
            return b"\x62\xF1\xA5" + ident["wsc"]
        if req[:3] == b"\x2E\xF1\x98" and "wsc" in ident:
            ident["wsc"] = bytes(req[3:])
            self.wsc_written = True
            return b"\x6E\xF1\x98"
        if req[:3] == b"\x2E\x06\x00":
            if not self.data.get("writable_coding"):
                return bytes([0x7F, sid, 0x31])
            # The real 09 takes the coding in VW session 0x89 (VCDS never asks for 0x03).
            if getattr(self, "session", None) != 0x89:
                return bytes([0x7F, sid, 0x7F])
            if "wsc" in ident and not getattr(self, "wsc_written", False):
                return bytes([0x7F, sid, 0x22])  # what the car answered without F198 (2026-11-06)
            if not self.data.get("ignore_writes"):
                ident["coding"] = bytes(req[3:])
            return b"\x6E\x06\x00"
        if sid == 0x1A and len(req) == 2:
            if req[1] in self.data["ident"]:
                return bytes([0x5A, req[1]]) + self.data["ident"][req[1]]
            return bytes([0x7F, sid, 0x12])
        if sid == 0x21 and len(req) == 2:
            groups = self.data.get("groups", {})
            if req[1] in groups:
                return bytes([0x61, req[1]]) + groups[req[1]]
            return bytes([0x7F, sid, 0x31])
        if req[:3] == bytes.fromhex("120004") and len(req) == 5:
            frames = self.data.get("freeze_frames", {})
            code = req[3] << 8 | req[4]
            if code not in frames:
                return bytes([0x7F, 0x12, 0x31])
            return b"\x52" + frames[code]
        if req == b"\x18\x02\xFF\x00":
            body = b"".join(code.to_bytes(2, "big") + bytes([st]) for code, st in self.data["dtcs"])
            return bytes([0x58, len(self.data["dtcs"])]) + body
        if req == b"\x14\xFF\x00":
            if not self.data.get("sticky_dtcs"):
                self.data["dtcs"] = []
            return b"\x54\xFF\x00"
        if sid == 0x3E:
            return b"\x7E"
        return bytes([0x7F, sid, 0x11])


class Tp20Bus(threading.Thread):
    """Runs all TP2.0 models on the virtual bus."""

    def __init__(self, ecus):
        super().__init__(daemon=True)
        self.models = [Tp20EcuModel(d) for d in ecus.values()]
        self.bus = can.Bus(interface="virtual", channel=SIM_CHANNEL)
        self.running = True

    def run(self):
        while self.running:
            msg = self.bus.recv(timeout=0.1)
            if msg is None:
                continue
            for m in self.models:
                for can_id, data in m.on_frame(msg.arbitration_id, bytes(msg.data)):
                    self.bus.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=False))
        self.bus.shutdown()


class FakeEcu(threading.Thread):
    def __init__(self, module, data):
        super().__init__(daemon=True)
        self.model = EcuModel(data)
        self.bus = can.Bus(interface="virtual", channel=SIM_CHANNEL)
        addr = isotp.Address(isotp.AddressingMode.Normal_11bits, txid=module.rx_id, rxid=module.tx_id)
        self.stack = isotp.CanStack(self.bus, address=addr, params={"tx_padding": 0x55})
        self.running = True

    def run(self):
        self.stack.start()
        while self.running:
            req = self.stack.recv(block=True, timeout=0.1)
            if req:
                self.stack.send(self.model.respond(bytes(req)))
        self.stack.stop()
        self.bus.shutdown()


class SimCar:
    """Context manager that runs all fake ECUs and hands out a tester bus."""

    def __init__(self, ecus=None, tp20_ecus=None):
        self.ecus = ecus or default_ecus()
        self.tp20_ecus = tp20_ecus if tp20_ecus is not None else default_tp20_ecus()
        self.threads = []

    def __enter__(self):
        for addr, data in self.ecus.items():
            t = FakeEcu(UDS_MODULES[addr], data)
            t.start()
            self.threads.append(t)
        if self.tp20_ecus:
            t = Tp20Bus(self.tp20_ecus)
            t.start()
            self.threads.append(t)
        self.bus = can.Bus(interface="virtual", channel=SIM_CHANNEL)
        return self.bus

    def __exit__(self, *exc):
        self.bus.shutdown()
        for t in self.threads:
            t.running = False
        for t in self.threads:
            t.join(timeout=2)


class FakeElmSerial:
    """Behaves like an ELM327 on a serial port (ATCAF0, headers on, spaces off)."""

    def __init__(self, ecus=None, voltage=12.6, tp20_ecus=None, buffer_frames=None, bus=None,
                 version="ELM327 v1.5 (sim)", atrv_offset=0.0):
        """buffer_frames: emulate the clone's small buffer (BUFFER FULL above that many
        frames in one reply); None = unlimited.
        bus: frames ("7E00322155A...") another tester puts on the bus; ATMA shows them
        (through the ATCF / ATCM filter), and stops with BUFFER FULL after buffer_frames.
        version / atrv_offset: another adapter, e.g. the Vgate ("ELM327 v2.1", ATRV ~1 V low).
        The engine's own voltage (PID 0x42) is `voltage`."""
        self.bus = list(bus or [])
        self.can_filter = None  # (ATCF, ATCM)
        self.version, self.atrv_offset = version, atrv_offset
        ecus = ecus or default_ecus()
        if "obd" in ecus.get("01", {}):
            ecus["01"]["obd"][0x42] = round(voltage * 1000).to_bytes(2, "big")
        self.buffer_frames = buffer_frames
        self.models = {UDS_MODULES[a].tx_id: (UDS_MODULES[a].rx_id, EcuModel(d)) for a, d in ecus.items()}
        tp20_ecus = tp20_ecus if tp20_ecus is not None else default_tp20_ecus()
        self.tp20 = [Tp20EcuModel(d) for d in tp20_ecus.values()]
        self.tp20_ids = {0x200} | {m.ecu_rx for m in self.tp20}
        self.voltage = voltage
        self.out = b""
        self.inbuf = b""
        self.header = None
        self.rx_state = {}     # tx_id -> (total, bytearray) while tester sends multi-frame
        self.pending_cf = {}   # tx_id -> frames waiting for the tester's flow control
        self.late = {}         # tx_id -> answer still being prepared after response pending
        self.monitoring = False
        self.last_cmd = ""

    def write(self, data):
        if self.monitoring and data:
            # Like the ELM: any one character stops ATMA and is consumed.
            self.monitoring = False
            self._reply(["STOPPED"])
            data = data[1:]
        self.inbuf += data
        while b"\r" in self.inbuf:
            line, self.inbuf = self.inbuf.split(b"\r", 1)
            line = line.decode("ascii").replace(" ", "").upper()
            if not line and self.last_cmd == "ATMA":
                line = "ATMA"  # a lone CR repeats the last command
            self.last_cmd = line or self.last_cmd
            self._handle(line)

    def read(self, n):
        chunk, self.out = self.out[:n], self.out[n:]
        return chunk

    def close(self):
        pass

    def _reply(self, lines):
        self.out += ("\r".join(lines) + "\r\r>").encode("ascii")

    def _handle(self, cmd):
        if self.monitoring:
            self.monitoring = False
            self._reply([])
            return
        if cmd.startswith("AT"):
            if cmd == "ATZ":
                self._reply([self.version])
            elif cmd == "ATRV":
                self._reply([f"{self.voltage + self.atrv_offset:.1f}V"])
            elif cmd == "ATMA":
                self.monitoring = True
                # A late answer (after response pending) shows up while monitoring:
                # a first frame alone, the rest waits for the tester's flow control.
                frames = self.late.pop(self.header, [])
                if frames:
                    rx_id = self.models[self.header][0]
                    first = frames[:1] if frames[0][0] >> 4 == 1 else frames
                    self.pending_cf[self.header] = frames[len(first):]
                    self.out += "".join(f"{rx_id:03X}{f.hex().upper()}\r" for f in first).encode()
                self._monitor_bus()
            elif cmd.startswith("ATCF"):
                self.can_filter = (int(cmd[4:], 16), (self.can_filter or (0, 0x7FF))[1])
                self._reply(["OK"])
            elif cmd.startswith("ATCM"):
                self.can_filter = ((self.can_filter or (0, 0))[0], int(cmd[4:], 16))
                self._reply(["OK"])
            elif cmd == "ATAR":
                self.can_filter = None
                self._reply(["OK"])
            elif cmd.startswith("ATSH"):
                self.header = int(cmd[4:], 16)
                self._reply(["OK"])
            elif cmd in ("ATR0", "ATR1"):
                self.responses = cmd == "ATR1"
                self._reply(["OK"])
            else:
                self._reply(["OK"])
            return
        if len(cmd) % 2:
            if len(cmd) > 16:
                self._reply(["?"])  # the clone on the car: no reply count after 8 bytes
                return
            cmd = cmd[:-1]  # trailing response-count digit
        frame = bytes.fromhex(cmd)
        if self.header in self.tp20_ids:
            out = [o for m in self.tp20 for o in m.on_frame(self.header, frame)]
            if not getattr(self, "responses", True):
                self._reply([])  # ATR0: frame sent, nothing reported
                return
            self._reply([f"{i:03X}{d.hex().upper()}" for i, d in out] or ["NO DATA"])
            return
        target = self.models.get(self.header)
        if self.late.pop(self.header, None):
            # Like the engine on the car: any frame while it prepares an answer
            # (after 7F xx 78) makes it drop that answer.
            pass
        if target is None:
            self._reply(["NO DATA"])
            return
        rx_id, model = target
        lines = [f"{rx_id:03X}{f.hex().upper()}" for f in self._frame(self.header, model, frame)]
        if self.buffer_frames is not None and len(lines) > self.buffer_frames:
            lines = ["BUFFER FULL"]  # what the clone on the car prints when frames come too fast
        self._reply(lines or ["NO DATA"])

    def _monitor_bus(self):
        if self.can_filter:
            cf, cm = self.can_filter
            self.bus = [b for b in self.bus if int(b[:3], 16) & cm == cf & cm]
        shown, self.bus = (self.bus[:self.buffer_frames], self.bus[self.buffer_frames:]) \
            if self.buffer_frames is not None else (self.bus, [])
        self.out += "".join(f"{b}\r" for b in shown).encode()
        if self.bus:  # more than the clone can pass on: it gives up
            self.monitoring = False
            self._reply(["BUFFER FULL"])

    def _frame(self, tx, model, frame):
        pci = frame[0] >> 4
        if pci == 0:
            return self._respond(tx, model, frame[1:1 + (frame[0] & 0x0F)])
        if pci == 1:
            total = (frame[0] & 0x0F) << 8 | frame[1]
            self.rx_state[tx] = (total, bytearray(frame[2:8]))
            return [bytes([0x30, 0x00, 0x00]).ljust(8, b"\x55")]
        if pci == 2 and tx in self.rx_state:
            total, buf = self.rx_state[tx]
            buf += frame[1:8]
            if len(buf) >= total:
                del self.rx_state[tx]
                return self._respond(tx, model, bytes(buf[:total]))
            return []
        if pci == 3:
            # Block size frame[1]: that many consecutive frames, then wait for the next FC.
            frames = self.pending_cf.pop(tx, [])
            if frame[1] and len(frames) > frame[1]:
                self.pending_cf[tx] = frames[frame[1]:]
                frames = frames[:frame[1]]
            return frames
        return []

    def _respond(self, tx, model, req):
        if model.data.get("pending_once") and req[0] in model.data["pending_once"]:
            # "Response pending" first; the real answer comes with the tester's next frame.
            model.data["pending_once"].discard(req[0])
            self.late[tx] = segment(model.respond(req))
            return [bytes([0x03, 0x7F, req[0], 0x78]).ljust(8, bytes([0xAA]))]
        frames = segment(model.respond(req))
        if len(frames) > 1:
            self.pending_cf[tx] = frames[1:]
        return frames[:1]
