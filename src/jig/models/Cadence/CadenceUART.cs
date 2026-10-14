//
// Cadence UART controller (Zynq-7000 PS UART0/UART1).
//
// References: UG585 (Zynq-7000 TRM) chapter 19 "UART Controller" and register
// reference B.33; register and bit names follow the Xilinx driver header
// xuartps_hw.h, which is what the firmware's driver programs against.
//
// Behaviour modelled:
//  - 64-byte TX and RX FIFOs; TX overflow (TOVR) and RX overrun (OVER) drop data.
//  - Line timing: one character occupies (start + data + parity + stop) bit
//    periods at the programmed baud rate. Characters leave the transmitter, and
//    characters arriving from the line enter the receiver, at that rate, so FIFO
//    levels, the TX-active flag and overruns behave like silicon. Bytes handed to
//    WriteChar faster than the line rate wait on the line, as a back-to-back
//    transmission from a peer at the same baud rate would.
//  - Receiver timeout: re-armed by every received character and by CR.TORST,
//    expires after RXTOUT x 4 bit periods and latches ISR.TOUT.
//  - Channel modes: normal, automatic echo, local loopback, remote loopback.
//  - Edge-latched interrupt status (write-one-to-clear), IER/IDR/IMR, one IRQ.
//  - Error injection for tests: parity, framing and break on received data.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure.Registers;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.UART;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.Cadence
{
    [AllowedTranslations(AllowedTranslation.ByteToDoubleWord | AllowedTranslation.WordToDoubleWord)]
    public sealed class CadenceUART : IUART, IDoubleWordPeripheral, IKnownSize
    {
        public CadenceUART(IMachine machine, ulong clockFrequency = 50000000, int fifoDepth = 64)
        {
            this.machine = machine;
            this.clockFrequency = clockFrequency;
            this.fifoDepth = fifoDepth;
            IRQ = new GPIO();
            registers = new DoubleWordRegisterCollection(this, BuildRegisterMap());
            Reset();
        }

        public void Reset()
        {
            lock(sync)
            {
                generation++;
                registers.Reset();
                txFifo.Clear();
                rxFifo.Clear();
                rxStatus.Clear();
                line.Clear();
                txActive = false;
                rxActive = false;
                rxTimeoutArmed = false;
                interruptStatus = 0;
                interruptMask = 0;
                lastConditions = 0;
                UpdateConditionsAndIrq();
            }
        }

        public uint ReadDoubleWord(long offset)
        {
            lock(sync)
            {
                var value = registers.Read(offset);
                UpdateConditionsAndIrq();
                return value;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            lock(sync)
            {
                registers.Write(offset, value);
                UpdateConditionsAndIrq();
            }
        }

        // A byte arriving on the RxD pin from the outside world.
        public void WriteChar(byte value)
        {
            lock(sync)
            {
                if(Mode == ChannelMode.LocalLoopback)
                {
                    // RxD is disconnected from the receiver in local loopback.
                    return;
                }
                line.Enqueue(new LineChar(value, LineError.None));
                StartReceiverIfIdle();
            }
        }

        public void InjectReceiveError(byte value, string kind)
        {
            LineError error;
            switch(kind)
            {
            case "parity": error = LineError.Parity; break;
            case "framing": error = LineError.Framing; break;
            case "break": error = LineError.Break; break;
            default: throw new ArgumentException($"unknown UART error kind '{kind}' (parity, framing, break)");
            }
            lock(sync)
            {
                line.Enqueue(new LineChar(value, error));
                StartReceiverIfIdle();
            }
        }

        public long Size => 0x1000;

        public GPIO IRQ { get; }

        public event Action<byte> CharReceived;

        public uint BaudRate
        {
            get
            {
                var cd = baudGenerator.Value;
                if(cd == 0)
                {
                    return 0;
                }
                var selectedClock = clockFrequency / (clockSelectDiv8.Value ? 8UL : 1UL);
                return (uint)(selectedClock / (cd * (baudDivider.Value + 1)));
            }
        }

        public Bits StopBits
        {
            get
            {
                switch(stopMode.Value)
                {
                case 1: return Bits.OneAndAHalf;
                case 2: return Bits.Two;
                default: return Bits.One;
                }
            }
        }

        public Parity ParityBit
        {
            get
            {
                switch(parityMode.Value)
                {
                case 0: return Parity.Even;
                case 1: return Parity.Odd;
                case 2: return Parity.Forced0;
                case 3: return Parity.Forced1;
                default: return Parity.None;
                }
            }
        }

        // ---------------------------------------------------------------- timing

        // CHRL: 0b11 = 6 bits, 0b10 = 7 bits, 0b0x = 8 bits.
        private int DataBits => charLength.Value == 3 ? 6 : charLength.Value == 2 ? 7 : 8;

        private double BitsPerCharacter
        {
            get
            {
                var stop = stopMode.Value == 1 ? 1.5 : stopMode.Value == 2 ? 2.0 : 1.0;
                var parity = parityMode.Value >= 4 ? 0 : 1;
                return 1 + DataBits + parity + stop;
            }
        }

        private bool TryGetNanoseconds(double bitPeriods, out ulong nanoseconds)
        {
            var baud = BaudRate;
            if(baud == 0)
            {
                nanoseconds = 0;
                return false;
            }
            nanoseconds = Math.Max(1UL, (ulong)Math.Round(bitPeriods * 1e9 / baud));
            return true;
        }

        // ------------------------------------------------------------ transmitter

        private bool TxEnabled => txEnable.Value && !txDisable.Value;

        private bool RxEnabled => rxEnable.Value && !rxDisable.Value;

        private ChannelMode Mode => (ChannelMode)channelMode.Value;

        private void PushTx(byte value)
        {
            if(txFifo.Count >= fifoDepth)
            {
                Latch(Interrupt.TxOverflow);
                this.Log(LogLevel.Warning, "TX FIFO overflow, byte 0x{0:X2} dropped", value);
                return;
            }
            txFifo.Enqueue((byte)(value & DataMask));
            // The byte sits in the FIFO before an idle transmitter takes it, so
            // the FIFO's empty -> not empty -> empty transition latches TEMPTY.
            UpdateConditionsAndIrq();
            StartTransmitterIfIdle();
        }

        private byte DataMask => (byte)((1 << DataBits) - 1);

        private void StartTransmitterIfIdle()
        {
            if(txActive || txFifo.Count == 0 || !TxEnabled)
            {
                return;
            }
            if(!TryGetNanoseconds(BitsPerCharacter, out var duration))
            {
                return; // baud generator disabled: the transmitter does not run
            }
            var character = txFifo.Dequeue();
            txActive = true;
            var token = generation;
            machine.ScheduleAction(TimeInterval.FromNanoseconds(duration), _ =>
            {
                lock(sync)
                {
                    if(token != generation)
                    {
                        return;
                    }
                    txActive = false;
                    EmitTransmitted(character);
                    StartTransmitterIfIdle();
                    UpdateConditionsAndIrq();
                }
            }, "uart-tx");
        }

        private void EmitTransmitted(byte character)
        {
            switch(Mode)
            {
            case ChannelMode.Normal:
                CharReceived?.Invoke(character);
                break;
            case ChannelMode.LocalLoopback:
                // TxD held idle; the character goes straight to our receiver.
                ReceiveCharacter(new LineChar(character, LineError.None));
                break;
            case ChannelMode.AutomaticEcho:
            case ChannelMode.RemoteLoopback:
                // TxD is driven from RxD; the CPU's transmitter is not connected.
                break;
            }
        }

        // --------------------------------------------------------------- receiver

        private void StartReceiverIfIdle()
        {
            if(rxActive || line.Count == 0)
            {
                return;
            }
            if(!TryGetNanoseconds(BitsPerCharacter, out var duration))
            {
                this.Log(LogLevel.Warning, "Baud generator disabled; {0} incoming byte(s) lost", line.Count);
                line.Clear();
                return;
            }
            var incoming = line.Dequeue();
            rxActive = true;
            var token = generation;
            machine.ScheduleAction(TimeInterval.FromNanoseconds(duration), _ =>
            {
                lock(sync)
                {
                    if(token != generation)
                    {
                        return;
                    }
                    rxActive = false;
                    switch(Mode)
                    {
                    case ChannelMode.Normal:
                        ReceiveCharacter(incoming);
                        break;
                    case ChannelMode.AutomaticEcho:
                        ReceiveCharacter(incoming);
                        CharReceived?.Invoke(incoming.Value);
                        break;
                    case ChannelMode.RemoteLoopback:
                        CharReceived?.Invoke(incoming.Value);
                        break;
                    }
                    StartReceiverIfIdle();
                    UpdateConditionsAndIrq();
                }
            }, "uart-rx");
        }

        private void ReceiveCharacter(LineChar incoming)
        {
            if(!RxEnabled)
            {
                return;
            }
            if(rxFifo.Count >= fifoDepth)
            {
                Latch(Interrupt.RxOverrun);
                this.Log(LogLevel.Debug, "RX FIFO overrun, byte 0x{0:X2} dropped", incoming.Value);
                return;
            }
            rxFifo.Enqueue((byte)(incoming.Value & DataMask));
            rxStatus.Enqueue(incoming.Error);
            switch(incoming.Error)
            {
            case LineError.Parity: Latch(Interrupt.Parity); break;
            case LineError.Framing: Latch(Interrupt.Framing); break;
            case LineError.Break: Latch(Interrupt.RxBreak); break;
            }
            ArmRxTimeout();
        }

        private byte PopRx()
        {
            if(!RxEnabled)
            {
                this.Log(LogLevel.Debug, "Reading the RX FIFO while the receiver is disabled");
            }
            if(rxFifo.Count == 0)
            {
                this.Log(LogLevel.Debug, "Reading an empty RX FIFO");
                return 0;
            }
            rxStatus.Dequeue();
            return rxFifo.Dequeue();
        }

        private void ArmRxTimeout()
        {
            timeoutToken++;
            rxTimeoutArmed = false;
            if(!RxEnabled || rxTimeout.Value == 0)
            {
                return;
            }
            if(!TryGetNanoseconds(4.0 * rxTimeout.Value, out var duration))
            {
                return;
            }
            rxTimeoutArmed = true;
            var token = timeoutToken;
            var resetGeneration = generation;
            machine.ScheduleAction(TimeInterval.FromNanoseconds(duration), _ =>
            {
                lock(sync)
                {
                    if(token != timeoutToken || resetGeneration != generation || !rxTimeoutArmed)
                    {
                        return;
                    }
                    rxTimeoutArmed = false;
                    Latch(Interrupt.Timeout);
                    UpdateConditionsAndIrq();
                }
            }, "uart-rx-timeout");
        }

        // ------------------------------------------------------------- interrupts

        private void Latch(Interrupt bit)
        {
            interruptStatus |= (uint)bit;
        }

        private uint CurrentConditions()
        {
            uint c = 0;
            var rx = rxFifo.Count;
            var tx = txFifo.Count;
            if(rxTriggerLevel.Value > 0 && rx >= (int)rxTriggerLevel.Value) c |= (uint)Interrupt.RxTrigger;
            if(rx == 0) c |= (uint)Interrupt.RxEmpty;
            if(rx >= fifoDepth) c |= (uint)Interrupt.RxFull;
            if(tx == 0) c |= (uint)Interrupt.TxEmpty;
            if(tx >= fifoDepth) c |= (uint)Interrupt.TxFull;
            if(tx >= (int)txTriggerLevel.Value) c |= (uint)Interrupt.TxTrigger;
            if(tx >= fifoDepth - 1) c |= (uint)Interrupt.TxNearlyFull;
            return c;
        }

        private void UpdateConditionsAndIrq()
        {
            var now = CurrentConditions();
            // Status-derived interrupts latch on the condition's rising edge.
            interruptStatus |= now & ~lastConditions;
            lastConditions = now;
            IRQ.Set((interruptStatus & interruptMask) != 0);
        }

        // -------------------------------------------------------------- registers

        private Dictionary<long, DoubleWordRegister> BuildRegisterMap()
        {
            return new Dictionary<long, DoubleWordRegister>
            {
                {(long)Registers.Control, new DoubleWordRegister(this, 0x128)
                    .WithReservedBits(9, 23)
                    .WithFlag(8, out stopBreak, name: "STOPBRK")
                    .WithFlag(7, out startBreak, name: "STARTBRK")
                    .WithFlag(6, FieldMode.Read | FieldMode.WriteOneToClear, name: "TORST",
                        writeCallback: (_, value) => { if(value) ArmRxTimeout(); })
                    .WithFlag(5, out txDisable, name: "TX_DIS")
                    .WithFlag(4, out txEnable, name: "TX_EN")
                    .WithFlag(3, out rxDisable, name: "RX_DIS")
                    .WithFlag(2, out rxEnable, name: "RX_EN")
                    .WithFlag(1, FieldMode.Read | FieldMode.WriteOneToClear, name: "TXRST",
                        writeCallback: (_, value) =>
                        {
                            if(!value) return;
                            txFifo.Clear();
                        })
                    .WithFlag(0, FieldMode.Read | FieldMode.WriteOneToClear, name: "RXRST",
                        writeCallback: (_, value) =>
                        {
                            if(!value) return;
                            rxFifo.Clear();
                            rxStatus.Clear();
                        })
                    .WithWriteCallback((_, __) => StartTransmitterIfIdle())
                },
                {(long)Registers.Mode, new DoubleWordRegister(this)
                    .WithReservedBits(11, 21)
                    .WithFlag(10, name: "CCLK")
                    .WithValueField(8, 2, out channelMode, name: "CHMODE")
                    .WithValueField(6, 2, out stopMode, name: "NBSTOP")
                    .WithValueField(3, 3, out parityMode, name: "PAR")
                    .WithValueField(1, 2, out charLength, name: "CHRL")
                    .WithFlag(0, out clockSelectDiv8, name: "CLKSEL")
                },
                {(long)Registers.InterruptEnable, new DoubleWordRegister(this)
                    .WithReservedBits(14, 18)
                    .WithValueField(0, 14, FieldMode.Write, name: "IER",
                        writeCallback: (_, value) => interruptMask |= (uint)value)
                },
                {(long)Registers.InterruptDisable, new DoubleWordRegister(this)
                    .WithReservedBits(14, 18)
                    .WithValueField(0, 14, FieldMode.Write, name: "IDR",
                        writeCallback: (_, value) => interruptMask &= ~(uint)value)
                },
                {(long)Registers.InterruptMask, new DoubleWordRegister(this)
                    .WithReservedBits(14, 18)
                    .WithValueField(0, 14, FieldMode.Read, name: "IMR",
                        valueProviderCallback: _ => interruptMask)
                },
                {(long)Registers.InterruptStatus, new DoubleWordRegister(this)
                    .WithReservedBits(14, 18)
                    .WithValueField(0, 14, name: "ISR",
                        valueProviderCallback: _ => interruptStatus,
                        writeCallback: (_, value) => interruptStatus &= ~(uint)value)
                },
                {(long)Registers.BaudRateGenerator, new DoubleWordRegister(this, 0x28B)
                    .WithReservedBits(16, 16)
                    .WithValueField(0, 16, out baudGenerator, name: "CD")
                },
                {(long)Registers.ReceiverTimeout, new DoubleWordRegister(this)
                    .WithReservedBits(8, 24)
                    .WithValueField(0, 8, out rxTimeout, name: "RTO")
                },
                {(long)Registers.RxFifoTriggerLevel, new DoubleWordRegister(this, 0x20)
                    .WithReservedBits(6, 26)
                    .WithValueField(0, 6, out rxTriggerLevel, name: "RTRIG")
                },
                {(long)Registers.ModemControl, new DoubleWordRegister(this)
                    .WithReservedBits(6, 26)
                    .WithFlag(5, out flowControlMode, name: "FCM")
                    .WithReservedBits(2, 3)
                    .WithFlag(1, name: "RTS")
                    .WithFlag(0, name: "DTR")
                },
                {(long)Registers.ModemStatus, new DoubleWordRegister(this)
                    .WithReservedBits(9, 23)
                    .WithFlag(8, FieldMode.Read, name: "FCMS", valueProviderCallback: _ => flowControlMode.Value)
                    // No modem lines are wired on this board: inputs read inactive, no deltas.
                    .WithValueField(0, 8, FieldMode.Read, name: "MODEM_IN", valueProviderCallback: _ => 0)
                },
                {(long)Registers.ChannelStatus, new DoubleWordRegister(this)
                    .WithReservedBits(15, 17)
                    .WithFlag(14, FieldMode.Read, name: "TNFUL", valueProviderCallback: _ => txFifo.Count >= fifoDepth - 1)
                    .WithFlag(13, FieldMode.Read, name: "TTRIG", valueProviderCallback: _ => txFifo.Count >= (int)txTriggerLevel.Value)
                    .WithFlag(12, FieldMode.Read, name: "FDELT", valueProviderCallback: _ => flowDelay.Value >= 4 && rxFifo.Count >= (int)flowDelay.Value)
                    .WithFlag(11, FieldMode.Read, name: "TACTIVE", valueProviderCallback: _ => txActive)
                    .WithFlag(10, FieldMode.Read, name: "RACTIVE", valueProviderCallback: _ => rxActive)
                    .WithReservedBits(5, 5)
                    .WithFlag(4, FieldMode.Read, name: "TFUL", valueProviderCallback: _ => txFifo.Count >= fifoDepth)
                    .WithFlag(3, FieldMode.Read, name: "TEMPTY", valueProviderCallback: _ => txFifo.Count == 0)
                    .WithFlag(2, FieldMode.Read, name: "RFUL", valueProviderCallback: _ => rxFifo.Count >= fifoDepth)
                    .WithFlag(1, FieldMode.Read, name: "REMPTY", valueProviderCallback: _ => rxFifo.Count == 0)
                    .WithFlag(0, FieldMode.Read, name: "RTRIG", valueProviderCallback: _ => rxTriggerLevel.Value > 0 && rxFifo.Count >= (int)rxTriggerLevel.Value)
                },
                {(long)Registers.Fifo, new DoubleWordRegister(this)
                    .WithReservedBits(8, 24)
                    .WithValueField(0, 8, name: "FIFO",
                        valueProviderCallback: _ => PopRx(),
                        writeCallback: (_, value) => PushTx((byte)value))
                },
                {(long)Registers.BaudRateDivider, new DoubleWordRegister(this, 0xF)
                    .WithReservedBits(8, 24)
                    .WithValueField(0, 8, out baudDivider, name: "BDIV")
                },
                {(long)Registers.FlowDelay, new DoubleWordRegister(this)
                    .WithReservedBits(6, 26)
                    .WithValueField(0, 6, out flowDelay, name: "FDEL")
                },
                {(long)Registers.TxFifoTriggerLevel, new DoubleWordRegister(this, 0x20)
                    .WithReservedBits(6, 26)
                    .WithValueField(0, 6, out txTriggerLevel, name: "TTRIG")
                },
                {(long)Registers.RxFifoByteStatus, new DoubleWordRegister(this)
                    .WithReservedBits(12, 20)
                    .WithValueField(0, 12, FieldMode.Read, name: "RXBS", valueProviderCallback: _ => ByteStatus())
                },
            };
        }

        private uint ByteStatus()
        {
            // Status of the next (up to) four bytes in the RX FIFO: per byte, bit0
            // parity, bit1 framing, bit2 break.
            uint value = 0;
            var index = 0;
            foreach(var error in rxStatus)
            {
                if(index == 4)
                {
                    break;
                }
                uint bits = error == LineError.Parity ? 1u : error == LineError.Framing ? 2u : error == LineError.Break ? 4u : 0u;
                value |= bits << (3 * index);
                index++;
            }
            return value;
        }

        private readonly IMachine machine;
        private readonly ulong clockFrequency;
        private readonly int fifoDepth;
        private readonly object sync = new object();
        private readonly DoubleWordRegisterCollection registers;
        private readonly Queue<byte> txFifo = new Queue<byte>();
        private readonly Queue<byte> rxFifo = new Queue<byte>();
        private readonly Queue<LineError> rxStatus = new Queue<LineError>();
        private readonly Queue<LineChar> line = new Queue<LineChar>();

        private ulong generation;
        private ulong timeoutToken;
        private bool txActive;
        private bool rxActive;
        private bool rxTimeoutArmed;
        private uint interruptStatus;
        private uint interruptMask;
        private uint lastConditions;

        private IFlagRegisterField stopBreak;
        private IFlagRegisterField startBreak;
        private IFlagRegisterField txDisable;
        private IFlagRegisterField txEnable;
        private IFlagRegisterField rxDisable;
        private IFlagRegisterField rxEnable;
        private IValueRegisterField channelMode;
        private IValueRegisterField stopMode;
        private IValueRegisterField parityMode;
        private IValueRegisterField charLength;
        private IFlagRegisterField clockSelectDiv8;
        private IValueRegisterField baudGenerator;
        private IValueRegisterField rxTimeout;
        private IValueRegisterField rxTriggerLevel;
        private IFlagRegisterField flowControlMode;
        private IValueRegisterField baudDivider;
        private IValueRegisterField flowDelay;
        private IValueRegisterField txTriggerLevel;

        private struct LineChar
        {
            public LineChar(byte value, LineError error)
            {
                Value = value;
                Error = error;
            }

            public readonly byte Value;
            public readonly LineError Error;
        }

        private enum LineError
        {
            None,
            Parity,
            Framing,
            Break,
        }

        private enum ChannelMode
        {
            Normal = 0,
            AutomaticEcho = 1,
            LocalLoopback = 2,
            RemoteLoopback = 3,
        }

        [Flags]
        private enum Interrupt : uint
        {
            RxTrigger = 1 << 0,
            RxEmpty = 1 << 1,
            RxFull = 1 << 2,
            TxEmpty = 1 << 3,
            TxFull = 1 << 4,
            RxOverrun = 1 << 5,
            Framing = 1 << 6,
            Parity = 1 << 7,
            Timeout = 1 << 8,
            ModemStatus = 1 << 9,
            TxTrigger = 1 << 10,
            TxNearlyFull = 1 << 11,
            TxOverflow = 1 << 12,
            RxBreak = 1 << 13,
        }

        private enum Registers : long
        {
            Control = 0x00,
            Mode = 0x04,
            InterruptEnable = 0x08,
            InterruptDisable = 0x0C,
            InterruptMask = 0x10,
            InterruptStatus = 0x14,
            BaudRateGenerator = 0x18,
            ReceiverTimeout = 0x1C,
            RxFifoTriggerLevel = 0x20,
            ModemControl = 0x24,
            ModemStatus = 0x28,
            ChannelStatus = 0x2C,
            Fifo = 0x30,
            BaudRateDivider = 0x34,
            FlowDelay = 0x38,
            TxFifoTriggerLevel = 0x44,
            RxFifoByteStatus = 0x48,
        }
    }
}
