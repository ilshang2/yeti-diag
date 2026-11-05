import json

import pytest

from yeti_diag import uds, writer
from yeti_diag.elm327 import Elm327, ElmChannel, segment
from yeti_diag.sim import (SIM_OIL_SERVICE_DID, SIM_REFRIGERANT_DID, FakeElmSerial,
                           SimCar, default_ecus)
from yeti_diag.vehicle import UDS_MODULES, VIN


def client(bus, addr, timeout=1.0):
    mod = UDS_MODULES[addr]
    return uds.UdsClient(uds.can_channel(bus, mod.tx_id, mod.rx_id), timeout=timeout)


def elm_client(elm, addr):
    mod = UDS_MODULES[addr]
    return uds.UdsClient(ElmChannel(elm, mod.tx_id, mod.rx_id))


def always(answer):
    return lambda old, new: answer


# --- reading ---------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    [0x14, 0xFF, 0xFF, 0xFF],  # clear DTCs
    [0x2E, 0x06, 0x00, 0x01],  # write DID (coding)
    [0x31, 0x01, 0x00, 0x00],  # routine control
    [0x11, 0x01],              # ECU reset
    [0x10, 0x03],              # extended session
    [0x27, 0x01],              # security access
])
def test_request_refuses_non_read_services(payload):
    with SimCar() as bus, client(bus, "17") as c:
        with pytest.raises(uds.ReadOnlyViolation):
            c.request(payload)


def test_format_dtc():
    assert uds.format_dtc(0xC10000) == "U0100-00"
    assert uds.format_dtc(0x012345) == "P0123-45"
    assert uds.format_dtc(0x9A1B08) == "B1A1B-08"


def test_reads_multiframe_identification():
    with SimCar() as bus, client(bus, "08") as c:
        assert uds.decode_did(0xF187, c.read_did(0xF187)) == "3T0820047AK"
        assert uds.decode_did(0xF190, c.read_did(0xF190)) == VIN


def test_unknown_did_is_negative_response():
    with SimCar() as bus, client(bus, "17") as c:
        with pytest.raises(uds.NegativeResponse) as e:
            c.read_did(0x1234)
        assert e.value.nrc == 0x31


def test_reads_dtcs():
    ecus = default_ecus()
    ecus["17"]["dtcs"] = [(0xC10000, 0x09), (0x9A1B08, 0x08)]
    with SimCar(ecus) as bus, client(bus, "17") as c:
        dtcs = c.read_dtcs()
    assert [(d["dtc"], d["status"]) for d in dtcs] == [(0xC10000, 0x09), (0x9A1B08, 0x08)]
    assert uds.describe_status(0x09) == "目前失效、已確認"


def test_refrigerant_example_did():
    with SimCar() as bus, client(bus, "08") as c:
        raw = c.read_did(SIM_REFRIGERANT_DID)
    assert int.from_bytes(raw, "big") / 10 == 10.0


def test_obd_values():
    with SimCar() as bus, client(bus, "01") as c:
        assert uds.read_obd_pid(c, 0x05) == ("冷卻水溫", 90, "°C")
        assert uds.read_obd_pid(c, 0x0C) == ("引擎轉速", 1200, "rpm")
        assert uds.read_obd_pid(c, 0x42)[1] == pytest.approx(13.932)
        assert uds.read_obd_dtcs(c) == []


def test_readiness():
    r = uds.decode_readiness(bytes([0x82, 0x07, 0x65, 0x04]))
    assert r["mil"] and r["dtc_count"] == 2 and r["engine"] == "spark"
    names = {m["name"]: m["complete"] for m in r["monitors"]}
    assert names == {"失火": True, "燃油系統": True, "綜合元件": True, "觸媒轉換器": True,
                     "油氣蒸發系統": False, "含氧感知器": True, "含氧感知器加熱": True}
    assert not r["all_complete"]
    diesel = uds.decode_readiness(bytes([0x00, 0x0F, 0x01, 0x00]))
    assert diesel["engine"] == "diesel" and diesel["all_complete"]
    assert [m["name"] for m in diesel["monitors"]][-1] == "NMHC 觸媒"
    with SimCar() as bus, client(bus, "01") as c:
        assert uds.read_readiness(c)["raw"] == "00 07 65 04"


def test_missing_module_times_out():
    ecus = default_ecus()
    del ecus["16"]
    with SimCar(ecus) as bus, client(bus, "16", timeout=0.3) as c:
        with pytest.raises(uds.NoResponse):
            c.read_did(0xF187)


# --- writing ---------------------------------------------------------------

def test_write_backs_up_writes_and_verifies(tmp_path):
    ecus = default_ecus()
    with SimCar(ecus) as bus, client(bus, "17") as c:
        result = writer.write_did(c, UDS_MODULES["17"], SIM_OIL_SERVICE_DID, bytes([0x00, 75]),
                                  voltage=13.9, confirm=always(True), backup_dir=tmp_path)
        assert c.read_did(SIM_OIL_SERVICE_DID) == bytes([0x00, 75])
    assert result["status"] == "written"
    backup = json.loads(next(tmp_path.iterdir()).read_text(encoding="utf-8"))
    assert backup["old"] == "0096" and backup["new"] == "004B"


