"""OBD-II Mode $06 misfire monitors (SAE J1979 record layout)."""
import json

import pytest

from yeti_diag import cli, uds


def test_record_layout():
    # MID A4 (cylinder 3), TID 0C, UASID 24 (counts): value 30, min 0, max FFFF
    rec = uds.mode06_records(bytes.fromhex("A40C24001E0000FFFF"))[0]
    assert rec["mid"] == "A4" and rec["tid"] == "0C" and rec["value"] == 30 and rec["unit"] == "次"


def test_misfire_command_on_simulator(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--sim", "--json", "misfire"])
    doc = json.loads(capsys.readouterr().out)
    assert e.value.code == 0 and doc["ok"], doc
    assert doc["data"]["worst"] == "第 3 缸"
    names = {r["name"] for r in doc["data"]["misfire"]}
    assert {"第 1 缸", "第 4 缸"} <= names and "全部汽缸" not in names  # A1 unsupported like the car


def test_mode06_is_a_read_only_service():
    assert 0x06 in uds.READ_ONLY_SERVICES


def test_cluster_staging_short_coding_write(capsys):
    """Needle sweep = flip byte1 bit0 of the 17 short coding (52 08 00 -> 52 09 00).
    Checks the whole UDS coding-write path the car will use: part number, 2E 0600, read-back."""
    import pytest
    with pytest.raises(SystemExit) as e:
        cli.main(["--sim", "--yes", "--json", "write-did", "17", "0600", "520900"])
    doc = json.loads(capsys.readouterr().out)
    assert e.value.code == 0 and doc["ok"], doc
    body = json.dumps(doc, ensure_ascii=False)
    assert "written" in body and "520900" in body and "520800" in body


def test_cluster_coding_write_rejects_non_writable_did(capsys):
    """Writing a DID the module does not accept must fail cleanly, not change anything."""
    import pytest
    with pytest.raises(SystemExit):
        cli.main(["--sim", "--yes", "--json", "write-did", "17", "F189", "1234"])
    doc = json.loads(capsys.readouterr().out)
    assert not doc["ok"]
