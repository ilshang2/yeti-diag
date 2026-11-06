"""`discover`: an unknown car's modules -> a profile the tool can run with."""
import json

import pytest

from yeti_diag import discover, vehicle
from yeti_diag.sim import SIM_TP20_DEST

from test_cli import run_json


@pytest.fixture
def profile_dir(tmp_path, monkeypatch):
    # A short UDS range around the simulated modules keeps the test fast.
    monkeypatch.setattr(discover, "UDS_SCAN", [0x700, 0x70C, 0x713, 0x714, 0x715, 0x746, 0x795, 0x7E0, 0x7E1])
    monkeypatch.setattr(vehicle, "PROFILE_DIR", tmp_path / "profiles")
    (tmp_path / "profiles").mkdir()
    return tmp_path / "profiles"


def test_discover_writes_a_profile_from_the_simulated_car(capsys, profile_dir):
    code, doc = run_json(capsys, "discover", "--name", "test_car", "--title", "Sim car")
    assert code == 0, doc["error"]
    path = profile_dir / "test_car_sim.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    assert vehicle.check_profile(profile) == []
    uds_mods = profile["uds_modules"]
    # The five UDS modules of the simulator, at their VCDS addresses, with part numbers.
    for addr, mod in vehicle.UDS_MODULES.items():
        assert uds_mods[addr]["tx_id"] == f"{mod.tx_id:03X}"
        assert uds_mods[addr]["part_no"] == mod.part_no.replace(" ", "")
    # Every simulated TP2.0 module is found, named where the address is a known one.
    dests = {m["tp20_dest"] for m in profile["tp20_modules"].values()}
    assert dests == {f"0x{d:02X}" for d in SIM_TP20_DEST.values()}
    assert profile["tp20_modules"]["19"]["part_no"] == vehicle.TP20_PART_NUMBERS["19"]


def test_discover_refuses_to_overwrite(capsys, profile_dir):
    (profile_dir / "taken_sim.json").write_text("{}", encoding="utf-8")
    code, doc = run_json(capsys, "discover", "--name", "taken")
    assert code != 0 and "已經存在" in doc["error"]["message"]


def test_unknown_ids_get_placeholder_addresses():
    rows = [{"tx": 0x7E0, "rx": 0x7E8, "part": "04E906016A", "vin": "WVWZZZ1KZAW000001"},
            {"tx": 0x7AB, "rx": 0x815, "part": "5Q0123456", "F197": "J999 Test"}]
    p = discover.build_profile("x", "X", rows, {"0x2A": "1K0000000A", "0x02": "(讀不到零件號碼)"})
    assert p["vin"] == "WVWZZZ1KZAW000000"  # serial masked by default: profiles get shared
    assert discover.build_profile("x", "X", rows, {}, keep_vin=True)["vin"] == "WVWZZZ1KZAW000001"
    assert p["uds_modules"]["UDS_7AB"] == {"name": "J999 Test", "part_no": "5Q0123456", "tx_id": "7AB",
                                           "rx_id": "815", "system_name": "J999 Test"}
    assert p["tp20_modules"]["TP20_2A"]["part_no"] == "1K0000000A"
    assert "part_no" not in p["tp20_modules"]["02"]


def test_mask_vin_keeps_car_type_drops_serial():
    assert discover.mask_vin("TMBJF65L8E6123456") == "TMBJF65L8E6000000"
    assert discover.mask_vin("") == ""
    assert discover.mask_vin("SHORT") == "SHORT"  # not a VIN: leave it alone
