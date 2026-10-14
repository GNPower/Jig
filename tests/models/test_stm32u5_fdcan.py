"""STM32U5 FDCAN (Jig.ST.STM32U5_FDCAN, ST RM0456 "FD controller area network
(FDCAN)", a fork of Renode's STM32_FDCAN): extended acceptance filters
compare the full 29-bit identifier, FDCAN_XIDAM masks the identifier for
filter types 00, 01 and 10 but not 11, standard filters, RX FIFO 0 and 1,
transmission to another controller, and the interrupt lines. Frames come
from another controller or from the Jig CanBus harness node, on a 1 Mbit/s
bus.

The last tests pin the defects of Renode's STM32_FDCAN this fork fixes: they
fail once a Renode release fixes them."""

from __future__ import annotations

import re

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

CAN_A, RAM_A = 0x4000A400, 0x4000AC00
CAN_B, RAM_B = 0x5000A400, 0x5000AC00
CAN_S, RAM_S = 0x6000A400, 0x6000AC00  # Renode's own STM32_FDCAN

CCCR, IR, IE, ILS, ILE = 0x18, 0x50, 0x54, 0x58, 0x5C
RXGFC, XIDAM, RXF0S, RXF0A, RXF1S, RXF1A = 0x80, 0x84, 0x90, 0x94, 0x98, 0x9C
TXBAR, TXBTO = 0xCC, 0xD4
INIT, CCE = 1 << 0, 1 << 1

# Message RAM layout (RM0456): standard filters, extended filters, RX FIFO 0
# and 1 (three 72-byte elements each), TX buffers.
STANDARD_FILTERS, EXTENDED_FILTERS, RX_FIFO0, RX_FIFO1, TX_BUFFERS = 0x0, 0x70, 0xB0, 0x188, 0x278
ELEMENT = 72

STORE_FIFO0, STORE_FIFO1, REJECT = 0b001, 0b010, 0b011
RANGE, DUAL, CLASSIC, RANGE_NO_XIDAM = 0b00, 0b01, 0b10, 0b11
ANFS_REJECT, ANFE_REJECT = 0b10 << 4, 0b10 << 2
XTD = 1 << 30

RF0N, RF0F, RF1N, TC, TFE = 1 << 0, 1 << 1, 1 << 3, 1 << 7, 1 << 9

RECORD = re.compile(r"(\d+):([0-9A-F]+):(E?R?)@(\S+):([0-9A-F]*)")

MACHINE = """\
ramA: Memory.ArrayMemory @ sysbus 0x4000AC00
    size: 0x400

canA: Jig.ST.STM32U5_FDCAN @ sysbus 0x4000A400
    messageRam: ramA
    Int0 -> probe@0
    Int1 -> probe@1

ramB: Memory.ArrayMemory @ sysbus 0x5000AC00
    size: 0x400

canB: Jig.ST.STM32U5_FDCAN @ sysbus 0x5000A400
    messageRam: ramB
    Int0 -> probe@2
    Int1 -> probe@3

probe: JigFixtures.GpioProbe @ sysbus
"""

STOCK = """\
ramS: Memory.ArrayMemory @ sysbus 0x6000AC00
    size: 0x400

stockCan: CAN.STM32_FDCAN @ sysbus 0x6000A400
    series: .L5
    messageRam: ramS
"""


@pytest.fixture
def can(bench: Bench) -> Bench:
    bench.load(MACHINE)
    bench.can_bus("canA", "canB")
    return bench


def bus(bench: Bench) -> str:
    return f"{bench.name}_can"


def extended_filter(config: int, kind: int, id1: int, id2: int) -> tuple[int, int]:
    return config << 29 | id1, kind << 30 | id2


def standard_filter(kind: int, config: int, id1: int, id2: int) -> int:
    return kind << 30 | config << 27 | id1 << 16 | id2


def configure(
    bench: Bench,
    base: int,
    ram: int,
    *,
    standard: tuple[int, ...] = (),
    extended: tuple[tuple[int, int], ...] = (),
    global_filter: int = 0,
    xidam: int | None = None,
) -> None:
    """Load filters and the global filter configuration, then leave INIT."""
    bench.write(base + CCCR, INIT | CCE)
    for index, word in enumerate(standard):
        bench.write(ram + STANDARD_FILTERS + 4 * index, word)
    for index, (first, second) in enumerate(extended):
        bench.write(ram + EXTENDED_FILTERS + 8 * index, first)
        bench.write(ram + EXTENDED_FILTERS + 8 * index + 4, second)
    bench.write(base + RXGFC, len(extended) << 24 | len(standard) << 16 | global_filter)
    if xidam is not None:
        bench.write(base + XIDAM, xidam)
    bench.write(base + CCCR, 0)


