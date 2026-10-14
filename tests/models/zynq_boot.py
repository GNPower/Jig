"""Zynq-7000 boot images for the BootROM model tests: a Boot Header laid out
as UG585 v1.7 section 6.3.2 (Table 6-5) gives it, then the FSBL/User code.

Header words used: 0x000-0x01C interrupt table (branches to self here),
0x020 width detection 0xAA995566, 0x024 image identification 0x584C4E58
("XLNX"), 0x028 encryption status, 0x02C user defined, 0x030 source offset,
0x034 length of image, 0x038 reserved (0), 0x03C start of execution, 0x040
total image length, 0x044 reserved (0), 0x048 header checksum (the inverted
sum of the words 0x020-0x044), 0x0A0-0x89F register initialization pairs
(address 0xFFFFFFFF ends the list). The code starts at the source offset,
0x8C0 or above and 64-byte aligned.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence

HEADER_SIZE = 0x8C0
WIDTH_DETECTION = 0xAA995566
IMAGE_IDENTIFICATION = 0x584C4E58
REGISTER_INIT_OFFSET = 0x0A0
REGISTER_INIT_PAIRS = 256
END_OF_LIST = 0xFFFFFFFF
BRANCH_TO_SELF = 0xEAFFFFFE
HEADER_SEARCH_STEP = 0x8000  # UG585 6.3.9: the header search steps in 32 KB


def checksum(words: Sequence[int]) -> int:
    return ~sum(words) & 0xFFFFFFFF


def boot_image(
    code: bytes,
    *,
    start: int = 0,
    source_offset: int = HEADER_SIZE,
    register_init: Sequence[tuple[int, int]] = (),
    length: int | None = None,
    total_length: int | None = None,
    encryption: int = 0,
    reserved_038: int = 0,
    header_checksum: int | None = None,
    user_defined: int = 0x01010000,
) -> bytes:
    """A header followed by ``code``. Keyword arguments override the fields the
    tests probe; by default every field is valid for a non-secure boot from
    OCM (UG585 6.3.2)."""
    if len(register_init) > REGISTER_INIT_PAIRS:
        raise ValueError(f"at most {REGISTER_INIT_PAIRS} register initialization pairs")
    length = len(code) if length is None else length
    total_length = length if total_length is None else total_length
    header = bytearray(HEADER_SIZE)
    struct.pack_into("<8I", header, 0x000, *([BRANCH_TO_SELF] * 8))
    fields = [
        WIDTH_DETECTION,
        IMAGE_IDENTIFICATION,
        encryption,
        user_defined,
        source_offset,
        length,
        reserved_038,
        start,
        total_length,
        0,
    ]
    struct.pack_into("<10I", header, 0x020, *fields)
    struct.pack_into(
        "<I", header, 0x048, checksum(fields) if header_checksum is None else header_checksum
    )
    pairs = list(register_init) + [(END_OF_LIST, 0)] * (REGISTER_INIT_PAIRS - len(register_init))
    for index, (address, value) in enumerate(pairs):
        struct.pack_into("<II", header, REGISTER_INIT_OFFSET + 8 * index, address, value)
    struct.pack_into("<8I", header, 0x8A0, *([0xFFFFFFFF] * 8))
    image = bytearray(header)
    if source_offset > HEADER_SIZE:
        image += b"\xff" * (source_offset - HEADER_SIZE)
    image += code
    return bytes(image)


def flash_image(images: dict[int, bytes], size: int) -> bytes:
    """A flash array of ``size`` erased bytes with ``images`` at their offsets."""
    array = bytearray(b"\xff" * size)
    for offset, data in images.items():
        array[offset : offset + len(data)] = data
    return bytes(array)
