"""VCDS-style plain-text label files (user supplied). The contents below are made up for
the tests; no real label file is shipped or copied."""
import json

from yeti_diag import cli, lbl, measure

SAMPLE = """;
; made-up test labels
;
001,0,Group title (ignored)
001,1,Speed,Front Left,Range: 0...250 km/h
001,2,Speed,Front Right
002,1,Pressure,,Spec: 1.0\\n...2.0 bar
003,1,
C00,Coding line (ignored)
"""


def test_parse_groups_fields_and_detail():
    g = lbl.parse(SAMPLE)
    assert g[1]["1"] == {"name": "Speed Front Left", "detail": "Range: 0...250 km/h"}
    assert g[1]["2"] == {"name": "Speed Front Right"}
    assert g[2]["1"]["detail"] == "Spec: 1.0\n...2.0 bar"
    assert 3 not in g  # empty name skipped
    assert "0" not in g[1]  # field 0 is a title, not a value


def test_part_dashed():
    assert lbl.part_dashed("1K0907379BM") == "1K0-907-379-BM"
    assert lbl.part_dashed("1K0-907-379") == "1K0-907-379"


def test_redirect_with_wildcards(tmp_path):
    (tmp_path / "00-03.lbl").write_text("; redirects\nREDIRECT,MY-ABS.LBL,1K0-907-379-??\n", encoding="cp1252")
    (tmp_path / "MY-ABS.lbl").write_text(SAMPLE, encoding="cp1252")
    labels, note = lbl.load(tmp_path, "1K0907379BM")
    assert labels[1]["1"]["name"] == "Speed Front Left"
    assert "MY-ABS.lbl" in note


def test_file_named_after_part(tmp_path):
    (tmp_path / "1K0-907-379.lbl").write_text(SAMPLE, encoding="cp1252")
    labels, _ = lbl.load(tmp_path, "1K0907379BM")
    assert labels[2]["1"]["name"] == "Pressure"


def test_encrypted_clb_is_reported_not_read(tmp_path):
    (tmp_path / "1K0-907-379-BM.clb").write_bytes(b"\x00\x01binary")
    labels, note = lbl.load(tmp_path, "1K0907379BM")
    assert labels == {} and ".clb" in note


def test_missing_folder_and_part(tmp_path):
    assert lbl.load(tmp_path / "nope", "1K0907379BM")[0] == {}
    assert "找不到" in lbl.load(tmp_path, "1K0907379BM")[1]


def test_checked_names_win_and_file_names_are_marked():
    merged = lbl.merge({"1": {"name": "車速", "checked": "car"}, "_note": "x"},
                       {"1": {"name": "Speed"}, "2": {"name": "Pressure", "detail": "d"}})
    assert merged["1"]["name"] == "車速" and merged["1"]["source"] == "profile"
    assert merged["2"]["source"] == "label file"
    values = measure.label([{"value": 1}, {"value": 2}], merged)
    assert "name_source" not in values[0]
    assert values[1] == {"value": 2, "name": "Pressure", "name_source": "label file", "name_detail": "d"}


def test_measure_uses_label_folder_on_simulator(tmp_path, capsys):
    # the simulator's 19 gateway (7N0 907 530) has a made-up group 1
    (tmp_path / "7N0-907-530.lbl").write_text("001,1,Made Up A\n001,2,Made Up B\n001,3,Made Up C\n001,4,Made Up D\n",
                                              encoding="cp1252")
    argv = ["--sim", "--json", "--labels", str(tmp_path), "measure", "19", "1"]
    try:
        cli.main(["--sim", "tp20-probe"])
    except SystemExit:
        pass
    capsys.readouterr()
    try:
        cli.main(argv)
    except SystemExit:
        pass
    doc = json.loads(capsys.readouterr().out)
    values = doc["data"]["groups"][0]["values"]
    from_file = [v for v in values if v.get("name_source") == "label file"]
    assert from_file and all(v["name"].startswith("Made Up") for v in from_file)
