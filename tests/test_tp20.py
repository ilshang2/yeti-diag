import pytest

from yeti_diag import tp20, uds
from yeti_diag.elm327 import Elm327
from yeti_diag.sim import SIM_TP20_DEST, FakeElmSerial, SimCar
from yeti_diag.vehicle import VCDS_TRUTH


def kwp_over_can(bus, addr):
    ch = tp20.Tp20Channel(tp20.CanLink(bus), SIM_TP20_DEST[addr]).open()
    k = tp20.KwpClient(ch)
    k.start_session()
    return ch, k


def kwp_over_elm(elm, addr):
    ch = tp20.Tp20Channel(tp20.ElmLink(elm), SIM_TP20_DEST[addr]).open()
    k = tp20.KwpClient(ch)
    k.start_session()
    return ch, k


def test_reads_door_dtc_over_can():
    with SimCar() as bus:
        ch, k = kwp_over_can(bus, "62")
        assert [d["code"] for d in k.read_dtcs()] == [VCDS_TRUTH["door_dtc"]]
        ch.close()


def test_reads_long_coding_multipacket_over_can():
    with SimCar() as bus:
        ch, k = kwp_over_can(bus, "09")
        assert k.read_ident(0x9A) == VCDS_TRUTH["09_coding"]
        assert tp20.ascii_part(k.read_ident(0x9B)) == "1K0937086AA"
        ch.close()


def test_reads_over_elm():
    elm = Elm327(serial_obj=FakeElmSerial())
    ch, k = kwp_over_elm(elm, "09")
    assert k.read_ident(0x9A) == VCDS_TRUTH["09_coding"]
    ch.close()
    ch, k = kwp_over_elm(elm, "72")
    assert [d["code"] for d in k.read_dtcs()] == [VCDS_TRUTH["door_dtc"]]
    ch.close()


def test_unsupported_ident_is_negative():
    with SimCar() as bus:
        ch, k = kwp_over_can(bus, "42")
        with pytest.raises(uds.NegativeResponse):
            k.read_ident(0x9C)
        ch.close()


def test_missing_address_raises_no_response():
    with SimCar() as bus:
        with pytest.raises(uds.NoResponse):
            tp20.Tp20Channel(tp20.CanLink(bus), 0x55, timeout=0.2).open()


@pytest.mark.parametrize("payload", [
    [0x10, 0x81],                 # other sessions
    [0x14, 0xFF, 0x00],           # clear DTCs
    [0x3B, 0x9A, 0x00],           # write coding
    [0x2E, 0x06, 0x00, 0x01],     # write data
    [0x31, 0xB8, 0x00],           # routine
    [0x11, 0x01],                 # reset
])
def test_kwp_refuses_writes(payload):
    with SimCar() as bus:
        ch, k = kwp_over_can(bus, "09")
        with pytest.raises(uds.ReadOnlyViolation):
            k.request(payload)
        ch.close()


def test_probe_maps_modules_by_part_number():
    from types import SimpleNamespace

    from yeti_diag.cli import Transport, probe_tp20
    args = SimpleNamespace(sim=True, sim_elm=False, elm=None, timeout=0.3)
    with Transport(args) as t:
        found, modules = probe_tp20(t, 0x01, 0x70)
    assert modules["09"] == "0x20"
    assert modules["62"] == "0x62" and modules["72"] == "0x63"
    assert len(modules) == 9


