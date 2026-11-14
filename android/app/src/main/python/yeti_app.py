"""Bridge between the Android app (Kotlin) and the yeti_diag command line.

Reads: `run` (any read command, text output), `scan`, `features`, `live`.
Writes: only through `plan` -> the app shows old / new and asks -> `apply`. The command
line does the checks (part number, voltage, backup, parked), sends once and reads back.
Only write kinds already proven on the car with the PC version are offered (PHONE_WRITES).
"""
import contextlib
import io
import json
import os
import pathlib
import shlex
import shutil
import traceback

HERE = pathlib.Path(__file__).resolve().parent
# The app's own files folder: the code folder is extracted from the APK and must not be written.
HOME = pathlib.Path(os.environ["HOME"]) / "yeti"
os.environ.setdefault("YETI_DIAG_HOME", str(HOME))

# feature: 09 long coding, written on the car from the PC (2026-11-06) and the phone (2026-11-14).
# clear-dtc: every module (user's decision 2026-11-14). TP2.0 clearing was proven on the car
# (72, 2026-11-03); UDS clearing (14 FF FF FF) had only run on the simulator when this was opened.
PHONE_WRITES = {"feature", "clear-dtc"}


class _Sink(io.TextIOBase):
    """stdout / stderr that forwards text to the app as it is printed."""

    def __init__(self, sink):
        self.sink = sink

    def writable(self):
        return True

    def write(self, s):
        if s:
            self.sink.append(s)
        return len(s)


def _seed():
    """First run: copy the TP2.0 address map probed on the PC, if the build bundled one."""
    (HOME / "reports").mkdir(parents=True, exist_ok=True)
    seed = HERE / "yeti_seed" / "tp20_map.json"
    dest = HOME / "reports" / "tp20_map.json"
    if seed.exists() and not dest.exists():
        shutil.copyfile(seed, dest)


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False)


def _json(link, argv, sink):
    """One command in --json mode: progress text goes to the app, the result comes back as a dict."""
    from yeti_diag import android_serial, cli

    _seed()
    android_serial.LINK = link
    result = io.StringIO()
    with contextlib.redirect_stdout(result), contextlib.redirect_stderr(_Sink(sink)):
        try:
            cli.main(["--elm", "android", "--json", *argv])
        except SystemExit:
            pass
        except Exception:  # noqa: BLE001 - shown on the phone instead of crashing the app
            return {"ok": False, "data": None, "error": {"kind": "internal", "message": traceback.format_exc()}}
    try:
        return json.loads(result.getvalue())
    except ValueError:
        return {"ok": False, "data": None,
                "error": {"kind": "internal", "message": result.getvalue() or "指令沒有回傳結果"}}


BUSY_HINT = ("中央電子 (09) 暫時拒絕診斷，這台車偶爾會這樣，模組沒有壞。"
             "把點火開關轉到 OFF、等 10 秒再轉回 ON (或發動)，然後重試。")
RETRY_HINT = "模組暫時沒有空的診斷通道 (D8)，不是故障。等幾秒再試一次就好。"


def _hint(text):
    """Plain words for the one refusal that needs the driver: 09 answers D7 until the ignition is cycled
    (seen on the car 2026-11-04 and 2026-11-14)."""
    if "(D7)" in text:
        return f"{BUSY_HINT}\n\n({text})"
    return f"{RETRY_HINT}\n\n({text})" if "(D8)" in text else text


def _error(doc):
    return _hint((doc.get("error") or {}).get("message") or "沒有結果")


