"""Zynq-7000 eFuse status (Jig.Xilinx.Zynq7000_EFuse): EFUSE_STATUS bit 7
(single CPU, which the standalone BSP's boot.S checks) and bit 10 (RSA
authentication, which the FSBL checks) follow the model's parameters."""

from __future__ import annotations

import pytest

from .bench import Bench

pytestmark = pytest.mark.renode

EFUSE_STATUS = 0xF800D010
SINGLE_CPU = 1 << 7
RSA_ENABLE = 1 << 10


@pytest.mark.parametrize(
    ("parameters", "status"),
    [
        ("", 0),
        ("\n    singleCpu: true", SINGLE_CPU),
        ("\n    rsaEnabled: true", RSA_ENABLE),
        ("\n    singleCpu: true\n    rsaEnabled: true", SINGLE_CPU | RSA_ENABLE),
    ],
)
def test_status_bits_follow_the_parameters(bench: Bench, parameters: str, status: int) -> None:
    bench.load(f"efuse: Jig.Xilinx.Zynq7000_EFuse @ sysbus <0xF800D000, +0x1000>{parameters}\n")
    assert bench.read(EFUSE_STATUS) == status


def test_the_status_is_read_only(bench: Bench) -> None:
    bench.load("efuse: Jig.Xilinx.Zynq7000_EFuse @ sysbus <0xF800D000, +0x1000>\n")
    bench.write(EFUSE_STATUS, 0xFFFFFFFF)
    assert bench.read(EFUSE_STATUS) == 0
