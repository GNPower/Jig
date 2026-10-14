//
// Xilinx AXI Quad SPI v3.2 in standard SPI master mode.
//
// References: Xilinx PG153 "AXI Quad SPI", register space; offsets and masks
// follow the driver header xspi_l.h. Power-on SPICR 0x180 (master transaction
// inhibit and manual slave select set) and SPISSR all-ones (no slave selected)
// are PG153's documented defaults.
//
// Behaviour modelled:
//  - SRR: writing 0x0A resets the core.
//  - SPICR: loopback, enable, master, CPOL/CPHA (stored), FIFO resets
//    (self-clearing), manual slave select, master transaction inhibit, LSB-first.
//  - TX/RX FIFOs (16 entries by default) with occupancy registers (count - 1).
//  - Slave select: SPISSR active-low; with manual slave select the selected
//    device follows SPISSR directly, otherwise it is selected for the duration
//    of each burst. Deselecting ends the device's transaction.
//  - Bytes shift at the SPI clock rate (ext_spi_clk / SCK_RATIO), MSB first
//    unless LSB-first is set; loopback returns the transmitted byte.
//  - IPISR toggle-on-write with TX empty, DRR full/overrun/not-empty, TX half
//    empty; IPIER and DGIER gate the IRQ.
// Not modelled: slave mode, dual/quad modes, XIP, mode-fault detection.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.SPI;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.Xilinx
{
    public sealed class XilinxAxiQuadSpi : SimpleContainer<ISPIPeripheral>, IDoubleWordPeripheral, IKnownSize
    {
        public XilinxAxiQuadSpi(IMachine machine, ulong spiClockFrequency = 1562500, int fifoDepth = 16, int slaveSelectBits = 1)
            : base(machine)
        {
            this.spiClockFrequency = spiClockFrequency;
            this.fifoDepth = fifoDepth;
            slaveSelectMask = slaveSelectBits >= 32 ? 0xFFFFFFFFu : (1u << slaveSelectBits) - 1;
            IRQ = new GPIO();
            Reset();
        }

        public override void Reset()
        {
            generation++;
            Deselect();
            control = ControlResetValue;
            slaveSelect = 0xFFFFFFFF;
            txFifo.Clear();
            rxFifo.Clear();
            busy = false;
            globalInterruptEnable = false;
            interruptEnable = 0;
            interruptStatus = 0;
            UpdateIrq();
        }

        public long Size => 0x80;

        public GPIO IRQ { get; }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case GlobalInterruptEnableOffset: return globalInterruptEnable ? GlobalEnableBit : 0;
            case InterruptStatusOffset: return interruptStatus;
            case InterruptEnableOffset: return interruptEnable;
            case ControlOffset: return control;
            case StatusOffset: return Status();
            case ReceiveDataOffset:
            {
                if(rxFifo.Count == 0)
                {
                    this.Log(LogLevel.Debug, "Reading an empty receive FIFO");
                    return 0;
                }
                var value = rxFifo.Dequeue();
                UpdateIrq();
                return value;
            }
            case SlaveSelectOffset: return slaveSelect;
            case TxOccupancyOffset: return txFifo.Count == 0 ? 0u : (uint)(txFifo.Count - 1);
            case RxOccupancyOffset: return rxFifo.Count == 0 ? 0u : (uint)(rxFifo.Count - 1);
            case TransmitDataOffset:
            case SoftwareResetOffset:
                return 0;
            default:
                this.Log(LogLevel.Warning, "Read from unhandled offset 0x{0:X2}", offset);
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case SoftwareResetOffset:
                if(value == SoftwareResetKey) Reset();
                else this.Log(LogLevel.Warning, "SRR write 0x{0:X} ignored: reset key is 0x0A", value);
                return;
            case GlobalInterruptEnableOffset:
                globalInterruptEnable = (value & GlobalEnableBit) != 0;
                break;
            case InterruptStatusOffset:
                interruptStatus ^= value & InterruptMask;
                break;
            case InterruptEnableOffset:
                interruptEnable = value & InterruptMask;
                break;
            case ControlOffset:
                if((value & ControlTxFifoReset) != 0) txFifo.Clear();
                if((value & ControlRxFifoReset) != 0) rxFifo.Clear();
                control = value & ~(ControlTxFifoReset | ControlRxFifoReset) & 0x3FF;
                UpdateManualSelect();
                break;
            case TransmitDataOffset:
                if(txFifo.Count >= fifoDepth)
                {
                    this.Log(LogLevel.Warning, "Transmit FIFO full: byte 0x{0:X2} dropped", (byte)value);
                    return;
                }
                txFifo.Enqueue((byte)value);
                break;
            case SlaveSelectOffset:
                slaveSelect = value | ~slaveSelectMask;
                UpdateManualSelect();
                break;
            default:
                this.Log(LogLevel.Warning, "Write 0x{0:X8} to unhandled offset 0x{1:X2}", value, offset);
                return;
            }
            TryStart();
            UpdateIrq();
        }

        private bool Active => (control & ControlEnable) != 0 && (control & ControlMaster) != 0 && (control & ControlInhibit) == 0;

        private bool ManualSelect => (control & ControlManualSlaveSelect) != 0;

        private int RequestedSlave()
        {
            for(var i = 0; i < 32; i++)
            {
                if((slaveSelectMask & (1u << i)) != 0 && (slaveSelect & (1u << i)) == 0)
                {
                    return i;
                }
            }
            return -1;
        }

        private void UpdateManualSelect()
        {
            if(!ManualSelect)
            {
                return;
            }
            var requested = RequestedSlave();
            if(requested != selectedIndex)
            {
                Deselect();
                Select(requested);
            }
        }

        private void Select(int index)
        {
            selectedIndex = index;
            if(index < 0 || !TryGetByAddress(index, out selected))
            {
                selected = null;
            }
        }

        private void Deselect()
        {
            if(selectedIndex >= 0)
            {
                selected?.FinishTransmission();
            }
            selected = null;
            selectedIndex = -1;
        }

        private void TryStart()
        {
            if(busy || txFifo.Count == 0 || !Active)
            {
                return;
            }
            if(!ManualSelect)
            {
                Select(RequestedSlave());
            }
            busy = true;
            ScheduleByte();
        }

        private void ScheduleByte()
        {
            var token = generation;
            var nanoseconds = (ulong)Math.Max(1.0, 8e9 / spiClockFrequency);
            machine.ScheduleAction(TimeInterval.FromNanoseconds(nanoseconds), _ =>
            {
                if(token != generation)
                {
                    return;
                }
                ExchangeByte();
                if(txFifo.Count > 0 && Active)
                {
                    ScheduleByte();
                }
                else
                {
                    busy = false;
                    interruptStatus |= InterruptTxEmpty;
                    if(!ManualSelect)
                    {
                        Deselect();
                    }
                }
                UpdateIrq();
            }, "axi-spi-byte");
        }

        private void ExchangeByte()
        {
            var outgoing = txFifo.Dequeue();
            byte incoming;
            if((control & ControlLoopback) != 0)
            {
                incoming = outgoing;
            }
            else if(selected != null)
            {
                var lsbFirst = (control & ControlLsbFirst) != 0;
                var wire = lsbFirst ? Reverse(outgoing) : outgoing;
                var reply = selected.Transmit(wire);
                incoming = lsbFirst ? Reverse(reply) : reply;
            }
            else
            {
                incoming = 0xFF;
            }
            if(rxFifo.Count >= fifoDepth)
            {
                interruptStatus |= InterruptRxOverrun;
                return;
            }
            rxFifo.Enqueue(incoming);
            interruptStatus |= InterruptRxNotEmpty;
            if(rxFifo.Count >= fifoDepth) interruptStatus |= InterruptRxFull;
            if(txFifo.Count == fifoDepth / 2) interruptStatus |= InterruptTxHalfEmpty;
        }

        private static byte Reverse(byte value)
        {
            byte result = 0;
            for(var i = 0; i < 8; i++)
            {
                if((value & (1 << i)) != 0) result |= (byte)(0x80 >> i);
            }
            return result;
        }

        private uint Status()
        {
            uint status = StatusSlaveModeSelect; // never selected as a slave
            if(rxFifo.Count == 0) status |= StatusRxEmpty;
            if(rxFifo.Count >= fifoDepth) status |= StatusRxFull;
            if(txFifo.Count == 0) status |= StatusTxEmpty;
            if(txFifo.Count >= fifoDepth) status |= StatusTxFull;
            return status;
        }

        private void UpdateIrq()
        {
            IRQ.Set(globalInterruptEnable && (interruptStatus & interruptEnable) != 0);
        }

        private readonly ulong spiClockFrequency;
        private readonly int fifoDepth;
        private readonly uint slaveSelectMask;
        private readonly Queue<byte> txFifo = new Queue<byte>();
        private readonly Queue<byte> rxFifo = new Queue<byte>();

        private ISPIPeripheral selected;
        private int selectedIndex = -1;
        private ulong generation;
        private bool busy;
        private uint control;
        private uint slaveSelect;
        private bool globalInterruptEnable;
        private uint interruptEnable;
        private uint interruptStatus;

        private const long GlobalInterruptEnableOffset = 0x1C;
        private const long InterruptStatusOffset = 0x20;
        private const long InterruptEnableOffset = 0x28;
        private const long SoftwareResetOffset = 0x40;
        private const long ControlOffset = 0x60;
        private const long StatusOffset = 0x64;
        private const long TransmitDataOffset = 0x68;
        private const long ReceiveDataOffset = 0x6C;
        private const long SlaveSelectOffset = 0x70;
        private const long TxOccupancyOffset = 0x74;
        private const long RxOccupancyOffset = 0x78;

        private const uint SoftwareResetKey = 0x0A;
        private const uint GlobalEnableBit = 0x80000000;
        private const uint ControlResetValue = 0x180;
        private const uint ControlLoopback = 1u << 0;
        private const uint ControlEnable = 1u << 1;
        private const uint ControlMaster = 1u << 2;
        private const uint ControlTxFifoReset = 1u << 5;
        private const uint ControlRxFifoReset = 1u << 6;
        private const uint ControlManualSlaveSelect = 1u << 7;
        private const uint ControlInhibit = 1u << 8;
        private const uint ControlLsbFirst = 1u << 9;

        private const uint StatusRxEmpty = 1u << 0;
        private const uint StatusRxFull = 1u << 1;
        private const uint StatusTxEmpty = 1u << 2;
        private const uint StatusTxFull = 1u << 3;
        private const uint StatusSlaveModeSelect = 1u << 5;

        private const uint InterruptTxEmpty = 1u << 2;
        private const uint InterruptRxFull = 1u << 4;
        private const uint InterruptRxOverrun = 1u << 5;
        private const uint InterruptTxHalfEmpty = 1u << 6;
        private const uint InterruptRxNotEmpty = 1u << 8;
        private const uint InterruptMask = 0x3FFF;
    }
}
