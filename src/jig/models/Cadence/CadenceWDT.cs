//
// Cadence watchdog timer (Zynq-7000 system watchdog SWDT, 0xF8005000).
//
// References: UG585 v1.7 sections 8.4.2-8.4.3 and register reference B.31.
// Reset values are B.31's: MODE 0x1C0 (WDEN, RSTEN and IRQEN clear, so the
// watchdog starts disabled) and CONTROL 0x3FFC (CRV 0xFFF, CLKSEL 0).
//
// Behaviour modelled:
//  - MODE (ZMR) and CONTROL (CCR) accept writes only with their keys (0xABC in
//    ZMR[23:12], 0x248 in CCR[25:14]); the key fields read back as 0.
//  - RESTART with key 0x1999 loads the 24-bit counter with (CRV << 12) | 0xFFF
//    and restarts the prescaler; STATUS.WDZ clears.
//  - The counter decrements at clock / {8, 64, 512, 4096} (CLKSEL) whether or not
//    WDEN is set (8.4.3). At zero it holds, WDZ sets, and if WDEN is set: RSTEN
//    requests a system watchdog reset (REBOOT_STATUS[16]); IRQEN pulses the
//    interrupt.
//
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Jig.Xilinx;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.Cadence
{
    public sealed class CadenceWDT : IDoubleWordPeripheral, IKnownSize
    {
        public CadenceWDT(IMachine machine, ulong frequency, Zynq7000_SLCR resetController = null)
        {
            this.resetController = resetController;
            IRQ = new GPIO();
            // Renode's LimitTimer refuses a Value above the limit it was built with:
            // start at the largest restart value, (0xFFF << 12) | 0xFFF.
            counter = new LimitTimer(machine.ClockSource, frequency, this, "swdt",
                limit: 0xFFFFFF, direction: Direction.Descending, enabled: false, workMode: WorkMode.OneShot, eventEnabled: true);
            counter.LimitReached += OnZero;
            Reset();
        }

        public void Reset()
        {
            mode = ModeResetValue;
            control = ControlResetValue;
            zero = false;
            IRQ.Unset();
            Restart();
        }

        public long Size => 0x1000;

        public GPIO IRQ { get; }

        /// <summary>Clears WDEN, RSTEN and IRQEN. MODE resets with them clear; the
        /// BootROM model calls this so that the watchdog is off when it hands over.</summary>
        public void DisableOutputs()
        {
            mode &= ~(ModeWdEnable | ModeResetEnable | ModeIrqEnable);
        }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case ModeOffset: return mode;
            case ControlOffset: return control;
            case RestartOffset: return 0;
            case StatusOffset: return zero ? 1u : 0u;
            default:
                this.Log(LogLevel.Warning, "Read from unknown offset 0x{0:X2}", offset);
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case ModeOffset:
                if(((value >> 12) & 0xFFF) != ModeKey)
                {
                    this.Log(LogLevel.Warning, "MODE write 0x{0:X8} ignored: wrong key", value);
                    return;
                }
                mode = value & ModeWritableMask;
                break;
            case ControlOffset:
                if(((value >> 14) & 0xFFF) != ControlKey)
                {
                    this.Log(LogLevel.Warning, "CONTROL write 0x{0:X8} ignored: wrong key", value);
                    return;
                }
                control = value & ControlWritableMask;
                break;
            case RestartOffset:
                if(value == RestartKey)
                {
                    Restart();
                }
                else
                {
                    this.Log(LogLevel.Warning, "RESTART write 0x{0:X8} ignored: wrong key", value);
                }
                break;
            case StatusOffset:
                break;
            default:
                this.Log(LogLevel.Warning, "Write 0x{0:X8} to unknown offset 0x{1:X2}", value, offset);
                break;
            }
        }

        private void Restart()
        {
            var crv = (control >> 2) & 0xFFF;
            var start = ((ulong)crv << 12) | 0xFFF;
            counter.Enabled = false;
            counter.Divider = Prescale(control & 0x3);
            counter.Limit = start;
            counter.Value = start;
            counter.Enabled = true;
            zero = false;
        }

        private static uint Prescale(uint clockSelect)
        {
            switch(clockSelect)
            {
            case 0: return 8;
            case 1: return 64;
            case 2: return 512;
            default: return 4096;
            }
        }

        private void OnZero()
        {
            zero = true;
            counter.Enabled = false;
            if((mode & ModeWdEnable) == 0)
            {
                return;
            }
            if((mode & ModeIrqEnable) != 0)
            {
                IRQ.Blink();
            }
            if((mode & ModeResetEnable) != 0)
            {
                this.Log(LogLevel.Info, "System watchdog expired: system reset");
                if(resetController != null)
                {
                    resetController.RequestReset(ZynqResetCause.SystemWatchdog);
                }
                else
                {
                    this.Log(LogLevel.Error, "No reset controller connected; watchdog reset not performed");
                }
            }
        }

        private readonly Zynq7000_SLCR resetController;
        private readonly LimitTimer counter;
        private uint mode;
        private uint control;
        private bool zero;

        private const long ModeOffset = 0x00;
        private const long ControlOffset = 0x04;
        private const long RestartOffset = 0x08;
        private const long StatusOffset = 0x0C;
        private const uint ModeResetValue = 0x1C0;
        private const uint ControlResetValue = 0x3FFC;
        private const uint ModeKey = 0xABC;
        private const uint ControlKey = 0x248;
        private const uint RestartKey = 0x1999;
        private const uint ModeWdEnable = 1u << 0;
        private const uint ModeResetEnable = 1u << 1;
        private const uint ModeIrqEnable = 1u << 2;
        private const uint ModeWritableMask = 0x1F7; // WDEN, RSTEN, IRQEN, RSTLN[6:4], IRQLN[8:7]
        private const uint ControlWritableMask = 0x3FFF; // CLKSEL[1:0], CRV[13:2]
    }
}
