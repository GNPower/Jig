"""STM32U5 ADC4 (Jig.ST.STM32U5_ADC4: Renode's STM32WBA_ADC, the same ST block,
ST RM0456 "12-bit analog-to-digital converter (ADC4)", with the IAnalogInputs
contract): CodeFor and GetChannelCode agree with a conversion, at 12 and 10
bits and left-aligned, inputs are limited to 0 V and the reference, a bad
channel is refused, and inputs set through SetChannelVoltage survive machine
resets.

The last test pins the Renode behaviour the model works around: it fails
once a Renode release keeps the inputs."""

from __future__ import annotations

import pytest

from jig.errors import RenodeError

from .bench import Bench

pytestmark = pytest.mark.renode

ADC4 = 0x46021000
ISR, CR, CFGR1, CHSELR, DR = 0x00, 0x08, 0x0C, 0x28, 0x40
ADEN, ADSTART = 1 << 0, 1 << 2
EOC = 1 << 2
RES_10_BITS, ALIGN_LEFT = 0b01 << 2, 1 << 5

MACHINE = """\
adc4: Jig.ST.STM32U5_ADC4 @ sysbus 0x46021000
    referenceVoltage: 3.3
    externalEventFrequency: 1000
"""


@pytest.fixture
def adc(bench: Bench) -> Bench:
    bench.load(MACHINE)
    return bench


def convert(bench: Bench, channel: int, configuration: int = 0) -> int:
    """One software-triggered conversion of ``channel``: the data register."""
    bench.write(ADC4 + CFGR1, configuration)
    bench.write(ADC4 + CR, ADEN)
    bench.write(ADC4 + CHSELR, 1 << channel)
    bench.write(ADC4 + CR, ADEN | ADSTART)
    assert bench.read(ADC4 + ISR) & EOC
    return bench.read(ADC4 + DR)


def test_the_contract_query(adc: Bench) -> None:
    assert adc.text('machine JigContracts "adc4"') == "found Jig.ST.STM32U5_ADC4 IAnalogInputs=14"
    assert adc.number("adc4 ChannelCount") == 14


def test_a_conversion_reports_the_code_for_the_input(adc: Bench) -> None:
    adc.execute("adc4 SetChannelVoltage 13 0.736")
    # 736 mV of 3300 mV at 12 bits: round(736 / 3300 * 4095) = 913
    assert adc.number("adc4 GetChannelCode 13") == 913
    assert adc.number("adc4 CodeFor 0.736") == 913
    assert convert(adc, 13) == 913


@pytest.mark.parametrize(
    ("configuration", "code"),
    [(RES_10_BITS, 228), (ALIGN_LEFT, 913 << 4), (RES_10_BITS | ALIGN_LEFT, 228 << 6)],
)
def test_the_code_follows_resolution_and_alignment(
    adc: Bench, configuration: int, code: int
) -> None:
    adc.execute("adc4 SetChannelVoltage 2 0.736")
    assert convert(adc, 2, configuration) == code
    assert adc.number("adc4 GetChannelCode 2") == code


def test_inputs_are_limited_to_the_reference(adc: Bench) -> None:
    adc.execute("adc4 SetChannelVoltage 0 5.0")
    adc.execute("adc4 SetChannelVoltage 1 -1.0")
    assert convert(adc, 0) == 4095
    assert convert(adc, 1) == 0
    assert adc.number("adc4 CodeFor 3.3") == 4095


def test_an_unset_channel_reads_0_volts(adc: Bench) -> None:
    assert adc.number("adc4 GetChannelCode 5") == 0
    assert convert(adc, 5) == 0


@pytest.mark.parametrize("channel", [-1, 14])
def test_a_bad_channel_is_refused(adc: Bench, channel: int) -> None:
    with pytest.raises(RenodeError, match="channels 0 to 13"):
        adc.execute(f"adc4 SetChannelVoltage {channel} 1.0")


def test_inputs_survive_machine_resets(adc: Bench) -> None:
    adc.execute("adc4 SetChannelVoltage 13 0.736")
    adc.execute("adc4 SetChannelVoltage 3 1.65")
    for _ in range(2):
        adc.execute("machine Reset")
        assert convert(adc, 13) == 913
        assert convert(adc, 3) == 2048  # round(1650 / 3300 * 4095)


def test_a_channel_source_from_the_platform_description(bench: Bench) -> None:
    # A board can put Renode's ADCChannelSource on a channel, with the voltage
    # it returns to at reset; SetChannelVoltage then holds over that.
    source = "ch5: Analog.ADCChannelSource @ adc4 5\n    defaultValueMicroVolts: 736000\n"
    bench.load(MACHINE + "\n" + source)
    assert convert(bench, 5) == 913
    bench.execute("machine Reset")
    assert convert(bench, 5) == 913
    bench.execute("adc4 SetChannelVoltage 5 1.65")
    bench.execute("machine Reset")
    assert convert(bench, 5) == 2048


STOCK = """\
stockAdc: Analog.STM32WBA_ADC @ sysbus 0x46021000
    referenceVoltage: 3.3
    externalEventFrequency: 1000
"""


def test_renodes_stock_adc_returns_its_inputs_to_0_volts_on_reset(bench: Bench) -> None:
    # Renode resets each channel's voltage source with the machine. When a
    # Renode release keeps the inputs, this test fails.
    bench.load(STOCK)
    bench.execute("stockAdc SetVoltage 736000 13")  # microvolts
    assert convert(bench, 13) == 913
    bench.execute("machine Reset")
    assert convert(bench, 13) == 0
