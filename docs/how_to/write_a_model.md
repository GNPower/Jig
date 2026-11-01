# Write a C# model against the contracts

A board's chips outside the SoC (flash, converters, clocks) need Renode
models before firmware can talk to them, and a plan can only drive them
through the interfaces Jig defines for that purpose. This guide writes a
small analog-to-digital converter model, wires it into a board, and lets
plans set its inputs.

## Where models live and how they load

Keep the `.cs` files in the board's repository and list them in the
board file's `models` setting, as globs relative to the board file:

```jig
board "my-board" {
    models: ["../../models/*.cs"]
    device mcu: stm32(part: "STM32F407VG") {
        firmware: "build/app.elf"
    }
}
```

When a session starts, Renode compiles Jig's own models first, then each
board repository's models in one set, against the assemblies compiled
before it. Boards from one repository share one set. Renode's own C#
compiler does the work, so no .NET SDK is needed to run plans. A compiler
error stops the run with exit code 3 and names the file, line and column.

Put the classes in a namespace under `Antmicro.Renode.Peripherals`, such
as `Antmicro.Renode.Peripherals.Acme.Sensors`. Renode looks up the type
names in platform descriptions only under `Antmicro.Renode`, and a
platform description names a type relative to
`Antmicro.Renode.Peripherals`: `Acme.Sensors.ExampleAdc`. A namespace per
organisation or repository keeps two repositories from defining one type
twice, which fails the compile.

## The contracts a plan relies on

Plans reach board chips through board file settings, and each setting
needs the peripheral it names to implement one interface from
`Antmicro.Renode.Peripherals.Jig.Contracts`:

```{list-table}
:header-rows: 1
:widths: 20 40 40

* - Board file
  - Interface and members
  - What plans do with it
* - `storage`
  - `IStorage`: `Size` in bytes, and `AttachImage(path)`, which makes a
    raw image file the device's contents and writes every change back to
    it
  - Start a run from an image file; keep writes across resets
* - Every peripheral off `backup_supply`
  - `IPowerCycled`: `PowerCycle()`, which returns every volatile register
    to its power-up value and keeps non-volatile contents and external
    inputs
  - Power the chip down and up with the board; a chip whose model
    lacks the interface keeps running
* - `sensor` lines
  - `IAnalogInputs`: `ChannelCount`, `SetChannelVoltage(channel, volts)`,
    `GetChannelCode(channel)` and `CodeFor(volts)`, the code an input
    voltage converts to
  - `board.set_sensor`, `board.drive`, `board.sensor_code`
* - `rtc`
  - `IRealTimeClock`: `SetDateTime(text)` and `GetDateTime()`, as ISO 8601
    calendar time
  - `board.set_time`, `board.time()`
* - (Quad-SPI flash)
  - `ISpiNorFlash`: size, data lines in use, linear reads, power cycle
  - Boot through Jig's Zynq-7000 BootROM and Quad-SPI models
```

`jig check --contracts` builds each board's machine in Renode, without
booting it, and confirms that every peripheral a board file setting
names implements the interface that setting needs, that a storage
device's `Size` equals the `size` its peripheral line gives, and that
each sensor's channel is below the converter's `ChannelCount`. A missing
method shows up there, before a plan reaches it. Each interface's
documentation comments in `jig/models/Contracts` give the units, the
ranges and what survives a reset or a power cycle.

## Write the model

The converter in this guide is generic: eight inputs, 12-bit codes, a
3.3 V reference. The controller sends a channel number in the first byte
of a transfer and reads the code in the next two bytes, most significant
byte first.

```csharp
//
// Generic 8-channel, 12-bit SPI ADC: byte 0 selects the channel, bytes 1
// and 2 return its code, most significant byte first.
//
using System;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Peripherals.Jig.Contracts;
using Antmicro.Renode.Peripherals.SPI;

namespace Antmicro.Renode.Peripherals.Acme.Sensors
{
    public sealed class ExampleAdc : ISPIPeripheral, IAnalogInputs,
        IPowerCycled
    {
        public ExampleAdc(double referenceVolts = 3.3)
        {
            this.referenceVolts = referenceVolts;
        }

        public int ChannelCount => inputs.Length;

        public void Reset()
        {
            // The input voltages come from outside the chip and stay.
            position = 0;
            selected = 0;
        }

        public void PowerCycle()
        {
            Reset();
        }

        public byte Transmit(byte data)
        {
            switch(position++)
            {
            case 0:
                selected = data & 0x7;
                return 0;
            case 1:
                return (byte)(GetChannelCode(selected) >> 8);
            default:
                return (byte)GetChannelCode(selected);
            }
        }

        public void FinishTransmission()
        {
            position = 0;
        }

        public void SetChannelVoltage(int channel, double volts)
        {
            inputs[Checked(channel)] = volts;
        }

        public int GetChannelCode(int channel)
        {
            return CodeFor(inputs[Checked(channel)]);
        }

        public int CodeFor(double volts)
        {
            var code = (int)Math.Round(volts / referenceVolts * 4095);
            return Math.Max(0, Math.Min(4095, code));
        }

        private int Checked(int channel)
        {
            if(channel < 0 || channel >= inputs.Length)
            {
                throw new RecoverableException(
                    $"channel {channel} is not 0 to {inputs.Length - 1}");
            }
            return channel;
        }

        private readonly double[] inputs = new double[8];
        private readonly double referenceVolts;
        private int selected;
        private int position;
    }
}
```

Cite the datasheet section each behaviour comes from in the file header.
Where a reset value or a timing is a guess, say so in a comment, so a
later reader knows which numbers came from the part's documentation and
which did not.

## Wire it into the board

Declare the model as a peripheral of the board, wire it to chip select 0
of the Zynq-7000's first SPI controller, and name its inputs:

```jig
board "my-board" {
    models: ["../../models/*.cs"]
    device zynq: zynq7000(part: "XC7Z020-CLG484",
                          clocks: {cpu_3x2x: 333.333333 MHz,
                                   cpu_1x: 111.111111 MHz}) {
        firmware: {app: "build/app.elf"}
    }
    peripheral adc0: "Acme.Sensors.ExampleAdc" (referenceVolts: 3.3)
    link housekeeping: spi(controller: zynq.spi0, targets: {0: adc0})
    sensor supply: adc0.channel[2]
    sensor temperature: adc0.channel[5]
}
```

Jig writes `adc0: Acme.Sensors.ExampleAdc @ spi0 0` into the platform
description with `referenceVolts: 3.3`. The model implements
`IPowerCycled`, so it restarts when the board's power cycles; listing
it in `backup_supply` would keep it running.

A plan can now set an input and learn the code the firmware will read:

```jig
test "the supply input reads back" {
    let code = dut.set_sensor("supply", 1.25 V)
    check code == dut.sensor_code("supply", volts: 1.25 V)
}
```

## Check the model

```bash
jig models check --board boards/my-board
jig models check --sdk --board boards/my-board
jig models ide --board boards/my-board
```

The first command compiles the board's model set in a throwaway Renode,
the way a run does. `--sdk` builds it with the .NET SDK instead, with
warnings treated as errors, which catches mistakes Renode's compiler lets
through. `jig models ide` writes a project file that references the
Renode assemblies Jig found, so an editor can complete Renode's types.
