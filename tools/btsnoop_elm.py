"""Pull the ELM327 conversation out of an Android Bluetooth HCI snoop log.

Records the text an app (Car Scanner, ...) sent to the adapter and what came back,
so its requests can be matched with the values it showed on screen.

    py -3.13 tools\\btsnoop_elm.py reports\\phone\\bugreport.zip > reports\\phone\\elm_log.txt

A bugreport can hold several snoop files; the newest is read. Each line shows the
seconds since the first ELM line and the phone's clock (to match screenshot times).
"""
import datetime
import re
import struct
import sys
import zipfile

ASCII = re.compile(rb"[ -~\r>]{2,}")
UNIX_OFFSET = 0x00DCDDB30F2F8000  # btsnoop microseconds since year 0 -> since 1970 (phone local time)


def records(data):
    assert data[:8] == b"btsnoop\0", "not a btsnoop file"
    pos = 16
    while pos + 24 <= len(data):
        _, incl, flags, _, ts = struct.unpack(">IIIIq", data[pos:pos + 24])
        yield flags, ts, data[pos + 24:pos + 24 + incl]
        pos += 24 + incl


def rfcomm_payload(pkt):
    """ACL -> L2CAP -> RFCOMM UIH data, or b'' for anything else."""
    if not pkt or pkt[0] != 0x02 or len(pkt) < 10:
        return b""
    l2 = pkt[5:]
    cid = l2[2] | l2[3] << 8
    if cid < 0x40:  # signalling channels, not RFCOMM data
        return b""
    r = l2[4:]
    if len(r) < 4 or (r[1] & 0xEF) != 0xEF:  # UIH frames only
        return b""
    length, hdr = (r[2] >> 1, 3) if r[2] & 1 else ((r[2] >> 1) | (r[3] << 7), 4)
    if r[1] & 0x10 and r[0] & 0xFC:  # P/F set on a data channel: one credit byte
        hdr += 1
    return r[hdr:hdr + length]


def main(path):
    if path.endswith(".zip"):
        z = zipfile.ZipFile(path)
        name = sorted(n for n in z.namelist() if "btsnoop_hci" in n)[-1]
        data = z.read(name)
    else:
        data = open(path, "rb").read()
    t0 = None
    for flags, ts, pkt in records(data):
        payload = rfcomm_payload(pkt)
        if not payload or not ASCII.fullmatch(payload.rstrip(b"\0")):
            continue
        t0 = t0 or ts
        who = "<<" if flags & 1 else ">>"
        text = payload.decode("ascii", "replace").replace("\r", "\\r")
        clock = (datetime.datetime(1970, 1, 1) + datetime.timedelta(microseconds=ts - UNIX_OFFSET)).strftime("%H:%M:%S")
        print(f"{(ts - t0) / 1e6:9.3f} {clock} {who} {text}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1])
