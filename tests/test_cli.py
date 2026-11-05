"""The command line as an AI agent uses it: --json envelope, exit codes, plan -> apply."""
import json

import pytest

from yeti_diag import cli, plans, writer
from yeti_diag.sim import SIM_TP20_DEST


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Reports, plans and backups go to a temp dir; the sim TP2.0 map is pre-filled."""
    monkeypatch.setattr(cli, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(plans, "PLAN_DIR", tmp_path / "plans")
    monkeypatch.setattr(writer, "BACKUP_DIR", tmp_path / "backups")
    (tmp_path / "reports").mkdir()
    modules = {a: f"0x{d:02X}" for a, d in SIM_TP20_DEST.items()}
    (tmp_path / "reports" / "tp20_map_sim.json").write_text(
        json.dumps({"sim": True, "found": {}, "modules": modules}), encoding="utf-8")
    return tmp_path


def run_json(capsys, *argv):
    with pytest.raises(SystemExit) as e:
        cli.main(["--sim", "--json", *argv])
    out = capsys.readouterr().out
    return e.value.code, json.loads(out)


def test_voltage_envelope(capsys):
    code, doc = run_json(capsys, "voltage")
    assert code == 0 and doc["ok"] and doc["command"] == "voltage"
    assert doc["data"]["voltage"] == pytest.approx(13.932) and doc["error"] is None
    assert doc["vin"] and doc["sim"] is True


def test_autoscan_covers_uds_and_tp20_with_dtc_text(capsys):
    code, doc = run_json(capsys, "autoscan")
    assert code == 0, doc["error"]
    mods = {m["address"]: m for m in doc["data"]["modules"]}
    assert {"01", "17", "09", "19", "62", "72"} <= set(mods)
    door = mods["62"]["dtcs"][0]
    assert door["code"] == "02115" and door["text"] == "中控鎖 門鎖單元"
    assert door["symptom"] == "008" and door["intermittent"] is True
    assert mods["09"]["coding"] == cli.VCDS_TRUTH["09_coding"].hex().upper()
    assert doc["data"]["report"].endswith(".json")


def test_measure_decodes_values(capsys):
    code, doc = run_json(capsys, "measure", "19", "1", "2")
    assert code == 0
    g1, g2 = doc["data"]["groups"]
    values = g1["values"]
    assert values[0]["value"] == pytest.approx(12.6) and values[0]["unit"] == "V"
    assert values[1]["value"] == 306430 and values[1]["unit"] == "km"
    assert values[2]["value"] is None and values[2]["raw"] == "FE 01 02"
    assert values[3]["unit"] == "bits"
    # 0x06 (volts, vs ATRV) and 0x24 (km, vs Car Scanner) were checked on the car.
    assert [v["verified"] for v in values] == [True, True, False, True]  # 0x10 confirmed with VCDS 2026-11-05
    assert "error" in g2


def test_bad_arguments_still_answer_json(capsys):
    code, doc = run_json(capsys, "measure", "19", "abc")
    assert code == cli.EXIT_CODES["usage"] and doc["error"]["kind"] == "usage"
    code, doc = run_json(capsys, "no-such-command")
    assert code == cli.EXIT_CODES["usage"] and not doc["ok"]


def test_unknown_module_is_usage_error(capsys):
    code, doc = run_json(capsys, "kwp-dtc", "99")
    assert code == cli.EXIT_CODES["usage"] and not doc["ok"]
    assert doc["error"]["kind"] == "usage"


def test_write_in_json_mode_only_plans_then_apply_writes_once(capsys):
    code, doc = run_json(capsys, "feature", "leaving-home", "on")
    assert code == 0 and doc["data"]["status"] == "planned"
    plan = doc["data"]["plan"]
    assert plan["old"] == cli.VCDS_TRUTH["09_coding"].hex().upper()
    assert plan["old"] != plan["new"] and plan["argv"] == ["feature", "leaving-home", "on"]

    code, doc = run_json(capsys, "apply", plan["id"])
    assert code == 0, doc["error"]
    assert doc["data"]["result"]["status"] == "written"

    code, doc = run_json(capsys, "apply", plan["id"])
    assert code == cli.EXIT_CODES["refused"] and "已經執行過" in doc["error"]["message"]


def test_plan_from_simulator_cannot_be_applied_to_the_car(capsys):
    # The simulator holds the same 09 coding as the car, so only the target check stops this.
    code, doc = run_json(capsys, "feature", "leaving-home", "on")
    plan_id = doc["data"]["plan"]["id"]
    assert doc["data"]["plan"]["target"] == "sim"
    with pytest.raises(SystemExit) as e:
        cli.main(["--elm", "COM99", "--json", "apply", plan_id])
    doc = json.loads(capsys.readouterr().out)
    assert e.value.code == cli.EXIT_CODES["refused"] and "sim" in doc["error"]["message"]


def test_apply_refuses_when_car_changed():
    plan = {"id": "x", "old": "AA", "new": "BB"}
    with pytest.raises(plans.PlanMismatch):
        plans.applier(plan)(b"\xAC", b"\xBB")
    assert plans.applier(plan)(b"\xAA", b"\xBB") is True


def test_blocked_module_refused_even_in_plan_mode(capsys):
    code, doc = run_json(capsys, "write-did", "01", "0600", "00")
    assert code == cli.EXIT_CODES["refused"] and "安全相關模組" in doc["error"]["message"]


def test_commands_catalog_marks_writes(capsys):
    code, doc = run_json(capsys, "commands")
    cmds = {c["name"]: c for c in doc["data"]["commands"]}
    assert cmds["feature"]["writes_to_car"] and not cmds["autoscan"]["writes_to_car"]
    assert {"autoscan", "measure", "apply", "verify"} <= set(cmds)
    assert doc["data"]["exit_codes"]["refused"] == 4


def test_clear_tp20_dtcs_plans_then_clears_once(capsys):
    # TP2.0 modules (the rear doors) clear with KWP 14 FF 00, through the same plan -> apply.
    code, doc = run_json(capsys, "clear-dtc", "72")
    assert code == 0 and doc["data"]["status"] == "planned"
    plan = doc["data"]["plan"]
    assert plan["old"] == [{"code": cli.VCDS_TRUTH["door_dtc"], "status": 0x28}] and plan["new"] is None

    code, doc = run_json(capsys, "apply", plan["id"])
    assert code == 0, doc["error"]
    assert doc["data"]["result"]["status"] == "cleared"
    code, doc = run_json(capsys, "apply", plan["id"])
    assert code == cli.EXIT_CODES["refused"]


def test_values_reads_the_confirmed_odometer(capsys):
    code, doc = run_json(capsys, "values")
    assert code == 0
    odo = next(v for v in doc["data"]["values"] if v["name"] == "總里程")
    assert odo["value"] == 306470 and odo["unit"] == "km" and odo["module"] == "17"
    engine = {v["did"]: v["value"] for v in doc["data"]["values"] if v["module"] == "01"}
    assert engine == {"155A": 104.15625, "2006": 83.88, "2029": 1054.64, "202A": 971.12, "20A1": 26,
                      "2206": 6, "15AA": 0}


def test_candidates_reads_raw_values_and_errors(capsys):
    code, doc = run_json(capsys, "candidates")
    assert code == 0
    odo = next(r for r in doc["data"]["uds"]["17"] if r["did"] == "2200")
    assert "error" in odo  # the simulator does not know it: reported, not fatal
    assert {r["group"] for r in doc["data"]["tp20"]["02"]} >= {"7A", "C8"}


def test_measure_names_values_from_the_profile(capsys):
    code, doc = run_json(capsys, "measure", "02", "122")
    assert code == 0
    v = doc["data"]["groups"][0]["values"]
    assert [x["value"] for x in v[:3]] == [206, 130, 90] and v[0]["unit"] == "s" and v[0]["verified"]
    assert v[1]["name"] == "離合器 2 工作時間 301-350 °C" and "name" not in v[3]


def test_measure_dsg_clutch_max_temperature(capsys):
    code, doc = run_json(capsys, "measure", "02", "118")
    assert code == 0
    v = doc["data"]["groups"][0]["values"][3]
    assert v["value"] == 451 and v["unit"] == "°C" and v["verified"]
    assert v["name"] == "離合器 2 曾達最高溫度"


def test_dtc_details_returns_raw_freeze_frame(capsys, monkeypatch):
    from yeti_diag import sim
    real = sim.default_ecus

    def ecus():
        e = real()
        e["17"]["dtcs"] = [(0xC10000, 0x09)]
        e["17"]["snapshots"] = {0xC10000: bytes.fromhex("01022002112E")}
        return e
    monkeypatch.setattr(sim, "default_ecus", ecus)
    code, doc = run_json(capsys, "dtc", "17", "--details")
    assert code == 0
    d = doc["data"]["dtcs"][0]
    assert d["code"] == "U0100-00" and d["details"] == {"snapshot": "01022002112E", "extended": None}


def run_elm_json(capsys, monkeypatch, elm_kwargs, *argv):
    """Like run_json, over the fake ELM made with elm_kwargs (another adapter)."""
    from yeti_diag.sim import FakeElmSerial
    monkeypatch.setattr(cli, "FakeElmSerial", lambda: FakeElmSerial(**elm_kwargs))
    with pytest.raises(SystemExit) as e:
        cli.main(["--sim-elm", "--json", *argv])
    captured = capsys.readouterr()
    return e.value.code, json.loads(captured.out), captured.err


VGATE = {"version": "ELM327 v2.1", "atrv_offset": -1.1}


def test_low_adapter_voltage_uses_the_engines_reading(capsys, monkeypatch):
    # Vgate 2026-11-04: ATRV 11.4 V while the engine said 12.42 V.
    code, doc, err = run_elm_json(capsys, monkeypatch, {**VGATE, "voltage": 12.4}, "voltage")
    assert code == 0 and doc["data"]["voltage"] == pytest.approx(12.4)
    assert "引擎模組" in err
    code, doc, err = run_elm_json(capsys, monkeypatch, {"voltage": 12.6}, "voltage")
    assert doc["data"]["voltage"] == pytest.approx(12.6) and "引擎模組" not in err  # agreeing: ATRV


def test_fast_mode_is_dropped_on_a_v2_adapter(capsys, monkeypatch):
    code, doc, err = run_elm_json(capsys, monkeypatch, VGATE, "--tp20-fast", "measure", "02", "122")
    assert code == 0 and doc["data"]["groups"][0]["values"][0]["value"] == 206
    assert "--tp20-fast" in err


def test_write_plan_lists_changed_bytes(capsys):
    code, doc = run_json(capsys, "--plan", "feature", "brake-flash", "off")
    assert code == 0
    assert doc["data"]["plan"]["changes"] == [{"byte": 16, "old": "4F", "new": "4B", "bits": "00000100 關"}]


def test_auto_port_tries_the_last_one_first_and_skips_incoming_ports(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS

    from serial.tools import list_ports

    from yeti_diag import elm327
    ports = [NS(device="COM3", description="Intel(R) Active Management Technology - SOL (COM3)", hwid="PCI"),
             NS(device="COM8", description="BT", hwid=r"BTHENUM\X_LOCALMFG&0000\7&0&000000000000_0"),
             NS(device="COM9", description="BT", hwid=r"BTHENUM\X_LOCALMFG&0002\7&0&0010CC4F3603_C"),
             NS(device="COM10", description="BT", hwid=r"BTHENUM\X_LOCALMFG&0002\7&0&AABBCC112233_C")]
    monkeypatch.setattr(list_ports, "comports", lambda: ports)
    tried = []
    monkeypatch.setattr(elm327, "probe_port", lambda port: (tried.append(port), port == "COM10")[1])
    cache = tmp_path / "elm_port.txt"
    cache.write_text("COM9", encoding="utf-8")  # last time the old adapter; now the Vgate is in
    assert elm327.find_port(cache, say=lambda *a: None) == "COM10"
    assert tried == ["COM9", "COM10"]  # cached first; Intel AMT and incoming BT ports never tried
    assert cache.read_text(encoding="utf-8") == "COM10"
    tried.clear()
    assert elm327.find_port(cache, say=lambda *a: None) == "COM10" and tried == ["COM10"]
    # --elm auto keeps the probed port open (reopening a Bluetooth port at once failed on COM9)
    opened = object()
    monkeypatch.setattr(elm327, "probe_port", lambda port, keep=False: opened if keep and port == "COM10" else False)
    assert elm327.find_port(cache, say=lambda *a: None, keep_open=True) == ("COM10", opened)


def test_verify_driving_skips_safety_modules(capsys):
    code, doc = run_json(capsys, "verify", "--driving")
    names = " ".join(c["name"] for c in doc["data"]["checks"])
    assert doc["data"]["skipped"] == ["03", "09", "15", "44"]
    for addr in ("03", "09", "15", "44"):
        assert f" {addr} " not in f" {names} "
    assert "UDS 17" in names and "TP2.0 19" in names