def run(link, args_text, sink):
    """Run one read command line (e.g. "verify"), text output. Returns the exit code."""
    from yeti_diag import android_serial, cli

    _seed()
    out = _Sink(sink)
    try:
        args = shlex.split(args_text)
    except ValueError as e:
        out.write(f"指令格式錯誤: {e}\n")
        return 2
    blocked = [a for a in args if a in cli.WRITE_COMMANDS]
    if blocked:
        out.write(f"這裡只能讀取，不執行 {blocked[0]}。寫入請用「功能」或「故障碼」頁 (會先顯示新舊值)。\n")
        return 4
    if any(a in ("verify", "tp20-probe") for a in args) and "--driving" not in args:
        out.write("這個指令會掃描 127 個位址，全部約 3 分鐘，掃描中不會印東西；上方會顯示執行秒數。\n")
    android_serial.LINK = link
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            cli.main(["--elm", "android", *args])
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 0
        except Exception:  # noqa: BLE001 - show the traceback on the phone instead of crashing
            out.write(traceback.format_exc())
            code = 70
    return code


def scan(link, sink):
    """Full scan for the fault-code page: one entry per module with ready-to-show lines."""
    from yeti_diag import cli, vehicle

    doc = _json(link, ["autoscan"], sink)
    data = doc.get("data")
    if not data:
        return _dump({"ok": False, "message": _error(doc)})
    modules = []
    for r in data["modules"]:
        dtcs = r["dtcs"]
        modules.append({
            "address": r["address"], "name": r["name"], "part": r["ids"].get("零件號碼", "?"),
            "count": None if dtcs is None else len(dtcs), "error": r["error"],
            "dtcs": [cli.dtc_line(d) for d in dtcs or []],
            "values": [f"{v['name']}: {v['value']} {v['unit']}" for v in r.get("values", []) if "error" not in v],
            "clearable": bool(dtcs),
        })
    try:
        report = pathlib.Path(data["report"]).with_suffix(".txt").read_text(encoding="utf-8")
    except OSError:
        report = ""
    volts = f"{data['voltage']:.2f} V" if data.get("voltage") is not None else "讀不到"
    summary = f"模組 {len(modules)} 個，故障碼 {data['dtc_total']} 個，電壓 {volts}"
    summary += _service_from_scan(data["modules"])
    if any("(D7)" in (m["error"] or "") for m in modules):
        summary += "\n" + BUSY_HINT
    elif any("(D8)" in (m["error"] or "") for m in modules):
        summary += "\n" + RETRY_HINT
    if data.get("not_found"):
        summary += "\n沒回應: " + "、".join(f"{a} {vehicle.TP20_MODULES[a]}" for a in data["not_found"])
    return _dump({"ok": True, "summary": summary, "modules": modules, "report": report})


def features(link, sink):
    """09 hidden functions and whether each is on."""
    doc = _json(link, ["features"], sink)
    data = doc.get("data")
    if not data:
        return _dump({"ok": False, "message": _error(doc)})
    return _dump({"ok": True, "coding": data["coding"], "features": data["features"]})


def live(link, sink, stop):
    """Engine OBD values over one open connection until `stop.get()` is true.
    Each round goes to `sink.update(json list of {name, value, unit})`. Engine only, so it
    is allowed while driving."""
    from yeti_diag import android_serial, cli, uds
    from yeti_diag.elm327 import ElmError
    from yeti_diag.vehicle import UDS_MODULES

    android_serial.LINK = link
    args = cli.build_parser().parse_args(["--elm", "android", "obd"])
    silent = 0
    try:
        with cli.Transport(args) as t, t.client(UDS_MODULES["01"]) as c:
            while not stop.get():
                values = []
                for pid in uds.OBD_PIDS:
                    if stop.get():
                        break
                    try:
                        name, value, unit = uds.read_obd_pid(c, pid)
                        values.append({"name": name, "value": f"{value:g}", "unit": unit})
                    except (uds.NegativeResponse, uds.NoResponse, ElmError):
                        continue
                if values:
                    silent = 0
                    sink.update(_dump(values))
                elif not stop.get():
                    silent += 1
                    if silent >= 3:
                        return _dump({"ok": False, "message": "引擎模組沒有回應。點火開關有打開嗎？"})
    except cli.CliError as e:
        return _dump({"ok": False, "message": str(e)})
    except Exception as e:  # noqa: BLE001 - a dropped link ends the loop with its message
        return _dump({"ok": False, "message": f"{type(e).__name__}: {e}"})
    return _dump({"ok": True, "message": ""})


