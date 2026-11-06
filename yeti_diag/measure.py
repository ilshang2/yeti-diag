"""VAG measuring blocks over KWP2000: 21 <group> -> 61 <group> + 3 bytes per value.

Each value is [formula, a, b]. The formula numbers below follow publicly
documented community reverse engineering of VAG measuring values. `VERIFIED`
lists the ones checked on this car against another reading (ATRV, odometer, ...);
the others are marked unverified in every result so nobody takes them as fact.
Unknown formulas are returned raw.
"""

# formula -> (unit, function(a, b))
def _s16(a, b):
    v = a * 256 + b
    return v - 65536 if v >= 32768 else v


FORMULAS = {
    0x01: ("/min", lambda a, b: a * b / 5),
    0x02: ("%", lambda a, b: a * 0.002 * b),
    0x03: ("°", lambda a, b: a * 0.002 * b),
    0x05: ("°C", lambda a, b: a * (b - 100) * 0.1),
    0x06: ("V", lambda a, b: a * 0.001 * b),
    0x07: ("km/h", lambda a, b: a * 0.01 * b),
    0x08: ("", lambda a, b: a * 0.1 * b),
    0x0F: ("ms", lambda a, b: a * 0.01 * b),
    0x12: ("mbar", lambda a, b: a * 0.04 * b),
    0x13: ("l", lambda a, b: a * 0.01 * b),
    0x14: ("%", lambda a, b: a * (b - 128) / 128),
    0x15: ("V", lambda a, b: a * 0.001 * b),
    0x16: ("ms", lambda a, b: a * 0.001 * b),
    0x17: ("%", lambda a, b: b / 256 * a),
    0x1A: ("°C", lambda a, b: b - a),
    0x21: ("%", lambda a, b: 100 * b / a if a else 0.0),
    0x23: ("l/h", lambda a, b: a * 0.01 * b),
    0x24: ("km", lambda a, b: a * 2560 + b * 10),
    0x2F: ("ms", lambda a, b: (b - 128) * a),
    0x31: ("mg/h", lambda a, b: b / 4 * a * 0.1),
    0x33: ("mg/h", lambda a, b: (b - 128) / 255 * a),
    0x2C: ("min", lambda a, b: a * 60 + b),  # a hours + b minutes
    0x37: ("s", lambda a, b: a * b / 200),
    0x36: ("", lambda a, b: a * 256 + b),  # 16-bit count
    # 2026-11-05 ABS 03 / steering 44, one parked sample each (VCDS same screen); confirm when driving:
    0x4B: ("", lambda a, b: a * 256 + b),  # fault code
    0x51: ("°", lambda a, b: _s16(a, b) * 0.04375),  # steering angle: FE CE = -13.39
    0x52: ("m/s²", lambda a, b: _s16(a, b) * 0.01),  # lateral acceleration: 00 04 = 0.04
    0x53: ("bar", lambda a, b: _s16(a, b) * 0.01),  # brake pressure: FF F7 = -0.09
    0x67: ("V", lambda a, b: a * b / 100 + a),  # only a = 5 seen: 0.05 V steps from 5 V
    0x9F: ("°C", lambda a, b: ((b - 127) * 256 + a) / 10),  # 16-bit word b:a, offset 0x7F00
}

# Filled in as formulas are confirmed on the car: formula -> how it was checked.
VERIFIED = {
    0x10: "2026-11-05 車門 42 / 62 群組 1 / 3 與 VCDS 同畫面: 10 FF 07 = 00000111、10 0F 05 = 0101 (a 是遮罩，決定位數)",
    0x36: "2026-11-05 09 群組 16 / 32 與 VCDS 同畫面: 36 22 34 = 8756 (雨刷次數)、36 00 20 = 32 (斷電保護次數)",
    0x67: "2026-11-05 09 群組 36 / 38 與 VCDS 同畫面: 67 05 96 / 97 / 98 / 9C / B4 = 12.50 / 12.55 / 12.60 / 12.80 / 14.00 V",
    0x11: "2026-11-04 02 DSG 行駛中 (約 90 km/h)：群組 4 = 'D' / 'D'、群組 22 = 'D' / '7' (兩個 ASCII 字元)；"
          "群組 109 / 119 的 R / P / N 是離合器記錄當下的檔位，不是目前檔位",
    0x07: "2026-11-04 02 DSG 群組 8 行駛中 12 輪：07 64 57 = 87 km/h，前後引擎 OBD 車速 87-88 km/h",
    0x06: "2026-11-03 03 ABS 群組 6: 06 47 B4 = 12.78 V，同時 ATRV 12.70 V (紅火未發動)",
    0x24: "2026-11-03 02 DSG 群組 109: 24 78 5B = 308110 km，與 Car Scanner 顯示相同",
    0x37: "2026-11-03 02 DSG 群組 122: 37 C8 82 / 5A = 130 / 90 s，與 Car Scanner 顯示相同",
    0x2C: "2026-11-04 02 DSG 群組 99 / 119: 2C 54 31 = 5089 min、2C 00 1F = 31 min、2C 58 21 = 5313 min、"
          "2C 00 20 = 32 min，與 Car Scanner 顯示相同",
    0x9F: "2026-11-04 02 DSG 群組 98 / 118: 9F 7A 8C = 345 °C、9F 9E 90 = 451 °C (離合器最高溫)；"
          "群組 11: 9F 04 80 = 26 °C，與 Car Scanner 顯示相同",
}


def label(values, labels):
    """Attach names ({"1": {"name": ..., "source": ..., "detail": ...}}) to values. The
    profile's names were checked on a car; "label file" names come from the user's own
    VCDS-style label files (see lbl.py) and keep that source so nobody takes them as checked."""
    for i, v in enumerate(values, 1):
        entry = (labels or {}).get(str(i))
        if entry:
            v["name"] = entry["name"]
            if entry.get("source") == "label file":
                v["name_source"] = "label file"
                if entry.get("detail"):
                    v["name_detail"] = entry["detail"]
    return values


def decode_value(formula, a, b):
    raw = f"{formula:02X} {a:02X} {b:02X}"
    if formula == 0x10:  # switch states as bits; a is the mask and sets the width (FF: 8, 0F: 4)
        width = max(1, a.bit_length()) if a else 8
        bits = "".join("1" if b & (1 << i) else "0" for i in range(width - 1, -1, -1))
        return {"formula": f"{formula:02X}", "raw": raw, "value": bits, "unit": "bits",
                "verified": formula in VERIFIED}
    if formula == 0x11:  # two ASCII characters
        return {"formula": f"{formula:02X}", "raw": raw, "value": bytes([a, b]).decode("ascii", "replace"),
                "unit": "", "verified": formula in VERIFIED}
    if formula in FORMULAS:
        unit, fn = FORMULAS[formula]
        return {"formula": f"{formula:02X}", "raw": raw, "value": round(fn(a, b), 3), "unit": unit,
                "verified": formula in VERIFIED}
    return {"formula": f"{formula:02X}", "raw": raw, "value": None, "unit": None, "verified": False}


def parse_group(data):
    """Body of a 61 xx answer (after the group byte) -> list of decoded values."""
    return [decode_value(*data[i:i + 3]) for i in range(0, len(data) - 2, 3)]


def read_group(kwp, group):
    resp = kwp.request([0x21, group])
    return parse_group(resp[2:])


def value_text(v):
    if v["value"] is None:
        return f"? [{v['raw']}]"
    mark = "" if v["verified"] else " (公式未驗證)"
    name = f"{v['name']} " if v.get("name") else ""
    return name + f"{v['value']} {v['unit']}".strip() + mark
