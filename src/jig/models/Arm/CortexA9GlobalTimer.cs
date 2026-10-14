//
// Cortex-A9 MPCore global timer (Zynq-7000: 0xF8F00200).
//
// References: ARM Cortex-A9 MPCore TRM (DDI 0407), section 4.4 "Global timer
// registers"; the Xilinx standalone BSP's globaltimer_sleep_zynq.c (usleep,
// sleep) drives it the same way.
//
// Renode's ARM_GlobalTimer returns CONTROL.Prescaler + 1 on a read, so every
// read-modify-write of CONTROL raises the prescaler by one and the timer slows
// down; this model replaces it.
//
// Behaviour modelled:
//  - A 64-bit up-counter at PERIPHCLK / (PRESCALER + 1) while CONTROL.ENABLE is
//    set (COUNTER_LO/HI, written only while the timer is disabled as the TRM
//    requires; a write while enabled is applied and reported).
//  - Banked per CPU (indexed by the accessing CPU's MPIDR.CPUID, CPU0 for
//    accesses from outside a CPU): CONTROL[3:1] (comparator enable, IRQ enable,
//    auto-increment), the comparator, the auto-increment value and the event
//    flag. CONTROL.ENABLE and the prescaler are shared.
//  - The event flag sets when the comparator is enabled and the counter
//    reaches the comparator, and at the moment the comparator, the counter or
//    CONTROL is written with the counter already at or past it. It is
//    write-one-to-clear and stays clear until the next of those. With
//    auto-increment the comparator advances by the increment at each event.
//  - Interrupt n (Connections[n], PPI 27 of CPU n) is high while that CPU's
//    event flag and IRQ enable are both set.
//
using System.Collections.Generic;
using System.Collections.ObjectModel;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.Arm
{
    public sealed class CortexA9GlobalTimer : IDoubleWordPeripheral, IKnownSize, INumberedGPIOOutput
    {
        public CortexA9GlobalTimer(IMachine machine, ulong frequency, int cpus = 1)
        {
            this.machine = machine;
            counter = new LimitTimer(machine.ClockSource, frequency, this, "counter",
                limit: ulong.MaxValue, direction: Direction.Ascending, enabled: false, workMode: WorkMode.Periodic);
            banks = new Bank[cpus];
            var connections = new Dictionary<int, IGPIO>();
            for(var i = 0; i < cpus; i++)
            {
                var index = i;
                var match = new LimitTimer(machine.ClockSource, frequency, this, $"comparator{i}",
                    limit: ulong.MaxValue, direction: Direction.Descending, enabled: false, workMode: WorkMode.OneShot, eventEnabled: true);
                match.LimitReached += () => OnMatch(index);
                banks[i] = new Bank(match);
                connections[i] = banks[i].Irq;
            }
            Connections = new ReadOnlyDictionary<int, IGPIO>(connections);
            Reset();
        }

        public void Reset()
        {
            lock(sync)
            {
                counter.Enabled = false;
                counter.Divider = 1;
                counter.Value = 0;
                prescaler = 0;
                foreach(var bank in banks)
                {
                    bank.Match.Enabled = false;
                    bank.Match.Divider = 1;
                    bank.Compare = 0;
                    bank.AutoIncrement = 0;
                    bank.ComparatorEnabled = false;
                    bank.IrqEnabled = false;
                    bank.AutoIncrementEnabled = false;
                    bank.Event = false;
                    bank.Irq.Unset();
                }
            }
        }

        public long Size => 0x100;

        public IReadOnlyDictionary<int, IGPIO> Connections { get; }

        public uint ReadDoubleWord(long offset)
        {
            lock(sync)
            {
                var bank = CurrentBank();
                switch((Registers)offset)
                {
                case Registers.CounterLow: return (uint)Count();
                case Registers.CounterHigh: return (uint)(Count() >> 32);
                case Registers.Control:
                    return (counter.Enabled ? ControlEnable : 0u)
                        | (bank.ComparatorEnabled ? ControlComparatorEnable : 0u)
                        | (bank.IrqEnabled ? ControlIrqEnable : 0u)
                        | (bank.AutoIncrementEnabled ? ControlAutoIncrement : 0u)
                        | (prescaler << 8);
                case Registers.InterruptStatus: return bank.Event ? 1u : 0u;
                case Registers.ComparatorLow: return (uint)bank.Compare;
                case Registers.ComparatorHigh: return (uint)(bank.Compare >> 32);
                case Registers.AutoIncrement: return bank.AutoIncrement;
                default:
                    this.Log(LogLevel.Warning, "Read from unknown offset 0x{0:X2}", offset);
                    return 0;
                }
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            lock(sync)
            {
                var bank = CurrentBank();
                switch((Registers)offset)
                {
                case Registers.CounterLow:
                    WriteCounter((Count() & 0xFFFFFFFF00000000UL) | value);
                    break;
                case Registers.CounterHigh:
                    WriteCounter((Count() & 0xFFFFFFFFUL) | ((ulong)value << 32));
                    break;
                case Registers.Control:
                {
                    var newPrescaler = (value >> 8) & 0xFF;
                    var enable = (value & ControlEnable) != 0;
                    var now = Count();
                    if(newPrescaler != prescaler)
                    {
                        prescaler = newPrescaler;
                        counter.Divider = prescaler + 1;
                    }
                    counter.Value = now;
                    counter.Enabled = enable;
                    bank.ComparatorEnabled = (value & ControlComparatorEnable) != 0;
                    bank.IrqEnabled = (value & ControlIrqEnable) != 0;
                    bank.AutoIncrementEnabled = (value & ControlAutoIncrement) != 0;
                    RearmAll();
                    break;
                }
                case Registers.InterruptStatus:
                    // Clearing the sticky flag does not compare again: a counter
                    // already past the comparator sets it only when the
                    // comparator, the counter or CONTROL is written.
                    if((value & 1) != 0)
                    {
                        bank.Event = false;
                        bank.Irq.Set(false);
                    }
                    break;
                case Registers.ComparatorLow:
                    bank.Compare = (bank.Compare & 0xFFFFFFFF00000000UL) | value;
                    Rearm(bank);
                    break;
                case Registers.ComparatorHigh:
                    bank.Compare = (bank.Compare & 0xFFFFFFFFUL) | ((ulong)value << 32);
                    Rearm(bank);
                    break;
                case Registers.AutoIncrement:
                    bank.AutoIncrement = value;
                    break;
                default:
                    this.Log(LogLevel.Warning, "Write 0x{0:X8} to unknown offset 0x{1:X2}", value, offset);
                    break;
                }
            }
        }

        private ulong Count()
        {
            if(machine.SystemBus.TryGetCurrentCPU(out var cpu))
            {
                cpu.SyncTime();
            }
            return counter.Value;
        }

        private void WriteCounter(ulong value)
        {
            if(counter.Enabled)
            {
                this.Log(LogLevel.Warning, "Counter written while the timer is enabled (the TRM requires it disabled)");
            }
            counter.Value = value;
            RearmAll();
        }

        private Bank CurrentBank()
        {
            if(machine.SystemBus.TryGetCurrentCPU(out var cpu) && cpu.MultiprocessingId < banks.Length)
            {
                return banks[cpu.MultiprocessingId];
            }
            return banks[0];
        }

        private void RearmAll()
        {
            foreach(var bank in banks)
            {
                Rearm(bank);
            }
        }

        // Sets the event if the comparator already matches, otherwise schedules
        // the match for the tick the counter reaches the comparator.
        private void Rearm(Bank bank)
        {
            bank.Match.Enabled = false;
            if(bank.ComparatorEnabled)
            {
                var now = Count();
                if(now >= bank.Compare)
                {
                    bank.Event = true;
                }
                else if(counter.Enabled)
                {
                    var ticks = bank.Compare - now;
                    bank.Match.Divider = prescaler + 1;
                    bank.Match.Limit = ticks;
                    bank.Match.Value = ticks;
                    bank.Match.Enabled = true;
                }
            }
            bank.Irq.Set(bank.Event && bank.IrqEnabled);
        }

        private void OnMatch(int index)
        {
            lock(sync)
            {
                var bank = banks[index];
                if(!bank.ComparatorEnabled)
                {
                    return;
                }
                bank.Event = true;
                if(bank.AutoIncrementEnabled)
                {
                    bank.Compare += bank.AutoIncrement;
                }
                Rearm(bank);
            }
        }

        private readonly IMachine machine;
        private readonly object sync = new object();
        private readonly LimitTimer counter;
        private readonly Bank[] banks;
        private uint prescaler;

        private const uint ControlEnable = 1u << 0;
        private const uint ControlComparatorEnable = 1u << 1;
        private const uint ControlIrqEnable = 1u << 2;
        private const uint ControlAutoIncrement = 1u << 3;

        private sealed class Bank
        {
            public Bank(LimitTimer match)
            {
                Match = match;
            }

            public readonly LimitTimer Match;
            public readonly GPIO Irq = new GPIO();
            public ulong Compare;
            public uint AutoIncrement;
            public bool ComparatorEnabled;
            public bool IrqEnabled;
            public bool AutoIncrementEnabled;
            public bool Event;
        }

        private enum Registers : long
        {
            CounterLow = 0x00,
            CounterHigh = 0x04,
            Control = 0x08,
            InterruptStatus = 0x0C,
            ComparatorLow = 0x10,
            ComparatorHigh = 0x14,
            AutoIncrement = 0x18,
        }
    }
}
