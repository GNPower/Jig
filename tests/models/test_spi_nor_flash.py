"""The test-only SPI NOR flash (tests/fixtures/models/SpiNorFlash.cs) that the
QSPI, SPI and BootROM tests use: its commands driven byte by byte through
ISPIPeripheral.Transmit, its SFDP tables read back against JESD216, and its
storage and power-cycle contracts."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from jig.errors import RenodeError

from .bench import Bench

pytestmark = pytest.mark.renode

SIZE = 0x100000  # 1 MiB: capacity code 20
MACHINE = """\
flash: JigFixtures.SpiNorFlash @ sysbus
    size: 0x100000
    manufacturerId: 0xA5
    memoryType: 0x5A
    pageProgramMicroseconds: 100
    sectorEraseMicroseconds: 1000
    blockEraseMicroseconds: 2000
    chipEraseMicroseconds: 5000
"""

BUSY = 0x01
WEL = 0x02


@pytest.fixture
def flash(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def command(bench: Bench, *data: int, end: bool = True) -> list[int]:
    """Clocks ``data`` into the flash one byte at a time; with ``end`` chip
    select then rises. Returns the bytes the flash sent back."""
    replies = [bench.number(f"flash Transmit {byte:#x}") for byte in data]
    if end:
        bench.execute("flash FinishTransmission")
    return replies


def address(value: int) -> tuple[int, int, int]:
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def status(bench: Bench) -> int:
    return command(bench, 0x05, 0x00)[1]


def read(bench: Bench, at: int, count: int, opcode: int = 0x03) -> bytes:
    dummy = () if opcode == 0x03 else (0x00,)
    replies = command(bench, opcode, *address(at), *dummy, *([0x00] * count))
    return bytes(replies[-count:])


def program(bench: Bench, at: int, data: bytes) -> None:
    command(bench, 0x06)
    command(bench, 0x02, *address(at), *data)


def pattern(size: int) -> bytes:
    return bytes((i * 7 + 3) & 0xFF for i in range(size))


def attach(bench: Bench, path: Path) -> None:
    bench.execute(f'flash AttachImage "{path.as_posix()}"')


def test_read_id_gives_manufacturer_type_and_capacity(flash: Bench) -> None:
    assert command(flash, 0x9F, 0, 0, 0, 0) == [0xFF, 0xA5, 0x5A, 20, 0x00]


def test_sfdp_describes_the_device(flash: Bench) -> None:
    replies = command(flash, 0x5A, 0, 0, 0, 0x00, *([0x00] * 52))
    table = bytes(replies[5:])
    signature, minor, major, headers, _ = struct.unpack_from("<4sBBBB", table, 0)
    assert (signature, major, minor, headers + 1) == (b"SFDP", 1, 0, 1)
    id_lsb, table_minor, table_major, length = struct.unpack_from("<BBBB", table, 8)
    pointer = int.from_bytes(table[12:15], "little")
    assert (id_lsb, table[15], table_major, table_minor, length) == (0x00, 0xFF, 1, 0, 9)
    bfpt = struct.unpack_from("<9I", table, pointer)
    assert bfpt[0] & 0x3 == 0b01  # uniform 4 KB erase
    assert (bfpt[0] >> 8) & 0xFF == 0x20
    assert bfpt[0] >> 16 & 1 and bfpt[0] >> 22 & 1  # 1-1-2 and 1-1-4 fast reads
    assert (bfpt[0] >> 17) & 0x3 == 0  # 3-byte addresses only
    assert bfpt[1] + 1 == SIZE * 8  # density in bits
    assert (bfpt[2] >> 24, (bfpt[2] >> 16) & 0x1F) == (0x6B, 8)  # 1-1-4: opcode, wait states
    assert (bfpt[3] >> 8 & 0xFF, bfpt[3] & 0x1F) == (0x3B, 8)  # 1-1-2
    erase_types = [(bfpt[7] >> (16 * n)) & 0xFFFF for n in range(2)]
    assert [(1 << (t & 0xFF), t >> 8) for t in erase_types] == [(0x1000, 0x20), (0x10000, 0xD8)]


def test_reads_with_and_without_dummy_clocks(flash: Bench, tmp_path: Path) -> None:
    image = tmp_path / "flash.img"
    image.write_bytes(pattern(SIZE))
    attach(flash, image)
    expected = pattern(SIZE)[0x1230:0x1238]
    for opcode in (0x03, 0x0B, 0x3B, 0x6B):
        assert read(flash, 0x1230, 8, opcode) == expected, hex(opcode)
    # Reads continue across the array and wrap at its end.
    assert read(flash, SIZE - 2, 4) == pattern(SIZE)[-2:] + pattern(SIZE)[:2]


def test_data_lines_follow_the_read_command(flash: Bench) -> None:
    for opcode, lines in ((0x0B, 1), (0x3B, 2), (0x6B, 4)):
        command(flash, opcode, 0, 0, 0, 0x00, 0x00, end=False)
        assert flash.number("flash CurrentDataLines") == lines
        flash.execute("flash FinishTransmission")
        assert flash.number("flash CurrentDataLines") == 1


def test_page_program_needs_write_enable_and_takes_time(flash: Bench) -> None:
    command(flash, 0x02, *address(0x100), 0x12, 0x34)  # no WEL: ignored
    assert status(flash) == 0
    assert read(flash, 0x100, 2) == b"\xff\xff"
    program(flash, 0x100, b"\x12\x34")
    assert status(flash) == BUSY | WEL
    assert command(flash, 0x9F, 0, 0, 0) == [0xFF] * 4  # busy: only READ STATUS answers
    flash.run_us(150)
    assert status(flash) == 0  # done: WEL cleared
    assert read(flash, 0x100, 2) == b"\x12\x34"
    program(flash, 0x100, b"\xf0\x0f")  # programming only clears bits
    flash.run_us(150)
    assert read(flash, 0x100, 2) == b"\x10\x04"


def test_page_program_wraps_inside_the_page(flash: Bench) -> None:
    program(flash, 0x2FE, b"\x01\x02\x03\x04")
    flash.run_us(150)
    assert read(flash, 0x2FE, 2) == b"\x01\x02"
    assert read(flash, 0x200, 2) == b"\x03\x04"
    assert read(flash, 0x300, 1) == b"\xff"


@pytest.mark.parametrize(
    ("opcode", "block", "microseconds"), [(0x20, 0x1000, 1000), (0xD8, 0x10000, 2000)]
)
def test_erase_clears_the_block_holding_the_address(
    flash: Bench, tmp_path: Path, opcode: int, block: int, microseconds: int
) -> None:
    image = tmp_path / "flash.img"
    image.write_bytes(bytes(SIZE))  # all zero
    attach(flash, image)
    command(flash, 0x06)
    command(flash, opcode, *address(3 * block + 0x10))
    assert status(flash) == BUSY | WEL
    flash.run_us(microseconds - 10)
    assert status(flash) & BUSY
    flash.run_us(20)
    assert status(flash) == 0
    assert read(flash, 3 * block, 2) == b"\xff\xff"
    assert read(flash, 4 * block - 1, 2) == b"\xff\x00"
    assert read(flash, 3 * block - 1, 1) == b"\x00"
    # Written through to the image file.
    data = image.read_bytes()
    assert data[3 * block : 4 * block] == b"\xff" * block
    assert data[4 * block] == 0


def test_chip_erase(flash: Bench, tmp_path: Path) -> None:
    image = tmp_path / "flash.img"
    image.write_bytes(bytes(SIZE))
    attach(flash, image)
    command(flash, 0x06)
    command(flash, 0xC7)
    flash.run_us(5010)
    assert status(flash) == 0
    assert image.read_bytes() == b"\xff" * SIZE


def test_block_protect_bits_are_non_volatile(flash: Bench) -> None:
    command(flash, 0x01, 0x1C)  # no WEL: ignored
    assert status(flash) == 0
    command(flash, 0x06)
    command(flash, 0x01, 0x1C)
    assert status(flash) == 0x1C
    flash.execute("flash PowerCycle")
    assert status(flash) == 0x1C


def test_a_power_cycle_drops_the_operation_in_progress(flash: Bench) -> None:
    program(flash, 0x40, b"\x00")
    flash.execute("flash PowerCycle")
    assert status(flash) == 0
    flash.run_us(150)
    assert read(flash, 0x40, 1) == b"\xff"
    command(flash, 0x06)
    flash.execute("flash PowerCycle")
    assert status(flash) == 0  # WEL is volatile


def test_reset_ends_a_command_without_running_it(flash: Bench) -> None:
    command(flash, 0x06)
    command(flash, 0x20, *address(0), end=False)
    flash.execute("flash Reset")
    flash.execute("flash FinishTransmission")
    assert status(flash) == WEL  # no erase started; the latch stays
    assert command(flash, 0x9F, 0)[1] == 0xA5  # the next byte is an opcode again


def test_an_attached_image_is_the_storage(flash: Bench, tmp_path: Path) -> None:
    assert flash.number("flash Size") == SIZE
    short = tmp_path / "short.img"
    short.write_bytes(b"\x11\x22\x33")
    attach(flash, short)
    assert read(flash, 0, 4) == b"\x11\x22\x33\xff"  # past the file's end: erased
    assert short.stat().st_size == SIZE  # extended to the device size
    program(flash, 0x10, b"\x5a")
    flash.run_us(150)
    assert short.read_bytes()[0x10] == 0x5A  # written through
    fresh = tmp_path / "fresh.img"  # a missing file takes the current contents
    attach(flash, fresh)
    assert fresh.read_bytes()[:4] == b"\x11\x22\x33\xff"
    assert flash.text("flash ImageFile").endswith("fresh.img")
    program(flash, 0x20, b"\x00")
    flash.run_us(150)
    assert fresh.read_bytes()[0x20] == 0 and short.read_bytes()[0x20] == 0xFF
    too_big = tmp_path / "big.img"
    too_big.write_bytes(bytes(SIZE + 1))
    with pytest.raises(RenodeError, match="larger than the flash"):
        attach(flash, too_big)
    flash.execute("flash PowerCycle")
    assert read(flash, 0x10, 1) == b"\x5a"  # contents survive a power cycle


def test_sizes_outside_three_byte_addressing_are_refused(bench: Bench) -> None:
    with pytest.raises(RenodeError, match="power of two"):
        bench.load("flash: JigFixtures.SpiNorFlash @ sysbus\n    size: 0x2000000\n")
