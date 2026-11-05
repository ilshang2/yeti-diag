"""ELM327 adapters that only speak Bluetooth Low Energy (no COM port on Windows).

`BleSerial` looks like the pyserial object `Elm327` uses (read / write /
reset_input_buffer / close), so everything above it works unchanged. The adapter's
text goes out on a write characteristic and comes back as notifications.

Clones use a few GATT layouts; known pairs are tried first, otherwise the first
notify + write characteristics of a non-standard service. Not yet tried on a real
BLE-only adapter (2026-11-04): only the simulator stack is tested.
"""
import asyncio
import re
import threading
import time

# (notify, write) characteristic pairs used by ELM327 BLE clones.
KNOWN_PAIRS = [
    ("0000fff1-0000-1000-8000-00805f9b34fb", "0000fff2-0000-1000-8000-00805f9b34fb"),  # most clones
    ("0000ffe1-0000-1000-8000-00805f9b34fb", "0000ffe1-0000-1000-8000-00805f9b34fb"),  # HM-10 style
    ("00002af0-0000-1000-8000-00805f9b34fb", "00002af1-0000-1000-8000-00805f9b34fb"),  # Vgate vLinker BLE
]
STANDARD_SERVICES = {"00001800", "00001801", "0000180a", "0000180f"}  # GAP, GATT, device info, battery
OBD_NAMES = re.compile(r"obd|elm|v-?link|vgate|icar|konnwei|viecar|veepeak|ios-?vlink|carista", re.I)
ADDRESS = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$|^[0-9A-F-]{36}$", re.I)
CHUNK = 20  # bytes per write: the default BLE payload


class BleError(Exception):
    pass


def pick_characteristics(services):
    """(notify uuid, write uuid, write needs response) from a bleak service collection."""
    chars = {}
    for s in services:
        for c in s.characteristics:
            chars[c.uuid.lower()] = (s.uuid.lower(), set(c.properties))
    for n, w in KNOWN_PAIRS:
        if n in chars and w in chars:
            return n, w, "write-without-response" not in chars[w][1]
    for s in services:
        if s.uuid.lower()[:8] in STANDARD_SERVICES:
            continue
        notify = next((c.uuid.lower() for c in s.characteristics
                       if {"notify", "indicate"} & set(c.properties)), None)
        write = next((c for c in s.characteristics
                      if {"write", "write-without-response"} & set(c.properties)), None)
        if notify and write:
            return notify, write.uuid.lower(), "write-without-response" not in write.properties
    raise BleError("找不到 ELM327 的 BLE 讀寫特徵值 (這可能不是 OBD 接頭)")


async def scan(seconds=6.0):
    """Nearby BLE devices: [(address, name, rssi, looks like OBD)]."""
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = []
    for addr, (dev, adv) in found.items():
        name = dev.name or adv.local_name or ""
        uuids = {u.lower()[4:8] for u in adv.service_uuids}
        rows.append((addr, name, adv.rssi, bool(OBD_NAMES.search(name)) or bool(uuids & {"fff0", "ffe0", "18f0"})))
    return sorted(rows, key=lambda r: (not r[3], -r[2]))


class BleSerial:
    """pyserial-like wrapper. target: BLE address, or part of the advertised name."""

    def __init__(self, target, timeout=0.05, scan_seconds=6.0, client_factory=None):
        self.timeout = timeout
        self.buf = bytearray()
        self.cond = threading.Condition()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        try:
            self._run(self._connect(target, scan_seconds, client_factory), 30)
        except Exception:
            self._stop_loop()
            raise

    def _run(self, coro, timeout):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _connect(self, target, scan_seconds, client_factory):
        if client_factory is None:
            from bleak import BleakClient
            address = target
            if not ADDRESS.match(target):
                matches = [r for r in await scan(scan_seconds) if target.lower() in r[1].lower()]
                if not matches:
                    raise BleError(f"附近找不到名稱含「{target}」的 BLE 裝置 (接頭有插在車上嗎？)")
                address = matches[0][0]
            self.client = BleakClient(address)
        else:
            self.client = client_factory(target)
        await self.client.connect()
        self.notify_uuid, self.write_uuid, self.response = pick_characteristics(self.client.services)
        await self.client.start_notify(self.notify_uuid, self._on_data)

    def _on_data(self, _sender, data):
        with self.cond:
            self.buf += data
            self.cond.notify_all()

    # --- pyserial interface used by Elm327 ---

    def write(self, data):
        for i in range(0, len(data), CHUNK):
            self._run(self.client.write_gatt_char(self.write_uuid, bytes(data[i:i + CHUNK]),
                                                  response=self.response), 5)
        return len(data)

    def read(self, n):
        deadline = time.monotonic() + self.timeout
        with self.cond:
            while not self.buf:
                left = deadline - time.monotonic()
                if left <= 0:
                    return b""
                self.cond.wait(left)
            out = bytes(self.buf[:n])
            del self.buf[:n]
            return out

    def reset_input_buffer(self):
        with self.cond:
            self.buf.clear()

    def close(self):
        try:
            self._run(self.client.disconnect(), 5)
        except Exception:  # noqa: BLE001 - closing a dead link must not hide the real error
            pass
        self._stop_loop()

    def _stop_loop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
