//
// Low-power timer (LPTIM) of the STM32U5 series, written from ST RM0456
// (chapter "Low-power timer (LPTIM)") and ST's CMSIS device headers
// stm32u575xx.h and stm32u595xx.h.
//
// The counter runs from the kernel clock given in the platform file, divided by
// CFGR.PRESC, and counts 0..ARR. Events happen when the counter moves into a
// count:
//  - CC1IF when CNT becomes CCR1. CCR1 = 0 matches when the counter wraps from
//    ARR to 0, so a compare value of 0 fires once per period like any other.
//  - ARRM when CNT becomes ARR.
//  - At each overflow (ARR to 0) the repetition counter is checked: UE is set
//    when it is 0, and it is reloaded from RCR; otherwise it is decremented.
//    With RCR = 0 every overflow is an update event.
// Writes to DIER, CCR1, CCR2, ARR and RCR take effect at once and set DIEROK,
// CMP1OK, CMP2OK, ARROK and REPOK at once (silicon synchronises them to the
// kernel clock over a few cycles). The interrupt line is the OR of every ISR
// flag that has its DIER enable bit set; DIEROK has no enable bit.
//
// The model keeps one clock entry and advances it from event to event, so each
// flag is raised exactly when the counter reaches its count. A CNT read first
// brings the CPU's elapsed time into the clock (cpu.SyncTime), as Renode's NVIC
// does for SysTick, so the value read is the count at the instruction that
// reads it.
//
// Not modelled: external clock and encoder modes (CKSEL, COUNTMODE, ENC),
// triggers (TRIGEN, EXTTRIG), input capture (CC1SEL/CC2SEL), waveform outputs,
// PRELOAD (registers always update at once), the UP/DOWN flags, DMA requests,
// and RSTARE. Setting ENABLE to 0 stops the counter and clears CNT and the
// repetition counter; ISR flags and the other registers keep their values.
//
using System;

