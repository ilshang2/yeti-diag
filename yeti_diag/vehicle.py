"""The vehicle profile: which modules a car has and how to reach them.

Profiles are JSON files in `profiles/` (see `profiles/skoda_yeti_2014.json`), so
another car needs a new profile, not code changes. The profile is chosen with
the environment variable YETI_DIAG_PROFILE (a profile name or a path); the
default is the Skoda Yeti 1.2 TSI 2014 this tool was developed on.

CAN IDs are the usual VAG UDS assignments; confirm them on a new car with
`probe` and `verify` before trusting a module's results.
"""
import json
import os
import pathlib
from dataclasses import dataclass

PROFILE_DIR = pathlib.Path(__file__).resolve().parent.parent / "profiles"
DEFAULT_PROFILE = "skoda_yeti_2014"


@dataclass(frozen=True)
class Module:
    address: str   # VCDS address, e.g. "17"
    name: str      # Chinese label used in reports
    part_no: str   # part number seen in the VCDS scan
    tx_id: int     # tester -> ECU
    rx_id: int     # ECU -> tester


def profile_path(choice=None):
    choice = choice or os.environ.get("YETI_DIAG_PROFILE") or DEFAULT_PROFILE
    path = pathlib.Path(choice)
    if path.suffix != ".json":
        path = PROFILE_DIR / f"{choice}.json"
    return path


def load_profile(choice=None):
    return json.loads(profile_path(choice).read_text(encoding="utf-8"))


def check_profile(profile):
    """Problems in a profile, as readable messages (empty list = fine)."""
    problems = []
    for key in ("profile", "name", "vin", "uds_modules", "tp20_modules"):
        if key not in profile:
            problems.append(f"缺少 {key}")
    for addr, m in profile.get("uds_modules", {}).items():
        for key in ("name", "part_no", "tx_id", "rx_id"):
            if key not in m:
                problems.append(f"UDS 模組 {addr} 缺少 {key}")
        for key in ("tx_id", "rx_id"):
            try:
                int(m.get(key, ""), 16)
            except ValueError:
                problems.append(f"UDS 模組 {addr} 的 {key} 不是十六進位: {m.get(key)!r}")
    for addr, m in profile.get("tp20_modules", {}).items():
        if "name" not in m:
            problems.append(f"TP2.0 模組 {addr} 缺少 name")
    for addr, dids in profile.get("uds_values", {}).items():
        if addr.startswith("_"):
            continue
        if addr not in profile.get("uds_modules", {}):
            problems.append(f"uds_values 的模組 {addr} 不在 uds_modules")
        for did, v in dids.items():
            missing = [k for k in ("name", "unit", "checked") if k not in v]
            if missing:
                problems.append(f"uds_values {addr} {did} 缺少 {', '.join(missing)} (checked = 怎麼在實車確認的)")
    return problems


def scaled(scale, offset=0):
    """Decoder for an unsigned big-endian value times `scale` plus `offset`."""
    return lambda d: round(int.from_bytes(d, "big") * scale + offset, 6)


PROFILE = load_profile()
PROFILE_NAME = PROFILE["profile"]
VIN = PROFILE["vin"]

# Diagnostic CAN on the OBD-II port (pins 6/14), normally 500 kbit/s.
DIAG_BITRATE = PROFILE.get("diag_bitrate", 500_000)

# Modules that answer UDS (VCDS showed an ASAM dataset for them).
UDS_MODULES = {a: Module(a, m["name"], m["part_no"], int(m["tx_id"], 16), int(m["rx_id"], 16))
               for a, m in PROFILE["uds_modules"].items()}

# Older modules speak KWP2000 over VW TP2.0 (see tp20.py). Their TP2.0 addresses
# are found by `tp20-probe`, which matches part numbers against TP20_PART_NUMBERS.
TP20_MODULES = {a: m["name"] for a, m in PROFILE["tp20_modules"].items()}
TP20_PART_NUMBERS = {a: m["part_no"] for a, m in PROFILE["tp20_modules"].items() if "part_no" in m}

# Modules sharing a part number (rear doors) told apart by a test on the car.
REAR_DOOR_DEST = {a: int(d, 16) for a, d in PROFILE.get("rear_door_dest", {}).items() if not a.startswith("_")}

# Readable values of UDS modules: module -> DID -> (label, unit, decode, how it was checked).
# Only DIDs confirmed on the car go in a profile; `scan-dids` finds candidates.
UDS_VALUES = {a: {int(did, 16): (v["name"], v["unit"], scaled(v.get("scale", 1), v.get("offset", 0)), v["checked"])
                  for did, v in dids.items()}
              for a, dids in PROFILE.get("uds_values", {}).items() if not a.startswith("_")}

# Ground truth from VCDS, used by `verify`.
_ref = PROFILE.get("reference", {})
VCDS_TRUTH = {
    "09_coding": bytes.fromhex(_ref.get("09_coding", "")),
    "door_dtc": _ref.get("door_dtc"),
    "72_coding": bytes.fromhex(_ref.get("72_coding", "")),
    "door_counts": _ref.get("door_counts", {}),
}

# Identification DIDs read during a scan (UDS / VAG), the same on every VAG car.
ID_DIDS = {
    0xF187: "零件號碼",
    0xF189: "軟體版本",
    0xF18C: "序號",
    0xF190: "車身號碼",
    0xF197: "系統名稱",
    0x0600: "編碼 (唯讀)",
}