def inject(bench: Bench, identifier: int, data: bytes, *, extended: bool) -> None:
    """A frame from the harness node, then time for it to cross the bus."""
    flag = "true" if extended else "false"
    assert bench.text(f'{bus(bench)} Write {identifier:#x} "{data.hex()}" {flag}') == "ack"
    bench.run_us(200)


def fill_level(bench: Bench, base: int, fifo: int = 0) -> int:
    return bench.read(base + (RXF0S if fifo == 0 else RXF1S)) & 0xF


def pop(bench: Bench, base: int, ram: int, fifo: int = 0) -> tuple[int, bytes]:
    """Reads the oldest element of an RX FIFO and acknowledges it: (R0, data)."""
    status = bench.read(base + (RXF0S if fifo == 0 else RXF1S))
    index = status >> 8 & 0x3
    element = ram + (RX_FIFO0 if fifo == 0 else RX_FIFO1) + ELEMENT * index
    word0 = bench.read(element)
    length = bench.read(element + 4) >> 16 & 0xF
    data = bench.read_bytes(element + 8, 8)[:length]
    bench.write(base + (RXF0A if fifo == 0 else RXF1A), index)
    return word0, data


def test_an_extended_filter_compares_all_29_bits(can: Bench) -> None:
    # The upstream report's case: a classic filter on identifier bits 19 to 23
    # (0x00780000 under mask 0x00F80000), non-matching frames rejected.
    filters = (extended_filter(STORE_FIFO0, CLASSIC, 0x00780000, 0x00F80000),)
    configure(can, CAN_A, RAM_A, extended=filters, global_filter=ANFS_REJECT | ANFE_REJECT)
    zero = (extended_filter(STORE_FIFO0, CLASSIC, 0x00000000, 0x00F80000),)
    configure(can, CAN_B, RAM_B, extended=zero, global_filter=ANFS_REJECT | ANFE_REJECT)
    inject(can, 0x00781234, b"\xaa", extended=True)
    assert fill_level(can, CAN_A) == 1
    assert fill_level(can, CAN_B) == 0
    assert pop(can, CAN_A, RAM_A) == (XTD | 0x00781234, b"\xaa")


@pytest.mark.parametrize(
    ("kind", "id1", "id2", "accepted"),
    [
        (RANGE, 0x12300, 0x12340, True),  # masked 0x12300 is in range
        (DUAL, 0x12300, 0x1FFFF, True),  # masked 0x12300 is the first ID
        (CLASSIC, 0x12300, 0x1FFFFFFF, True),  # masked 0x12300 equals the filter
        (RANGE_NO_XIDAM, 0x12300, 0x12340, False),  # 0x12345 itself is out of range
        (RANGE_NO_XIDAM, 0x12340, 0x12350, True),
    ],
)
def test_xidam_masks_every_extended_filter_type_but_11(
    can: Bench, kind: int, id1: int, id2: int, accepted: bool
) -> None:
    filters = (extended_filter(STORE_FIFO0, kind, id1, id2),)
    configure(can, CAN_A, RAM_A, extended=filters, global_filter=ANFE_REJECT, xidam=0x1FFFFF00)
    inject(can, 0x12345, b"\x01", extended=True)
    assert fill_level(can, CAN_A) == (1 if accepted else 0)


def test_extended_filters_store_reject_and_pick_a_fifo(can: Bench) -> None:
    filters = (
        extended_filter(REJECT, DUAL, 0x1ABCDEF5, 0x1ABCDEF5),
        extended_filter(STORE_FIFO1, RANGE, 0x1A000000, 0x1AFFFFFF),
    )
    configure(can, CAN_A, RAM_A, extended=filters, global_filter=ANFE_REJECT)
    inject(can, 0x1ABCDEF5, b"\x01", extended=True)  # the first matching filter rejects
    inject(can, 0x1ABCDEF6, b"\x02", extended=True)  # the second stores in FIFO 1
    inject(can, 0x00000001, b"\x03", extended=True)  # none matches: rejected
    assert fill_level(can, CAN_A, 0) == 0
    assert fill_level(can, CAN_A, 1) == 1
    assert pop(can, CAN_A, RAM_A, 1) == (XTD | 0x1ABCDEF6, b"\x02")


def test_standard_filters(can: Bench) -> None:
    filters = (
        standard_filter(RANGE, STORE_FIFO0, 0x100, 0x1FF),
        standard_filter(DUAL, STORE_FIFO1, 0x050, 0x060),
        standard_filter(CLASSIC, REJECT, 0x700, 0x700),
    )
    configure(can, CAN_A, RAM_A, standard=filters, global_filter=ANFS_REJECT)
    inject(can, 0x150, b"\x01", extended=False)
    inject(can, 0x060, b"\x02", extended=False)
    inject(can, 0x7AB, b"\x03", extended=False)  # rejected by the classic filter
    inject(can, 0x020, b"\x04", extended=False)  # matches nothing: rejected
    assert fill_level(can, CAN_A, 0) == 1
    assert fill_level(can, CAN_A, 1) == 1
    assert pop(can, CAN_A, RAM_A, 0) == (0x150 << 18, b"\x01")
    assert pop(can, CAN_A, RAM_A, 1) == (0x060 << 18, b"\x02")