class ScriptedLink:
    """Returns pre-set frame lists for each exchange and records what was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def exchange(self, tx, data, rx, timeout, expect=None, no_reply=False):
        self.sent.append(bytes(data))
        return self.replies.pop(0) if self.replies else []

    def listen(self, rx, timeout):
        return []


def test_params_ask_for_one_second_ack_wait():
    link = ScriptedLink([[bytes([0x00, 0xD0, 0x00, 0x03, 0x40, 0x07, 0x01])],
                         [bytes([0xA1, 0x0F, 0x8A, 0xFF, 0x4A, 0xFF])]])
    tp20.Tp20Channel(link, 0x20).open()
    assert link.sent[1][:3] == bytes([0xA0, 0x0F, 0xCA])


def test_busy_setup_reply_raises_busy():
    # On the car the gateway answers D8 for ~2 s after a channel ended badly.
    link = ScriptedLink([[bytes([0x00, 0xD8, 0x00, 0x10, 0x00, 0x10, 0x00])]])
    with pytest.raises(tp20.Tp20Busy):
        tp20.Tp20Channel(link, 0x24).open()


def open_scripted(replies):
    link = ScriptedLink([[bytes([0x00, 0xD0, 0x00, 0x03, 0x16, 0x03, 0x01])],
                         [bytes([0xA1, 0x0F, 0x8A, 0xFF, 0x4A, 0xFF])]] + replies)
    return link, tp20.Tp20Channel(link, 0x25).open()


def test_module_channel_test_is_answered():
    # A door module sends A3 while reading DTCs and closes unless we answer A1.
    link, ch = open_scripted([[bytes([0xA3])], [bytes([0x10, 0x00, 0x02, 0x5A, 0x9A])]])
    ch.send([0x1A, 0x9A])
    assert link.sent[3][0] == 0xA1 and link.sent[3][1:] == ch.params
    assert ch.messages == [bytes([0x5A, 0x9A])]


def test_repeated_channel_tests_do_not_loop():
    link, ch = open_scripted([[bytes([0xA3])]] + [[bytes([0xA3])]] * 50)
    ch.send([0x1A, 0x9A])
    assert sum(1 for s in link.sent if s[0] == 0xA1) == 1


def test_failed_session_start_closes_channel():
    from types import SimpleNamespace

    from yeti_diag.cli import KwpSession
    link, ch = open_scripted([])
    ch.tx = None  # reopened by KwpSession below
    link.replies = [[bytes([0x00, 0xD0, 0x00, 0x03, 0x16, 0x03, 0x01])],
                    [bytes([0xA1, 0x0F, 0x8A, 0xFF, 0x4A, 0xFF])]]  # then silence
    ch.timeout = 0.05
    with pytest.raises(uds.NoResponse):
        with KwpSession(ch):
            pass
    assert link.sent[-1] == bytes([0xA8])


def test_packet_arriving_with_duplicate_ack_is_kept():
    # seq 0 is a repeat (we already expect seq 1); the ACK for it brings the real seq 1.
    link = ScriptedLink([[bytes([0x11, 0x00, 0x02, 0x5A, 0x9A])]])
    ch = tp20.Tp20Channel(link, 0x20)
    ch.tx, ch.rx, ch.rx_seq = 0x740, 0x300, 1
    ch._process([bytes([0x10, 0x00, 0x02, 0x5A, 0x9A])])
    assert ch.messages == [bytes([0x5A, 0x9A])]
    assert link.sent == [bytes([0xB1]), bytes([0xB2])]


def test_ack_after_response_pending_returns_on_the_answer():
    # ABS on the Vgate (2026-11-04): after 7F 22 78 the answer comes at once. The ACK must
    # use a reply count so the ELM returns on that packet, or the module closes the channel.
    class CountingLink(ScriptedLink):
        stepwise = True

        def exchange(self, tx, data, rx, timeout, expect=None, no_reply=False):
            self.expects = getattr(self, "expects", []) + [expect]
            return super().exchange(tx, data, rx, timeout, expect, no_reply)

    link = CountingLink([[bytes([0x13, 0x00, 0x03, 0x62, 0xF1, 0x87])]])
    ch = tp20.Tp20Channel(link, 0x03)
    ch.tx, ch.rx, ch.rx_seq = 0x790, 0x300, 2
    ch._process([bytes([0x12, 0x80, 0x03, 0x7F, 0x22, 0x78])])
    assert link.sent[0] == bytes([0xB3]) and link.expects[0] == 1
    assert ch.messages == [bytes([0x7F, 0x22, 0x78]), bytes([0x62, 0xF1, 0x87])]


def test_fast_mode_reads_the_same_over_the_elm():
    # Experimental Car Scanner style (block size 15, no reply counts) on the fake ELM.
    from yeti_diag.elm327 import Elm327
    elm = Elm327(serial_obj=FakeElmSerial())
    link = tp20.ElmLink(elm)
    for fast in (False, True):
        with tp20.Tp20Channel(link, SIM_TP20_DEST["62"], fast=fast) as ch:
            k = tp20.KwpClient(ch)
            k.start_session()
            assert ch.block_size == (0x0F if fast else 0x01)
            assert [d["code"] for d in k.read_dtcs()] == [VCDS_TRUTH["door_dtc"]]
            assert k.read_part() == "7N0959795E"
