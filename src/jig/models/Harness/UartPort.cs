//
// Harness-side UART endpoint.
//
// A UartPort attaches to one UART like a terminal would. Every character the
// UART transmits is recorded with its virtual-time stamp; the harness collects
// them with Read (or ReadTimed) between simulation steps and sends characters
// into the UART's receive line with Write. Because the harness only calls these
// while the emulation is paused, data crosses at deterministic virtual times.
//
//   emulation CreateUartPort "cli"
//   connector Connect sysbus.uart0 cli
//   cli Write "7379732076657273696F6E0D"
//   cli Read
//
// As an IHarnessPort it also reports its data after every harness step:
//   U <name> <stamps> <hex>
// where <stamps> is the first character's virtual time in microseconds followed
// by the gap to each next character, comma-separated ("1200,20,20").
//
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Peripherals.UART;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.Harness
{
    public static class UartPortExtensions
    {
        public static void CreateUartPort(this Emulation emulation, string name)
        {
            emulation.ExternalsManager.AddExternal(new UartPort(), name);
        }
    }

    public sealed class UartPort : IHarnessPort, IConnectable<IUART>
    {
        public void AttachTo(IUART uart)
        {
            lock(sync)
            {
                if(this.uart != null)
                {
                    throw new RecoverableException("This port is already connected to a UART");
                }
                this.uart = uart;
                machine = uart.GetMachine();
                lastStamp = 0;
                uart.CharReceived += OnTransmitted;
            }
        }

        public void DetachFrom(IUART uart)
        {
            lock(sync)
            {
                if(this.uart != uart)
                {
                    throw new RecoverableException("This port is not connected to that UART");
                }
                uart.CharReceived -= OnTransmitted;
                this.uart = null;
                machine = null;
            }
        }

        /// <summary>Characters transmitted since the last read, as hex.</summary>
        public string Read()
        {
            lock(sync)
            {
                var builder = new StringBuilder(received.Count * 2);
                foreach(var entry in received)
                {
                    builder.Append(entry.Value.ToString("X2", CultureInfo.InvariantCulture));
                }
                received.Clear();
                return builder.ToString();
            }
        }

        /// <summary>Characters transmitted since the last read as
        /// "microseconds:hex" pairs separated by spaces.</summary>
        public string ReadTimed()
        {
            lock(sync)
            {
                var builder = new StringBuilder(received.Count * 16);
                foreach(var entry in received)
                {
                    if(builder.Length > 0)
                    {
                        builder.Append(' ');
                    }
                    builder.Append(entry.Microseconds.ToString(CultureInfo.InvariantCulture));
                    builder.Append(':');
                    builder.Append(entry.Value.ToString("X2", CultureInfo.InvariantCulture));
                }
                received.Clear();
                return builder.ToString();
            }
        }

        public void Collect(string name, StringBuilder output)
        {
            lock(sync)
            {
                if(received.Count == 0)
                {
                    return;
                }
                output.Append("U ").Append(name).Append(' ');
                var previous = 0UL;
                for(var i = 0; i < received.Count; i++)
                {
                    var stamp = received[i].Microseconds;
                    if(i > 0)
                    {
                        output.Append(',');
                    }
                    output.Append((i == 0 ? stamp : stamp - previous).ToString(CultureInfo.InvariantCulture));
                    previous = stamp;
                }
                output.Append(' ');
                foreach(var entry in received)
                {
                    output.Append(entry.Value.ToString("X2", CultureInfo.InvariantCulture));
                }
                output.Append('\n');
                received.Clear();
            }
        }

        /// <summary>Sends hex-encoded characters into the UART's receive line.</summary>
        public void Write(string hex)
        {
            if(hex == null || hex.Length % 2 != 0)
            {
                throw new RecoverableException("Write expects an even-length hex string");
            }
            IUART target;
            lock(sync)
            {
                target = uart ?? throw new RecoverableException("This port is not connected to a UART");
            }
            for(var i = 0; i < hex.Length; i += 2)
            {
                if(!byte.TryParse(hex.AsSpan(i, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var value))
                {
                    throw new RecoverableException($"Invalid hex at position {i}: '{hex.Substring(i, 2)}'");
                }
                target.WriteChar(value);
            }
        }

        public int Pending
        {
            get
            {
                lock(sync)
                {
                    return received.Count;
                }
            }
        }

        // A CPU thread stamps with its own progress through the quantum; other
        // threads see the machine's last sync point. With several machines the
        // two interleave and a stamp can land up to one quantum before the one
        // it follows. A wire carries characters in order, so a stamp never goes
        // back: it takes the previous character's time instead.
        private void OnTransmitted(byte value)
        {
            var stamp = (ulong)Math.Round(Now().TotalMicroseconds);
            lock(sync)
            {
                if(stamp < lastStamp)
                {
                    stamp = lastStamp;
                }
                lastStamp = stamp;
                received.Add(new Entry(stamp, value));
            }
        }

        private TimeInterval Now()
        {
            if(TimeDomainsManager.Instance.TryGetVirtualTimeStamp(out var threadStamp))
            {
                return threadStamp.TimeElapsed;
            }
            var owner = machine;
            if(owner != null)
            {
                return owner.ElapsedVirtualTime.TimeElapsed;
            }
            return EmulationManager.Instance.CurrentEmulation.MasterTimeSource.ElapsedVirtualTime;
        }

        private readonly object sync = new object();
        private readonly List<Entry> received = new List<Entry>();
        private IUART uart;
        private IMachine machine;
        private ulong lastStamp;

        private readonly struct Entry
        {
            public Entry(ulong microseconds, byte value)
            {
                Microseconds = microseconds;
                Value = value;
            }

            public ulong Microseconds { get; }
            public byte Value { get; }
        }
    }
}
