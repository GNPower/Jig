//
// Xilinx AXI GPIO v2.0, dual channel.
//
// References: Xilinx PG144 "AXI GPIO", register space; offsets and masks
// follow the driver header xgpio_l.h.
//
// Behaviour modelled:
//  - GPIO_DATA/GPIO_TRI per channel (channel 2 at +0x8). TRI bit 1 = input.
//    Reading DATA returns the pin level for input bits and the output register
//    for output bits, so read-modify-write sequences keep their outputs.
//  - Output bits drive GPIO outputs: channel 1 bit n is Connections[n],
//    channel 2 bit n is Connections[32 + n].
//  - Input bits are driven through IGPIOReceiver: input n (0-31) is channel 1
//    bit n, input 32 + n is channel 2 bit n. Board wiring that holds a level
//    (e.g. a reset-done signal tied high) is set with SetInput.
//  - Interrupts: IP ISR bit 0/1 latch when a channel's input bits change; IP IER
//    enables them; GIER bit 31 gates the IRQ output. IP ISR is
//    toggle-on-write ("write 1 to toggle", PG144).
//  - Power-on values come from the IP configuration parameters (C_DOUT_DEFAULT,
//    C_TRI_DEFAULT per channel), passed as constructor arguments.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Jig.Xilinx
{
    public sealed class XilinxAxiGpio : IDoubleWordPeripheral, IKnownSize, IGPIOReceiver, INumberedGPIOOutput
    {
        public XilinxAxiGpio(IMachine machine, uint dataDefault1 = 0, uint triDefault1 = 0xFFFFFFFF,
                             uint dataDefault2 = 0, uint triDefault2 = 0xFFFFFFFF, bool dualChannel = true)
        {
            this.dataDefault = new[] { dataDefault1, dataDefault2 };
            this.triDefault = new[] { triDefault1, triDefault2 };
            this.dualChannel = dualChannel;
            IRQ = new GPIO();
            var outputs = new Dictionary<int, IGPIO>();
            for(var i = 0; i < 64; i++)
            {
                outputs[i] = new GPIO();
            }
            Connections = outputs;
            Reset();
        }

        public void Reset()
        {
            for(var channel = 0; channel < 2; channel++)
            {
                data[channel] = dataDefault[channel];
                tri[channel] = triDefault[channel];
            }
            globalInterruptEnable = false;
            interruptEnable = 0;
            interruptStatus = 0;
            DriveOutputs();
            UpdateIrq();
        }

        public long Size => 0x200;

        public GPIO IRQ { get; }

        public IReadOnlyDictionary<int, IGPIO> Connections { get; }

        public void OnGPIO(int number, bool value)
        {
            if(number < 0 || number >= 64)
            {
                this.Log(LogLevel.Warning, "Input {0} out of range", number);
                return;
            }
            SetInputBit(number / 32, number % 32, value);
        }

        /// <summary>Holds an input pin at a level (board wiring).</summary>
        public void SetInput(int channel, int bit, bool value)
        {
            SetInputBit(channel - 1, bit, value);
        }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case Data1Offset: return Visible(0);
            case Tri1Offset: return tri[0];
            case Data2Offset: return dualChannel ? Visible(1) : 0;
            case Tri2Offset: return dualChannel ? tri[1] : 0;
            case GlobalInterruptEnableOffset: return globalInterruptEnable ? GlobalEnableBit : 0;
            case InterruptStatusOffset: return interruptStatus;
            case InterruptEnableOffset: return interruptEnable;
            default:
                this.Log(LogLevel.Warning, "Read from unhandled offset 0x{0:X3}", offset);
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case Data1Offset: data[0] = value; break;
            case Tri1Offset: tri[0] = value; break;
            case Data2Offset: if(dualChannel) data[1] = value; break;
            case Tri2Offset: if(dualChannel) tri[1] = value; break;
            case GlobalInterruptEnableOffset: globalInterruptEnable = (value & GlobalEnableBit) != 0; break;
            case InterruptStatusOffset: interruptStatus ^= value & 0x3; break;
            case InterruptEnableOffset: interruptEnable = value & 0x3; break;
            default:
                this.Log(LogLevel.Warning, "Write 0x{0:X8} to unhandled offset 0x{1:X3}", value, offset);
                return;
            }
            DriveOutputs();
            UpdateIrq();
        }

        private uint Visible(int channel)
        {
            return (inputs[channel] & tri[channel]) | (data[channel] & ~tri[channel]);
        }

        private void SetInputBit(int channel, int bit, bool value)
        {
            var before = inputs[channel] & tri[channel];
            if(value) inputs[channel] |= 1u << bit;
            else inputs[channel] &= ~(1u << bit);
            if((inputs[channel] & tri[channel]) != before)
            {
                interruptStatus |= 1u << channel;
                UpdateIrq();
            }
        }

        private void DriveOutputs()
        {
            for(var channel = 0; channel < 2; channel++)
            {
                for(var bit = 0; bit < 32; bit++)
                {
                    var isOutput = (tri[channel] & (1u << bit)) == 0;
                    var level = isOutput && (data[channel] & (1u << bit)) != 0;
                    Connections[channel * 32 + bit].Set(level);
                }
            }
        }

        private void UpdateIrq()
        {
            IRQ.Set(globalInterruptEnable && (interruptStatus & interruptEnable) != 0);
        }

        private readonly uint[] dataDefault;
        private readonly uint[] triDefault;
        private readonly bool dualChannel;
        private readonly uint[] data = new uint[2];
        private readonly uint[] tri = new uint[2];
        private readonly uint[] inputs = new uint[2];
        private bool globalInterruptEnable;
        private uint interruptEnable;
        private uint interruptStatus;

        private const long Data1Offset = 0x000;
        private const long Tri1Offset = 0x004;
        private const long Data2Offset = 0x008;
        private const long Tri2Offset = 0x00C;
        private const long GlobalInterruptEnableOffset = 0x11C;
        private const long InterruptStatusOffset = 0x120;
        private const long InterruptEnableOffset = 0x128;
        private const uint GlobalEnableBit = 0x80000000;
    }
}