def _plan_text(plan):
    old, new = plan.get("old"), plan.get("new")
    if new is None:  # clearing fault codes
        lines = [f"將清除 {len(old or [])} 個故障碼。", "故障還在的話，清除後會馬上再出現。"]
    else:
        lines = [f"Byte {c['byte']}：{c['old']} → {c['new']}  ({c['bits']})" for c in plan.get("changes") or []]
        lines += ["", f"舊值：{old}", f"新值：{new}"]
    lines += ["", "舊值已備份。按「寫入」後只送出一次，寫完自動讀回確認。", f"有效到 {plan['expires'][11:16]}。"]
    return "\n".join(lines)


def _module_name(address):
    from yeti_diag import vehicle

    if address in vehicle.UDS_MODULES:
        return f"{address} {vehicle.UDS_MODULES[address].name}"
    return f"{address} {vehicle.TP20_MODULES.get(address, '')}".strip()


def _code_text(d):
    """One fault code of a clear plan: UDS records carry "dtc", TP2.0 records a 5-digit "code"."""
    from yeti_diag import uds

    if "dtc" in d or d.get("uds"):
        return uds.format_dtc(d.get("dtc", d.get("code")))
    return f"{d['code']:05d}"


def _plan_clear(link, addresses, sink):
    """Clear plans for several modules: one plan each, shown and approved together."""
    from yeti_diag import vehicle

    known = set(vehicle.UDS_MODULES) | set(vehicle.TP20_MODULES)
    lines, ids, problems = [], [], []
    for address in dict.fromkeys(addresses):
        if address not in known:
            problems.append(f"{address}：不認得這個模組")
            continue
        doc = _json(link, ["clear-dtc", address], sink)
        data = doc.get("data") or {}
        if data.get("status") == "planned":
            p = data["plan"]
            codes = "、".join(_code_text(d) for d in p.get("old") or [])
            lines.append(f"{_module_name(address)}：{len(p.get('old') or [])} 個 ({codes})")
            ids.append(f"{address}:{p['id']}")
        elif not doc.get("ok"):
            problems.append(f"{_module_name(address)}：{_error(doc)}")
    if not ids:
        text = "\n".join(problems) if problems else "這些模組目前沒有故障碼。"
        return _dump({"ok": not problems, "plan_id": None, "title": "沒有要清除的故障碼", "message": text})
    lines += ["", "故障還在的話，清除後會馬上再出現。舊記錄已備份；每個模組只送出一次，清完自動讀回確認。"]
    if any(i.startswith("01:") for i in ids):
        lines += ["", "注意：清除引擎故障碼會把排放自我檢測歸零，要再正常行駛一段時間才會完成；近期要驗車先不要清引擎。"]
    if any(i.split(":")[0] in vehicle.UDS_MODULES for i in ids):
        lines += ["", "新式模組 (引擎、冷氣、氣囊、方向盤、儀表) 的清除之前只在模擬器測過，這是第一次在車上做；"
                      "模組不接受的話會顯示失敗，不會重試。"]
    if problems:
        lines += ["", "以下模組不清除："] + problems
    return _dump({"ok": True, "plan_id": ",".join(ids), "title": f"清除 {len(ids)} 個模組的故障碼",
                  "message": "\n".join(lines)})


def plan(link, args_text, sink):
    """Step 1 of a write: all checks, read the old value, save a backup and a plan; nothing is written.
    `clear-dtc` takes one or more module addresses."""
    args = shlex.split(args_text)
    if not args or args[0] not in PHONE_WRITES:
        return _dump({"ok": False, "message": f"手機版還沒開放 {args[0] if args else '這個指令'}。"})
    if args[0] == "clear-dtc":
        if len(args) < 2:
            return _dump({"ok": False, "message": "沒有指定要清除的模組。"})
        return _plan_clear(link, args[1:], sink)
    doc = _json(link, args, sink)
    data = doc.get("data") or {}
    if data.get("status") == "planned":
        p = data["plan"]
        return _dump({"ok": True, "plan_id": p["id"], "title": p["summary"], "message": _plan_text(p)})
    if doc.get("ok"):
        text = "這個模組目前沒有故障碼。" if data.get("status") == "nothing" else "車上已經是這個設定，不需要寫入。"
        return _dump({"ok": True, "plan_id": None, "title": "不需要寫入", "message": text})
    return _dump({"ok": False, "message": _error(doc)})


