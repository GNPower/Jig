//
// Cadence SPI controller (Zynq-7000 PS SPI0/SPI1), master mode.
//
// References: UG585 chapter 17 "SPI Controller" and register reference B.30;
// names follow the Xilinx driver header xspips_hw.h.
//
// Behaviour modelled:
//  - 128-entry TX and RX FIFOs with programmable watermarks (TXWR, RXWR).
//  - Reset values of UG585 v1.7 B.30 (CR 0x00020000, ISR 0x04, SICR 0xFF,
//    TXWR and RXWR 1, MOD_ID 0x00090106) and a read/write delay register,
//    which XSpiPs_SelfTest checks.
//  - Slave select: one-hot active-low SSCTRL (or decoded), automatic or manual
//    (SSFORCE) chip select, automatic or manual (MANSTRTEN/MANSTRT) start.
//  - Byte timing at SCLK = reference clock / 2^(PRESC+1); bytes move from the TX
//    FIFO to the selected slave and its replies land in the RX FIFO one byte
//    time apart.
//  - Sticky RXOVR/MODF/TXUF (write-one-to-clear), live TXOW/TXFULL/RXNEMPTY/RXFULL.
// Not modelled: slave mode, multi-master mode fault, the inter-byte delays
// programmed in DR (stored and read back only).
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Core.Structure.Registers;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.SPI;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.Cadence
{
    public sealed class CadenceSPI : SimpleContainer<ISPIPeripheral>, IDoubleWordPeripheral, IKnownSize
    {
        public CadenceSPI(IMachine machine, ulong referenceClockFrequency = 166666667, byte floatingMisoValue = 0xFF)
            : base(machine)
        {
            this.referenceClockFrequency = referenceClockFrequency;
            this.floatingMisoValue = floatingMisoValue;
            IRQ = new GPIO();
            registers = new DoubleWordRegisterCollection(this, BuildRegisterMap());
            Reset();
        }

        public override void Reset()
        {
            lock(sync)
            {
                generation++;
                DeselectSlave();
                registers.Reset();
                txFifo.Clear();
                rxFifo.Clear();
                busy = false;
                sticky = 0;
                interruptMask = 0;
                UpdateIrq();
            }
        }

        public uint ReadDoubleWord(long offset)
        {
            lock(sync)
            {
                var value = registers.Read(offset);
                UpdateIrq();
                return value;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            lock(sync)
            {
                registers.Write(offset, value);
                UpdateSlaveSelectForManualMode();
                TryStartTransfer(fromManualStart: false);
                UpdateIrq();
            }
        }

        public long Size => 0x1000;

        public GPIO IRQ { get; }

        // ------------------------------------------------------------ chip select

        private int SelectedIndex()
        {
            var ss = (int)slaveSelect.Value;
            if(decodeEnable.Value)
            {
                return ss == 0xF ? -1 : ss;
            }
            for(var i = 0; i < 4; i++)
            {
                if((ss & (1 << i)) == 0)
                {
                    return i;
                }
            }
            return -1;
        }

        private void SelectSlave(int index)
        {
            if(index == selectedIndex)
            {
                return;
            }
            DeselectSlave();
            if(index >= 0)
            {
                if(!TryGetByAddress(index, out selected))
                {
                    this.Log(LogLevel.Debug, "Slave select {0} asserted, but nothing is attached there", index);
                    selected = null;
                }
                selectedIndex = index;
            }
        }

        private void DeselectSlave()
        {
            if(selectedIndex >= 0)
            {
                selected?.FinishTransmission();
            }
            selected = null;
            selectedIndex = -1;
        }

        private void UpdateSlaveSelectForManualMode()
        {
            if(manualChipSelect.Value)
            {
                SelectSlave(SelectedIndex());
            }
            else if(!busy)
            {
                DeselectSlave();
            }
        }

        // -------------------------------------------------------------- transfers

        private bool Enabled => enable.Value && masterMode.Value;

        private void TryStartTransfer(bool fromManualStart)
        {
            if(busy || txFifo.Count == 0 || !Enabled)
            {
                return;
            }
            if(manualStartEnable.Value && !fromManualStart)
            {
                return;
            }
            if(!manualChipSelect.Value)
            {
                SelectSlave(SelectedIndex());
            }
            busy = true;
            ScheduleNextByte();
        }

        private ulong ByteNanoseconds()
        {
            var divider = 1UL << (int)(prescaler.Value + 1);
            var sclk = referenceClockFrequency / divider;
            return Math.Max(1UL, 8UL * 1000000000UL / Math.Max(1UL, sclk));
        }

        private void ScheduleNextByte()
        {
            var token = generation;
            machine.ScheduleAction(TimeInterval.FromNanoseconds(ByteNanoseconds()), _ =>
            {
                lock(sync)
                {
                    if(token != generation)
                    {
                        return;
                    }
                    ExchangeOneByte();
                    if(txFifo.Count > 0 && Enabled)
                    {
                        ScheduleNextByte();
                    }
                    else
                    {
                        busy = false;
                        if(!manualChipSelect.Value)
                        {
                            DeselectSlave();
                        }
                    }
                    UpdateIrq();
                }
            }, "spi-byte");
        }

        private void ExchangeOneByte()
        {
            var outgoing = txFifo.Dequeue();
            var incoming = selected != null ? selected.Transmit(outgoing) : floatingMisoValue;
            if(rxFifo.Count >= FifoDepth)
            {
                sticky |= (uint)Status.RxOverflow;
                this.Log(LogLevel.Debug, "RX FIFO overflow, byte 0x{0:X2} dropped", incoming);
                return;
            }
            rxFifo.Enqueue(incoming);
        }

        // ------------------------------------------------------------- interrupts

        private uint LiveStatus()
        {
            uint s = 0;
            if(txFifo.Count < (int)txWatermark.Value) s |= (uint)Status.TxUnderWatermark;
            if(txFifo.Count >= FifoDepth) s |= (uint)Status.TxFull;
            if(rxFifo.Count >= Math.Max(1, (int)rxWatermark.Value)) s |= (uint)Status.RxNotEmpty;
            if(rxFifo.Count >= FifoDepth) s |= (uint)Status.RxFull;
            return s;
        }

        private void UpdateIrq()
        {
            IRQ.Set(((sticky | LiveStatus()) & interruptMask) != 0);
        }

        // -------------------------------------------------------------- registers

        private Dictionary<long, DoubleWordRegister> BuildRegisterMap()
        {
            return new Dictionary<long, DoubleWordRegister>
            {
                {(long)Registers.Config, new DoubleWordRegister(this, 0x00020000)
                    .WithReservedBits(18, 14)
                    .WithFlag(17, name: "MODF_GEN_EN")
                    .WithFlag(16, FieldMode.Read | FieldMode.WriteOneToClear, name: "MANSTRT",
                        writeCallback: (_, value) => { if(value) TryStartTransfer(fromManualStart: true); })
                    .WithFlag(15, out manualStartEnable, name: "MANSTRTEN")
                    .WithFlag(14, out manualChipSelect, name: "SSFORCE")
                    .WithValueField(10, 4, out slaveSelect, name: "SSCTRL")
                    .WithFlag(9, out decodeEnable, name: "SSDECEN")
                    .WithFlag(8, name: "REFCLK")
                    .WithReservedBits(6, 2)
                    .WithValueField(3, 3, out prescaler, name: "PRESC")
                    .WithFlag(2, name: "CPHA")
                    .WithFlag(1, name: "CPOL")
                    .WithFlag(0, out masterMode, name: "MSTREN")
                },
                {(long)Registers.InterruptStatus, new DoubleWordRegister(this)
                    .WithReservedBits(7, 25)
                    .WithValueField(0, 7, name: "ISR",
                        valueProviderCallback: _ => sticky | LiveStatus(),
                        writeCallback: (_, value) => sticky &= ~((uint)value & StickyMask))
                },
                {(long)Registers.InterruptEnable, new DoubleWordRegister(this)
                    .WithReservedBits(7, 25)
                    .WithValueField(0, 7, FieldMode.Write, name: "IER",
                        writeCallback: (_, value) => interruptMask |= (uint)value)
                },
                {(long)Registers.InterruptDisable, new DoubleWordRegister(this)
                    .WithReservedBits(7, 25)
                    .WithValueField(0, 7, FieldMode.Write, name: "IDR",
                        writeCallback: (_, value) => interruptMask &= ~(uint)value)
                },
                {(long)Registers.InterruptMask, new DoubleWordRegister(this)
                    .WithReservedBits(7, 25)
                    .WithValueField(0, 7, FieldMode.Read, name: "IMR", valueProviderCallback: _ => interruptMask)
                },
                {(long)Registers.Enable, new DoubleWordRegister(this)
                    .WithReservedBits(1, 31)
                    .WithFlag(0, out enable, name: "SPI_EN")
                },
                {(long)Registers.Delay, new DoubleWordRegister(this)
                    .WithValueField(24, 8, name: "D_NSS")
                    .WithValueField(16, 8, name: "D_BTWN")
                    .WithValueField(8, 8, name: "D_AFTER")
                    .WithValueField(0, 8, name: "D_INIT")
                },
                {(long)Registers.TxData, new DoubleWordRegister(this)
                    .WithValueField(0, 32, FieldMode.Write, name: "TXD",
                        writeCallback: (_, value) =>
                        {
                            if(txFifo.Count >= FifoDepth)
                            {
                                this.Log(LogLevel.Warning, "TX FIFO full, byte 0x{0:X2} dropped", (byte)value);
                                return;
                            }
                            txFifo.Enqueue((byte)value);
                        })
                },
                {(long)Registers.RxData, new DoubleWordRegister(this)
                    .WithValueField(0, 32, FieldMode.Read, name: "RXD",
                        valueProviderCallback: _ =>
                        {
                            if(rxFifo.Count == 0)
                            {
                                this.Log(LogLevel.Debug, "Reading an empty RX FIFO");
                                return 0;
                            }
                            return rxFifo.Dequeue();
                        })
                },
                {(long)Registers.SlaveIdleCount, new DoubleWordRegister(this, 0xFF)
                    .WithReservedBits(8, 24)
                    .WithValueField(0, 8, name: "SICR")
                },
                {(long)Registers.ModuleId, new DoubleWordRegister(this, ModuleIdValue)
                    .WithValueField(0, 32, FieldMode.Read, name: "MOD_ID")
                },
                {(long)Registers.TxWatermark, new DoubleWordRegister(this, 0x1)
                    .WithReservedBits(7, 25)
                    .WithValueField(0, 7, out txWatermark, name: "TXWR")
                },
                {(long)Registers.RxWatermark, new DoubleWordRegister(this, 0x1)
                    .WithReservedBits(7, 25)
                    .WithValueField(0, 7, out rxWatermark, name: "RXWR")
                },
            };
        }

        private const int FifoDepth = 128;
        private const uint ModuleIdValue = 0x00090106;
        private const uint StickyMask = (uint)(Status.RxOverflow | Status.ModeFault | Status.TxUnderflow);

        private readonly ulong referenceClockFrequency;
        private readonly byte floatingMisoValue;
        private readonly object sync = new object();
        private readonly DoubleWordRegisterCollection registers;
        private readonly Queue<byte> txFifo = new Queue<byte>();
        private readonly Queue<byte> rxFifo = new Queue<byte>();

        private ISPIPeripheral selected;
        private int selectedIndex = -1;
        private bool busy;
        private ulong generation;
        private uint sticky;
        private uint interruptMask;

        private IFlagRegisterField manualStartEnable;
        private IFlagRegisterField manualChipSelect;
        private IValueRegisterField slaveSelect;
        private IFlagRegisterField decodeEnable;
        private IValueRegisterField prescaler;
        private IFlagRegisterField masterMode;
        private IFlagRegisterField enable;
        private IValueRegisterField txWatermark;
        private IValueRegisterField rxWatermark;

        [Flags]
        private enum Status : uint
        {
            RxOverflow = 1 << 0,
            ModeFault = 1 << 1,
            TxUnderWatermark = 1 << 2,
            TxFull = 1 << 3,
            RxNotEmpty = 1 << 4,
            RxFull = 1 << 5,
            TxUnderflow = 1 << 6,
        }

        private enum Registers : long
        {
            Config = 0x00,
            InterruptStatus = 0x04,
            InterruptEnable = 0x08,
            InterruptDisable = 0x0C,
            InterruptMask = 0x10,
            Enable = 0x14,
            Delay = 0x18,
            TxData = 0x1C,
            RxData = 0x20,
            SlaveIdleCount = 0x24,
            TxWatermark = 0x28,
            RxWatermark = 0x2C,
            ModuleId = 0xFC,
        }
    }
}
