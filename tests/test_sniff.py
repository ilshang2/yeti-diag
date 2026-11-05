"""Listening to another tester (VCDS through a Y-cable): record, rebuild, name values."""
import datetime
import io
import json

from yeti_diag import sniff
from yeti_diag.elm327 import Elm327
from yeti_diag.sim import FakeElmSerial

from test_cli import run_json

T0 = datetime.datetime(2026, 11, 5, 7, 30, 15).timestamp()


def isotp(can_id, payload):
    """ISO-TP frames as the module / VCDS would send them."""
    if len(payload) <= 7:
        return [(can_id, bytes([len(payload)]) + payload)]
    out = [(can_id, bytes([0x10 | len(payload) >> 8, len(payload) & 0xFF]) + payload[:6])]
    rest, seq = payload[6:], 1
    while rest:
        out.append((can_id, bytes([0x20 | seq & 0x0F]) + rest[:7]))
        rest, seq = rest[7:], seq + 1
    return out


def tp20(can_id, payload, seq=0):
    """TP2.0 data packets: 2-byte length first, last packet op 1 (wants an ACK)."""
    data = len(payload).to_bytes(2, "big") + payload
    chunks = [data[i:i + 7] for i in range(0, len(data), 7)]
    return [(can_id, bytes([(0x10 if i == len(chunks) - 1 else 0x20) | (seq + i) & 0x0F]) + c)
            for i, c in enumerate(chunks)]


def capture_text(frames):
    """frames: [(seconds after T0, can_id, data)] -> capture file text."""
    lines = ["# yeti_diag sniff start 2026-11-05T07:30:15 filter=none"]
    lines += [f"{T0 + t:.3f} {i:03X} {d.hex().upper()}" for t, i, d in frames]
    return "\n".join(lines) + "\n"


def car_session():
    """VCDS reading engine torque / oil temperature, a DSG group, then a coding write."""
    frames = []
    for n, (torque, oil) in enumerate([(3333, 66), (742, 70), (4262, 138)]):
        t = 1.0 + n
        frames += [(t, i, d) for i, d in isotp(0x7E0, bytes.fromhex("22155A20A1"))]
        answer = bytes.fromhex("62155A") + torque.to_bytes(2, "big") + bytes.fromhex("20A1") + bytes([oil])
        frames += [(t + 0.02, 0x7E8, bytes.fromhex("037F2278"))]  # response pending first
        frames += [(t + 0.05, i, d) for i, d in isotp(0x7E8, answer)]
    # TP2.0 channel to the DSG (address 0x02): VCDS asks on 740, the module answers on 300.
    frames += [(5.0, 0x200, bytes.fromhex("02C00010000301")),
               (5.01, 0x202, bytes.fromhex("00D00003400701"))]
    frames += [(5.1, i, d) for i, d in tp20(0x740, bytes.fromhex("2176"))]
    frames += [(5.11, 0x300, bytes.fromhex("B1"))]
    frames += [(5.2, i, d) for i, d in tp20(0x300, bytes.fromhex("61761F14820D027F2500009F9E90"))]
    frames += [(5.21, 0x740, bytes.fromhex("B3"))]
    # A coding write on module 17 (714/77E): session, write, answer.
    frames += [(6.0, i, d) for i, d in isotp(0x714, bytes.fromhex("1003"))]
    frames += [(6.01, i, d) for i, d in isotp(0x77E, bytes.fromhex("5003003201F4"))]
    frames += [(6.1, i, d) for i, d in isotp(0x714, bytes.fromhex("2E060001020304050607"))]
    frames += [(6.2, i, d) for i, d in isotp(0x77E, bytes.fromhex("6E0600"))]
    return frames


MODS = {"01": (0x7E0, 0x7E8), "17": (0x714, 0x77E)}