def test_rx_fifo0_fills_and_drains_in_order(can: Bench) -> None:
    configure(can, CAN_A, RAM_A)  # non-matching frames go to FIFO 0 (reset value)
    can.execute(f'{bus(can)} WriteFrames "101::01 102::02 103::03 104::04"')
    can.run_us(800)
    status = can.read(CAN_A + RXF0S)
    assert status & 0xF == 3 and status >> 24 & 1  # F0FL = 3, F0F
    assert can.read(CAN_A + IR) & (RF0N | RF0F) == RF0N | RF0F
    received = [pop(can, CAN_A, RAM_A)]
    # A frame that arrived while the FIFO was full waits and enters as it
    # drains (kept from Renode's model: silicon in blocking mode drops it).
    can.run_us(100)
    while fill_level(can, CAN_A):
        received.append(pop(can, CAN_A, RAM_A))
    assert received == [(n << 18, bytes([n - 0x100])) for n in (0x101, 0x102, 0x103, 0x104)]


def test_a_transmitted_frame_reaches_the_other_controller(can: Bench) -> None:
    configure(can, CAN_A, RAM_A)
    configure(can, CAN_B, RAM_B)
    buffer = RAM_A + TX_BUFFERS
    can.write(buffer, XTD | 0x1ABCDEF5)  # T0: extended identifier
    can.write(buffer + 4, 3 << 16)  # T1: DLC 3
    can.write_bytes(buffer + 8, b"\x01\x02\x03\x00")
    can.write(CAN_A + TXBAR, 1)
    can.run_us(300)
    assert can.read(CAN_A + TXBTO) == 1
    assert can.read(CAN_A + IR) & (TC | TFE) == TC | TFE
    assert pop(can, CAN_B, RAM_B) == (XTD | 0x1ABCDEF5, b"\x01\x02\x03")
    frames = [RECORD.fullmatch(f) for f in can.text(f"{bus(can)} Read").split()]
    assert [(m[2], m[3], m[5]) for m in frames if m] == [("1ABCDEF5", "E", "010203")]
    assert all(m and m[4].endswith("canA") for m in frames)


def test_the_interrupt_lines_follow_ie_ils_and_ile(can: Bench) -> None:
    configure(can, CAN_A, RAM_A)
    can.write(CAN_A + IE, RF0N)
    can.write(CAN_A + ILS, 1)  # RXFIFO0 group on line 1
    can.write(CAN_A + ILE, 0b10)  # EINT1
    inject(can, 0x123, b"\x01", extended=False)
    assert can.flag("probe Level 1") and not can.flag("probe Level 0")
    can.write(CAN_A + IR, RF0N)  # write one to clear
    assert not can.flag("probe Level 1")


def stock_receives(bench: Bench, first: tuple[int, int], frame_id: int, xidam: int) -> int:
    """Renode's STM32_FDCAN with one extended filter: RX FIFO 0's fill level
    after one extended frame from the harness."""
    bench.load(STOCK)
    bench.can_bus("stockCan")
    configure(
        bench,
        CAN_S,
        RAM_S,
        extended=(first,),
        global_filter=ANFS_REJECT | ANFE_REJECT,
        xidam=xidam,
    )
    inject(bench, frame_id, b"\xaa", extended=True)
    return fill_level(bench, CAN_S)


def test_renodes_stock_fdcan_filters_a_16_bit_cast_of_extended_ids(bench: Bench) -> None:
    # Renode's STM32_FDCAN passes (ushort)message.Id to the filter, so a filter
    # on identifier bits 19 to 23 never matches its own address. When a Renode
    # release fixes it, this test fails.
    own = extended_filter(STORE_FIFO0, CLASSIC, 0x00780000, 0x00F80000)
    assert stock_receives(bench, own, 0x00781234, 0x1FFFFFFF) == 0


def test_renodes_stock_fdcan_accepts_frames_for_address_0(bench: Bench) -> None:
    # The same cast lets a node filtering on address 0 accept every frame.
    zero = extended_filter(STORE_FIFO0, CLASSIC, 0x00000000, 0x00F80000)
    assert stock_receives(bench, zero, 0x00781234, 0x1FFFFFFF) == 1


def test_renodes_stock_fdcan_masks_only_range_filters_with_xidam(bench: Bench) -> None:
    # Identifiers below 0x10000, so the cast plays no part: a dual-ID filter
    # ignores FDCAN_XIDAM in Renode's model.
    dual = extended_filter(STORE_FIFO0, DUAL, 0x2300, 0x2300)
    assert stock_receives(bench, dual, 0x2345, 0x1FFFFF00) == 0
