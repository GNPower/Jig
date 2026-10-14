//
// Xilinx AXI Central DMA in simple mode.
//
// References: Xilinx PG034 "AXI Central Direct Memory Access", register space
// (CDMACR, CDMASR, SA, DA, BTT); offsets and masks follow the Xilinx driver
// header xaxicdma_hw.h.
//
// Behaviour modelled:
//  - CDMACR.Reset (bit 2) resets the engine and self-clears.
//  - Writing BTT starts a transfer of BTT bytes from SA to DA. The engine reads
//    and writes the system bus through its own master port, which reaches only
//    the ranges given in reachableRanges (the block design's address map).
//    Any address outside them ends the transfer with a decode error
//    (CDMASR.DMADecErr), as the AXI interconnect would.
//  - Duration: BTT bytes at the data-path rate (dataBytesPerSecond).
//  - CDMASR.Idle, IOC_Irq / Err_Irq (write-one-to-clear), interrupt enables in
//    CDMACR (IOC_IrqEn bit 12, Err_IrqEn bit 14), IRQ output.
//  - CDMASR.SGIncld reads 0: the core is built without scatter-gather.
// Not modelled: scatter-gather, key-hole transfers, interrupt coalescing.
//
using System;
using System.Collections.Generic;
using System.Globalization;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.Xilinx
{
    public sealed class XilinxAxiCdma : IDoubleWordPeripheral, IKnownSize
    {
        public XilinxAxiCdma(IMachine machine, string reachableRanges = "", double dataBytesPerSecond = 400e6)
        {
            this.machine = machine;
            this.dataBytesPerSecond = dataBytesPerSecond;
            ranges = ParseRanges(reachableRanges);
            IRQ = new GPIO();
            Reset();
        }

        public void Reset()
        {
            generation++;
            control = 0;
            idle = true;
            errors = 0;
            completed = false;
            errorIrq = false;
            sourceAddress = 0;
            destinationAddress = 0;
            UpdateIrq();
        }

        public long Size => 0x1000;

        public GPIO IRQ { get; }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case ControlOffset: return control;
            case StatusOffset:
                return (idle ? StatusIdle : 0) | errors | (completed ? StatusIocIrq : 0) | (errorIrq ? StatusErrIrq : 0);
            case SourceOffset: return (uint)sourceAddress;
            case SourceMsbOffset: return (uint)(sourceAddress >> 32);
            case DestinationOffset: return (uint)destinationAddress;
            case DestinationMsbOffset: return (uint)(destinationAddress >> 32);
            case BytesToTransferOffset: return bytesToTransfer;
            default:
                this.Log(LogLevel.Warning, "Read from unhandled offset 0x{0:X2}", offset);
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case ControlOffset:
                if((value & ControlReset) != 0)
                {
                    Reset();
                    return;
                }
                control = value & ~ControlReset;
                break;
            case StatusOffset:
                if((value & StatusIocIrq) != 0) completed = false;
                if((value & StatusErrIrq) != 0) errorIrq = false;
                break;
            case SourceOffset: sourceAddress = (sourceAddress & 0xFFFFFFFF00000000UL) | value; break;
            case SourceMsbOffset: sourceAddress = (sourceAddress & 0xFFFFFFFFUL) | ((ulong)value << 32); break;
            case DestinationOffset: destinationAddress = (destinationAddress & 0xFFFFFFFF00000000UL) | value; break;
            case DestinationMsbOffset: destinationAddress = (destinationAddress & 0xFFFFFFFFUL) | ((ulong)value << 32); break;
            case BytesToTransferOffset:
                bytesToTransfer = value & 0x03FFFFFF;
                Start();
                break;
            default:
                this.Log(LogLevel.Warning, "Write 0x{0:X8} to unhandled offset 0x{1:X2}", value, offset);
                return;
            }
            UpdateIrq();
        }

        private void Start()
        {
            if(!idle)
            {
                this.Log(LogLevel.Warning, "BTT written while a transfer is in progress");
                return;
            }
            if(bytesToTransfer == 0)
            {
                return;
            }
            idle = false;
            var length = (int)bytesToTransfer;
            var source = sourceAddress;
            var destination = destinationAddress;
            var token = generation;
            var nanoseconds = (ulong)Math.Max(1.0, length * 1e9 / dataBytesPerSecond);
            machine.ScheduleAction(TimeInterval.FromNanoseconds(nanoseconds), _ =>
            {
                if(token != generation)
                {
                    return;
                }
                if(!Reachable(source, length) || !Reachable(destination, length))
                {
                    errors |= StatusDecodeError;
                    errorIrq = true;
                    this.Log(LogLevel.Warning, "Decode error: 0x{0:X8} -> 0x{1:X8} ({2} bytes) leaves the reachable address map",
                        source, destination, length);
                }
                else
                {
                    var data = machine.SystemBus.ReadBytes(source, length);
                    machine.SystemBus.WriteBytes(data, destination);
                    completed = true;
                }
                idle = true;
                UpdateIrq();
            }, "cdma");
        }

        private bool Reachable(ulong address, int length)
        {
            if(ranges.Count == 0)
            {
                return true;
            }
            foreach(var (start, size) in ranges)
            {
                if(address >= start && address + (ulong)length <= start + size)
                {
                    return true;
                }
            }
            return false;
        }

        private void UpdateIrq()
        {
            var ioc = completed && (control & ControlIocIrqEnable) != 0;
            var err = errorIrq && (control & ControlErrIrqEnable) != 0;
            IRQ.Set(ioc || err);
        }

        private static List<(ulong, ulong)> ParseRanges(string text)
        {
            var result = new List<(ulong, ulong)>();
            if(string.IsNullOrWhiteSpace(text))
            {
                return result;
            }
            foreach(var part in text.Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var fields = part.Trim().Split(':');
                if(fields.Length != 2)
                {
                    throw new ConstructionException($"Range '{part}' must be <start>:<size>");
                }
                result.Add((ParseNumber(fields[0]), ParseNumber(fields[1])));
            }
            return result;
        }

        private static ulong ParseNumber(string text)
        {
            text = text.Trim();
            if(text.StartsWith("0x", StringComparison.OrdinalIgnoreCase))
            {
                return ulong.Parse(text.Substring(2), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
            }
            return ulong.Parse(text, CultureInfo.InvariantCulture);
        }

        private readonly IMachine machine;
        private readonly double dataBytesPerSecond;
        private readonly List<(ulong Start, ulong Size)> ranges;

        private ulong generation;
        private uint control;
        private bool idle;
        private uint errors;
        private bool completed;
        private bool errorIrq;
        private ulong sourceAddress;
        private ulong destinationAddress;
        private uint bytesToTransfer;

        private const long ControlOffset = 0x00;
        private const long StatusOffset = 0x04;
        private const long SourceOffset = 0x18;
        private const long SourceMsbOffset = 0x1C;
        private const long DestinationOffset = 0x20;
        private const long DestinationMsbOffset = 0x24;
        private const long BytesToTransferOffset = 0x28;

        private const uint ControlReset = 1u << 2;
        private const uint ControlIocIrqEnable = 1u << 12;
        private const uint ControlErrIrqEnable = 1u << 14;
        private const uint StatusIdle = 1u << 1;
        private const uint StatusDecodeError = 1u << 6;
        private const uint StatusIocIrq = 1u << 12;
        private const uint StatusErrIrq = 1u << 14;
    }
}
