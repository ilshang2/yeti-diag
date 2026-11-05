"""12 V lead-acid battery checks from voltage alone (this car has no 61 battery manager,
so there is no state of charge / health from the car itself).

Thresholds are the usual workshop rules of thumb for a 12 V flooded / EFB battery at
about 25 °C; they are estimates, not a load tester.
"""

# Open-circuit (resting) voltage -> state of charge. Only valid after the car has been
# off for a few hours; right after driving the surface charge reads too high.
REST_SOC = [(11.89, 0), (12.06, 25), (12.24, 50), (12.45, 75), (12.65, 100)]

CRANK_GOOD = 10.0      # lowest voltage while cranking at or above this: healthy
CRANK_WEAK = 9.6       # below this the battery (or its connections) should be replaced
CHARGE_LOW, CHARGE_HIGH = 13.5, 14.8
CRANK_DROP = 1.0       # a dip of at least this much below the start level = cranking seen
RUNNING = 13.2         # above this after the dip = alternator charging, engine running
INRUSH_S = 0.1         # first part of the dip that is the starter's inrush, not graded
SHORT_CRANK = 0.8      # engine caught this fast (warm): little sustained load, result optimistic


def resting_soc(v):
    """Estimated state of charge (0-100 %) from a resting voltage."""
    if v <= REST_SOC[0][0]:
        return 0
    for (v1, s1), (v2, s2) in zip(REST_SOC, REST_SOC[1:]):
        if v <= v2:
            return round(s1 + (v - v1) / (v2 - v1) * (s2 - s1))
    return 100


def rest_verdict(v):
    soc = resting_soc(v)
    if soc >= 75:
        text = "電量充足"
    elif soc >= 50:
        text = "電量偏低，建議開一段長路或用充電器充飽"
    else:
        text = "電量不足，建議充電後再測；充飽後靜置電壓仍低就是電瓶老化"
    return {"voltage": v, "soc": soc, "text": text}


def charging_verdict(v):
    if v < CHARGE_LOW:
        text = f"充電電壓偏低 (< {CHARGE_LOW} V)：檢查發電機、皮帶、接線"
        ok = False
    elif v > CHARGE_HIGH:
        text = f"充電電壓偏高 (> {CHARGE_HIGH} V)：發電機調壓器可能有問題"
        ok = False
    else:
        text = "充電正常"
        ok = True
    return {"voltage": v, "ok": ok, "text": text}


def analyze_crank(samples):
    """samples: [(seconds, volts)] recorded while the engine is started.
    Returns the voltage before cranking, the lowest point, how long it stayed down and
    whether the alternator came in afterwards."""
    if len(samples) < 3:
        return {"detected": False, "text": "樣本太少，沒辦法分析"}
    lead = [v for _, v in samples[:5]]
    before = sorted(lead)[len(lead) // 2]
    t_min, v_min = min(samples, key=lambda s: s[1])
    if before - v_min < CRANK_DROP:
        return {"detected": False, "before": before, "min": v_min,
                "text": "沒偵測到啟動 (電壓沒有明顯下降)。錄音開始後再轉鑰匙發動"}
    low = [(t, v) for t, v in samples if v < before - CRANK_DROP / 2]
    crank_s = round(low[-1][0] - low[0][0], 2) if low else 0.0
    after = [v for t, v in samples if t > t_min + 2]
    running = max(after) if after else None
    # The starter's inrush current gives a dip of a few tens of ms (9.2 V for one sample,
    # 10.0 V at +63 ms, 2026-11-04 warm start). Grade the sustained cranking voltage.
    inrush_min = v_min
    sustained = [v for t, v in low if t > low[0][0] + INRUSH_S]
    if sustained:
        v_min = min(sustained)
    if v_min >= CRANK_GOOD:
        grade, text = "good", f"啟動時最低 {v_min:.2f} V (≥ {CRANK_GOOD} V)：電瓶健康"
    elif v_min >= CRANK_WEAK:
        grade, text = "weak", f"啟動時最低 {v_min:.2f} V：電瓶開始老化，天冷可能發不動，建議近期檢查"
    else:
        grade, text = "replace", f"啟動時最低 {v_min:.2f} V (< {CRANK_WEAK} V)：建議更換電瓶 (先確認樁頭沒鬆、沒鏽)"
    rate = len(samples) / (samples[-1][0] - samples[0][0]) if samples[-1][0] > samples[0][0] else 0
    notes = []
    if rate < 8:
        notes.append(f"取樣只有每秒 {rate:.0f} 次，最低點可能沒抓到 (實際可能更低)")
    if crank_s < SHORT_CRANK:
        notes.append("引擎很快就發動 (熱車)，電瓶負載時間短，結果偏樂觀；冷車 (放一整晚) 再測比較準")
    note = "；".join(notes) or None
    return {"detected": True, "before": before, "min": round(v_min, 2), "inrush_min": round(inrush_min, 2),
            "min_at": round(t_min, 2),
            "crank_seconds": crank_s, "after": running,
            "charging": running is not None and running >= RUNNING,
            "grade": grade, "text": text, "sample_rate": round(rate, 1), "note": note}
