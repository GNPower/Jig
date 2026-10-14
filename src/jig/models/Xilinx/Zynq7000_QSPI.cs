//
// Zynq-7000 Quad-SPI flash controller (registers 0xE000D000, linear window
// 0xFC000000), with up to two flash dies (dual-stacked).
//
// References: UG585 chapter 12 "Quad-SPI Flash Controller" and register
// reference B.27; names follow the Xilinx header xqspips_hw.h. The receive
// path behaviour (bytes shifted in from the top of a 32-bit shift register, one
// word per transmit entry) is the one XQspiPs_GetReadData decodes.
//
// Behaviour modelled:
//  - I/O mode: TXD0 (4 bytes), TXD1/TXD2/TXD3 (1/2/3 bytes) queue entries in a
//    63-word TX FIFO; bytes go out least-significant first. Every byte shifts the
//    reply into a 32-bit register from the top; each entry pushes that register
//    into the 63-word RX FIFO.
//  - Chip select: automatic, or manual (SSFORCE) where PCS = 0 asserts. Releasing
//    it ends the flash command. Manual start (MANSTRTEN/MANSTRT) or auto start.
//  - Dual-stacked: LQSPI_CFG.TWO_MEM with U_PAGE routes I/O-mode commands to the
//    upper die; in linear mode A[24] picks the die.
//  - Byte timing at SCLK = reference clock / 2^(BAUD_RATE_DIV+1), on as many data
//    lines as the flash reports for that byte (quad data phases run 4x faster).
//  - Linear mode (LQSPI_CFG.LQ_MODE): reads of the window issue LQSPI_CFG.INST_CODE
//    to the flash; writes are rejected and latch LQSPI_STS.WR_RECVD.
//  - Watermarks TXWR/RXWR, sticky RXOVR/TXUF, live TXOW/TXFULL/RXNEMPTY/RXFULL,
//    a read/write delay register (checked by XQspiPs_SelfTest).
//  - SLCR LQSPI_RST_CTRL (0x230) pulses reset the controller.
// Reset values (UG585 v1.7 B.26): CR 0x80020000 (IFMODE set; bit 17 is
// undefined there), TXWR/RXWR 1, SICR 0xFF, GPIO 1, LPBK_DLY_ADJ 0x33,
// MOD_ID 0x01090101. LQSPI_CFG differs from B.26, whose field resets read
// 0x03A002EB (linear mode off, 0xEB quad I/O read, two dummy bytes, mode bits
// on; bit 26 undefined): the model starts at 0x8000016B, the driver's
// XQSPIPS_LQSPI_CR_RST_STATE (linear mode, 0x6B, one dummy byte), the state a
// Quad-SPI boot leaves for the FSBL, because the BootROM model reads the flash
// without programming the controller.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Jig.Contracts;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.Xilinx
{
    public sealed class Zynq7000_QSPI : SimpleContainer<ISpiNorFlash>, IBusPeripheral
    {
        public Zynq7000_QSPI(IMachine machine, ulong referenceClockFrequency = 200000000, Zynq7000_SLCR slcr = null)
            : base(machine)
        {
            this.referenceClockFrequency = referenceClockFrequency;
            IRQ = new GPIO();
            if(slcr != null)
            {
                slcr.ResetControlWritten += (offset, value) =>
                {
                    if(offset == LqspiResetControl && (value & 0x3) != 0)
                    {
                        this.Log(LogLevel.Debug, "Reset through SLCR LQSPI_RST_CTRL");
                        Reset();
                    }
                };
            }
            Reset();
        }

        public override void Reset()
        {
            lock(sync)
            {
                generation++;
                ReleaseChipSelect();
                control = ConfigResetValue;
                interruptMask = 0;
                sticky = 0;
                enabled = false;
                delays = 0;
                slaveIdleCount = SlaveIdleCountResetValue;
                txWatermark = 1;
                rxWatermark = 1;
                gpio = GpioResetValue;
                loopbackDelay = LoopbackDelayResetValue;
                linearConfig = LinearConfigResetValue;
                linearStatus = 0;
                txFifo.Clear();
                rxFifo.Clear();
                shiftRegister = 0;
                busy = false;
                UpdateIrq();
            }
        }

        public GPIO IRQ { get; }

        /// <summary>The flash die on chip select 0 (lower) or 1 (upper), or null.</summary>
        public ISpiNorFlash GetDie(int chipSelect)
        {
            return TryGetByAddress(chipSelect, out var die) ? die : null;
        }

        // ------------------------------------------------------------ registers

        [ConnectionRegion("registers")]
        public uint ReadDoubleWordFromRegisters(long offset)
        {
            lock(sync)
            {
                switch(offset)
                {
                case ConfigOffset: return control;
                case StatusOffset: return sticky | LiveStatus();
                case InterruptMaskOffset: return interruptMask;
                case EnableOffset: return enabled ? 1u : 0u;
                case DelayOffset: return delays;
                case RxDataOffset:
                {
                    if(rxFifo.Count == 0)
                    {
                        this.Log(LogLevel.Debug, "Reading an empty RX FIFO");
                        return 0;
                    }
                    var word = rxFifo.Dequeue();
                    UpdateIrq();
                    return word;
                }
                case SlaveIdleCountOffset: return slaveIdleCount;
                case TxWatermarkOffset: return txWatermark;
                case RxWatermarkOffset: return rxWatermark;
                case GpioOffset: return gpio;
                case LoopbackDelayOffset: return loopbackDelay;
                case LinearConfigOffset: return linearConfig;
                case LinearStatusOffset: return linearStatus;
                case ModuleIdOffset: return ModuleId;
                case InterruptEnableOffset:
                case InterruptDisableOffset:
                case TxData0Offset:
                case TxData1Offset:
                case TxData2Offset:
                case TxData3Offset:
                    return 0;
                default:
                    this.Log(LogLevel.Warning, "Read from unhandled offset 0x{0:X2}", offset);
                    return 0;
                }
            }
        }

        [ConnectionRegion("registers")]
        public void WriteDoubleWordToRegisters(long offset, uint value)
        {
            lock(sync)
            {
                switch(offset)
                {
                case ConfigOffset:
                    WriteConfig(value);
                    break;
                case StatusOffset:
                    sticky &= ~(value & StickyMask);
                    break;
                case InterruptEnableOffset:
                    interruptMask |= value & 0x7D;
                    break;
                case InterruptDisableOffset:
                    interruptMask &= ~value;
                    break;
                case EnableOffset:
                    enabled = (value & 1) != 0;
                    if(!enabled && !ManualChipSelect)
                    {
                        ReleaseChipSelect();
                    }
                    break;
                case DelayOffset:
                    delays = value;
                    break;
                case TxData0Offset:
                    PushTx(value, 4);
                    break;
                case TxData1Offset:
                    PushTx(value, 1);
                    break;
                case TxData2Offset:
                    PushTx(value, 2);
                    break;
                case TxData3Offset:
                    PushTx(value, 3);
                    break;
                case SlaveIdleCountOffset:
                    slaveIdleCount = value & 0xFF;
                    break;
                case TxWatermarkOffset:
                    txWatermark = value & 0x3F;
                    break;
                case RxWatermarkOffset:
                    rxWatermark = value & 0x3F;
                    break;
                case GpioOffset:
                    gpio = value & 1;
                    break;
                case LoopbackDelayOffset:
                    loopbackDelay = value & 0x3F;
                    break;
                case LinearConfigOffset:
                    linearConfig = value;
                    UpdateChipSelectForManualMode();
                    break;
                case LinearStatusOffset:
                    linearStatus &= ~value;
                    break;
                case ModuleIdOffset:
                    break;
                default:
                    this.Log(LogLevel.Warning, "Write 0x{0:X8} to unhandled offset 0x{1:X2}", value, offset);
                    break;
                }
                TryStartTransfer(fromManualStart: false);
                UpdateIrq();
            }
        }

        // ---------------------------------------------------------- linear mode

        [ConnectionRegion("linear")]
        public uint ReadDoubleWordFromLinear(long offset)
        {
            var bytes = LinearRead(offset, 4);
            return (uint)(bytes[0] | bytes[1] << 8 | bytes[2] << 16 | bytes[3] << 24);
        }

        [ConnectionRegion("linear")]
        public ushort ReadWordFromLinear(long offset)
        {
            var bytes = LinearRead(offset, 2);
            return (ushort)(bytes[0] | bytes[1] << 8);
        }

        [ConnectionRegion("linear")]
        public byte ReadByteFromLinear(long offset)
        {
            return LinearRead(offset, 1)[0];
        }

        [ConnectionRegion("linear")]
        public void WriteDoubleWordToLinear(long offset, uint value) => RejectLinearWrite(offset);

        [ConnectionRegion("linear")]
        public void WriteWordToLinear(long offset, ushort value) => RejectLinearWrite(offset);

        [ConnectionRegion("linear")]
        public void WriteByteToLinear(long offset, byte value) => RejectLinearWrite(offset);

        private byte[] LinearRead(long offset, int count)
        {
            var buffer = new byte[count];
            lock(sync)
            {
                if((linearConfig & LinearModeEnable) == 0)
                {
                    this.Log(LogLevel.Warning, "Linear window read at +0x{0:X} while linear mode is off", offset);
                    return buffer;
                }
                var twoMemories = (linearConfig & LinearTwoMemories) != 0 && (linearConfig & LinearSeparateBus) == 0;
                var dieIndex = twoMemories && (offset & 0x1000000) != 0 ? 1 : 0;
                if(!TryGetByAddress(dieIndex, out var die))
                {
                    this.Log(LogLevel.Warning, "Linear read at +0x{0:X}: no flash die {1}", offset, dieIndex);
                    for(var i = 0; i < count; i++) buffer[i] = 0xFF;
                    return buffer;
                }
                die.LinearRead((byte)(linearConfig & 0xFF), offset & 0xFFFFFF, buffer, 0, count);
            }
            return buffer;
        }

        private void RejectLinearWrite(long offset)
        {
            lock(sync)
            {
                linearStatus |= LinearStatusWriteReceived;
            }
            this.Log(LogLevel.Warning, "Write to the read-only linear window at +0x{0:X} rejected", offset);
        }

        // ------------------------------------------------------------- I/O mode

        private bool ManualChipSelect => (control & ConfigSlaveSelectForce) != 0;

        private bool ManualStartEnabled => (control & ConfigManualStartEnable) != 0;

        private int SelectedDieIndex()
        {
            var twoMemories = (linearConfig & LinearTwoMemories) != 0;
            return twoMemories && (linearConfig & LinearUpperPage) != 0 ? 1 : 0;
        }

        private void WriteConfig(uint value)
        {
            var manualStart = (value & ConfigManualStart) != 0;
            control = value & ~ConfigManualStart;
            UpdateChipSelectForManualMode();
            if(manualStart)
            {
                TryStartTransfer(fromManualStart: true);
            }
        }

        private void UpdateChipSelectForManualMode()
        {
            if(!ManualChipSelect)
            {
                if(!busy)
                {
                    ReleaseChipSelect();
                }
                return;
            }
            var asserted = (control & ConfigPeripheralChipSelect) == 0;
            if(!asserted)
            {
                ReleaseChipSelect();
                return;
            }
            var index = SelectedDieIndex();
            if(selected != null && selectedIndex != index)
            {
                ReleaseChipSelect();
            }
            if(selected == null)
            {
                Select(index);
            }
        }

        private void Select(int index)
        {
            if(TryGetByAddress(index, out var die))
            {
                selected = die;
                selectedIndex = index;
            }
            else
            {
                this.Log(LogLevel.Debug, "Chip select asserted for die {0}, which is not attached", index);
                selected = null;
                selectedIndex = -1;
            }
        }

        private void ReleaseChipSelect()
        {
            selected?.FinishTransmission();
            selected = null;
            selectedIndex = -1;
        }

        private void PushTx(uint word, int bytes)
        {
            if(txFifo.Count >= FifoDepth)
            {
                this.Log(LogLevel.Warning, "TX FIFO full, entry 0x{0:X8} dropped", word);
                return;
            }
            txFifo.Enqueue(new TxEntry(word, bytes));
        }

        private void TryStartTransfer(bool fromManualStart)
        {
            if(busy || txFifo.Count == 0 || !enabled || (control & ConfigMasterEnable) == 0)
            {
                return;
            }
            if(ManualStartEnabled && !fromManualStart)
            {
                return;
            }
            if(!ManualChipSelect && selected == null)
            {
                Select(SelectedDieIndex());
            }
            busy = true;
            ScheduleNextEntry();
        }

        private void ScheduleNextEntry()
        {
            var entry = txFifo.Peek();
            var nanoseconds = EntryNanoseconds(entry);
            var token = generation;
            machine.ScheduleAction(TimeInterval.FromNanoseconds(nanoseconds), _ =>
            {
                lock(sync)
                {
                    if(token != generation)
                    {
                        return;
                    }
                    ExchangeEntry(txFifo.Dequeue());
                    if(txFifo.Count > 0 && enabled)
                    {
                        ScheduleNextEntry();
                    }
                    else
                    {
                        busy = false;
                        if(!ManualChipSelect)
                        {
                            ReleaseChipSelect();
                        }
                    }
                    UpdateIrq();
                }
            }, "qspi-word");
        }

        private ulong EntryNanoseconds(TxEntry entry)
        {
            var divider = 1UL << (int)(((control >> 3) & 0x7) + 1);
            var sclk = Math.Max(1UL, referenceClockFrequency / divider);
            var lines = selected?.CurrentDataLines ?? 1;
            var bitClocks = 8.0 * entry.Bytes / Math.Max(1, lines);
            return Math.Max(1UL, (ulong)(bitClocks * 1e9 / sclk));
        }

        private void ExchangeEntry(TxEntry entry)
        {
            for(var i = 0; i < entry.Bytes; i++)
            {
                var outgoing = (byte)(entry.Word >> (8 * i));
                var incoming = selected != null ? selected.Transmit(outgoing) : (byte)0xFF;
                shiftRegister = (shiftRegister >> 8) | ((uint)incoming << 24);
            }
            if(rxFifo.Count >= FifoDepth)
            {
                sticky |= StatusRxOverflow;
                this.Log(LogLevel.Debug, "RX FIFO overflow, word 0x{0:X8} dropped", shiftRegister);
                return;
            }
            rxFifo.Enqueue(shiftRegister);
        }

        // ------------------------------------------------------------ interrupts

        private uint LiveStatus()
        {
            uint s = 0;
            if(txFifo.Count < Math.Max(1, (int)txWatermark)) s |= StatusTxUnderWatermark;
            if(txFifo.Count >= FifoDepth) s |= StatusTxFull;
            if(rxFifo.Count >= Math.Max(1, (int)rxWatermark)) s |= StatusRxNotEmpty;
            if(rxFifo.Count >= FifoDepth) s |= StatusRxFull;
            return s;
        }

        private void UpdateIrq()
        {
            IRQ.Set(((sticky | LiveStatus()) & interruptMask) != 0);
        }

        private readonly ulong referenceClockFrequency;
        private readonly object sync = new object();
        private readonly Queue<TxEntry> txFifo = new Queue<TxEntry>();
        private readonly Queue<uint> rxFifo = new Queue<uint>();

        private ISpiNorFlash selected;
        private int selectedIndex = -1;
        private ulong generation;
        private bool busy;
        private uint shiftRegister;
        private uint control;
        private uint interruptMask;
        private uint sticky;
        private bool enabled;
        private uint delays;
        private uint slaveIdleCount;
        private uint txWatermark;
        private uint rxWatermark;
        private uint gpio;
        private uint loopbackDelay;
        private uint linearConfig;
        private uint linearStatus;

        private readonly struct TxEntry
        {
            public TxEntry(uint word, int bytes)
            {
                Word = word;
                Bytes = bytes;
            }

            public uint Word { get; }
            public int Bytes { get; }
        }

        private const int FifoDepth = 63;
        private const long LqspiResetControl = 0x230;

        private const long ConfigOffset = 0x00;
        private const long StatusOffset = 0x04;
        private const long InterruptEnableOffset = 0x08;
        private const long InterruptDisableOffset = 0x0C;
        private const long InterruptMaskOffset = 0x10;
        private const long EnableOffset = 0x14;
        private const long DelayOffset = 0x18;
        private const long TxData0Offset = 0x1C;
        private const long RxDataOffset = 0x20;
        private const long SlaveIdleCountOffset = 0x24;
        private const long TxWatermarkOffset = 0x28;
        private const long RxWatermarkOffset = 0x2C;
        private const long GpioOffset = 0x30;
        private const long LoopbackDelayOffset = 0x38;
        private const long TxData1Offset = 0x80;
        private const long TxData2Offset = 0x84;
        private const long TxData3Offset = 0x88;
        private const long LinearConfigOffset = 0xA0;
        private const long LinearStatusOffset = 0xA4;
        private const long ModuleIdOffset = 0xFC;

        private const uint ConfigResetValue = 0x80020000;
        private const uint SlaveIdleCountResetValue = 0xFF;
        private const uint GpioResetValue = 0x1;
        private const uint LoopbackDelayResetValue = 0x33;
        private const uint ModuleId = 0x01090101;
        private const uint ConfigMasterEnable = 1u << 0;
        private const uint ConfigPeripheralChipSelect = 1u << 10;
        private const uint ConfigSlaveSelectForce = 1u << 14;
        private const uint ConfigManualStartEnable = 1u << 15;
        private const uint ConfigManualStart = 1u << 16;

        private const uint StatusRxOverflow = 1u << 0;
        private const uint StatusTxUnderWatermark = 1u << 2;
        private const uint StatusTxFull = 1u << 3;
        private const uint StatusRxNotEmpty = 1u << 4;
        private const uint StatusRxFull = 1u << 5;
        private const uint StatusTxUnderflow = 1u << 6;
        private const uint StickyMask = StatusRxOverflow | StatusTxUnderflow;

        private const uint LinearModeEnable = 1u << 31;
        private const uint LinearTwoMemories = 1u << 30;
        private const uint LinearSeparateBus = 1u << 29;
        private const uint LinearUpperPage = 1u << 28;
        private const uint LinearStatusWriteReceived = 1u << 1;
        // XQSPIPS_LQSPI_CR_RST_STATE in xqspips_hw.h: linear mode, 1 dummy byte, 0x6B.
        private const uint LinearConfigResetValue = 0x8000016B;
    }
}
