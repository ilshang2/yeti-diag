"""Oil change reminder: a record on the phone / PC, shared by the Android app and the GUI."""
import pytest

from yeti_diag import service


def test_empty_state_asks_for_a_reading_and_a_record(tmp_path):
    doc = service.view(service.load(tmp_path / "missing.json"))
    assert doc["level"] == "none" and "left" not in doc and service.summary_line(doc) == ""
    assert "還沒讀過里程" in doc["message"] and "還沒有換機油的記錄" in doc["message"]


@pytest.mark.parametrize("since, level, line", [
    (1000, "fine", "換機油：還有 6,500 km"),
    (7000, "soon", "換機油：還有 500 km"),
    (7500, "soon", "換機油：還有 0 km"),
    (7600, "over", "換機油：已超過 100 km"),
])
def test_distance_left(tmp_path, since, level, line):
    path = tmp_path / "service.json"
    state = service.record_oil(service.set_odometer({}, 300000 + since), "300000", 7500)
    service.save(path, state)
    doc = service.view(service.load(path))  # survives a restart
    assert doc["level"] == level and service.summary_line(doc) == line
    assert "下次換機油：307,500 km" in doc["message"]


@pytest.mark.parametrize("odometer, interval", [("", 7500), ("abc", 7500), (0, 7500), (300000, 1234), (300000, None)])
def test_bad_input_is_refused_and_keeps_the_old_record(odometer, interval):
    state = service.record_oil({}, 300000, 5000)
    with pytest.raises(ValueError):
        service.record_oil(state, odometer, interval)
    assert state["oil"]["interval"] == 5000


def test_odometer_among_values():
    rows = [{"name": "油箱油量", "value": 46}, {"name": "總里程", "value": 307170, "unit": "km"}]
    assert service.odometer_in(rows) == 307170
    assert service.odometer_in([{"name": "總里程", "error": "沒有回應"}]) is None
