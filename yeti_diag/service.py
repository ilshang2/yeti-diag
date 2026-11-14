"""Oil change reminder kept in a file on the phone / PC. Nothing here talks to the car.

Writing the interval into the instrument cluster needs its Security Access, which this
project does not implement, so the reminder is the tool's own record: the odometer at the
last oil change, the chosen interval, and the last odometer value read from the car.
"""
import datetime
import json

INTERVALS = (5000, 7500, 10000)
SOON_KM = 500


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def set_odometer(state, km):
    state["odometer"] = int(km)
    state["odometer_date"] = datetime.date.today().isoformat()
    return state


def odometer_in(values):
    """The odometer among value rows ({name, value, ...}) of `values` or a scan; None if not read."""
    return next((v.get("value") for v in values if v.get("name") == "總里程" and "error" not in v), None)


def record_oil(state, odometer, interval):
    """Record an oil change. Raises ValueError with a message for the person."""
    try:
        odometer, interval = int(odometer), int(interval)
    except (TypeError, ValueError):
        raise ValueError("里程要填數字。") from None
    if not 0 < odometer < 2_000_000 or interval not in INTERVALS:
        raise ValueError("里程或間隔不對。")
    state["oil"] = {"odometer": odometer, "interval": interval, "date": datetime.date.today().isoformat()}
    return state


def view(state):
    """What to show: text, the distance left and a level (none / fine / soon / over)."""
    oil, odometer = state.get("oil"), state.get("odometer")
    doc = {"ok": True, "odometer": odometer, "oil": oil, "level": "none", "intervals": list(INTERVALS)}
    if odometer is not None:
        lines = [f"目前里程：{odometer:,} km (讀取於 {state.get('odometer_date', '?')}；儀表模組以 10 km 為單位回報)"]
    else:
        lines = ["還沒讀過里程。連線後按「讀取里程」。"]
    if not oil:
        lines.append("還沒有換機油的記錄。換完機油後按「記錄換機油」。")
    else:
        due = oil["odometer"] + oil["interval"]
        lines += ["", f"上次換機油：{oil['odometer']:,} km (記錄於 {oil['date']})",
                  f"間隔：{oil['interval']:,} km", f"下次換機油：{due:,} km"]
        if odometer is not None:
            left = due - odometer
            doc["left"] = left
            if left < 0:
                doc["level"] = "over"
                lines.append(f"已經超過 {-left:,} km，該換機油了。")
            else:
                doc["level"] = "soon" if left <= SOON_KM else "fine"
                lines.append(f"還有 {left:,} km" + ("，快到了。" if doc["level"] == "soon" else "。"))
    doc["message"] = "\n".join(lines)
    return doc


def summary_line(doc):
    """One line for a scan summary, or "" when there is nothing to say yet."""
    if "left" not in doc:
        return ""
    left = doc["left"]
    return f"換機油：已超過 {-left:,} km" if left < 0 else f"換機油：還有 {left:,} km"
