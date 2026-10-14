//
// Executes a register-initialisation program against the system bus, the way a
// debugger runs ps7_init.tcl before downloading an ELF.
//
// Program format, one operation per line ('#' starts a comment):
//   W <address> <value>           32-bit write
//   M <address> <mask> <value>    read-modify-write of the bits in <mask>
//   P <address> <mask>            poll until (read & mask) != 0
// Numbers are hexadecimal with or without 0x.
//
//   machine RunRegisterProgram @out/run/ps7_init.prog
//
// A poll that is still unmet after PollLimit reads raises an error naming the
// address, so a model that fails to model a status bit is caught at once.
//
using System;
using System.Globalization;
using System.IO;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Jig.Harness
{
    public static class RegisterProgramExtensions
    {
        public const int PollLimit = 100000;

        public static string RunRegisterProgram(this IMachine machine, string path)
        {
            if(!File.Exists(path))
            {
                throw new RecoverableException($"Register program not found: {path}");
            }
            var bus = machine.SystemBus;
            int writes = 0, polls = 0, lineNumber = 0;
            foreach(var rawLine in File.ReadLines(path))
            {
                lineNumber++;
                var line = rawLine;
                var hash = line.IndexOf('#');
                if(hash >= 0)
                {
                    line = line.Substring(0, hash);
                }
                var parts = line.Split((char[])null, StringSplitOptions.RemoveEmptyEntries);
                if(parts.Length == 0)
                {
                    continue;
                }
                try
                {
                    switch(parts[0].ToUpperInvariant())
                    {
                    case "W":
                        Expect(parts, 3);
                        bus.WriteDoubleWord(Parse(parts[1]), (uint)Parse(parts[2]));
                        writes++;
                        break;
                    case "M":
                    {
                        Expect(parts, 4);
                        var address = Parse(parts[1]);
                        var mask = (uint)Parse(parts[2]);
                        var value = (uint)Parse(parts[3]);
                        var current = bus.ReadDoubleWord(address);
                        bus.WriteDoubleWord(address, (current & ~mask) | (value & mask));
                        writes++;
                        break;
                    }
                    case "P":
                    {
                        Expect(parts, 3);
                        var address = Parse(parts[1]);
                        var mask = (uint)Parse(parts[2]);
                        var satisfied = false;
                        uint last = 0;
                        for(var i = 0; i < PollLimit && !satisfied; i++)
                        {
                            last = bus.ReadDoubleWord(address);
                            satisfied = (last & mask) != 0;
                        }
                        if(!satisfied)
                        {
                            throw new RecoverableException(
                                $"poll of 0x{address:X8} for mask 0x{mask:X8} never succeeded (last value 0x{last:X8})");
                        }
                        polls++;
                        break;
                    }
                    default:
                        throw new RecoverableException($"unknown operation '{parts[0]}'");
                    }
                }
                catch(RecoverableException e)
                {
                    throw new RecoverableException($"{Path.GetFileName(path)}:{lineNumber}: {e.Message}");
                }
            }
            machine.Log(LogLevel.Info, "Register program {0}: {1} writes, {2} polls", Path.GetFileName(path), writes, polls);
            return $"{writes} writes, {polls} polls";
        }

        private static void Expect(string[] parts, int count)
        {
            if(parts.Length != count)
            {
                throw new RecoverableException($"'{parts[0]}' takes {count - 1} operands, got {parts.Length - 1}");
            }
        }

        private static ulong Parse(string text)
        {
            var digits = text.StartsWith("0x", StringComparison.OrdinalIgnoreCase) ? text.Substring(2) : text;
            if(!ulong.TryParse(digits, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var value))
            {
                throw new RecoverableException($"'{text}' is not a hexadecimal number");
            }
            return value;
        }
    }
}
