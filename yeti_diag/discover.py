"""Find the modules of an unknown VW-group car and write a vehicle profile for it.

Read-only. UDS: every diagnostic CAN ID 0x700-0x795 and 0x7E0-0x7E7 is asked for its part number
(22 F187); VW answers on tx + 0x6A (engine range 7E0-7E7: tx + 8). TP2.0: the address
scan of `tp20-probe`. VCDS addresses come from IDs confirmed on the Yeti and common
VW ones; anything else gets a placeholder name to be renamed by hand.
"""
import datetime

from . import uds, vehicle

# tx ID -> (VCDS address, name). Yeti 2014 (confirmed) plus common VW group IDs (unconfirmed).
COMMON_UDS = {
    0x7E0: ("01", "引擎"), 0x7E1: ("02", "變速箱"), 0x713: ("03", "ABS/ESP"),
    0x746: ("08", "冷氣"), 0x70E: ("09", "中央電子"), 0x715: ("15", "安全氣囊"),
    0x70C: ("16", "方向盤模組"), 0x714: ("17", "儀表"), 0x710: ("19", "閘道器"),
}
# TP2.0 address -> (VCDS address, name), as found on the Yeti (PQ35).
COMMON_TP20 = {
    0x02: ("02", "變速箱"), 0x03: ("03", "ABS/ESP"), 0x09: ("44", "電子轉向輔助"),
    0x1F: ("19", "閘道器"), 0x20: ("09", "中央電子"), 0x22: ("42", "駕駛座車門"),
    0x23: ("52", "副駕車門"), 0x24: ("62", "左後門"), 0x25: ("72", "右後門"),
}
# Answers must fit in 11 bits: tx + 0x6A <= 0x7FF, so 700-795, plus the engine range 7E0-7E7.
UDS_SCAN = [*range(0x700, 0x796), *range(0x7E0, 0x7E8)]
IDENT = (0xF187, 0xF197, 0xF189)  # part number, system name, software version


def uds_rx_for(tx):
    return tx + 8 if 0x7E0 <= tx <= 0x7E7 else tx + 0x6A


def scan_uds(client_for, ids=None, say=print):
    """client_for(Module) -> UdsClient context manager. Returns answering modules."""
    rows = []
    for tx in ids or UDS_SCAN:
        mod = vehicle.Module(f"{tx:03X}", "", "", tx, uds_rx_for(tx))
        with client_for(mod) as c:
            try:
                part = uds.decode_did(0xF187, c.read_did(0xF187))
            except uds.NoResponse:
                continue
            except uds.NegativeResponse as e:
                part = f"(零件號碼被拒絕 {e.nrc:02X})" if hasattr(e, "nrc") else "(零件號碼被拒絕)"
            row = {"tx": tx, "rx": mod.rx_id, "part": part}
            for did in IDENT[1:]:
                try:
                    row[f"{did:04X}"] = uds.decode_did(did, c.read_did(did))
                except (uds.NoResponse, uds.NegativeResponse):
                    pass
            if tx == 0x7E0:
                try:
                    row["vin"] = uds.decode_did(0xF190, c.read_did(0xF190))
                except (uds.NoResponse, uds.NegativeResponse):
                    pass
        say(f"UDS {tx:03X}/{mod.rx_id:03X}: {part}  {row.get('F197', '')}")
        rows.append(row)
    return rows


def mask_vin(vin):
    """Keep the first 11 characters (maker, model, model year, plant: what identifies the car
    type) and zero the last 6 (the serial: what identifies one car), so a profile can be
    shared without giving away the owner's car."""
    vin = (vin or "").strip()
    return vin[:11] + "0" * (len(vin) - 11) if len(vin) == 17 else vin


def build_profile(key, title, uds_rows, tp20_found, adapter="", keep_vin=False):
    """Profile dict (same format as profiles/skoda_yeti_2014.json) from the scan results.
    The VIN's serial is masked unless keep_vin: profiles are meant to be shared."""
    vin = next((r["vin"] for r in uds_rows if r.get("vin")), "")
    if not keep_vin:
        vin = mask_vin(vin)
    uds_modules, used = {}, set()
    for r in uds_rows:
        addr, name = COMMON_UDS.get(r["tx"], (f"UDS_{r['tx']:03X}", ""))
        if addr in used:
            addr = f"UDS_{r['tx']:03X}"
        used.add(addr)
        system = r.get("F197", "").strip()
        uds_modules[addr] = {"name": name or system or f"未知模組 (CAN {r['tx']:03X})",
                             "part_no": r["part"], "tx_id": f"{r['tx']:03X}", "rx_id": f"{r['rx']:03X}"}
        if system:
            uds_modules[addr]["system_name"] = system
    tp20_modules = {}
    for dest_text, part in tp20_found.items():
        dest = int(dest_text, 16)
        addr, name = COMMON_TP20.get(dest, (f"TP20_{dest:02X}", f"未知模組 (TP2.0 0x{dest:02X})"))
        if addr in uds_modules or addr in tp20_modules:
            addr = f"TP20_{dest:02X}"
        tp20_modules[addr] = {"name": name, "tp20_dest": f"0x{dest:02X}"}
        clean = part.split(" (")[0]
        if not clean.startswith("("):  # "(讀不到零件號碼)": no part number to match by
            tp20_modules[addr]["part_no"] = clean
    return {
        "profile": key,
        "name": title,
        "vin": vin,
        "_note": (f"`discover` 自動產生 {datetime.datetime.now():%Y-%m-%d %H:%M} ({adapter})。"
                  "模組位址是依常見 CAN ID / TP2.0 位址推定，名稱可以手動修改；"
                  "UDS_xxx / TP20_xx 是對不到 VCDS 位址的模組。數值名稱 (uds_values / group_labels) 要另外確認。"),
        "uds_modules": uds_modules,
        "tp20_modules": tp20_modules,
        "uds_values": {},
        "group_labels": {},
        "candidates": {},
        "reference": {},
    }
