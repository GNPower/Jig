"""Zynq-7000 CAN controllers (Jig.Xilinx.Zynq7000_CAN): two controllers on a
Jig CanBus exchanging frames at 1 Mbit/s, frame timing, the error counters
without acknowledgment, a bit-rate mismatch, loopback, acceptance filters,
harness frames, sleep and wake-up, and the reset values and ISR rules of
UG585 v1.7 B.5."""

from __future__ import annotations

import re

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

CAN0 = 0xE0008000
CAN1 = 0xE0009000
SRR, MSR, BRPR, BTR, ECR, ESR, SR, ISR = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C
IER, ICR, TCR, WIR = 0x20, 0x24, 0x28, 0x2C
TX_ID, TX_DLC, TX_DW1, TX_DW2 = 0x30, 0x34, 0x38, 0x3C
RX_ID, RX_DLC, RX_DW1, RX_DW2 = 0x50, 0x54, 0x58, 0x5C
AFR, AFMR1, AFIR1 = 0x60, 0x64, 0x68

CEN = 1 << 1
SRST = 1 << 0
SLEEP, LBACK = 1 << 0, 1 << 1

SR_CONFIG, SR_LBACK, SR_SLEEP, SR_NORMAL = 1 << 0, 1 << 1, 1 << 2, 1 << 3
SR_BIDLE, SR_ERRWRN = 1 << 4, 1 << 6
ESTAT_ACTIVE, ESTAT_PASSIVE = 0b01 << 7, 0b11 << 7

TXOK, RXOK, RXUFLW, RXNEMP, ERROR = 1 << 1, 1 << 4, 1 << 5, 1 << 7, 1 << 8
SLP, WKUP, TXFWMEMP, TXFEMP = 1 << 10, 1 << 11, 1 << 13, 1 << 14
ACKER = 1 << 4

RECORD = re.compile(r"(\d+):([0-9A-F]+):(E?R?)@(\S+):([0-9A-F]*)")

# 100 MHz reference: prescaler 10, 1 + 6 + 3 time quanta per bit = 1 Mbit/s.
BRPR_1M = 9
BTR_1M = 2 << 4 | 5

MACHINE = """\
can0: Jig.Xilinx.Zynq7000_CAN @ sysbus <0xE0008000, +0x1000>
    referenceClockFrequency: 100000000
    -> probe@0

can1: Jig.Xilinx.Zynq7000_CAN @ sysbus <0xE0009000, +0x1000>
    referenceClockFrequency: 100000000
    -> probe@1

probe: JigFixtures.GpioProbe @ sysbus
"""


@pytest.fixture
def can(bench: Bench) -> Bench:
    bench.load(MACHINE)
    bench.can_bus("can0", "can1")
    return bench


def bus(bench: Bench) -> str:
    return f"{bench.name}_can"


def start(bench: Bench, base: int, *, brpr: int = BRPR_1M, mode: int = 0) -> None:
    bench.write(base + MSR, mode)
    bench.write(base + BRPR, brpr)
    bench.write(base + BTR, BTR_1M)
    bench.write(base + SRR, CEN)


def standard_id(identifier: int) -> int:
    return identifier << 21


def extended_id(identifier: int) -> int:
    # IDH (the top 11 bits), SRR, IDE, IDL (the low 18 bits) as xcanps_hw.h lays them out.
    return (identifier >> 18) << 21 | 1 << 20 | 1 << 19 | (identifier & 0x3FFFF) << 1


def send(bench: Bench, base: int, word_id: int, data: bytes) -> None:
    padded = data.ljust(8, b"\x00")
    bench.write(base + TX_ID, word_id)
    bench.write(base + TX_DLC, len(data) << 28)
    bench.write(base + TX_DW1, int.from_bytes(padded[:4], "big"))
    bench.write(base + TX_DW2, int.from_bytes(padded[4:], "big"))


def receive(bench: Bench, base: int) -> tuple[int, bytes]:
    """Pops one message: (ID word, data)."""
    word_id = bench.read(base + RX_ID)
    length = bench.read(base + RX_DLC) >> 28
    first = bench.read(base + RX_DW1).to_bytes(4, "big")
    data = first + bench.read(base + RX_DW2).to_bytes(4, "big")
    return word_id, data[:length]


