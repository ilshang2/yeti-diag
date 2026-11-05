"""Fault code text for VAG 5-digit (KWP) codes and their status byte.

Written in our own words from this car's own readings, never copied from VCDS
label files (Ross-Tech's work). Each entry says where it was confirmed. Unknown
codes stay unknown: better "未知" than a guess an AI would repeat as fact.
Add entries as they are confirmed on a car, with the source.
"""

# code -> (中文說明, English, 來源)
VAG_DTC = {
    2115: ("中控鎖 門鎖單元", "Central locking: locking unit", "本車 62/72，2026-11-03 VCDS 對照"),
    1305: ("影音娛樂資料匯流排", "Infotainment data bus", "本車 19，2026-11-03 VCDS 對照"),
}

# Low 5 bits of the KWP status byte: the failure type ("elaboration").
# 0x28 on this car's doors = VCDS "008 Implausible Signal - Intermittent".
VAG_SYMPTOM = {
    4: ("沒有訊號/通訊", "No signal/communication", "本車 19 01305 VCDS 對照"),
    8: ("訊號不合理", "Implausible signal", "本車 62/72 02115 VCDS 對照 (狀態 0x28)"),
}

# Bits 5-6: storage state (ISO 14230 statusOfDTC). On this car 2026-11-03 18:22 the
# right rear door read 0x28 (state 1, VCDS "Intermittent") while the left read 0x68
# (state 3) for the same 02115.
STORAGE_STATE = {
    0: ("未儲存", "not stored", "ISO 14230"),
    1: ("間歇性 (讀取當下不存在)", "intermittent, not present now", "VCDS 對照 (0x28)"),
    2: ("未定義", "undefined", "ISO 14230"),
    3: ("目前存在", "present now", "ISO 14230，未與 VCDS 對照"),
}
LAMP_BIT = 0x80  # not yet seen on this car


def vag_dtc(code, status):
    """Structured view of one KWP fault code (dict, JSON-ready)."""
    name = VAG_DTC.get(code)
    symptom_no = status & 0x1F
    symptom = VAG_SYMPTOM.get(symptom_no)
    state = (status >> 5) & 0x03
    return {
        "code": f"{code:05d}",
        "status": f"{status:02X}",
        "text": name[0] if name else None,
        "text_en": name[1] if name else None,
        "symptom": f"{symptom_no:03d}",
        "symptom_text": symptom[0] if symptom else None,
        "symptom_text_en": symptom[1] if symptom else None,
        "state": STORAGE_STATE[state][1],
        "state_text": STORAGE_STATE[state][0],
        "intermittent": state == 1,
        "present_now": state == 3,
        "warning_lamp": bool(status & LAMP_BIT),
        "known": bool(name),
    }


def vag_dtc_line(d):
    """One-line human text for a vag_dtc() dict."""
    text = d["text"] or "未知代碼 (說明表還沒有)"
    symptom = d["symptom_text"] or "未知類型"
    lamp = "，要求亮警示燈" if d["warning_lamp"] else ""
    return f"{d['code']} {text} - {d['symptom']} {symptom}，{d['state_text']}{lamp} (狀態 {d['status']})"
