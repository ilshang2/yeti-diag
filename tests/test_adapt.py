"""Adaptation reading (routine 01 03), answers as sniffed from the car's 09 on 2026-11-05."""
import pytest

from yeti_diag import tp20
from yeti_diag.elm327 import Elm327
from yeti_diag.sim import FakeElmSerial, SimCar
from yeti_diag.uds import NegativeResponse, ReadOnlyViolation

from test_features import open_09


def test_read_adaptation_matches_vcds_screen():
    with SimCar() as bus:
        ch, k = open_09(tp20.CanLink(bus))
        blink = k.read_adaptation(31)
        heater = k.read_adaptation(25)
        door = k.read_adaptation(3)
        ch.close()
    assert blink["value"] == 5                 # VCDS: Comfort Blink 5
    assert heater["value"] == 15               # VCDS: Current Value 15 ...
    assert any(f.get("value") == 600 for f in heater["fields"])  # ... shown as 600
    assert door["value"] == 1


def test_missing_channel_is_refused_like_the_car():
    with SimCar() as bus:
        ch, k = open_09(tp20.CanLink(bus))
        with pytest.raises(NegativeResponse) as e:
            k.read_adaptation(2)
        assert e.value.nrc == 0x31
        assert k.read_adaptation(31)["value"] == 5  # routine stopped cleanly, next read works
        ch.close()


def test_read_adaptation_over_elm():
    elm = Elm327(serial_obj=FakeElmSerial())
    ch, k = open_09(tp20.ElmLink(elm))
    assert k.read_adaptation(31)["value"] == 5
    ch.close()


@pytest.mark.parametrize("req", ["31B80104", "31B9010403", "31BB0103", "32B80104", "3101", "2E0600"])
def test_only_the_adaptation_read_routine_passes_the_guard(req):
    k = tp20.KwpClient(channel=None)
    with pytest.raises(ReadOnlyViolation):
        k.request(bytes.fromhex(req))


def test_freeze_frame_of_door_fault_matches_vcds():
    body = bytes.fromhex("006C01280369430495E90001FFBEB987040843")
    ff = tp20.parse_freeze_frame(body)
    assert ff["status"] == 0x28 and ff["priority"] == 3 and ff["frequency"] == 105
    assert ff["reset_counter"] == 67 and ff["mileage_km"] == 300521
    assert ff["date"] == "2063.14.31" and not ff["date_valid"]  # what VCDS showed too
    assert ff["time"][:5] == "11:38"


def _bcm():
    from yeti_diag.vehicle import Module
    return Module("09", "中央電子", "1K0937086AA", 0, 0)


def test_adaptation_write_with_workshop_code_and_readback(tmp_path):
    from test_features import always
    from yeti_diag import writer
    with SimCar() as bus:
        ch, k = open_09(tp20.CanLink(bus))
        r = writer.write_adaptation(k, _bcm(), 31, 4, voltage=12.6, confirm=always(True), backup_dir=tmp_path)
        assert r["status"] == "written" and r["old"] == 5 and r["new"] == 4
        assert k.read_adaptation(31)["value"] == 4
        ch.close()
    assert any(p.name.endswith("_09_adapt031.json") for p in tmp_path.iterdir())


def test_adaptation_out_of_range_is_refused_and_reported(tmp_path):
    """What happened on the car: VCDS sent 0 for channel 31, the 09 said 7F 31 31, value stayed."""
    from test_features import always
    from yeti_diag import writer
    with SimCar() as bus:
        ch, k = open_09(tp20.CanLink(bus))
        with pytest.raises(writer.VerifyFailed):
            writer.write_adaptation(k, _bcm(), 31, 0, voltage=12.6, confirm=always(True), backup_dir=tmp_path)
        assert k.read_adaptation(31)["value"] == 5
        ch.close()


def test_adaptation_write_needs_voltage_and_consent(tmp_path):
    from test_features import always
    from yeti_diag import writer
    with SimCar() as bus:
        ch, k = open_09(tp20.CanLink(bus))
        with pytest.raises(writer.WriteRefused):
            writer.write_adaptation(k, _bcm(), 31, 4, voltage=11.9, confirm=always(True), backup_dir=tmp_path)
        with pytest.raises(writer.WriteRefused):
            writer.write_adaptation(k, _bcm(), 31, 4, voltage=12.6, confirm=always(False), backup_dir=tmp_path)
        with pytest.raises(writer.WriteRefused):
            writer.write_adaptation(k, _bcm(), 0, 1, voltage=12.6, confirm=always(True), backup_dir=tmp_path)
        assert k.read_adaptation(31)["value"] == 5
        ch.close()


def test_value_writes_cannot_go_through_the_read_only_guard():
    k = tp20.KwpClient(channel=None)
    for req in ("31B90103000004", "31BB01030004000212345678", "31B901030004"):
        with pytest.raises(ReadOnlyViolation):
            k.request(bytes.fromhex(req))
