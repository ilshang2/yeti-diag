"""Shared test setup."""
import pytest

from yeti_diag import cli


@pytest.fixture(autouse=True)
def quick_tp20_probe(monkeypatch):
    # The simulator answers a TP2.0 setup within milliseconds; on the car the quick
    # pass waits 0.3 s per address. A short wait keeps full-range probes fast in tests.
    monkeypatch.setattr(cli, "TP20_PROBE_TIMEOUT", 0.05)
