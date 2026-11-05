import json

import pytest

from yeti_diag import features, tp20, writer
from yeti_diag.elm327 import Elm327
from yeti_diag.sim import SIM_TP20_DEST, FakeElmSerial, SimCar, default_tp20_ecus
from yeti_diag.vehicle import VCDS_TRUTH


def always(answer):
    return lambda old, new: answer


def open_09(link):
    ch = tp20.Tp20Channel(link, SIM_TP20_DEST["09"]).open()
    k = tp20.KwpClient(ch)
    k.start_session()
    return ch, k


def test_feature_states_match_todays_vcds_changes():
    coding = VCDS_TRUTH["09_coding"]
    assert features.is_on(coding, features.BY_KEY["brake-flash"])
    assert features.is_on(coding, features.BY_KEY["fog-cornering"])
    assert features.is_on(coding, features.BY_KEY["rear-wiper-drip"])
    assert not features.is_on(coding, features.BY_KEY["leaving-home"])


def test_with_feature_changes_exactly_one_bit():
    coding = VCDS_TRUTH["09_coding"]
    new = features.with_feature(coding, features.BY_KEY["leaving-home"], True)
    diff = [i for i in range(len(coding)) if coding[i] != new[i]]
    assert diff == [17] and new[17] == coding[17] | 0x40


def test_set_feature_over_can(tmp_path):
    with SimCar() as bus:
        ch, k = open_09(tp20.CanLink(bus))
        result = features.set_feature(k, features.BY_KEY["leaving-home"], True,
                                      voltage=13.5, confirm=always(True), backup_dir=tmp_path)
        assert result["status"] == "written"
        assert features.is_on(features.read_coding(k), features.BY_KEY["leaving-home"])
        ch.close()
    backup = json.loads(next(tmp_path.iterdir()).read_text(encoding="utf-8"))
    assert bytes.fromhex(backup["old"]) == VCDS_TRUTH["09_coding"]


def test_set_feature_over_elm_multipacket_write(tmp_path):
    # 30-byte coding = 5 TP2.0 packets from the tester, sent with ATR0 between them.
    elm = Elm327(serial_obj=FakeElmSerial())
    ch, k = open_09(tp20.ElmLink(elm))
    result = features.set_feature(k, features.BY_KEY["leaving-home"], True,
                                  voltage=12.6, confirm=always(True), backup_dir=tmp_path)
    assert result["status"] == "written"
    assert features.is_on(features.read_coding(k), features.BY_KEY["leaving-home"])
    ch.close()


def test_feature_already_set_is_unchanged(tmp_path):
    with SimCar() as bus:
        ch, k = open_09(tp20.CanLink(bus))
        result = features.set_feature(k, features.BY_KEY["brake-flash"], True,
                                      voltage=13.5, confirm=always(True), backup_dir=tmp_path)
        ch.close()
    assert result["status"] == "unchanged"


def test_feature_refused_on_other_bcm_version(tmp_path):
    tp = default_tp20_ecus()
    tp["09"]["ident"][0x9B] = b"1K0937049F  SIM COMPONENT"
    with SimCar(tp20_ecus=tp) as bus:
        ch, k = open_09(tp20.CanLink(bus))
        with pytest.raises(writer.WriteRefused, match="零件號碼"):
            features.set_feature(k, features.BY_KEY["rear-wiper-drip"], True,
                                 voltage=13.5, confirm=always(True), backup_dir=tmp_path)
        ch.close()


def test_feature_write_not_stored_is_detected(tmp_path):
    tp = default_tp20_ecus()
    tp["09"]["ignore_writes"] = True
    with SimCar(tp20_ecus=tp) as bus:
        ch, k = open_09(tp20.CanLink(bus))
        with pytest.raises(writer.VerifyFailed):
            features.set_feature(k, features.BY_KEY["leaving-home"], True,
                                 voltage=13.5, confirm=always(True), backup_dir=tmp_path)
        ch.close()


def test_kwp_request_still_refuses_writes():
    with SimCar() as bus:
        ch, k = open_09(tp20.CanLink(bus))
        with pytest.raises(tp20.ReadOnlyViolation):
            k.request([0x2E, 0x06, 0x00, 0x00])
        ch.close()



def test_coding_rewrite_same_value_goes_through_full_sequence(tmp_path):
    """Same coding written back: not skipped as 'unchanged', and the simulated 09 only takes it
    after session 0x89 and the F198 workshop code (VCDS order recorded 2026-11-05)."""
    with SimCar() as bus:
        ch, k = open_09(tp20.CanLink(bus))
        before = features.read_coding(k)
        result = features.rewrite_coding(k, voltage=13.0, confirm=always(True), backup_dir=tmp_path)
        assert result["status"] == "written"
        assert result["old"] == result["new"] == before.hex().upper()
        assert features.read_coding(k) == before
        ch.close()
