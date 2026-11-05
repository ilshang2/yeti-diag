from yeti_diag import labels, measure


def test_door_codes_as_read_on_the_car():
    # 2026-11-03 18:27: right rear 0x28 (VCDS "008 Implausible Signal - Intermittent"), left rear 0x68.
    right = labels.vag_dtc(2115, 0x28)
    assert right["text"] == "中控鎖 門鎖單元" and right["symptom"] == "008"
    assert right["intermittent"] and not right["present_now"]
    left = labels.vag_dtc(2115, 0x68)
    assert left["symptom"] == "008" and left["present_now"] and not left["intermittent"]


def test_unknown_code_stays_unknown():
    d = labels.vag_dtc(1234, 0x14)
    assert d["known"] is False and d["text"] is None and d["symptom_text"] is None
    assert "未知代碼" in labels.vag_dtc_line(d)


def test_measure_formulas():
    assert measure.decode_value(0x06, 100, 126)["value"] == 12.6
    assert measure.decode_value(0x24, 119, 179)["value"] == 306430
    # DQ200 readings matched with Car Scanner on 2026-11-04
    assert measure.decode_value(0x2C, 0x54, 0x31)["value"] == 5089
    assert measure.decode_value(0x9F, 0x9E, 0x90)["value"] == 451
    assert measure.decode_value(0x9F, 0x7A, 0x8C)["value"] == 345
    assert measure.decode_value(0x9F, 0x04, 0x80)["value"] == 26
    unknown = measure.decode_value(0xFE, 1, 2)
    assert unknown["value"] is None and unknown["raw"] == "FE 01 02"
    assert measure.decode_value(0x11, ord("O"), ord("K"))["value"] == "OK"