def test_reset_values(can: Bench) -> None:
    expected = {SRR: 0, MSR: 0, BRPR: 0, BTR: 0, ECR: 0, ESR: 0, SR: SR_CONFIG}
    expected |= {ISR: TXFEMP | TXFWMEMP, IER: 0, WIR: 0x3F3F, AFR: 0}
    assert {offset: can.read(CAN0 + offset) for offset in expected} == expected


def test_two_controllers_exchange_frames(can: Bench) -> None:
    start(can, CAN0)
    start(can, CAN1)
    assert can.read(CAN0 + SR) == SR_NORMAL | SR_BIDLE | ESTAT_ACTIVE
    send(can, CAN0, standard_id(0x123), bytes.fromhex("1122334455667788"))
    send(can, CAN0, extended_id(0x1ABCDEF5), b"\x99")
    can.run_us(300)
    assert receive(can, CAN1) == (standard_id(0x123), bytes.fromhex("1122334455667788"))
    assert receive(can, CAN1) == (extended_id(0x1ABCDEF5), b"\x99")
    assert can.read(CAN1 + ISR) & RXOK
    assert can.read(CAN0 + ISR) & TXOK
    # The harness record: "<us>:<ID>:<flags>@<sender>:<data>" per frame.
    frames = [RECORD.fullmatch(frame) for frame in can.text(f"{bus(can)} Read").split()]
    assert [(m[2], m[3], m[5]) for m in frames if m] == [
        ("123", "", "1122334455667788"),
        ("1ABCDEF5", "E", "99"),
    ]
    assert all(m and m[4].endswith("can0") for m in frames)


def test_a_frame_occupies_its_bit_time(can: Bench) -> None:
    start(can, CAN0)
    start(can, CAN1)
    send(can, CAN0, standard_id(0x7), bytes(8))  # 47 + 64 bits: 111 us
    can.run_us(105)
    assert can.read(CAN1 + ISR) & RXOK == 0
    assert can.read(CAN0 + SR) & SR_BIDLE == 0  # bus busy
    can.run_us(10)
    assert can.read(CAN1 + ISR) & RXOK


def test_unacknowledged_frames_raise_tec_to_error_passive(can: Bench) -> None:
    can.execute(f"{bus(can)} HarnessAcknowledges false")
    start(can, CAN0)  # can1 stays in configuration mode: nobody acknowledges
    send(can, CAN0, standard_id(0x10), b"")
    can.run_ms(3)  # 47 frame bits + 25 error-frame bits per attempt
    assert can.read(CAN0 + ECR) & 0xFF == 128  # stops at error passive (ISO 11898 rule 3)
    assert can.read(CAN0 + SR) & (0b11 << 7) == ESTAT_PASSIVE
    assert can.read(CAN0 + SR) & SR_ERRWRN
    assert can.read(CAN0 + ESR) & ACKER and can.read(CAN0 + ISR) & ERROR
    start(can, CAN1)  # an acknowledging node joins: the retransmission succeeds
    can.run_us(200)
    assert can.read(CAN0 + ECR) & 0xFF == 127
    assert can.read(CAN0 + SR) & (0b11 << 7) == ESTAT_ACTIVE
    assert receive(can, CAN1) == (standard_id(0x10), b"")


def test_disabling_clears_the_counters_and_events(can: Bench) -> None:
    can.execute(f"{bus(can)} HarnessAcknowledges false")
    start(can, CAN0)
    send(can, CAN0, standard_id(0x10), b"")
    can.run_us(500)
    assert can.read(CAN0 + ECR) > 0
    can.write(CAN0 + SRR, 0)
    assert can.read(CAN0 + ECR) == 0
    assert can.read(CAN0 + ISR) & ERROR == 0
    assert can.read(CAN0 + SR) == SR_CONFIG


def test_a_controller_at_the_wrong_rate_is_not_heard(can: Bench) -> None:
    start(can, CAN0, brpr=19)  # 500 kbit/s on a 1 Mbit/s bus
    start(can, CAN1)
    send(can, CAN0, standard_id(0x20), b"\x01")
    can.run_us(500)
    assert can.read(CAN1 + ISR) & RXOK == 0
    assert can.read(CAN0 + ECR) & 0xFF > 0


def test_loopback_keeps_frames_off_the_bus(can: Bench) -> None:
    start(can, CAN0, mode=LBACK)
    start(can, CAN1)
    assert can.read(CAN0 + SR) & SR_LBACK
    send(can, CAN0, standard_id(0x30), b"\x0a\x0b")
    can.run_us(200)
    assert receive(can, CAN0) == (standard_id(0x30), b"\x0a\x0b")
    assert can.read(CAN0 + ISR) & (TXOK | RXOK) == TXOK | RXOK
    assert can.read(CAN1 + ISR) & RXOK == 0
    assert can.text(f"{bus(can)} Read") == ""


