//
// Cortex-A9 MPCore private timer and private watchdog for one CPU
// (Zynq-7000: 0xF8F00600, timer at +0x00, watchdog at +0x20).
//
// References: ARM Cortex-A9 MPCore TRM (DDI 0407), sections 4.1-4.3 "Private
// timer and watchdog registers"; names follow the Xilinx headers xscutimer_hw.h
// and xscuwdt_hw.h. On Zynq-7000 both count PERIPHCLK (CPU_3x2x), the timer
// interrupt is PPI 29 and the watchdog interrupt is PPI 30 (UG585 table 7-3).
//
// Behaviour modelled:
//  - Timer: 32-bit down-counter at PERIPHCLK/(PRESCALER+1). Reaching zero sets
//    the event flag and, with IRQ enable, asserts the timer interrupt; with
//    auto-reload it reloads LOAD, else it stops at zero. Writing LOAD also
//    loads the counter. The event flag is write-one-to-clear. Every register
//    reads 0 after reset (UG585 v1.7 B.24).
//  - Watchdog in timer mode: as the timer, with its own interrupt.
//  - Watchdog mode (WD_MODE=1): reaching zero sets RST_STS.RESET_FLAG and asks
//    the reset controller for a CPU0-watchdog reset. WD_MODE can only be left
//    through the disable sequence 0x12345678 then 0x87654321 written to the
//    DISABLE register. Writing LOAD restarts the count (the kick).
//  - RST_STS survives the reset it caused; only a power-on reset clears it
//    (the reset controller reports one through ResetRequested).
//
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Jig.Xilinx;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.Arm
{
    public sealed class CortexA9PrivateTimerWatchdog : IDoubleWordPeripheral, IKnownSize
    {
        public CortexA9PrivateTimerWatchdog(IMachine machine, ulong frequency, Zynq7000_SLCR resetController = null)
        {
            this.resetController = resetController;
            if(resetController != null)
            {
                resetController.ResetRequested += cause =>
                {
                    if(cause == ZynqResetCause.PowerOn)
                    {
                        watchdogResetFlag = false;
                    }
                };
            }
            TimerIRQ = new GPIO();
            WatchdogIRQ = new GPIO();
            // Renode's LimitTimer refuses a Value above the limit it was built with,
            // so both start at the largest reload (LOAD + 1 = 2^32).
            timer = new LimitTimer(machine.ClockSource, frequency, this, "timer",
                limit: MaximumReload, direction: Direction.Descending, enabled: false, workMode: WorkMode.OneShot, eventEnabled: true);
            timer.LimitReached += OnTimerZero;
            watchdog = new LimitTimer(machine.ClockSource, frequency, this, "watchdog",
                limit: MaximumReload, direction: Direction.Descending, enabled: false, workMode: WorkMode.OneShot, eventEnabled: true);
            watchdog.LimitReached += OnWatchdogZero;
            Reset();
        }

        public void Reset()
        {
            timer.Reset();
            watchdog.Reset();
            // Both counters read 0 after reset (UG585 v1.7 B.24).
            timer.Value = 0;
            watchdog.Value = 0;
            timerLoad = 0;
            timerControl = 0;
            timerEvent = false;
            watchdogLoad = 0;
            watchdogControl = 0;
            watchdogEvent = false;
            disableSequence = 0;
            // watchdogResetFlag is deliberately preserved: see PowerOnReset.
            Update();
        }

        /// <summary>Power-on reset: also clears the watchdog reset flag.</summary>
        public void PowerOnReset()
        {
            watchdogResetFlag = false;
            Reset();
        }

        public long Size => 0x40;

        public GPIO TimerIRQ { get; }

        public GPIO WatchdogIRQ { get; }

        public uint ReadDoubleWord(long offset)
        {
            switch((Registers)offset)
            {
            case Registers.TimerLoad: return timerLoad;
            case Registers.TimerCounter: return CounterValue(timer);
            case Registers.TimerControl: return timerControl;
            case Registers.TimerInterruptStatus: return timerEvent ? 1u : 0u;
            case Registers.WatchdogLoad: return watchdogLoad;
            case Registers.WatchdogCounter: return CounterValue(watchdog);
            case Registers.WatchdogControl: return watchdogControl;
            case Registers.WatchdogInterruptStatus: return watchdogEvent ? 1u : 0u;
            case Registers.WatchdogResetStatus: return watchdogResetFlag ? 1u : 0u;
            case Registers.WatchdogDisable: return 0;
            default:
                this.Log(LogLevel.Warning, "Read from unknown offset 0x{0:X2}", offset);
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch((Registers)offset)
            {
            case Registers.TimerLoad:
                timerLoad = value;
                LoadCounter(timer, value);
                break;
            case Registers.TimerCounter:
                LoadCounter(timer, value);
                break;
            case Registers.TimerControl:
                timerControl = value & (ControlEnable | ControlAutoReload | ControlIrqEnable | ControlPrescalerMask);
                ApplyControl(timer, timerControl, autoReloadAllowed: true);
                break;
            case Registers.TimerInterruptStatus:
                if((value & 1) != 0) timerEvent = false;
                break;
            case Registers.WatchdogLoad:
                watchdogLoad = value;
                LoadCounter(watchdog, value);
                break;
            case Registers.WatchdogCounter:
                LoadCounter(watchdog, value);
                break;
            case Registers.WatchdogControl:
            {
                var wdMode = (watchdogControl & ControlWatchdogMode) != 0 || (value & ControlWatchdogMode) != 0;
                watchdogControl = (value & (ControlEnable | ControlAutoReload | ControlIrqEnable | ControlPrescalerMask))
                                  | (wdMode ? ControlWatchdogMode : 0u);
                ApplyControl(watchdog, watchdogControl, autoReloadAllowed: !wdMode);
                break;
            }
            case Registers.WatchdogInterruptStatus:
                if((value & 1) != 0) watchdogEvent = false;
                break;
            case Registers.WatchdogResetStatus:
                if((value & 1) != 0) watchdogResetFlag = false;
                break;
            case Registers.WatchdogDisable:
                if(value == DisableKey1)
                {
                    disableSequence = 1;
                }
                else if(value == DisableKey2 && disableSequence == 1)
                {
                    disableSequence = 0;
                    watchdogControl &= ~ControlWatchdogMode;
                    ApplyControl(watchdog, watchdogControl, autoReloadAllowed: true);
                }
                else
                {
                    disableSequence = 0;
                }
                break;
            default:
                this.Log(LogLevel.Warning, "Write 0x{0:X8} to unknown offset 0x{1:X2}", value, offset);
                break;
            }
            Update();
        }

        private static uint CounterValue(LimitTimer counter)
        {
            // LimitTimer counts Limit..1 and fires on reaching 0; the ARM counter
            // runs LOAD..0, so the visible value is one less.
            var value = counter.Value;
            return value == 0 ? 0u : (uint)(value - 1);
        }

        private static void LoadCounter(LimitTimer counter, uint value)
        {
            counter.Limit = (ulong)value + 1;
            counter.Value = (ulong)value + 1;
        }

        private static void ApplyControl(LimitTimer counter, uint control, bool autoReloadAllowed)
        {
            counter.Divider = (uint)(((control & ControlPrescalerMask) >> 8) + 1);
            counter.Mode = autoReloadAllowed && (control & ControlAutoReload) != 0 ? WorkMode.Periodic : WorkMode.OneShot;
            counter.Enabled = (control & ControlEnable) != 0;
        }

        private void OnTimerZero()
        {
            timerEvent = true;
            if((timerControl & ControlAutoReload) != 0)
            {
                LoadCounter(timer, timerLoad);
                timer.Enabled = true;
            }
            else
            {
                timer.Value = 0; // single shot: the counter stays at zero
            }
            Update();
        }

        private void OnWatchdogZero()
        {
            if((watchdogControl & ControlWatchdogMode) != 0)
            {
                watchdogResetFlag = true;
                this.Log(LogLevel.Info, "Private watchdog expired in watchdog mode: CPU0 watchdog reset");
                if(resetController != null)
                {
                    resetController.RequestReset(ZynqResetCause.Cpu0Watchdog);
                }
                else
                {
                    this.Log(LogLevel.Error, "No reset controller connected; watchdog reset not performed");
                }
                return;
            }
            watchdogEvent = true;
            if((watchdogControl & ControlAutoReload) != 0)
            {
                LoadCounter(watchdog, watchdogLoad);
                watchdog.Enabled = true;
            }
            else
            {
                watchdog.Value = 0;
            }
            Update();
        }

        private void Update()
        {
            TimerIRQ.Set(timerEvent && (timerControl & ControlIrqEnable) != 0);
            var timerModeWatchdog = (watchdogControl & ControlWatchdogMode) == 0;
            WatchdogIRQ.Set(timerModeWatchdog && watchdogEvent && (watchdogControl & ControlIrqEnable) != 0);
        }

        private readonly Zynq7000_SLCR resetController;
        private readonly LimitTimer timer;
        private readonly LimitTimer watchdog;

        private uint timerLoad;
        private uint timerControl;
        private bool timerEvent;
        private uint watchdogLoad;
        private uint watchdogControl;
        private bool watchdogEvent;
        private bool watchdogResetFlag;
        private int disableSequence;

        private const uint ControlEnable = 1u << 0;
        private const uint ControlAutoReload = 1u << 1;
        private const uint ControlIrqEnable = 1u << 2;
        private const uint ControlWatchdogMode = 1u << 3;
        private const uint ControlPrescalerMask = 0xFF00;
        private const uint DisableKey1 = 0x12345678;
        private const uint DisableKey2 = 0x87654321;
        private const ulong MaximumReload = 0x100000000UL;

        private enum Registers : long
        {
            TimerLoad = 0x00,
            TimerCounter = 0x04,
            TimerControl = 0x08,
            TimerInterruptStatus = 0x0C,
            WatchdogLoad = 0x20,
            WatchdogCounter = 0x24,
            WatchdogControl = 0x28,
            WatchdogInterruptStatus = 0x2C,
            WatchdogResetStatus = 0x30,
            WatchdogDisable = 0x34,
        }
    }
}
