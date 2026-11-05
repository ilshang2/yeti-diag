"""Guarded writes: every change is backed up, checked, confirmed and verified.

Order for each write:
  1. module not on the blocked list
  2. module part number matches the vehicle profile
  3. read the current value and save a backup file
  4. battery voltage high enough
  5. user confirms old -> new
  6. extended session, write, read back, back to default session
  7. read-back must equal the new value, otherwise VerifyFailed
"""
import datetime
import json
import pathlib

from . import uds
from .paths import DATA_ROOT

BLOCKED_MODULES = {
    "01": "引擎",
    "03": "ABS/ESP",
    "15": "安全氣囊",
    "25": "防盜",
    "44": "電子轉向輔助",
}
MIN_VOLTAGE = 12.2

BACKUP_DIR = DATA_ROOT / "backups"


class WriteRefused(Exception):
    pass


class VerifyFailed(Exception):
    pass


def check_module(mod):
    if mod.address in BLOCKED_MODULES:
        raise WriteRefused(f"{mod.address} {BLOCKED_MODULES[mod.address]} 是安全相關模組，不允許寫入")


def check_part_number(client, mod):
    actual = uds.decode_did(0xF187, client.read_did(0xF187)).replace(" ", "")
    expected = mod.part_no.replace(" ", "")
    if actual != expected:
        raise WriteRefused(f"{mod.address} 零件號碼 {actual} 與設定檔 {expected} 不符，不允許寫入")


def check_voltage(voltage):
    if voltage is None:
        raise WriteRefused("讀不到電瓶電壓，不允許寫入")
    if voltage < MIN_VOLTAGE:
        raise WriteRefused(f"電瓶電壓 {voltage:.2f} V 低於 {MIN_VOLTAGE} V，不允許寫入 (請發動引擎或接充電器)")


def save_backup(mod, kind, payload, backup_dir=None):
    backup_dir = pathlib.Path(backup_dir or BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    path = backup_dir / f"{now:%Y%m%d-%H%M%S-%f}_{mod.address}_{kind}.json"
    path.write_text(json.dumps({"time": now.isoformat(), "module": mod.address, "kind": kind, **payload},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_did(client, mod, did, new, *, voltage, confirm, backup_dir=None, same_ok=False):
    """Write `new` to `did`. Returns a result dict; raises WriteRefused / VerifyFailed.

    same_ok: write even when the module already holds `new` (tests the whole write
    sequence on the car without changing anything)."""
    new = bytes(new)
    check_module(mod)
    check_part_number(client, mod)
    old = client.read_did(did)
    if old == new and not same_ok:
        return {"status": "unchanged", "did": f"{did:04X}", "value": new.hex().upper()}
    backup = save_backup(mod, f"did{did:04X}", {"did": f"{did:04X}", "old": old.hex().upper(),
                                                "new": new.hex().upper()}, backup_dir)
    check_voltage(voltage)
    if not confirm(old, new):
        raise WriteRefused("使用者取消")

    client.enter_write_session()
    try:
        client._transact(bytes([0x2E, did >> 8, did & 0xFF]) + new)
        after = client.read_did(did)
    finally:
        client.exit_write_session()
    if after != new:
        raise VerifyFailed(f"DID {did:04X} 寫入後讀回 {after.hex().upper()}，與預期 {new.hex().upper()} 不同。"
                           f"備份: {backup}")
    return {"status": "written", "did": f"{did:04X}", "old": old.hex().upper(), "new": new.hex().upper(),
            "backup": str(backup)}


def write_adaptation(client, mod, channel, new, *, voltage, confirm, backup_dir=None):
    """Adaptation value of a TP2.0 module (KwpClient): backup, voltage, confirm, write once,
    read back. The workshop code goes with the save (taken from the module like VCDS)."""
    check_module(mod)
    if not 1 <= channel <= 255:
        raise WriteRefused("通道要 1-255；00 是清除所有學習值")
    if mod.part_no:
        actual = client.read_part().replace(" ", "")
        if actual != mod.part_no.replace(" ", ""):
            raise WriteRefused(f"{mod.address} 零件號碼 {actual} 與設定檔 {mod.part_no} 不符，不允許寫入")
    old = client.read_adaptation(channel)["value"]
    if old == new:
        return {"status": "unchanged", "channel": channel, "value": new}
    wsc = client.workshop_code()
    if not wsc or len(wsc) != 6:
        raise WriteRefused("讀不到工作站碼 (F198 / F1A5)，不寫")
    backup = save_backup(mod, f"adapt{channel:03d}", {"channel": channel, "old": old, "new": new}, backup_dir)
    check_voltage(voltage)
    if not confirm(old.to_bytes(2, "big"), new.to_bytes(2, "big")):  # shown as 00 05 -> 00 04
        raise WriteRefused("使用者取消")
    error = None
    try:
        client._write_adaptation(channel, new, wsc)
    except Exception as e:  # the read-back below decides; the error explains a refusal
        error = e
    after = client.read_adaptation(channel)["value"]
    if after != new:
        why = f"模組回應: {error}" if error else "模組接受了但值沒變"
        raise VerifyFailed(f"通道 {channel} 寫入後讀回 {after}，預期 {new} ({why})。備份: {backup}")
    return {"status": "written", "channel": channel, "old": old, "new": new, "backup": str(backup)}


def restore(client, mod, backup_file, *, voltage, confirm, backup_dir=None):
    data = json.loads(pathlib.Path(backup_file).read_text(encoding="utf-8"))
    if data["module"] != mod.address or "did" not in data:
        raise WriteRefused(f"備份檔 {backup_file} 不是 {mod.address} 的 DID 備份")
    return write_did(client, mod, int(data["did"], 16), bytes.fromhex(data["old"]),
                     voltage=voltage, confirm=confirm, backup_dir=backup_dir)


def clear_dtcs(client, mod, *, confirm, backup_dir=None):
    """Clear DTCs and verify. Detects 'reported erased but nothing changed'."""
    before = client.read_dtcs()
    if not before:
        return {"status": "nothing", "before": [], "after": []}
    # UDS records carry "dtc", KWP (TP2.0) records "code"; KWP clears with 14 FF 00.
    backup = save_backup(mod, "dtc", {"dtcs": [{"dtc": f"{d.get('dtc', d.get('code')):06X}",
                                                "status": f"{d['status']:02X}"} for d in before]}, backup_dir)
    if not confirm(before, None):
        raise WriteRefused("使用者取消")
    client._transact(getattr(client, "CLEAR_DTCS", bytes([0x14, 0xFF, 0xFF, 0xFF])))
    after = client.read_dtcs()
    if after == before:
        raise VerifyFailed(f"模組回報已清除，但故障碼完全沒變 (共 {len(after)} 個)。備份: {backup}")
    status = "cleared" if not after else "partly"
    return {"status": status, "before": before, "after": after, "backup": str(backup)}
