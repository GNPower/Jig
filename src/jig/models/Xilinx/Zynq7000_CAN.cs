//
// Zynq-7000 CAN controller (CANPS, 0xE0008000 / 0xE0009000).
//
// References: UG585 v1.7 chapter 18 "CAN Controller" and register reference
// B.5; names and register formats follow the Xilinx header xcanps_hw.h, the
// field layout XCanPs_SelfTest compares.
//
// Behaviour modelled:
//  - Modes: configuration (SRR.CEN = 0), normal, loopback, sleep, snoop, with
//    SR reporting the mode at once (BIDLE and BBSY read 0 in configuration
//    mode). MSR, BRPR, BTR and WIR are writable only in configuration mode.
//    SRR.SRST resets the controller. Writing CEN = 0 clears TEC and REC and
//    the ISR event bits (B.5 ECR and ISR).
//  - TX: 64-message FIFO plus a 1-message high-priority buffer (served first); a
//    message is committed by writing its DW2 word. Each frame occupies its
//    nominal bit count (47 + 8n standard, 67 + 8n extended, no stuffing) at the
//    rate set by BRPR/BTR and the reference clock.
//  - Loopback: frames return to the receiver internally and never reach the bus.
//  - Normal: frames go to the attached bus; without an acknowledgment the
//    controller flags ESR.ACKER, raises TEC by 8 (not while error-passive, per
//    the CAN fault-confinement rules) and retransmits after an error frame.
//    TEC >= 256 enters bus-off (ISR.BSOFF); recovery follows 128 x 11 recessive
//    bits. Successful transmissions decrement TEC.
//  - RX: frames pass the enabled acceptance filters (AFR/AFMRn/AFIRn) into a
//    64-message FIFO; reading DW2 pops it. RXNEMP re-asserts while messages
//    remain, RXOFLW flags overflow, reading an empty FIFO flags RXUFLW. The DLC
//    word carries a 16-bit bit-time timestamp (cleared by TCR).
//  - Sleep: a received frame wakes the controller (ISR.WKUP) into normal mode.
//  - ISR/IER/ICR interrupt logic, ESR write-one-to-clear, ECR. WIR resets to
//    0x3F3F. A set condition wins over an ICR clear (B.5 ISR): TXFEMP stays
//    set while the TX FIFO is empty, TXFWMEMP while its empty slots exceed
//    WIR.EW, RXNEMP while messages remain, RXFWMFLL while their number
//    exceeds WIR.FW.
//  - Bus acknowledgment, when the controller is attached to a Jig CanBus,
//    comes from the other nodes; on a plain Renode CANHub frames are assumed
//    acknowledged, since that hub has no notion of it.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.CAN;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CAN;
using Antmicro.Renode.Peripherals.Jig.Harness;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.Xilinx
{
    public sealed class Zynq7000_CAN : IDoubleWordPeripheral, IKnownSize, IJigCanNode
    {
        public Zynq7000_CAN(IMachine machine, ulong referenceClockFrequency = 100000000)
        {
            this.machine = machine;
            this.referenceClockFrequency = referenceClockFrequency;
            IRQ = new GPIO();
            Reset();
        }

        public void Reset()
        {
            lock(sync)
            {
                generation++;
                softReset();
            }
        }

        public long Size => 0x1000;

        public GPIO IRQ { get; }

        public event Action<CANMessageFrame> FrameSent;

        public bool Acknowledges
        {
            get
            {
                lock(sync)
                {
                    return enabled && Mode == ControllerMode.Normal && !busOff;
                }
            }
        }

        public double BitRate
        {
            get
            {
                var quanta = 1.0 + ((btr & 0xF) + 1) + (((btr >> 4) & 0x7) + 1);
                return referenceClockFrequency / ((brpr & 0xFF) + 1.0) / quanta;
            }
        }

        public void AttachedToBus(CanBus bus)
        {
            lock(sync) this.bus = bus;
        }

        public void DetachedFromBus(CanBus bus)
        {
            lock(sync)
            {
                if(this.bus == bus) this.bus = null;
            }
        }

        // ---------------------------------------------------------------- registers

        public uint ReadDoubleWord(long offset)
        {
            lock(sync)
            {
                switch(offset)
                {
                case SrrOffset: return enabled ? SrrEnable : 0u;
                case MsrOffset: return msr;
                case BrprOffset: return brpr;
                case BtrOffset: return btr;
                case EcrOffset: return (uint)(Math.Min(rec, 255) << 8 | Math.Min(tec, 255));
                case EsrOffset: return esr;
                case SrOffset: return StatusRegister();
                case IsrOffset: return isr;
                case IerOffset: return ier;
                case IcrOffset: return 0;
                case TcrOffset: return 0;
                case WirOffset: return wir;
                case RxIdOffset: return rxFifo.Count > 0 ? rxFifo.Peek().Id : Underflow();
                case RxDlcOffset: return rxFifo.Count > 0 ? rxFifo.Peek().Dlc : 0;
                case RxDw1Offset: return rxFifo.Count > 0 ? rxFifo.Peek().Dw1 : 0;
                case RxDw2Offset:
                {
                    if(rxFifo.Count == 0)
                    {
                        return Underflow();
                    }
                    var message = rxFifo.Dequeue();
                    UpdateRxStatus();
                    UpdateIrq();
                    return message.Dw2;
                }
                case AfrOffset: return afr;
                default:
                    if(offset >= AfmrBase && offset < AfmrBase + 0x20)
                    {
                        var index = (int)((offset - AfmrBase) / 8);
                        return (offset - AfmrBase) % 8 == 0 ? filterMasks[index] : filterIds[index];
                    }
                    if(offset >= TxIdOffset && offset <= TxHpbDw2Offset)
                    {
                        return 0; // the TX FIFO and buffer are write-only
                    }
                    this.Log(LogLevel.Warning, "Read from unhandled offset 0x{0:X2}", offset);
                    return 0;
                }
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            lock(sync)
            {
                switch(offset)
                {
                case SrrOffset:
                    if((value & SrrSoftReset) != 0)
                    {
                        generation++;
                        softReset();
                        break;
                    }
                    SetEnabled((value & SrrEnable) != 0);
                    break;
                case MsrOffset:
                    if(enabled)
                    {
                        // Only the sleep bit may change outside configuration mode.
                        if(Mode == ControllerMode.Normal && (value & MsrSleep) != 0)
                        {
                            msr = MsrSleep;
                            isr |= IxrSleep;
                        }
                        else if(Mode == ControllerMode.Sleep && (value & MsrSleep) == 0)
                        {
                            msr = 0;
                        }
                        else
                        {
                            this.Log(LogLevel.Warning, "MSR write 0x{0:X} ignored outside configuration mode", value);
                        }
                    }
                    else
                    {
                        msr = value & (MsrSleep | MsrLoopback | MsrSnoop);
                    }
                    break;
                case BrprOffset:
                    if(enabled) this.Log(LogLevel.Warning, "BRPR is writable only in configuration mode");
                    else brpr = value & 0xFF;
                    break;
                case BtrOffset:
                    if(enabled) this.Log(LogLevel.Warning, "BTR is writable only in configuration mode");
                    else btr = value & 0x1FF;
                    break;
                case EsrOffset:
                    esr &= ~value;
                    break;
                case IerOffset:
                    ier = value & IxrAll;
                    break;
                case IcrOffset:
                    isr &= ~value;
                    // A condition that still holds sets its bit again.
                    UpdateRxStatus();
                    UpdateTxStatus();
                    break;
                case TcrOffset:
                    if((value & 1) != 0) timestampEpoch = Now();
                    break;
                case WirOffset:
                    if(enabled)
                    {
                        this.Log(LogLevel.Warning, "WIR is writable only in configuration mode");
                    }
                    else
                    {
                        wir = value & 0x3F3F;
                    }
                    break;
                case TxIdOffset: txStaging.Id = value; break;
                case TxDlcOffset: txStaging.Dlc = value; break;
                case TxDw1Offset: txStaging.Dw1 = value; break;
                case TxDw2Offset:
                    txStaging.Dw2 = value;
                    QueueTransmit(txStaging, highPriority: false);
                    break;
                case TxHpbIdOffset: hpbStaging.Id = value; break;
                case TxHpbDlcOffset: hpbStaging.Dlc = value; break;
                case TxHpbDw1Offset: hpbStaging.Dw1 = value; break;
                case TxHpbDw2Offset:
                    hpbStaging.Dw2 = value;
                    QueueTransmit(hpbStaging, highPriority: true);
                    break;
                case AfrOffset:
                    afr = value & 0xF;
                    break;
                default:
                    if(offset >= AfmrBase && offset < AfmrBase + 0x20)
                    {
                        var index = (int)((offset - AfmrBase) / 8);
                        if((afr & (1u << index)) != 0)
                        {
                            this.Log(LogLevel.Warning, "Acceptance filter {0} changed while enabled", index + 1);
                        }
                        if((offset - AfmrBase) % 8 == 0) filterMasks[index] = value;
                        else filterIds[index] = value;
                        break;
                    }
                    this.Log(LogLevel.Warning, "Write 0x{0:X8} to unhandled offset 0x{1:X2}", value, offset);
                    break;
                }
                UpdateIrq();
            }
        }

        // -------------------------------------------------------------- bus side

        public void OnFrameReceived(CANMessageFrame frame)
        {
            lock(sync)
            {
                if(!enabled || busOff)
                {
                    return;
                }
                var mode = Mode;
                if(mode == ControllerMode.Loopback)
                {
                    return; // loopback is internal: the bus is not listened to
                }
                if(mode == ControllerMode.Sleep)
                {
                    msr = 0;
                    isr |= IxrWakeUp;
                }
                Receive(Message.FromFrame(frame));
                UpdateIrq();
            }
        }

        // ------------------------------------------------------------- internals

        private void softReset()
        {
            enabled = false;
            msr = 0;
            brpr = 0;
            btr = 0;
            tec = 0;
            rec = 0;
            esr = 0;
            isr = 0;
            ier = 0;
            wir = WirResetValue;
            afr = 0;
            Array.Clear(filterMasks, 0, filterMasks.Length);
            Array.Clear(filterIds, 0, filterIds.Length);
            txFifo.Clear();
            hpb = null;
            rxFifo.Clear();
            transmitting = false;
            busOff = false;
            timestampEpoch = Now();
            UpdateTxStatus();
            UpdateIrq();
        }

        private ControllerMode Mode
        {
            get
            {
                if(!enabled) return ControllerMode.Configuration;
                if((msr & MsrLoopback) != 0) return ControllerMode.Loopback;
                if((msr & MsrSleep) != 0) return ControllerMode.Sleep;
                if((msr & MsrSnoop) != 0) return ControllerMode.Snoop;
                return ControllerMode.Normal;
            }
        }

        private void SetEnabled(bool value)
        {
            if(enabled == value)
            {
                return;
            }
            enabled = value;
            if(!enabled)
            {
                generation++;      // abandon a frame in flight
                transmitting = false;
                busOff = false;
                tec = 0;
                rec = 0;
                isr &= ~IxrClearedByDisable;
            }
            TryStartTransmission();
        }

        private uint StatusRegister()
        {
            uint sr = 0;
            switch(Mode)
            {
            case ControllerMode.Configuration: sr |= SrConfig; break;
            case ControllerMode.Loopback: sr |= SrLoopback; break;
            case ControllerMode.Sleep: sr |= SrSleep; break;
            case ControllerMode.Snoop: sr |= SrSnoop; break;
            case ControllerMode.Normal: sr |= SrNormal; break;
            }
            if(enabled)
            {
                sr |= transmitting ? SrBusBusy : SrBusIdle;
            }
            if(tec > 96 || rec > 96) sr |= SrErrorWarning;
            if(enabled)
            {
                var state = busOff ? 0x2u : (tec >= 128 || rec >= 128) ? 0x3u : 0x1u;
                sr |= state << 7;
            }
            if(hpb.HasValue) sr |= SrTxHpbFull;
            if(txFifo.Count >= FifoDepth) sr |= SrTxFifoFull;
            return sr;
        }

        private uint Underflow()
        {
            isr |= IxrRxUnderflow;
            UpdateIrq();
            return 0;
        }

        private void QueueTransmit(Message message, bool highPriority)
        {
            if(highPriority)
            {
                if(hpb.HasValue)
                {
                    this.Log(LogLevel.Warning, "TX high-priority buffer full: message dropped");
                    return;
                }
                hpb = message;
            }
            else
            {
                if(txFifo.Count >= FifoDepth)
                {
                    this.Log(LogLevel.Warning, "TX FIFO full: message dropped");
                    return;
                }
                txFifo.Enqueue(message);
            }
            UpdateTxStatus();
            TryStartTransmission();
        }

        private void TryStartTransmission()
        {
            if(transmitting || busOff || !enabled)
            {
                return;
            }
            var mode = Mode;
            if(mode != ControllerMode.Normal && mode != ControllerMode.Loopback)
            {
                return;
            }
            if(!hpb.HasValue && txFifo.Count == 0)
            {
                return;
            }
            var fromHpb = hpb.HasValue;
            var message = fromHpb ? hpb.Value : txFifo.Peek();
            transmitting = true;
            ScheduleBits(message.FrameBits, () => FinishTransmission(message, fromHpb));
        }

        private void FinishTransmission(Message message, bool fromHpb)
        {
            transmitting = false;
            var mode = Mode;
            bool acknowledged;
            if(mode == ControllerMode.Loopback)
            {
                Receive(message);
                acknowledged = true;
            }
            else
            {
                var frame = message.ToFrame();
                var currentBus = bus;
                if(currentBus != null)
                {
                    acknowledged = currentBus.Transmit(this, frame);
                }
                else if(FrameSent != null)
                {
                    FrameSent(frame);
                    acknowledged = true;
                }
                else
                {
                    acknowledged = false; // nothing on the bus to acknowledge
                }
            }

            if(acknowledged)
            {
                if(fromHpb) hpb = null; else txFifo.Dequeue();
                if(tec > 0) tec--;
                isr |= IxrTxOk;
                UpdateTxStatus();
                TryStartTransmission();
            }
            else
            {
                esr |= EsrAckError;
                isr |= IxrError;
                var errorPassive = tec >= 128;
                if(!errorPassive)
                {
                    tec += 8;
                }
                if(tec >= 256)
                {
                    EnterBusOff();
                }
                else
                {
                    // error frame (up to 14 bits) + error delimiter (8) + intermission (3)
                    transmitting = true;
                    ScheduleBits(25, () => { transmitting = false; TryStartTransmission(); });
                }
            }
            UpdateIrq();
        }

        private void EnterBusOff()
        {
            busOff = true;
            isr |= IxrBusOff;
            this.Log(LogLevel.Warning, "Bus-off (TEC {0})", tec);
            // Recovery: 128 occurrences of 11 consecutive recessive bits.
            ScheduleBits(128 * 11, () =>
            {
                busOff = false;
                tec = 0;
                rec = 0;
                TryStartTransmission();
                UpdateIrq();
            });
        }

        private void Receive(Message message)
        {
            if(!PassesFilters(message.Id))
            {
                return;
            }
            if(rxFifo.Count >= FifoDepth)
            {
                isr |= IxrRxOverflow;
                this.Log(LogLevel.Debug, "RX FIFO overflow: message dropped");
                return;
            }
            var stamped = message;
            stamped.Dlc = (message.Dlc & 0xF0000000) | Timestamp();
            rxFifo.Enqueue(stamped);
            if(rec > 0) rec--;
            isr |= IxrRxOk;
            UpdateRxStatus();
        }

        private bool PassesFilters(uint id)
        {
            if(afr == 0)
            {
                return true;
            }
            for(var i = 0; i < 4; i++)
            {
                if((afr & (1u << i)) != 0 && (id & filterMasks[i]) == (filterIds[i] & filterMasks[i]))
                {
                    return true;
                }
            }
            return false;
        }

        private void UpdateRxStatus()
        {
            if(rxFifo.Count > 0) isr |= IxrRxNotEmpty;
            var watermark = (int)(wir & 0x3F);
            if(watermark > 0 && rxFifo.Count > watermark) isr |= IxrRxWatermarkFull;
        }

        private void UpdateTxStatus()
        {
            if(txFifo.Count >= FifoDepth) isr |= IxrTxFifoFull;
            if(hpb.HasValue) isr |= IxrTxHpbFull;
            if(txFifo.Count == 0) isr |= IxrTxFifoEmpty;
            var emptyWatermark = (int)((wir >> 8) & 0x3F);
            if(FifoDepth - txFifo.Count > emptyWatermark) isr |= IxrTxWatermarkEmpty;
        }

        private void UpdateIrq()
        {
            IRQ.Set((isr & ier) != 0);
        }

        private void ScheduleBits(int bits, Action action)
        {
            var rate = BitRate;
            if(rate <= 0 || double.IsInfinity(rate))
            {
                this.Log(LogLevel.Warning, "Bit timing not configured; transmission stalls");
                return;
            }
            var nanoseconds = (ulong)Math.Max(1.0, bits * 1e9 / rate);
            var token = generation;
            machine.ScheduleAction(TimeInterval.FromNanoseconds(nanoseconds), _ =>
            {
                lock(sync)
                {
                    if(token != generation)
                    {
                        return;
                    }
                    action();
                }
            }, "can-bits");
        }

        private double Now() => machine.ElapsedVirtualTime.TimeElapsed.TotalSeconds;

        private uint Timestamp()
        {
            var rate = BitRate;
            if(rate <= 0 || double.IsInfinity(rate)) return 0;
            return (uint)((ulong)((Now() - timestampEpoch) * rate) & 0xFFFF);
        }

        private struct Message
        {
            public uint Id;
            public uint Dlc;
            public uint Dw1;
            public uint Dw2;

            private bool Extended => (Id & IdrIde) != 0;

            private bool Remote => Extended ? (Id & IdrRtr) != 0 : (Id & IdrSrr) != 0;

            private int Length => Math.Min(8, (int)(Dlc >> 28));

            public int FrameBits => (Extended ? 67 : 47) + (Remote ? 0 : 8 * Length);

            public CANMessageFrame ToFrame()
            {
                var data = new byte[Remote ? 0 : Length];
                for(var i = 0; i < data.Length; i++)
                {
                    var word = i < 4 ? Dw1 : Dw2;
                    data[i] = (byte)(word >> (24 - 8 * (i % 4)));
                }
                var standard = Id >> 21;
                var id = Extended ? (standard << 18) | ((Id >> 1) & 0x3FFFF) : standard;
                return new CANMessageFrame(id, data, Extended, Remote);
            }

            public static Message FromFrame(CANMessageFrame frame)
            {
                var message = new Message();
                if(frame.ExtendedFormat)
                {
                    message.Id = ((frame.Id >> 18) & 0x7FF) << 21 | IdrSrr | IdrIde | (frame.Id & 0x3FFFF) << 1 | (frame.RemoteFrame ? IdrRtr : 0);
                }
                else
                {
                    message.Id = (frame.Id & 0x7FF) << 21 | (frame.RemoteFrame ? IdrSrr : 0);
                }
                var length = Math.Min(8, frame.Data.Length);
                message.Dlc = (uint)length << 28;
                for(var i = 0; i < length; i++)
                {
                    var value = (uint)frame.Data[i] << (24 - 8 * (i % 4));
                    if(i < 4) message.Dw1 |= value; else message.Dw2 |= value;
                }
                return message;
            }
        }

        private enum ControllerMode
        {
            Configuration,
            Normal,
            Loopback,
            Sleep,
            Snoop,
        }

        private readonly IMachine machine;
        private readonly ulong referenceClockFrequency;
        private readonly object sync = new object();
        private readonly Queue<Message> txFifo = new Queue<Message>();
        private readonly Queue<Message> rxFifo = new Queue<Message>();
        private readonly uint[] filterMasks = new uint[4];
        private readonly uint[] filterIds = new uint[4];

        private CanBus bus;
        private ulong generation;
        private bool enabled;
        private uint msr;
        private uint brpr;
        private uint btr;
        private int tec;
        private int rec;
        private uint esr;
        private uint isr;
        private uint ier;
        private uint wir;
        private uint afr;
        private Message txStaging;
        private Message hpbStaging;
        private Message? hpb;
        private bool transmitting;
        private bool busOff;
        private double timestampEpoch;

        private const int FifoDepth = 64;

        private const long SrrOffset = 0x00;
        private const long MsrOffset = 0x04;
        private const long BrprOffset = 0x08;
        private const long BtrOffset = 0x0C;
        private const long EcrOffset = 0x10;
        private const long EsrOffset = 0x14;
        private const long SrOffset = 0x18;
        private const long IsrOffset = 0x1C;
        private const long IerOffset = 0x20;
        private const long IcrOffset = 0x24;
        private const long TcrOffset = 0x28;
        private const long WirOffset = 0x2C;
        private const long TxIdOffset = 0x30;
        private const long TxDlcOffset = 0x34;
        private const long TxDw1Offset = 0x38;
        private const long TxDw2Offset = 0x3C;
        private const long TxHpbIdOffset = 0x40;
        private const long TxHpbDlcOffset = 0x44;
        private const long TxHpbDw1Offset = 0x48;
        private const long TxHpbDw2Offset = 0x4C;
        private const long RxIdOffset = 0x50;
        private const long RxDlcOffset = 0x54;
        private const long RxDw1Offset = 0x58;
        private const long RxDw2Offset = 0x5C;
        private const long AfrOffset = 0x60;
        private const long AfmrBase = 0x64;

        private const uint SrrSoftReset = 1u << 0;
        private const uint SrrEnable = 1u << 1;
        private const uint MsrSleep = 1u << 0;
        private const uint MsrLoopback = 1u << 1;
        private const uint MsrSnoop = 1u << 2;
        private const uint EsrAckError = 1u << 4;

        private const uint SrConfig = 1u << 0;
        private const uint SrLoopback = 1u << 1;
        private const uint SrSleep = 1u << 2;
        private const uint SrNormal = 1u << 3;
        private const uint SrBusIdle = 1u << 4;
        private const uint SrBusBusy = 1u << 5;
        private const uint SrErrorWarning = 1u << 6;
        private const uint SrTxHpbFull = 1u << 9;
        private const uint SrTxFifoFull = 1u << 10;
        private const uint SrSnoop = 1u << 12;

        private const uint IxrTxOk = 1u << 1;
        private const uint IxrTxFifoFull = 1u << 2;
        private const uint IxrTxHpbFull = 1u << 3;
        private const uint IxrRxOk = 1u << 4;
        private const uint IxrRxUnderflow = 1u << 5;
        private const uint IxrRxOverflow = 1u << 6;
        private const uint IxrRxNotEmpty = 1u << 7;
        private const uint IxrError = 1u << 8;
        private const uint IxrBusOff = 1u << 9;
        private const uint IxrSleep = 1u << 10;
        private const uint IxrWakeUp = 1u << 11;
        private const uint IxrRxWatermarkFull = 1u << 12;
        private const uint IxrTxWatermarkEmpty = 1u << 13;
        private const uint IxrTxFifoEmpty = 1u << 14;
        private const uint IxrAll = 0x7FFF;
        // ISR bits that writing CEN = 0 clears (B.5): WKUP, SLP, BSOFF, ERROR,
        // RXOFLW, RXOK, TXOK, ARBLST.
        private const uint IxrClearedByDisable = IxrWakeUp | IxrSleep | IxrBusOff | IxrError | IxrRxOverflow | IxrRxOk | IxrTxOk | 1u << 0;
        private const uint WirResetValue = 0x3F3F;

        private const uint IdrSrr = 1u << 20;
        private const uint IdrIde = 1u << 19;
        private const uint IdrRtr = 1u << 0;
    }
}