def test_restore_puts_old_value_back(tmp_path):
    with SimCar() as bus, client(bus, "17") as c:
        mod = UDS_MODULES["17"]
        writer.write_did(c, mod, SIM_OIL_SERVICE_DID, bytes([0x00, 75]),
                         voltage=13.9, confirm=always(True), backup_dir=tmp_path)
        backup_file = next(tmp_path.iterdir())
        writer.restore(c, mod, backup_file, voltage=13.9, confirm=always(True), backup_dir=tmp_path)
        assert c.read_did(SIM_OIL_SERVICE_DID) == bytes([0x00, 150])


@pytest.mark.parametrize("addr", sorted(writer.BLOCKED_MODULES))
def test_safety_modules_are_never_written(addr, tmp_path):
    mod = UDS_MODULES.get(addr) or type("M", (), {"address": addr})()
    with pytest.raises(writer.WriteRefused):
        writer.write_did(None, mod, 0x0600, b"\x00", voltage=13.9, confirm=always(True), backup_dir=tmp_path)


def test_low_voltage_refused_but_backup_kept(tmp_path):
    with SimCar() as bus, client(bus, "17") as c:
        with pytest.raises(writer.WriteRefused, match="低於"):
            writer.write_did(c, UDS_MODULES["17"], SIM_OIL_SERVICE_DID, bytes([0, 75]),
                             voltage=11.8, confirm=always(True), backup_dir=tmp_path)
        assert c.read_did(SIM_OIL_SERVICE_DID) == bytes([0x00, 150])
    assert len(list(tmp_path.iterdir())) == 1


def test_wrong_part_number_refused(tmp_path):
    ecus = default_ecus()
    ecus["17"]["dids"][0xF187] = b"5L0920800X"
    with SimCar(ecus) as bus, client(bus, "17") as c:
        with pytest.raises(writer.WriteRefused, match="零件號碼"):
            writer.write_did(c, UDS_MODULES["17"], SIM_OIL_SERVICE_DID, bytes([0, 75]),
                             voltage=13.9, confirm=always(True), backup_dir=tmp_path)


def test_user_cancel_leaves_value(tmp_path):
    with SimCar() as bus, client(bus, "17") as c:
        with pytest.raises(writer.WriteRefused, match="取消"):
            writer.write_did(c, UDS_MODULES["17"], SIM_OIL_SERVICE_DID, bytes([0, 75]),
                             voltage=13.9, confirm=always(False), backup_dir=tmp_path)
        assert c.read_did(SIM_OIL_SERVICE_DID) == bytes([0x00, 150])


def test_write_that_does_not_stick_is_detected(tmp_path):
    ecus = default_ecus()
    ecus["17"]["ignore_writes"] = True
    with SimCar(ecus) as bus, client(bus, "17") as c:
        with pytest.raises(writer.VerifyFailed):
            writer.write_did(c, UDS_MODULES["17"], SIM_OIL_SERVICE_DID, bytes([0, 75]),
                             voltage=13.9, confirm=always(True), backup_dir=tmp_path)


def test_clear_dtcs_verified(tmp_path):
    ecus = default_ecus()
    ecus["08"]["dtcs"] = [(0x9A1B08, 0x08)]
    with SimCar(ecus) as bus, client(bus, "08") as c:
        result = writer.clear_dtcs(c, UDS_MODULES["08"], confirm=always(True), backup_dir=tmp_path)
    assert result["status"] == "cleared"


def test_clear_that_does_not_stick_is_detected(tmp_path):
    # Same symptom as the rear doors on 2026-11-03: "erased", but nothing changed.
    ecus = default_ecus()
    ecus["08"]["dtcs"] = [(0x9A1B08, 0x08)]
    ecus["08"]["sticky_dtcs"] = True
    with SimCar(ecus) as bus, client(bus, "08") as c:
        with pytest.raises(writer.VerifyFailed, match="完全沒變"):
            writer.clear_dtcs(c, UDS_MODULES["08"], confirm=always(True), backup_dir=tmp_path)


# --- ELM327 ----------------------------------------------------------------

def test_segment_multiframe():
    frames = segment(bytes(range(20)))
    assert frames[0][:2] == bytes([0x10, 20]) and frames[1][0] == 0x21 and frames[2][0] == 0x22
    assert all(len(f) == 8 for f in frames)


def test_elm_reads_multiframe_and_voltage():
    elm = Elm327(serial_obj=FakeElmSerial(voltage=12.7))
    assert elm.battery_voltage() == pytest.approx(12.7)
    with elm_client(elm, "08") as c:
        assert uds.decode_did(0xF190, c.read_did(0xF190)) == VIN
        assert uds.decode_did(0xF187, c.read_did(0xF187)) == "3T0820047AK"


