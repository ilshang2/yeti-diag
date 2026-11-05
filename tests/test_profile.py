"""Vehicle profiles: every shipped profile is valid, and a broken one is reported."""
import json

import pytest

from yeti_diag import vehicle


@pytest.mark.parametrize("path", sorted(vehicle.PROFILE_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_shipped_profiles_are_valid(path):
    assert vehicle.check_profile(json.loads(path.read_text(encoding="utf-8"))) == []


def test_default_profile_is_the_yeti():
    assert vehicle.PROFILE_NAME == "skoda_yeti_2014"
    assert vehicle.UDS_MODULES["17"].tx_id == 0x714 and vehicle.UDS_MODULES["17"].rx_id == 0x77E
    assert vehicle.TP20_PART_NUMBERS["62"] == vehicle.TP20_PART_NUMBERS["72"] == "7N0959795E"
    assert vehicle.UDS_VALUES["17"][0x2203][2](bytes.fromhex("77B7")) == 306470


def test_broken_profile_is_explained(tmp_path):
    bad = {"profile": "x", "name": "x", "vin": "x", "tp20_modules": {},
           "uds_modules": {"17": {"name": "儀表", "part_no": "1", "tx_id": "71G"}},
           "uds_values": {"08": {"2203": {"name": "里程"}}}}
    problems = vehicle.check_profile(bad)
    assert any("rx_id" in p for p in problems) and any("71G" in p for p in problems)
    assert any("08" in p and "不在" in p for p in problems) and any("checked" in p for p in problems)


def test_profile_by_path(tmp_path):
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"profile": "other"}), encoding="utf-8")
    assert vehicle.load_profile(str(path))["profile"] == "other"
