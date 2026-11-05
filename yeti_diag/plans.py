"""Two-step writes for scripts and AI agents: plan first, apply by id.

`--plan` (or any write in --json mode without --yes) runs every check of the
write, reads the current value, saves a plan file and stops before writing.
`apply <id>` re-runs the same command; it writes only if the car still holds the
old value the plan recorded and the plan has not expired. Writes still go out once.
"""
import datetime
import json
import pathlib
import secrets
from .paths import DATA_ROOT

PLAN_DIR = DATA_ROOT / "plans"
PLAN_TTL = datetime.timedelta(minutes=15)


class PlanCreated(Exception):
    """Raised from the confirm step in plan mode; carries the saved plan."""

    def __init__(self, plan):
        super().__init__(plan["id"])
        self.plan = plan


class PlanMismatch(Exception):
    pass


def describe(value):
    """Old/new values as JSON: bytes as hex, DTC lists as dicts."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex().upper()
    return json.loads(json.dumps(value, default=lambda v: v.hex().upper() if isinstance(v, bytes) else str(v)))


def byte_changes(old, new):
    """Changed bytes of an equal-length write, for the person approving it:
    [{"byte": 14, "old": "4F", "new": "4B", "bits": "00000100 關"}] (byte 0 = first)."""
    if not (isinstance(old, (bytes, bytearray)) and isinstance(new, (bytes, bytearray))) or len(old) != len(new):
        return None
    out = []
    for i, (a, b) in enumerate(zip(old, new)):
        if a != b:
            on, off = b & ~a, a & ~b
            bits = " ".join(t for t in (f"{on:08b} 開" if on else "", f"{off:08b} 關" if off else "") if t)
            out.append({"byte": i, "old": f"{a:02X}", "new": f"{b:02X}", "bits": bits})
    return out


def save(argv, old, new, summary, target, plan_dir=None):
    """`target` names where the plan was made ("sim", "elm:COM9", ...). A plan made on
    the simulator holds the same old value as the car, so apply must check it."""
    plan_dir = pathlib.Path(plan_dir or PLAN_DIR)
    plan_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    plan = {"id": secrets.token_hex(4), "created": now.isoformat(timespec="seconds"),
            "expires": (now + PLAN_TTL).isoformat(timespec="seconds"), "target": target,
            "argv": list(argv), "summary": summary, "old": describe(old), "new": describe(new)}
    changes = byte_changes(old, new)
    if changes is not None:
        plan["changes"] = changes
    (plan_dir / f"{plan['id']}.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
    return plan


def load(plan_id, target, plan_dir=None):
    path = pathlib.Path(plan_dir or PLAN_DIR) / f"{plan_id}.json"
    if not path.exists():
        raise PlanMismatch(f"沒有這個寫入計畫: {plan_id}")
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("target") != target:
        raise PlanMismatch(f"寫入計畫 {plan_id} 是對 {plan.get('target')} 產生的，不能用在 {target}")
    if datetime.datetime.now() > datetime.datetime.fromisoformat(plan["expires"]):
        raise PlanMismatch(f"寫入計畫 {plan_id} 已過期 ({plan['expires']})，請重新產生")
    if plan.get("applied"):
        raise PlanMismatch(f"寫入計畫 {plan_id} 已經執行過 ({plan['applied']})，不會再送一次")
    return plan


def mark_applied(plan, result, plan_dir=None):
    plan["applied"] = datetime.datetime.now().isoformat(timespec="seconds")
    plan["result"] = describe(result)
    path = pathlib.Path(plan_dir or PLAN_DIR) / f"{plan['id']}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def planner(argv, summary, target, plan_dir=None):
    """confirm() for plan mode: save the plan and stop before the write."""
    def confirm(old, new):
        raise PlanCreated(save(argv, old, new, summary, target, plan_dir))
    return confirm


def applier(plan):
    """confirm() for apply: allow the write only if the car still matches the plan."""
    def confirm(old, new):
        if describe(old) != plan["old"]:
            raise PlanMismatch(f"車上目前的值 {describe(old)} 與計畫記錄的 {plan['old']} 不同，"
                               "已拒絕寫入，請重新產生計畫")
        if describe(new) != plan["new"]:
            raise PlanMismatch(f"這次算出的新值 {describe(new)} 與計畫的 {plan['new']} 不同，已拒絕寫入")
        return True
    return confirm
