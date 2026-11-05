"""BLE-only adapters: the whole stack over a fake BLE link to the fake ELM327."""
from types import SimpleNamespace as NS

import pytest

from yeti_diag import ble, tp20, uds, vehicle
from yeti_diag.elm327 import Elm327, ElmChannel
from yeti_diag.sim import FakeElmSerial


def gatt(*services):
    """services: (service uuid, [(char uuid, [properties])])."""
    return [NS(uuid=s, characteristics=[NS(uuid=c, properties=p) for c, p in chars]) for s, chars in services]


FFF0 = gatt(("00001800-0000-1000-8000-00805f9b34fb", [("00002a00-0000-1000-8000-00805f9b34fb", ["read"])]),
            ("0000fff0-0000-1000-8000-00805f9b34fb",
             [("0000fff1-0000-1000-8000-00805f9b34fb", ["notify"]),
              ("0000fff2-0000-1000-8000-00805f9b34fb", ["write", "write-without-response"])]))


class FakeBleClient:
    """A BLE ELM327: writes go to the fake ELM, its output comes back as 20-byte notifications."""

    def __init__(self, services=FFF0, **elm_kwargs):
        self.services = services
        self.elm = FakeElmSerial(**elm_kwargs)
        self.writes = []

    async def connect(self):
        pass

    async def start_notify(self, uuid, callback):
        self.notify_uuid, self.callback = uuid, callback

    async def write_gatt_char(self, uuid, data, response):
        assert len(data) <= ble.CHUNK
        self.writes.append((uuid, bytes(data), response))
        self.elm.write(bytes(data))
        out = self.elm.read(1 << 20)
        for i in range(0, len(out), ble.CHUNK):
            self.callback(None, bytearray(out[i:i + ble.CHUNK]))

    async def disconnect(self):
        pass


def test_known_and_unknown_layouts_are_found():
    assert ble.pick_characteristics(FFF0) == (ble.KNOWN_PAIRS[0][0], ble.KNOWN_PAIRS[0][1], False)
    hm10 = gatt(("0000ffe0-0000-1000-8000-00805f9b34fb",
                 [("0000ffe1-0000-1000-8000-00805f9b34fb", ["read", "write", "notify"])]))
    assert ble.pick_characteristics(hm10) == ("0000ffe1-0000-1000-8000-00805f9b34fb",) * 2 + (True,)
    other = gatt(("e7810a71-73ae-499d-8c15-faa9aef0c3f2",
                  [("bef8d6c9-9c21-4c9e-b632-bd58c1009f9f", ["notify", "write"])]))
    assert ble.pick_characteristics(other)[0] == "bef8d6c9-9c21-4c9e-b632-bd58c1009f9f"
    with pytest.raises(ble.BleError):
        ble.pick_characteristics(FFF0[:1])  # only the standard GAP service


def test_elm_works_over_ble():
    client = FakeBleClient(voltage=12.4)
    link = ble.BleSerial("OBDII", client_factory=lambda target: client)
    elm = Elm327(serial_obj=link)
    try:
        assert elm.battery_voltage() == pytest.approx(12.4)
        mod = vehicle.UDS_MODULES["17"]
        with uds.UdsClient(ElmChannel(elm, mod.tx_id, mod.rx_id), timeout=1.0) as c:
            assert uds.decode_did(0xF187, c.read_did(0xF187)).replace(" ", "") == mod.part_no.replace(" ", "")
        # TP2.0 (KWP) too: a DSG measuring group, multi-packet.
        ch = tp20.Tp20Channel(tp20.ElmLink(elm), 0x02, timeout=1.0).open()
        k = tp20.KwpClient(ch)
        k.start_session()
        assert k.request([0x21, 122])[:2] == bytes([0x61, 122])
        ch.close()
        # Long commands are split into 20-byte writes without response (the fff2 layout allows it).
        assert all(len(d) <= ble.CHUNK and r is False for _, d, r in client.writes)
    finally:
        elm.close()
