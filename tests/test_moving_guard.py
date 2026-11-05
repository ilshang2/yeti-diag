"""Moving guard: no TP2.0 module and no write while the car moves (2026-11-06 ABS/ESC flash)."""
import pytest

from yeti_diag import cli


def run(monkeypatch, speed, *argv):
    monkeypatch.setattr(cli.Transport, "vehicle_speed", lambda self: speed)
    args = cli.build_parser().parse_args(["--sim", *argv])
    return cli.run(args)


def test_tp20_refused_while_moving(monkeypatch):
    kind, data, error = run(monkeypatch, 60, "kwp-dtc", "0x03")
    assert kind == "refused"
    assert "60 km/h" in error["message"]


def test_write_refused_while_moving(monkeypatch):
    kind, _, error = run(monkeypatch, 40, "--yes", "write-did", "17", "0600", "520900")
    assert kind == "refused"
    assert "40 km/h" in error["message"]


def test_engine_obd_allowed_while_moving(monkeypatch):
    kind, data, error = run(monkeypatch, 80, "obd")
    assert kind == "ok", error


def test_parked_allows_tp20(monkeypatch):
    kind, _, error = run(monkeypatch, 0, "kwp-dtc", "0x03")
    assert kind != "refused", error


def test_allow_moving_flag(monkeypatch):
    kind, _, error = run(monkeypatch, 60, "--allow-moving", "kwp-dtc", "0x03")
    assert kind != "refused", error
