"""A handful of A32 instruction encodings for the short programs some model
tests run (ARM Architecture Reference Manual ARMv7-A/R, ARM DDI 0406C, chapter
A8, encoding A1 of each instruction, condition AL). PC reads as the address of
the instruction plus 8."""

from __future__ import annotations

import struct

_AL = 0xE << 28


def ldr_literal(rt: int, offset: int) -> int:
    """LDR Rt, [PC, #offset]: loads the word at this instruction + 8 + offset."""
    up = 1 if offset >= 0 else 0
    return _AL | 0x05100000 | up << 23 | 0xF << 16 | rt << 12 | (abs(offset) & 0xFFF)


def ldr(rt: int, rn: int, offset: int = 0) -> int:
    """LDR Rt, [Rn, #offset]."""
    up = 1 if offset >= 0 else 0
    return _AL | 0x05100000 | up << 23 | rn << 16 | rt << 12 | (abs(offset) & 0xFFF)


def str_(rt: int, rn: int, offset: int = 0) -> int:
    """STR Rt, [Rn, #offset]."""
    up = 1 if offset >= 0 else 0
    return _AL | 0x05000000 | up << 23 | rn << 16 | rt << 12 | (abs(offset) & 0xFFF)


def mov(rd: int, imm8: int) -> int:
    """MOV Rd, #imm8."""
    return _AL | 0x03A00000 | rd << 12 | (imm8 & 0xFF)


def add(rd: int, rn: int, imm8: int) -> int:
    """ADD Rd, Rn, #imm8."""
    return _AL | 0x02800000 | rn << 16 | rd << 12 | (imm8 & 0xFF)


def branch_to_self() -> int:
    """B . (an endless loop)."""
    return _AL | 0x0AFFFFFE


def words(*values: int) -> bytes:
    """Little-endian 32-bit words."""
    return struct.pack(f"<{len(values)}I", *values)