def test_elm_multiframe_write_request(tmp_path):
    # A write longer than 7 bytes must go out as First Frame + Consecutive Frames.
    ecus = default_ecus()
    long_value = bytes(range(1, 31))  # 30 bytes, like the 09 long coding
    ecus["17"]["dids"][0x0600] = bytes(30)
    ecus["17"]["writable"].add(0x0600)
    elm = Elm327(serial_obj=FakeElmSerial(ecus))
    with elm_client(elm, "17") as c:
        result = writer.write_did(c, UDS_MODULES["17"], 0x0600, long_value,
                                  voltage=elm.battery_voltage(), confirm=always(True), backup_dir=tmp_path)
        assert result["status"] == "written"
        assert c.read_did(0x0600) == long_value


def test_test_not_completed_codes_are_not_faults():
    # On the car 08 listed 41 and 16 listed 93 codes with status 10 only; VCDS shows none.
    ecus = default_ecus()
    ecus["16"]["dtcs"] = [(0x06D40B, 0x10), (0xC10000, 0x09), (0x9A1B08, 0x50)]
    with SimCar(ecus) as bus, client(bus, "16") as c:
        assert [d["dtc"] for d in c.read_dtcs()] == [0xC10000]
        assert len(c.read_dtcs(0xFF)) == 3


def test_long_answer_fits_a_small_adapter_buffer():
    # 30 stored faults = 123 bytes = 18 frames: block size 0 overflowed the clone on 01/15.
    ecus = default_ecus()
    ecus["15"]["dtcs"] = [(0x900000 + i, 0x09) for i in range(30)]
    elm = Elm327(serial_obj=FakeElmSerial(ecus, buffer_frames=10))
    with elm_client(elm, "15") as c:
        assert len(c.read_dtcs()) == 30


def test_elm_recovers_after_can_error():
    class OnceBroken(FakeElmSerial):
        def __init__(self):
            super().__init__()
            self.broken, self.commands = True, []

        def _handle(self, cmd):
            self.commands.append(cmd)
            if self.broken and not cmd.startswith("AT"):
                self.broken = False
                self._reply(["CAN ERROR"])
                return
            super()._handle(cmd)

    fake = OnceBroken()
    elm = Elm327(serial_obj=fake)
    with elm_client(elm, "17") as c:
        with pytest.raises(Exception):
            c.read_did(0xF187)
        fake.commands.clear()
        assert c.read_did(0xF187) == b"5L0920800L"
    # The next request set protocol, header and filter again.
    assert {"ATPC", "ATSP6"} & set(fake.commands) == {"ATSP6"} and any(x.startswith("ATSH") for x in fake.commands)


def test_elm_parse_drops_cut_short_lines():
    # Seen on the car during autoscan: a frame line with an odd hex length.
    frames = Elm327._parse(["7E81003590204", "7E8100359020", "NO DATA", "7E8 02 59 02"])
    assert frames == [(0x7E8, bytes.fromhex("1003590204")), (0x7E8, bytes.fromhex("025902"))]


def test_missed_consecutive_frame_is_an_error_not_wrong_data():
    # A clone that loses one frame must not shift the rest into wrong fault codes.
    class DropsOne(FakeElmSerial):
        dropped = False

        def _frame(self, tx, model, frame):
            out = super()._frame(tx, model, frame)
            if frame[0] >> 4 == 3 and len(out) > 2 and not self.dropped:
                DropsOne.dropped = True
                out = out[:1] + out[2:]
            return out

    ecus = default_ecus()
    ecus["15"]["dtcs"] = [(0x900000 + i, 0x09) for i in range(10)]
    elm = Elm327(serial_obj=DropsOne(ecus))
    with elm_client(elm, "15") as c:
        with pytest.raises(Exception, match="序號"):
            c.read_dtcs()
        assert len(c.read_dtcs()) == 10  # the retry gets the whole answer


def test_engine_answer_after_response_pending():
    # The engine answers 19 02 with 7F 19 78 first; its long answer must still come in.
    ecus = default_ecus()
    ecus["01"]["dtcs"] = [(0x012345, 0x09), (0x0A1B08, 0x08), (0x030000, 0x0D)]
    ecus["01"]["pending_once"] = {0x19}
    elm = Elm327(serial_obj=FakeElmSerial(ecus))
    with elm_client(elm, "01") as c:
        assert len(c.read_dtcs()) == 3


def test_reply_count_never_follows_an_8_byte_frame():
    # The clone on the car answered '?' to "3008005555555555" + reply count.
    fake = FakeElmSerial()
    elm = Elm327(serial_obj=fake)
    with elm_client(elm, "08") as c:
        assert uds.decode_did(0xF187, c.read_did(0xF187)) == "3T0820047AK"
