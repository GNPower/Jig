//
// ADC4 (the 12-bit analog-to-digital converter) of the STM32U5 series: Renode's
// STM32WBA_ADC, the same ST block at the same registers (ST RM0456, chapter
// "12-bit analog-to-digital converter (ADC4)"), with the inputs a test sets
// kept across resets, so that it meets the IAnalogInputs contract.
//
// Renode resets each channel's voltage source to 0 V with the machine, while
// the voltage at a pin comes from outside the chip and stays. This model
// remembers every voltage set through SetChannelVoltage and puts it back right
// after its channel's source resets, on any machine reset: one the harness
// commands, a watchdog's, or AIRCR.SYSRESETREQ.
//
// CodeFor and GetChannelCode give the code a conversion reports with the
// resolution and alignment ADC4_CFGR1 selects: Renode's conversion (whole
// millivolts, scaled by the reference voltage to 2^n - 1, rounded half to
// even) with the input limited to 0 V and the reference voltage.
//
// Everything else is Renode's STM32WBA_ADC, including its gaps: conversions
// take no simulated time, and with CFGR1.CHSELRMOD = 1 it reads CHSELR as a
// channel bitmap, so it converts the channels whose bits the SQx fields set
// instead of the channels they name.
//
using System;

using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Peripherals.Analog;
using Antmicro.Renode.Peripherals.DMA;
using Antmicro.Renode.Peripherals.Jig.Contracts;
using Antmicro.Renode.Peripherals.Sensor;

namespace Antmicro.Renode.Peripherals.Jig.ST
{
    public class STM32U5_ADC4 : STM32WBA_ADC, IAnalogInputs
    {
        public STM32U5_ADC4(IMachine machine, double referenceVoltage, uint externalEventFrequency, int dmaChannel = 0, IDMA dmaPeripheral = null)
            : base(machine, referenceVoltage, externalEventFrequency, dmaChannel, dmaPeripheral)
        {
            this.referenceVoltage = referenceVoltage;
            inputs = new double?[ADCChannelCount];
            machine.PeripheralReset += OnPeripheralReset;
        }

        public int ChannelCount => ADCChannelCount;

        public void SetChannelVoltage(int channel, double volts)
        {
            CheckChannel(channel);
            inputs[channel] = volts;
            Apply(channel);
        }

        public int GetChannelCode(int channel)
        {
            CheckChannel(channel);
            return CodeFor(inputs[channel] ?? 0);
        }

        public int CodeFor(double volts)
        {
            // ADC4_CFGR1: RES in bits 3:2 (12, 10, 8 or 6 bits), ALIGN in bit 5
            var configuration = RegistersCollection.Read(Configuration1Offset);
            var bits = 12 - 2 * (int)((configuration >> 2) & 0x3);
            var millivolts = (double)(Microvolts(volts) / 1000);
            var code = (uint)Math.Round(millivolts / (referenceVoltage * 1000) * ((1 << bits) - 1));
            if((configuration & LeftAlignment) != 0)
            {
                code <<= 16 - bits;
            }
            return (int)code;
        }

        private void CheckChannel(int channel)
        {
            if(channel < 0 || channel >= ADCChannelCount)
            {
                throw new RecoverableException($"ADC4 has channels 0 to {ADCChannelCount - 1}, not {channel}");
            }
        }

        // The input as the channel's source holds it: microvolts, limited to
        // 0 V and the reference voltage.
        private uint Microvolts(double volts)
        {
            var limited = Math.Max(0, Math.Min(volts, referenceVoltage));
            return (uint)Math.Round(limited * 1e6);
        }

        private void Apply(int channel)
        {
            if(inputs[channel] is double volts)
            {
                this.SetVoltage(Microvolts(volts), channel);
            }
        }

        // Machine.PeripheralReset follows each peripheral's Reset, the channel
        // sources' included, on every reset path.
        private void OnPeripheralReset(IMachine machine, IPeripheral peripheral)
        {
            for(var channel = 0; channel < ADCChannelCount; channel++)
            {
                if(inputs[channel].HasValue && ADCContainer.TryGetByAddress(channel, out var source) && ReferenceEquals(source, peripheral))
                {
                    Apply(channel);
                }
            }
        }

        private readonly double referenceVoltage;
        private readonly double?[] inputs;

        private const long Configuration1Offset = 0x0C;
        private const uint LeftAlignment = 1u << 5;
    }
}
