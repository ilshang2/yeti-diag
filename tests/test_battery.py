"""Battery check: resting charge, charging voltage, cranking dip (pure analysis + CLI)."""
import json

import pytest

from yeti_diag import battery, cli
from yeti_diag.sim import FakeElmSerial, default_ecus
from test_cli import isolated_dirs  # noqa: F401 - autouse fixture: reports to a temp dir


def curve(drop, before=12.5, after=14.2, step=0.05):
    """[(s, V)]: 1 s before, 1.5 s cranking at `drop`, then charging."""
    out, t = [], 0.0
    while t < 6:
        out.append((round(t, 2), before if t < 1 else drop if t < 2.5 else after))
        t += step
    return out


def test_resting_soc():
    assert battery.resting_soc(12.7) == 100 and battery.resting_soc(11.5) == 0
    assert battery.resting_soc(12.45) == 75 and battery.resting_soc(12.35) == pytest.approx(63, abs=1)
    assert battery.rest_verdict(12.1)["soc"] < 50


def test_charging_verdict():
    assert battery.charging_verdict(14.0)["ok"]
    assert not battery.charging_verdict(13.1)["ok"] and not battery.charging_verdict(15.2)["ok"]


@pytest.mark.parametrize("drop,grade", [(10.4, "good"), (9.8, "weak"), (8.9, "replace")])
def test_crank_grades(drop, grade):
    r = battery.analyze_crank(curve(drop))
    assert r["detected"] and r["grade"] == grade and r["min"] == drop
    assert r["before"] == 12.5 and r["charging"] and 1.3 <= r["crank_seconds"] <= 1.6
    assert r["note"] is None  # 20 samples per second


def test_crank_not_detected_and_slow_sampling():
    assert not battery.analyze_crank([(i * 0.1, 12.5) for i in range(30)])["detected"]
    slow = battery.analyze_crank(curve(10.2, step=0.4))
    assert slow["detected"] and "取樣" in slow["note"]


class CrankElm(FakeElmSerial):
    """Engine off (0 rpm); ATRV follows a start: 12.5 V, a dip to 9.8 V, then 14.2 V."""

    def __init__(self, atrv_offset=0.0):
        ecus = default_ecus()
        ecus["01"]["obd"][0x0C] = bytes([0, 0])
        super().__init__(ecus=ecus, voltage=12.5, atrv_offset=atrv_offset)
        self.reads = 0

    @property
    def voltage(self):
        self.reads += 1
        return 12.5 if self.reads < 20 else 9.8 if self.reads < 40 else 14.2

    @voltage.setter
    def voltage(self, value):
        pass


def run_battery(capsys, monkeypatch, elm, *argv):
    monkeypatch.setattr(cli, "FakeElmSerial", lambda: elm)
    with pytest.raises(SystemExit) as e:
        cli.main(["--sim-elm", "--json", "battery", *argv])
    return e.value.code, json.loads(capsys.readouterr().out)


def test_cli_crank(capsys, monkeypatch):
    code, doc = run_battery(capsys, monkeypatch, CrankElm(), "--crank", "5")
    assert code == 0, doc["error"]
    crank = doc["data"]["crank"]
    assert crank["detected"] and crank["grade"] == "weak" and crank["min"] == pytest.approx(9.8)
    assert crank["charging"] and doc["data"]["samples"]
    assert doc["data"]["report"].endswith("battery_crank_sim")


def test_cli_crank_corrects_a_low_adapter(capsys, monkeypatch):
    # Vgate-like adapter reading 1 V low: the engine's 12.5 V sets the offset
    code, doc = run_battery(capsys, monkeypatch, CrankElm(atrv_offset=-1.0), "--crank", "5")
    assert code == 0, doc["error"]
    assert doc["data"]["crank"]["min"] == pytest.approx(9.8, abs=0.06)


def test_cli_rest_and_running(capsys, monkeypatch):
    code, doc = run_battery(capsys, monkeypatch, CrankElm())
    assert code == 0 and doc["data"]["rest"]["soc"] == 81  # 12.5 V, engine off
    code, doc = run_battery(capsys, monkeypatch, FakeElmSerial(voltage=14.1), "--crank", "5")
    assert code != 0 and "熄火" in doc["error"]["message"]  # engine already running


def test_crank_inrush_spike_is_not_graded():
    # 2026-11-04 warm start, 16 samples/s: one 9.2 V sample, then 10.0, 10.4 ... back up
    s = [(i / 16, 12.3) for i in range(16)]
    s += [(1 + i / 16, v) for i, v in enumerate([9.2, 10.0, 10.4, 10.9, 10.9, 11.2, 11.3, 11.6, 11.7, 12.1])]
    s += [(2 + i / 16, 14.1) for i in range(64)]
    r = battery.analyze_crank(s)
    assert r["inrush_min"] == 9.2 and r["min"] == 10.4 and r["grade"] == "good"
    assert "熱車" in r["note"]  # caught in about 0.5 s