def apply(link, plan_id, sink):
    """Step 2: the person agreed. `plan_id` is one plan id, or "module:id,module:id" from a
    clear plan: those run one after another, each sent once; a failure does not stop the rest."""
    if ":" in plan_id:
        lines, ok = [], True
        for part in plan_id.split(","):
            address, one = part.split(":", 1)
            r = json.loads(_apply_one(link, one, sink))
            ok = ok and r["ok"]
            lines.append(f"{_module_name(address)}：" + ("" if r["ok"] else "沒有成功。") + r["message"].split("\n\n備份")[0])
        return _dump({"ok": ok, "message": "\n\n".join(lines)})
    return _apply_one(link, plan_id, sink)


def _apply_one(link, plan_id, sink):
    doc = _json(link, ["apply", plan_id], sink)
    if not doc.get("ok"):
        kind = (doc.get("error") or {}).get("kind")
        text = _error(doc)
        if kind == "verify_failed":
            text = "寫入已送出，但讀回的值不對。先不要再寫入，把這個畫面給 Claude 看。\n\n" + text
        elif kind == "connection":
            text = "寫入過程連線出錯，不確定有沒有寫進去。請重新讀取確認車上的值。\n\n" + text
        return _dump({"ok": False, "message": text})
    result = (doc.get("data") or {}).get("result") or {}
    status = result.get("status")
    if status == "written":
        text = "寫入成功，讀回的值與新值一致。"
    elif status == "cleared":
        text = "已清除，讀回 0 個故障碼。"
    elif status == "partly":
        text = f"已送出清除，還剩 {len(result.get('after') or [])} 個 (故障仍存在，所以馬上又出現)。"
    else:
        text = f"完成 ({status})。"
    if result.get("backup"):
        text += f"\n\n備份：{pathlib.Path(result['backup']).name}"
    return _dump({"ok": True, "message": text})


# --- oil change reminder: kept on the phone, nothing is written to the car (yeti_diag.service) ---
def _service_file():
    return HOME / "service.json"


def _service_from_scan(modules):
    """The full scan reads the odometer anyway: keep it and return a line about the oil change."""
    from yeti_diag import service

    km = service.odometer_in([v for r in modules for v in r.get("values", [])])
    if km is None:
        return ""
    state = service.set_odometer(service.load(_service_file()), km)
    service.save(_service_file(), state)
    line = service.summary_line(service.view(state))
    return "\n" + line if line else ""


def service_status():
    """Saved state only; does not talk to the car."""
    from yeti_diag import service

    return _dump(service.view(service.load(_service_file())))


def service_read(link, sink):
    """Read the odometer from the instrument cluster and show the distance left."""
    from yeti_diag import service

    doc = _json(link, ["values"], sink)
    km = service.odometer_in((doc.get("data") or {}).get("values") or [])
    if km is None:
        return _dump({"ok": False, "message": "讀不到里程。" + _error(doc)})
    state = service.set_odometer(service.load(_service_file()), km)
    service.save(_service_file(), state)
    return _dump(service.view(state))


def service_done(odometer, interval):
    """Record an oil change at `odometer` km with the chosen interval. Phone only."""
    from yeti_diag import service

    try:
        state = service.record_oil(service.load(_service_file()), odometer, interval)
    except ValueError as e:
        return _dump({"ok": False, "message": str(e)})
    service.save(_service_file(), state)
    return _dump(service.view(state))


def data_dir():
    return str(HOME)
