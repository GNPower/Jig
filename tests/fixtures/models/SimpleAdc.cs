//
// Test fixture: an ideal memory-mapped analog-to-digital converter that
// implements the IAnalogInputs and IPowerCycled contracts.
//
// Registers (32-bit): channel n's code at 4 * n; at 0x80 the number of code
// reads since power-up (volatile state, for power-cycle tests). Transfer
// function: code = round(volts / referenceVoltage * 2^bits), limited to
// 0..2^bits - 1. The device has no reset input: Reset changes nothing, and
// PowerCycle clears the read count but keeps the inputs.
//
//   adc: JigFixtures.SimpleAdc @ sysbus <0x40020000, +0x100>
//       channels: 8
//
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Jig.Contracts;

namespace Antmicro.Renode.Peripherals.JigFixtures
{
    public sealed class SimpleAdc : IAnalogInputs, IPowerCycled, IDoubleWordPeripheral, IKnownSize
    {
        public SimpleAdc(IMachine machine, int channels = 8, int bits = 12, double referenceVoltage = 2.5)
        {
            if(channels < 1 || channels > MaximumChannels || bits < 1 || bits > 24 || referenceVoltage <= 0)
            {
                throw new ConstructionException($"channels 1-{MaximumChannels}, bits 1-24 and a positive reference voltage are required");
            }
            ChannelCount = channels;
            this.bits = bits;
            this.referenceVoltage = referenceVoltage;
            volts = new double[channels];
        }

        public long Size => 0x100;

        public int ChannelCount { get; }

        public int Reads { get; private set; }

        public void Reset()
        {
        }

        public void PowerCycle()
        {
            Reads = 0;
        }

        public void SetChannelVoltage(int channel, double value)
        {
            Check(channel);
            volts[channel] = value;
        }

        public int GetChannelCode(int channel)
        {
            Check(channel);
            return CodeFor(volts[channel]);
        }

        public int CodeFor(double value)
        {
            var fullScale = (1 << bits) - 1;
            var code = Math.Round(value / referenceVoltage * (1 << bits));
            return (int)Math.Max(0, Math.Min(fullScale, code));
        }

        public uint ReadDoubleWord(long offset)
        {
            if(offset == ReadCountOffset)
            {
                return (uint)Reads;
            }
            var channel = (int)(offset / 4);
            if(offset % 4 != 0 || channel >= ChannelCount)
            {
                this.Log(LogLevel.Warning, "Read from unhandled offset 0x{0:X2}", offset);
                return 0;
            }
            Reads++;
            return (uint)GetChannelCode(channel);
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            this.Log(LogLevel.Warning, "Write 0x{0:X8} to read-only offset 0x{1:X2} ignored", value, offset);
        }

        private void Check(int channel)
        {
            if(channel < 0 || channel >= ChannelCount)
            {
                throw new RecoverableException($"Channel {channel} does not exist (0-{ChannelCount - 1})");
            }
        }

        private readonly int bits;
        private readonly double referenceVoltage;
        private readonly double[] volts;

        private const int MaximumChannels = 32;
        private const long ReadCountOffset = 0x80;
    }
}