def test_acceptance_filters(can: Bench) -> None:
    can.write(CAN1 + AFMR1, 0xFFE00000)  # compare the 11-bit standard ID
    can.write(CAN1 + AFIR1, standard_id(0x123))
    can.write(CAN1 + AFR, 0x1)
    start(can, CAN0)
    start(can, CAN1)
    send(can, CAN0, standard_id(0x124), b"\x01")
    send(can, CAN0, standard_id(0x123), b"\x02")
    can.run_us(300)
    assert receive(can, CAN1) == (standard_id(0x123), b"\x02")
    assert can.read(CAN1 + ISR) & RXNEMP == 0 or can.read(CAN1 + RX_ID) == 0


def test_harness_frames_reach_the_controllers(can: Bench) -> None:
    start(can, CAN1)
    assert can.text(f'{bus(can)} Write 0x55 "0102" false') == "ack"
    can.run_us(200)
    assert receive(can, CAN1) == (standard_id(0x55), b"\x01\x02")


def test_level_bits_survive_a_clear_while_their_condition_holds(can: Bench) -> None:
    start(can, CAN0)
    start(can, CAN1)
    can.write(CAN1 + ICR, TXFEMP)  # the TX FIFO is still empty
    assert can.read(CAN1 + ISR) & TXFEMP
    send(can, CAN0, standard_id(1), b"\x01")
    send(can, CAN0, standard_id(2), b"\x02")
    can.run_us(300)
    can.write(CAN1 + ICR, RXNEMP | RXOK)
    assert can.read(CAN1 + ISR) & (RXNEMP | RXOK) == RXNEMP  # two messages remain
    receive(can, CAN1)
    receive(can, CAN1)
    can.write(CAN1 + ICR, RXNEMP)
    assert can.read(CAN1 + ISR) & RXNEMP == 0
    can.read(CAN1 + RX_DW2)  # reading an empty FIFO
    assert can.read(CAN1 + ISR) & RXUFLW


def test_the_tx_watermark_counts_empty_slots(can: Bench) -> None:
    can.write(CAN0 + WIR, 62 << 8 | 0x3F)
    send(can, CAN0, standard_id(1), b"")  # queued in configuration mode
    send(can, CAN0, standard_id(2), b"")
    can.write(CAN0 + ICR, TXFWMEMP)
    assert can.read(CAN0 + ISR) & TXFWMEMP == 0  # 62 empty slots: not above 62
    start(can, CAN1)
    start(can, CAN0)
    can.run_us(80)  # one frame (47 bits) gone: 63 empty slots
    assert can.read(CAN0 + ISR) & TXFWMEMP
    can.write(CAN0 + SRR, 0)
    can.write(CAN0 + WIR, 0)  # only writable in configuration mode
    assert can.read(CAN0 + WIR) == 0


def test_sleep_ends_with_bus_activity(can: Bench) -> None:
    start(can, CAN0)
    start(can, CAN1)
    can.write(CAN1 + MSR, SLEEP)
    assert can.read(CAN1 + SR) & SR_SLEEP and can.read(CAN1 + ISR) & SLP
    send(can, CAN0, standard_id(0x40), b"\x40")
    can.run_us(200)
    assert can.read(CAN1 + ISR) & WKUP
    assert can.read(CAN1 + SR) & SR_NORMAL
    assert receive(can, CAN1) == (standard_id(0x40), b"\x40")


def test_the_interrupt_follows_ier(can: Bench) -> None:
    start(can, CAN0)
    start(can, CAN1)
    can.write(CAN1 + IER, RXOK)
    send(can, CAN0, standard_id(0x50), b"")
    can.run_us(100)
    assert can.flag("probe Level 1")
    can.write(CAN1 + ICR, RXOK)
    assert not can.flag("probe Level 1")


def test_software_reset(can: Bench) -> None:
    start(can, CAN0)
    can.write(CAN0 + IER, RXOK)
    can.write(CAN0 + SRR, SRST)
    assert can.read(CAN0 + SRR) == 0
    assert can.read(CAN0 + IER) == 0 and can.read(CAN0 + BRPR) == 0
    assert can.read(CAN0 + SR) == SR_CONFIG