def test_record_filters_and_survives_buffer_full():
    bus = [f"{i:03X}{d.hex().upper()}" for _, i, d in car_session()]
    elm = Elm327(serial_obj=FakeElmSerial(bus=bus + ["5F0AABBCC"], buffer_frames=4))
    out = io.StringIO()
    counts = sniff.record(elm, out, seconds=0.5, ids=[0x7E0, 0x7E8], say=lambda *a: None)
    cap = sniff.read_capture(out.getvalue())
    assert {f.can_id for f in cap.frames} == {0x7E0, 0x7E8}  # the filter let only the engine through
    assert counts["frames"] == len([b for b in bus if b[:3] in ("7E0", "7E8")])
    assert counts["buffer_full"] >= 1 and len(cap.gaps) == counts["buffer_full"]
    assert elm.command("ATRV") == "12.6V"  # monitoring stopped cleanly; the ELM answers again


def test_exchanges_rebuild_uds_and_tp20():
    cap = sniff.read_capture(capture_text(car_session()))
    exs = sniff.exchanges(cap.frames, MODS, {0x02: "02"})
    engine = [e for e in exs if e.module == "01"]
    assert len(engine) == 3 and engine[0].pending == 1
    assert engine[0].response == bytes.fromhex("62155A0D0520A142")
    dsg = next(e for e in exs if e.module == "02")
    assert dsg.request == bytes.fromhex("2176") and dsg.response[:2] == bytes.fromhex("6176")
    obs = sniff.observations(exs)
    assert [r[1].hex() for r in obs["01 DID 155A"]] == ["0d05", "02e6", "10a6"]
    assert obs["02 群組 118.4"][0][2] == 451  # decoded with the DSG formula 0x9F
    acts = sniff.actions(exs)
    assert [(a.module, a.request.hex().upper(), a.response.hex().upper()) for a in acts] == [
        ("17", "1003", "5003003201F4"), ("17", "2E060001020304050607", "6E0600")]


def vcds_csv():
    rows = ["Wednesday,05,November,2026,07:30:15:00000,VCDS Version: Release 24.7.0",
            "Address 01: Engine",
            ",,IDE00069,IDE00072",
            "Marker,TIME,Engine torque,Calculated oil temperature",
            ",,Nm,°C"]
    for n, (nm, oil) in enumerate([("104.16", "26"), ("23.19", "30"), ("133.19", "98")]):
        rows.append(f",{1.06 + n:.2f},{nm},{oil}")
    return "\n".join(rows) + "\n"


def test_vcds_columns_get_their_did_and_formula():
    cap = sniff.read_capture(capture_text(car_session()))
    obs = sniff.observations(sniff.exchanges(cap.frames, MODS))
    vcds = sniff.read_vcds_csv(vcds_csv())
    assert set(vcds) == {"IDE00069 Engine torque", "IDE00072 Calculated oil temperature"}
    assert vcds["IDE00069 Engine torque"]["unit"] == "Nm"
    found = {m["vcds"]: m["matches"][0] for m in sniff.match_vcds(obs, vcds)}
    torque = found["IDE00069 Engine torque"]
    assert (torque["key"], torque["how"], torque["factor"], torque["offset"]) == ("01 DID 155A", "無號整數", "1/32", 0)
    oil = found["IDE00072 Calculated oil temperature"]
    assert (oil["key"], oil["factor"], oil["offset"]) == ("01 DID 20A1", "1", -40)


def test_sniff_analyze_command(capsys, tmp_path):
    cap = tmp_path / "x_sniff.txt"
    cap.write_text(capture_text(car_session()), encoding="utf-8")
    csv_file = tmp_path / "vcds.csv"
    csv_file.write_text(vcds_csv(), encoding="utf-8")
    code, doc = run_json(capsys, "sniff-analyze", str(cap), "--vcds", str(csv_file))
    assert code == 0
    d = doc["data"]
    assert any(a["request"].startswith("2E06") for a in d["actions"])
    assert any(m["matches"] and m["matches"][0]["key"] == "01 DID 155A" for m in d["vcds_matches"])
    saved = json.loads((tmp_path / "x_sniff_analysis.json").read_text(encoding="utf-8"))
    assert saved["frames"] == d["frames"]


def test_sniff_needs_an_elm(capsys):
    code, doc = run_json(capsys, "sniff", "--seconds", "0.1")
    assert code != 0 and "ELM327" in doc["error"]["message"]
