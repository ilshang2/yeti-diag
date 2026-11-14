"""`--elm android`: the APK's Bluetooth link, simulated by a fake ELM327 behind a BtLink-like object."""
import importlib.util
import json
import pathlib

import pytest

from yeti_diag import android_serial, cli, plans, writer
from yeti_diag.sim import FakeElmSerial

ROOT = pathlib.Path(__file__).resolve().parent.parent


class FakeLink:
    """Same methods as the Kotlin BtLink."""

    def __init__(self):
        self.elm = FakeElmSerial()
        self.cleared = 0

    def read(self, n, timeout_ms):
        return self.elm.read(n)

    def write(self, data):
        assert isinstance(data, bytes)
        self.elm.write(data)

    def clear(self):
        self.cleared += 1
        self.elm.out = b""


@pytest.fixture
def link(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPORT_DIR", tmp_path / "reports")
    fake = FakeLink()
    monkeypatch.setattr(android_serial, "LINK", fake)
    return fake


def test_voltage_over_the_android_link(link, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--elm", "android", "--json", "voltage"])
    assert e.value.code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] and link.cleared > 0


def test_no_link_is_a_connection_error(monkeypatch, capsys):
    monkeypatch.setattr(android_serial, "LINK", None)
    with pytest.raises(SystemExit) as e:
        cli.main(["--elm", "android", "--json", "voltage"])
    assert e.value.code == cli.EXIT_CODES["connection"]


def load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = ROOT / "android" / "app" / "src" / "main" / "python" / "yeti_app.py"
    spec = importlib.util.spec_from_file_location("yeti_app", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sink:
    def __init__(self):
        self.text = ""

    def append(self, s):
        self.text += s


def test_app_runs_a_read_and_streams_its_output(link, tmp_path, monkeypatch):
    app = load_app(tmp_path, monkeypatch)
    sink = Sink()
    assert app.run(link, "voltage", sink) == 0
    assert "V" in sink.text


@pytest.mark.parametrize("cmd", ["clear-dtc 01", "feature drl off", "apply abc", "adapt-write 09 31 4"])
def test_command_field_refuses_every_write(link, tmp_path, monkeypatch, cmd):
    app = load_app(tmp_path, monkeypatch)
    sink = Sink()
    written = []
    monkeypatch.setattr(link, "write", written.append)
    assert app.run(link, cmd, sink) == 4
    assert "只能讀取" in sink.text and not written


@pytest.fixture
def app(link, tmp_path, monkeypatch):
    """The bridge with a TP2.0 address map already found (the APK bundles one)."""
    monkeypatch.setattr(plans, "PLAN_DIR", tmp_path / "plans")
    monkeypatch.setattr(writer, "BACKUP_DIR", tmp_path / "backups")
    mod = load_app(tmp_path, monkeypatch)
    assert mod.run(link, "tp20-probe", Sink()) == 0
    return mod


def feature(app, link, key):
    doc = json.loads(app.features(link, Sink()))
    assert doc["ok"]
    return next(f for f in doc["features"] if f["key"] == key)


def set_dtcs(link, address, dtcs):
    """Put stored fault codes [(code, status)] into a simulated UDS module."""
    from yeti_diag.vehicle import UDS_MODULES
    link.elm.models[UDS_MODULES[address].tx_id][1].data["dtcs"] = list(dtcs)


def test_scan_lists_modules_with_ready_lines(app, link):
    doc = json.loads(app.scan(link, Sink()))
    assert doc["ok"] and "故障碼" in doc["summary"] and doc["report"]
    door = next(m for m in doc["modules"] if m["address"] == "62")
    assert door["count"] == 1 and "02115" in door["dtcs"][0] and door["clearable"]
    assert not next(m for m in doc["modules"] if m["address"] == "17")["clearable"]  # no fault codes


def test_clear_several_modules_with_one_approval(app, link):
    """UDS and TP2.0 modules together: one plan each, applied in turn; modules without codes are skipped."""
    set_dtcs(link, "08", [(0x90ABF0, 0x08)])
    planned = json.loads(app.plan(link, "clear-dtc 08 17 62 72", Sink()))
    assert planned["ok"] and planned["title"] == "清除 3 個模組的故障碼"
    assert "B10AB-F0" in planned["message"] and "02115" in planned["message"] and "17 " not in planned["message"]
    assert [p.split(":")[0] for p in planned["plan_id"].split(",")] == ["08", "62", "72"]
    done = json.loads(app.apply(link, planned["plan_id"], Sink()))
    assert done["ok"] and done["message"].count("已清除") == 3
    scan = json.loads(app.scan(link, Sink()))
    assert all(m["count"] == 0 for m in scan["modules"])
    again = json.loads(app.apply(link, planned["plan_id"], Sink()))  # each plan only once
    assert not again["ok"]


def test_clear_plan_warns_before_clearing_the_engine(app, link):
    set_dtcs(link, "01", [(0x030000, 0x08)])
    planned = json.loads(app.plan(link, "clear-dtc 01", Sink()))
    assert planned["ok"] and "排放自我檢測" in planned["message"]


def test_nothing_to_clear(app, link):
    doc = json.loads(app.plan(link, "clear-dtc 17", Sink()))
    assert doc["ok"] and doc["plan_id"] is None


def test_feature_write_is_plan_then_apply(app, link):
    before = feature(app, link, "leaving-home")["on"]
    state = "off" if before else "on"
    planned = json.loads(app.plan(link, f"feature leaving-home {state}", Sink()))
    assert planned["ok"] and planned["plan_id"] and "Byte 17" in planned["message"]
    assert feature(app, link, "leaving-home")["on"] == before  # the plan wrote nothing
    done = json.loads(app.apply(link, planned["plan_id"], Sink()))
    assert done["ok"] and "寫入成功" in done["message"]
    assert feature(app, link, "leaving-home")["on"] != before
    again = json.loads(app.apply(link, planned["plan_id"], Sink()))  # one plan, one write
    assert not again["ok"] and "已經執行過" in again["message"]


def test_plan_for_the_current_state_needs_no_write(app, link):
    state = "on" if feature(app, link, "leaving-home")["on"] else "off"
    doc = json.loads(app.plan(link, f"feature leaving-home {state}", Sink()))
    assert doc["ok"] and doc["plan_id"] is None


def test_clear_dtc_on_an_old_module(app, link):
    planned = json.loads(app.plan(link, "clear-dtc 62", Sink()))
    assert planned["ok"] and "02115" in planned["message"]
    done = json.loads(app.apply(link, planned["plan_id"], Sink()))
    assert done["ok"] and "已清除" in done["message"]


@pytest.mark.parametrize("cmd", ["clear-dtc", "clear-dtc 99", "adapt-write 09 31 4", "write-did 17 0600 520900",
                                 "coding-rewrite", "apply abc", "voltage"])
def test_plan_offers_only_writes_proven_on_the_car(app, link, monkeypatch, cmd):
    written = []
    monkeypatch.setattr(link, "write", written.append)
    doc = json.loads(app.plan(link, cmd, Sink()))
    assert not doc["ok"] and not written


class Stop:
    def __init__(self, rounds):
        self.left = rounds * 20

    def get(self):
        self.left -= 1
        return self.left < 0


class LiveSink(Sink):
    def __init__(self):
        super().__init__()
        self.rounds = []

    def update(self, text):
        self.rounds.append(json.loads(text))


def test_live_reads_engine_values_until_stopped(app, link):
    sink = LiveSink()
    assert json.loads(app.live(link, sink, Stop(2)))["ok"]
    assert sink.rounds and {"name", "value", "unit"} <= set(sink.rounds[0][0])


def test_a_dropped_link_is_a_connection_error(link, monkeypatch, capsys):
    def dead(*args):
        raise RuntimeError("藍牙連線已中斷")  # stands in for java.io.IOException

    monkeypatch.setattr(link, "write", dead)
    with pytest.raises(SystemExit) as e:
        cli.main(["--elm", "android", "--json", "voltage"])
    assert e.value.code == cli.EXIT_CODES["connection"]
    assert "藍牙連線已中斷" in capsys.readouterr().out


def test_oil_change_reminder_is_kept_on_the_phone(app, link, monkeypatch):
    assert json.loads(app.service_status())["level"] == "none"
    read = json.loads(app.service_read(link, Sink()))
    assert read["ok"] and read["odometer"] > 0 and "還沒有換機油的記錄" in read["message"]
    written = []
    monkeypatch.setattr(link, "write", written.append)  # recording never talks to the car
    done = json.loads(app.service_done(str(read["odometer"] - 7100), 7500))
    assert done["ok"] and done["level"] == "soon" and done["left"] == 400 and not written
    assert json.loads(app.service_done(read["odometer"] - 8000, 7500))["level"] == "over"
    assert json.loads(app.service_done(read["odometer"], 10000))["level"] == "fine"
    assert json.loads(app.service_status())["oil"]["interval"] == 10000  # survives a restart
    for bad in (("", 7500), ("abc", 7500), (300000, 1234), (300000, -1)):
        assert not json.loads(app.service_done(*bad))["ok"]


def test_scan_updates_the_odometer_and_mentions_the_oil_change(app, link):
    first = json.loads(app.scan(link, Sink()))
    assert "換機油" not in first["summary"]
    km = json.loads(app.service_status())["odometer"]
    assert km > 0
    app.service_done(km - 1000, 5000)
    assert "換機油：還有 4,000 km" in json.loads(app.scan(link, Sink()))["summary"]