using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure.Registers;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.ST
{
    [AllowedTranslations(AllowedTranslation.ByteToDoubleWord | AllowedTranslation.WordToDoubleWord)]
    public class STM32U5_LPTIM : BasicDoubleWordPeripheral, IKnownSize
    {
        public STM32U5_LPTIM(IMachine machine, ulong frequency) : base(machine)
        {
            if(frequency == 0)
            {
                throw new ArgumentException("Kernel clock frequency must be greater than 0");
            }
            kernelFrequency = frequency;
            IRQ = new GPIO();
            // The entry's limit is the distance to the next event, at most one
            // counter period (65536 counts); construct with a larger limit so the
            // LimitTimer's checks against its initial limit never trip.
            counter = new LimitTimer(machine.ClockSource, frequency, this, "counter", limit: InitialLimit,
                direction: Direction.Ascending, enabled: false, workMode: WorkMode.Periodic, eventEnabled: true);
            counter.LimitReached += OnCounterEvent;
            DefineRegisters();
            Reset();
        }

        public override void Reset()
        {
            base.Reset();
            counter.Reset();
            counter.Frequency = kernelFrequency;
            running = false;
            continuous = false;
            segmentStart = 0;
            segmentLength = InitialLimit;
            stoppedCount = 0;
            repetitionCounter = 0;
            UpdateInterrupts();
        }

        public long Size => 0x400;

        public GPIO IRQ { get; }

        // Kernel clock of the counter before the prescaler, in Hz.
        public ulong Frequency
        {
            get => kernelFrequency;
            set
            {
                if(value == 0)
                {
                    throw new ArgumentException("Kernel clock frequency must be greater than 0");
                }
                var count = CurrentCount();
                kernelFrequency = value;
                counter.Frequency = value;
                if(running)
                {
                    RestartSegmentAt(count);
                }
            }
        }

        // Counter value as software would read it.
        public uint Counter => (uint)CurrentCount();

        private void DefineRegisters()
        {
            Registers.InterruptAndStatus.Define(this)
                .WithFlag(0, out compare1Match, FieldMode.Read, name: "CC1IF")
                .WithFlag(1, out autoReloadMatch, FieldMode.Read, name: "ARRM")
                .WithFlag(2, out externalTrigger, FieldMode.Read, name: "EXTTRIG")
                .WithFlag(3, out compare1UpdateOk, FieldMode.Read, name: "CMP1OK")
                .WithFlag(4, out autoReloadUpdateOk, FieldMode.Read, name: "ARROK")
                .WithFlag(5, out counterUp, FieldMode.Read, name: "UP")
                .WithFlag(6, out counterDown, FieldMode.Read, name: "DOWN")
                .WithFlag(7, out updateEvent, FieldMode.Read, name: "UE")
                .WithFlag(8, out repetitionUpdateOk, FieldMode.Read, name: "REPOK")
                .WithFlag(9, out compare2Match, FieldMode.Read, name: "CC2IF")
                .WithReservedBits(10, 2)
                .WithFlag(12, out capture1Overcapture, FieldMode.Read, name: "CC1OF")
                .WithFlag(13, out capture2Overcapture, FieldMode.Read, name: "CC2OF")
                .WithReservedBits(14, 5)
                .WithFlag(19, out compare2UpdateOk, FieldMode.Read, name: "CMP2OK")
                .WithReservedBits(20, 4)
                .WithFlag(24, out interruptEnableUpdateOk, FieldMode.Read, name: "DIEROK")
                .WithReservedBits(25, 7);

            Registers.InterruptClear.Define(this)
                .WithFlag(0, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, compare1Match), name: "CC1CF")
                .WithFlag(1, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, autoReloadMatch), name: "ARRMCF")
                .WithFlag(2, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, externalTrigger), name: "EXTTRIGCF")
                .WithFlag(3, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, compare1UpdateOk), name: "CMP1OKCF")
                .WithFlag(4, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, autoReloadUpdateOk), name: "ARROKCF")
                .WithFlag(5, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, counterUp), name: "UPCF")
                .WithFlag(6, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, counterDown), name: "DOWNCF")
                .WithFlag(7, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, updateEvent), name: "UECF")
                .WithFlag(8, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, repetitionUpdateOk), name: "REPOKCF")
                .WithFlag(9, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, compare2Match), name: "CC2CF")
                .WithReservedBits(10, 2)
                .WithFlag(12, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, capture1Overcapture), name: "CC1OCF")
                .WithFlag(13, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, capture2Overcapture), name: "CC2OCF")
                .WithReservedBits(14, 5)
                .WithFlag(19, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, compare2UpdateOk), name: "CMP2OKCF")
                .WithReservedBits(20, 4)
                .WithFlag(24, FieldMode.WriteOneToClear, writeCallback: (_, v) => Clear(v, interruptEnableUpdateOk), name: "DIEROKCF")
                .WithReservedBits(25, 7)
                .WithWriteCallback((_, __) => UpdateInterrupts());

            Registers.InterruptEnable.Define(this)
                .WithFlag(0, out compare1MatchEnable, name: "CC1IE")
                .WithFlag(1, out autoReloadMatchEnable, name: "ARRMIE")
                .WithFlag(2, out externalTriggerEnable, name: "EXTTRIGIE")
                .WithFlag(3, out compare1UpdateOkEnable, name: "CMP1OKIE")
                .WithFlag(4, out autoReloadUpdateOkEnable, name: "ARROKIE")
                .WithFlag(5, out counterUpEnable, name: "UPIE")
                .WithFlag(6, out counterDownEnable, name: "DOWNIE")
                .WithFlag(7, out updateEventEnable, name: "UEIE")
                .WithFlag(8, out repetitionUpdateOkEnable, name: "REPOKIE")
                .WithFlag(9, out compare2MatchEnable, name: "CC2IE")
                .WithReservedBits(10, 2)
                .WithFlag(12, out capture1OvercaptureEnable, name: "CC1OIE")
                .WithFlag(13, out capture2OvercaptureEnable, name: "CC2OIE")
                .WithReservedBits(14, 2)
                .WithFlag(16, name: "CC1DE")
                .WithReservedBits(17, 2)
                .WithFlag(19, out compare2UpdateOkEnable, name: "CMP2OKIE")
                .WithReservedBits(20, 3)
                .WithFlag(23, name: "UEDE")
                .WithReservedBits(24, 1)
                .WithFlag(25, name: "CC2DE")
                .WithReservedBits(26, 6)
                .WithWriteCallback((_, __) =>
                {
                    interruptEnableUpdateOk.Value = true;
                    UpdateInterrupts();
                });

            Registers.Configuration.Define(this)
                .WithFlag(0, name: "CKSEL")
                .WithValueField(1, 2, name: "CKPOL")
                .WithValueField(3, 2, name: "CKFLT")
                .WithReservedBits(5, 1)
                .WithValueField(6, 2, name: "TRGFLT")
                .WithReservedBits(8, 1)
                .WithValueField(9, 3, out prescaler, name: "PRESC")
                .WithReservedBits(12, 1)
                .WithValueField(13, 3, name: "TRIGSEL")
                .WithReservedBits(16, 1)
                .WithValueField(17, 2, name: "TRIGEN")
                .WithFlag(19, name: "TIMOUT")
                .WithFlag(20, name: "WAVE")
                .WithFlag(21, name: "WAVPOL")
                .WithFlag(22, name: "PRELOAD")
                .WithFlag(23, name: "COUNTMODE")
                .WithFlag(24, name: "ENC")
                .WithReservedBits(25, 7)
                .WithWriteCallback((_, __) => UpdatePrescaler());

            Registers.Control.Define(this)
                .WithFlag(0, out enable, name: "ENABLE")
                .WithFlag(1, FieldMode.Read | FieldMode.Write, valueProviderCallback: _ => false, name: "SNGSTRT")
                .WithFlag(2, FieldMode.Read | FieldMode.Write, valueProviderCallback: _ => false, name: "CNTSTRT")
                .WithFlag(3, FieldMode.Read | FieldMode.Write, valueProviderCallback: _ => false, name: "COUNTRST")
                .WithFlag(4, name: "RSTARE")
                .WithReservedBits(5, 27)
                .WithWriteCallback((_, value) => OnControlWrite((uint)value));

            Registers.CaptureCompare1.Define(this)
                .WithValueField(0, 16, out compare1Value, writeCallback: (_, __) =>
                {
                    compare1UpdateOk.Value = true;
                    Replan();
                    UpdateInterrupts();
                }, name: "CCR1")
                .WithReservedBits(16, 16);

            Registers.AutoReload.Define(this, 0x1)
                .WithValueField(0, 16, out autoReloadValue, writeCallback: (_, __) =>
                {
                    autoReloadUpdateOk.Value = true;
                    Replan();
                    UpdateInterrupts();
                }, name: "ARR")
                .WithReservedBits(16, 16);

            Registers.Counter.Define(this)
                .WithValueField(0, 16, FieldMode.Read, valueProviderCallback: _ => CurrentCount(), name: "CNT")
                .WithReservedBits(16, 16);

            Registers.Configuration2.Define(this)
                .WithValueField(0, 32, name: "CFGR2");

            Registers.Repetition.Define(this)
                .WithValueField(0, 8, out repetitionValue, writeCallback: (_, value) =>
                {
                    repetitionCounter = (uint)value;
                    repetitionUpdateOk.Value = true;
                    UpdateInterrupts();
                }, name: "REP")
                .WithReservedBits(8, 24);

            Registers.CaptureCompareMode1.Define(this)
                .WithValueField(0, 32, name: "CCMR1");

            Registers.CaptureCompare2.Define(this)
                .WithValueField(0, 16, writeCallback: (_, __) =>
                {
                    compare2UpdateOk.Value = true;
                    UpdateInterrupts();
                }, name: "CCR2")
                .WithReservedBits(16, 16);
        }

        private void Clear(bool written, IFlagRegisterField flag)
        {
            if(written)
            {
                flag.Value = false;
            }
        }

        private void OnControlWrite(uint value)
        {
            if(!enable.Value)
            {
                if(running || stoppedCount != 0)
                {
                    this.Log(LogLevel.Debug, "Disabled, counter stopped and cleared");
                }
                StopCounter(0);
                repetitionCounter = (uint)repetitionValue.Value;
                return;
            }

            var singleStart = (value & SingleStartBit) != 0;
            var continuousStart = (value & ContinuousStartBit) != 0;
            var counterReset = (value & CounterResetBit) != 0;

            if(counterReset)
            {
                if(running)
                {
                    RestartSegmentAt(0);
                }
                else
                {
                    stoppedCount = 0;
                }
            }

            if(singleStart && continuousStart)
            {
                this.Log(LogLevel.Warning, "SNGSTRT and CNTSTRT written together, ignoring both");
                return;
            }
            if(singleStart || continuousStart)
            {
                continuous = continuousStart;
                if(!running)
                {
                    running = true;
                    counter.Enabled = true;
                    RestartSegmentAt(stoppedCount);
                    this.Log(LogLevel.Debug, "Counter started in {0} mode at {1}", continuous ? "continuous" : "single", stoppedCount);
                }
            }
        }

        private void UpdatePrescaler()
        {
            var divider = 1UL << (int)prescaler.Value;
            if(counter.Divider == divider)
            {
                return;
            }
            var count = CurrentCount();
            counter.Divider = divider;
            if(running)
            {
                RestartSegmentAt(count);
            }
        }

        private ulong Period => autoReloadValue.Value + 1;

        // Counter value now. On the CPU thread the CPU's executed instructions are
        // brought into the clock first, so events due by now have been raised.
        private ulong CurrentCount()
        {
            if(!running)
            {
                return stoppedCount;
            }
            if(machine.SystemBus.TryGetCurrentCPU(out var cpu))
            {
                cpu.SyncTime();
            }
            var value = counter.Value;
            if(counter.Limit != segmentLength)
            {
                // A limit set from OnCounterEvent can be overwritten when the clock
                // source runs the handler from inside a value read; restore it.
                counter.Limit = segmentLength;
            }
            return segmentStart + value;
        }

        private void StopCounter(ulong count)
        {
            running = false;
            counter.Enabled = false;
            stoppedCount = count;
            segmentStart = 0;
        }

        // Start a new segment of the clock entry at the given count.
        private void RestartSegmentAt(ulong count)
        {
            segmentStart = count;
            counter.ResetValue();
            segmentLength = NextEventDistance(count);
            counter.Limit = segmentLength;
        }

        // A register that moves an event changed: keep the time already counted in
        // this segment and move the segment's end to the next event.
        private void Replan()
        {
            if(!running)
            {
                return;
            }
            var current = CurrentCount();
            var nextEvent = current + NextEventDistance(current);
            segmentLength = nextEvent - segmentStart;
            counter.Limit = segmentLength;
        }

        // Counts from position `count` to the next event (at least 1).
        private ulong NextEventDistance(ulong count)
        {
            var period = Period;
            // The overflow happens when the counter leaves ARR. If software moved
            // ARR below the current count, the counter wraps at its next count.
            var overflowAt = Math.Max(period, count + 1);
            var next = overflowAt;

            var arr = autoReloadValue.Value;
            if(arr > count && arr < next)
            {
                next = arr;
            }

            var ccr1 = compare1Value.Value;
            if(ccr1 != 0 && ccr1 <= arr && ccr1 > count && ccr1 < next)
            {
                next = ccr1;
            }
            return next - count;
        }

        private void OnCounterEvent()
        {
            var position = segmentStart + segmentLength;
            var period = Period;
            var overflow = position >= period;
            ulong count;

            if(overflow)
            {
                count = 0;
                if(compare1Value.Value == 0)
                {
                    compare1Match.Value = true;
                }
                if(repetitionCounter == 0)
                {
                    updateEvent.Value = true;
                    repetitionCounter = (uint)repetitionValue.Value;
                }
                else
                {
                    repetitionCounter--;
                }
            }
            else
            {
                count = position;
                if(count == compare1Value.Value)
                {
                    compare1Match.Value = true;
                }
                if(count == autoReloadValue.Value)
                {
                    autoReloadMatch.Value = true;
                }
            }

            if(overflow && !continuous)
            {
                StopCounter(0);
            }
            else
            {
                // The clock entry restarts from 0 on reaching its limit
                segmentStart = count;
                segmentLength = NextEventDistance(count);
                counter.Limit = segmentLength;
            }
            UpdateInterrupts();
        }

        private void UpdateInterrupts()
        {
            var pending = (compare1Match.Value && compare1MatchEnable.Value)
                || (autoReloadMatch.Value && autoReloadMatchEnable.Value)
                || (externalTrigger.Value && externalTriggerEnable.Value)
                || (compare1UpdateOk.Value && compare1UpdateOkEnable.Value)
                || (autoReloadUpdateOk.Value && autoReloadUpdateOkEnable.Value)
                || (counterUp.Value && counterUpEnable.Value)
                || (counterDown.Value && counterDownEnable.Value)
                || (updateEvent.Value && updateEventEnable.Value)
                || (repetitionUpdateOk.Value && repetitionUpdateOkEnable.Value)
                || (compare2Match.Value && compare2MatchEnable.Value)
                || (capture1Overcapture.Value && capture1OvercaptureEnable.Value)
                || (capture2Overcapture.Value && capture2OvercaptureEnable.Value)
                || (compare2UpdateOk.Value && compare2UpdateOkEnable.Value);
            IRQ.Set(pending);
        }

        private ulong kernelFrequency;
        private bool running;
        private bool continuous;
        private ulong segmentStart;
        private ulong segmentLength;
        private ulong stoppedCount;
        private uint repetitionCounter;

        private IFlagRegisterField compare1Match;
        private IFlagRegisterField autoReloadMatch;
        private IFlagRegisterField externalTrigger;
        private IFlagRegisterField compare1UpdateOk;
        private IFlagRegisterField autoReloadUpdateOk;
        private IFlagRegisterField counterUp;
        private IFlagRegisterField counterDown;
        private IFlagRegisterField updateEvent;
        private IFlagRegisterField repetitionUpdateOk;
        private IFlagRegisterField compare2Match;
        private IFlagRegisterField capture1Overcapture;
        private IFlagRegisterField capture2Overcapture;
        private IFlagRegisterField compare2UpdateOk;
        private IFlagRegisterField interruptEnableUpdateOk;

        private IFlagRegisterField compare1MatchEnable;
        private IFlagRegisterField autoReloadMatchEnable;
        private IFlagRegisterField externalTriggerEnable;
        private IFlagRegisterField compare1UpdateOkEnable;
        private IFlagRegisterField autoReloadUpdateOkEnable;
        private IFlagRegisterField counterUpEnable;
        private IFlagRegisterField counterDownEnable;
        private IFlagRegisterField updateEventEnable;
        private IFlagRegisterField repetitionUpdateOkEnable;
        private IFlagRegisterField compare2MatchEnable;
        private IFlagRegisterField capture1OvercaptureEnable;
        private IFlagRegisterField capture2OvercaptureEnable;
        private IFlagRegisterField compare2UpdateOkEnable;

        private IFlagRegisterField enable;
        private IValueRegisterField prescaler;
        private IValueRegisterField compare1Value;
        private IValueRegisterField autoReloadValue;
        private IValueRegisterField repetitionValue;

        private readonly LimitTimer counter;

        private const ulong InitialLimit = 0x20000;
        private const uint SingleStartBit = 1u << 1;
        private const uint ContinuousStartBit = 1u << 2;
        private const uint CounterResetBit = 1u << 3;

        private enum Registers
        {
            InterruptAndStatus = 0x00,  // LPTIM_ISR
            InterruptClear = 0x04,      // LPTIM_ICR
            InterruptEnable = 0x08,     // LPTIM_DIER
            Configuration = 0x0C,       // LPTIM_CFGR
            Control = 0x10,             // LPTIM_CR
            CaptureCompare1 = 0x14,     // LPTIM_CCR1
            AutoReload = 0x18,          // LPTIM_ARR
            Counter = 0x1C,             // LPTIM_CNT
            Configuration2 = 0x24,      // LPTIM_CFGR2
            Repetition = 0x28,          // LPTIM_RCR
            CaptureCompareMode1 = 0x2C, // LPTIM_CCMR1
            CaptureCompare2 = 0x34,     // LPTIM_CCR2
        }
    }
}
