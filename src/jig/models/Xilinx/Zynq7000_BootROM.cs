//
// Zynq-7000 BootROM, behavioural: what the boot ROM does between a reset and
// the first FSBL instruction. Registered on the system bus without an address
// (the ROM is not visible once it hands over).
//
// References: UG585 v1.15 chapter 6 "Boot and Configuration" (BootROM header,
// header search, MultiBoot, lockdown and error codes, APU/OCM state after the
// BootROM); the Xilinx FSBL for what the hand-over must look like.
//
// On every reset (Machine.MachineReset) and on PowerOn():
//  1. OCM is cleared when clearOcmOnReset is set. UG585 v1.7 26.2.2 says a
//     system reset erases all PS memory including OCM, and 26.2.3 and Table
//     26-1 say the same of the other system resets; not checked on hardware,
//     so the board decides.
//  2. The system watchdog is left disabled: its MODE resets with WDEN clear
//     (UG585 B.31) and Table 6-20 lists no SWDT register among those the
//     BootROM changes.
//  3. BOOT_MODE[2:0] picks the boot device. JTAG (0): the core waits for a
//     debugger (halted). Quad-SPI (1): the header search below. Other devices
//     are not modelled and lock down with a diagnostic.
//  4. HoldCoreForDebugger: a debugger holding the core at reset (the JTAG load
//     flow) leaves it halted after steps 1-2 instead of booting.
//
// Quad-SPI boot (die on chip select 0 only, 3-byte reads, so an extended
// address register left non-zero redirects the reads as it would on silicon):
//  - Header search from MULTIBOOT_ADDR x 32 KB in 32 KB steps up to the search
//    limit (16 MB for a single or dual-stacked x4 device). A header is valid when
//    0x020 = 0xAA995566 (width detection), 0x024 = 0x584C4E58 ("XLNX") and
//    0x048 = ~sum of the words 0x020-0x044. MULTIBOOT_ADDR is left at the index
//    of the header that boots. None found: lockdown 0x200C.
//  - Encrypted images (0x028 = 0xA5C3C5A3 or 0x3A5C3C5A) and execute-in-place
//    (0x034 = 0) are not modelled: lockdown with a diagnostic.
//  - Register initialisation pairs at 0x0A0-0x89F, ending at address
//    0xFFFFFFFF, written in order; an address outside the ranges UG585 allows
//    locks down with 0x2111. The SLCR is unlocked around the writes.
//  - Checks: source offset 0x030 below 0x8C0 (0x2102) or not 64-byte aligned
//    (0x2103); image length 0x034 above 192 KB (0x2106); load address 0x038 not
//    zero (0x2109); start of execution 0x03C at or above 0x30000 (0x2108). UG585
//    also requires 0x040 = 0x034 and a 64-byte aligned start of execution
//    without naming the error codes; those lock down with code 0.
//  - The image (0x034 bytes from header + 0x030) is copied to OCM 0x0 and the
//    core starts at 0x03C in its reset state (supervisor mode, IRQ and FIQ
//    masked, MMU and caches off).
// Lockdown: REBOOT_STATUS[15:0] = error code, the core halts. The BootROM takes
// no simulated time.
//
// Power: PowerOff() removes the supply. The core halts and every reset until
// the next PowerOn() leaves it halted (a power-on reset while unpowered is the
// harness taking the board down); PowerOn() then runs the boot sequence.
//
using System;
using System.Collections.Generic;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Peripherals.Memory;
using Antmicro.Renode.Peripherals.Jig.Cadence;
using Antmicro.Renode.Peripherals.Jig.Contracts;

namespace Antmicro.Renode.Peripherals.Jig.Xilinx
{
    public enum BootRomState
    {
        NotRun,
        Unpowered,
        WaitingForDebugger,
        HeldForDebugger,
        Booted,
        LockedDown,
    }

    public sealed class Zynq7000_BootROM : IPeripheral
    {
        public Zynq7000_BootROM(IMachine machine, ICPU cpu, Zynq7000_SLCR slcr, Zynq7000_DevC devc,
                                Zynq7000_QSPI qspi, CadenceWDT systemWatchdog,
                                MappedMemory ocmLow = null, MappedMemory ocmHigh = null,
                                bool clearOcmOnReset = true, long searchLimit = 0x1000000)
        {
            this.machine = machine;
            this.cpu = cpu;
            this.slcr = slcr;
            this.devc = devc;
            this.qspi = qspi;
            this.systemWatchdog = systemWatchdog;
            this.ocmLow = ocmLow;
            this.ocmHigh = ocmHigh;
            this.clearOcmOnReset = clearOcmOnReset;
            this.searchLimit = searchLimit;
            machine.MachineReset += _ => Run();
        }

        public void Reset()
        {
            // The ROM itself holds no state across a reset; Run() follows it.
        }

        /// <summary>Power-up: runs the BootROM. After a power-down the OCM held
        /// no charge, whatever clearOcmOnReset says about resets.</summary>
        public void PowerOn()
        {
            if(!powered)
            {
                ocmLow?.ZeroAll();
                ocmHigh?.ZeroAll();
                // A fresh power-up: only the power-on cause, whatever the
                // unpowered board's timers did meanwhile.
                slcr.RebootStatus = 1u << (int)ZynqResetCause.PowerOn;
            }
            powered = true;
            Run();
        }

        /// <summary>Power-down: the core halts and stays halted until PowerOn.</summary>
        public void PowerOff()
        {
            powered = false;
            Halt(BootRomState.Unpowered, "power removed");
        }

        public bool Powered => powered;

        /// <summary>A debugger holds the core at reset: the BootROM does its
        /// hardware set-up and leaves the core halted instead of booting.</summary>
        public bool HoldCoreForDebugger { get; set; }

        public BootRomState State { get; private set; } = BootRomState.NotRun;

        /// <summary>Error code of the last lockdown (REBOOT_STATUS[15:0]), 0 if none.</summary>
        public uint ErrorCode { get; private set; }

        /// <summary>Flash offset of the header the last Quad-SPI boot used, -1 if none.</summary>
        public long BootedHeaderOffset { get; private set; } = -1;

        /// <summary>What the last run did, one line per step.</summary>
        public string BootLog => string.Join("\n", log);

        private void Run()
        {
            if(!powered)
            {
                // Nothing runs without power: the core stays halted and the
                // system watchdog stays disabled.
                cpu.IsHalted = true;
                systemWatchdog?.DisableOutputs();
                Note("reset while unpowered: the core stays halted");
                return;
            }
            log.Clear();
            ErrorCode = 0;
            BootedHeaderOffset = -1;
            Note($"reset cause {slcr.LastResetCause}, REBOOT_STATUS 0x{slcr.RebootStatus:X8}, MULTIBOOT_ADDR {devc.MultiBootAddress}");
            if(clearOcmOnReset)
            {
                ocmLow?.ZeroAll();
                ocmHigh?.ZeroAll();
            }
            systemWatchdog?.DisableOutputs();

            if(HoldCoreForDebugger)
            {
                Halt(BootRomState.HeldForDebugger, "core held for the debugger");
                return;
            }

            var device = slcr.BootMode & 0x7;
            switch(device)
            {
            case BootModeJtag:
                Halt(BootRomState.WaitingForDebugger, "JTAG boot mode: waiting for a debugger");
                return;
            case BootModeQuadSpi:
                BootFromQuadSpi();
                return;
            default:
                LockDown(0, $"boot mode {device} (BOOT_MODE[2:0]) is not modelled");
                return;
            }
        }

        private void BootFromQuadSpi()
        {
            var die = qspi.GetDie(0);
            if(die == null)
            {
                LockDown(HeaderNotFound, "Quad-SPI boot with no flash on chip select 0");
                return;
            }
            var limit = Math.Min(searchLimit, die.Size);
            var startIndex = devc.MultiBootAddress;
            for(var index = startIndex; (long)index * SearchStep < limit; index++)
            {
                var offset = (long)index * SearchStep;
                var header = Read(die, offset, HeaderSize);
                if(!IsValidHeader(header, out var reason))
                {
                    Note($"0x{offset:X7}: {reason}");
                    continue;
                }
                devc.MultiBootAddress = index;
                Note($"0x{offset:X7}: valid header, MULTIBOOT_ADDR = {index}");
                Boot(die, offset, header);
                return;
            }
            LockDown(HeaderNotFound, $"no valid boot header from 0x{(long)startIndex * SearchStep:X7} to 0x{limit:X7}");
        }

        private void Boot(ISpiNorFlash die, long headerOffset, byte[] header)
        {
            var encryption = Word(header, 0x028);
            var sourceOffset = Word(header, 0x030);
            var length = Word(header, 0x034);
            var loadAddress = Word(header, 0x038);
            var start = Word(header, 0x03C);
            var totalLength = Word(header, 0x040);

            if(encryption == EncryptedEfuseKey || encryption == EncryptedBbramKey)
            {
                LockDown(0, $"encrypted image (0x028 = 0x{encryption:X8}): secure boot is not modelled");
                return;
            }
            if(!RunRegisterInitialisation(header))
            {
                return;
            }
            if(length == 0)
            {
                LockDown(0, "execute-in-place image (0x034 = 0) is not modelled");
                return;
            }
            if(sourceOffset < HeaderSize)
            {
                LockDown(0x2102, $"source offset 0x{sourceOffset:X} lies inside the header");
                return;
            }
            if((sourceOffset & 0x3F) != 0)
            {
                LockDown(0x2103, $"source offset 0x{sourceOffset:X} is not 64-byte aligned");
                return;
            }
            if(length > MaximumImageLength)
            {
                LockDown(0x2106, $"image length 0x{length:X} exceeds 192 KB");
                return;
            }
            if(loadAddress != 0)
            {
                LockDown(0x2109, $"load address 0x{loadAddress:X8} is not 0");
                return;
            }
            if(start >= MaximumImageLength)
            {
                LockDown(0x2108, $"start of execution 0x{start:X8} is at or above 0x30000");
                return;
            }
            if((start & 0x3F) != 0)
            {
                LockDown(0, $"start of execution 0x{start:X8} is not 64-byte aligned (UG585 names no error code)");
                return;
            }
            if(totalLength != length)
            {
                LockDown(0, $"total image length 0x{totalLength:X} differs from the image length 0x{length:X} (UG585 names no error code)");
                return;
            }

            var image = Read(die, headerOffset + sourceOffset, (int)length);
            machine.SystemBus.WriteBytes(image, 0);
            BootedHeaderOffset = headerOffset;
            cpu.PC = start;
            cpu.IsHalted = false;
            State = BootRomState.Booted;
            Note($"copied {length} bytes from flash 0x{headerOffset + sourceOffset:X7} to OCM 0x0, start at 0x{start:X8}");
            this.Log(LogLevel.Info, "Quad-SPI boot: image at 0x{0:X7}, {1} bytes, start 0x{2:X8}", headerOffset, length, start);
        }

        private bool RunRegisterInitialisation(byte[] header)
        {
            var writes = new List<(uint Address, uint Value)>();
            for(var i = 0; i < RegisterInitPairs; i++)
            {
                var address = Word(header, RegisterInitOffset + 8 * i);
                if(address == 0xFFFFFFFF)
                {
                    break;
                }
                var value = Word(header, RegisterInitOffset + 8 * i + 4);
                if(!RegisterInitAllowed(address))
                {
                    LockDown(0x2111, $"register initialisation address 0x{address:X8} is outside the allowed ranges");
                    return false;
                }
                writes.Add((address, value));
            }
            if(writes.Count == 0)
            {
                return true;
            }
            var relock = slcr.Locked;
            machine.SystemBus.WriteDoubleWord(SlcrUnlockAddress, SlcrUnlockKey);
            foreach(var (address, value) in writes)
            {
                machine.SystemBus.WriteDoubleWord(address, value);
            }
            if(relock)
            {
                machine.SystemBus.WriteDoubleWord(SlcrLockAddress, SlcrLockKey);
            }
            Note($"register initialisation: {writes.Count} writes");
            return true;
        }

        private static bool RegisterInitAllowed(uint address)
        {
            if(address == 0xE0100058 || address == 0xF80001B0 || address == 0xF8000200)
            {
                return false;
            }
            return AllowedRegisterInitRanges.Any(range => address >= range.First && address <= range.Last);
        }

        private static bool IsValidHeader(byte[] header, out string reason)
        {
            var width = Word(header, 0x020);
            if(width != WidthDetection)
            {
                reason = $"width word 0x{width:X8}";
                return false;
            }
            var id = Word(header, 0x024);
            if(id != ImageIdentification)
            {
                reason = $"image identification 0x{id:X8}";
                return false;
            }
            uint sum = 0;
            for(var offset = 0x020; offset <= 0x044; offset += 4)
            {
                sum += Word(header, offset);
            }
            var checksum = Word(header, 0x048);
            if(checksum != ~sum)
            {
                reason = $"header checksum 0x{checksum:X8}, expected 0x{~sum:X8}";
                return false;
            }
            reason = null;
            return true;
        }

        private byte[] Read(ISpiNorFlash die, long offset, int count)
        {
            var buffer = new byte[count];
            die.LinearRead(ReadCommand, offset, buffer, 0, count);
            return buffer;
        }

        private static uint Word(byte[] data, int offset)
        {
            return (uint)(data[offset] | data[offset + 1] << 8 | data[offset + 2] << 16 | data[offset + 3] << 24);
        }

        private void Halt(BootRomState state, string message)
        {
            cpu.IsHalted = true;
            State = state;
            Note(message);
            this.Log(LogLevel.Info, message);
        }

        private void LockDown(uint code, string reason)
        {
            ErrorCode = code;
            slcr.RebootStatus = (slcr.RebootStatus & 0xFFFF0000) | code;
            cpu.IsHalted = true;
            State = BootRomState.LockedDown;
            Note($"lockdown 0x{code:X4}: {reason}");
            this.Log(LogLevel.Error, "BootROM lockdown 0x{0:X4}: {1}", code, reason);
        }

        private void Note(string message)
        {
            log.Add(message);
        }

        private bool powered = true;
        private readonly IMachine machine;
        private readonly ICPU cpu;
        private readonly Zynq7000_SLCR slcr;
        private readonly Zynq7000_DevC devc;
        private readonly Zynq7000_QSPI qspi;
        private readonly CadenceWDT systemWatchdog;
        private readonly MappedMemory ocmLow;
        private readonly MappedMemory ocmHigh;
        private readonly bool clearOcmOnReset;
        private readonly long searchLimit;
        private readonly List<string> log = new List<string>();

        private const uint BootModeJtag = 0;
        private const uint BootModeQuadSpi = 1;
        private const long SearchStep = 0x8000;
        private const int HeaderSize = 0x8C0;
        private const int RegisterInitOffset = 0x0A0;
        private const int RegisterInitPairs = 256;
        private const uint MaximumImageLength = 0x30000;
        private const uint WidthDetection = 0xAA995566;
        private const uint ImageIdentification = 0x584C4E58;
        private const uint EncryptedEfuseKey = 0xA5C3C5A3;
        private const uint EncryptedBbramKey = 0x3A5C3C5A;
        private const uint HeaderNotFound = 0x200C;
        private const byte ReadCommand = 0x03;
        private const ulong SlcrLockAddress = 0xF8000004;
        private const ulong SlcrUnlockAddress = 0xF8000008;
        private const uint SlcrLockKey = 0x767B;
        private const uint SlcrUnlockKey = 0xDF0D;

        // UG585 "Register Initialization Parameters": UART1, QSPI, SMC, SDIO0
        // (except 0xE0100058), DDRC, and SLCR 0xF8000100-0xF8000234 (except
        // 0xF80001B0 and 0xF8000200), 0xF800024C, 0xF8000304-0xF8000834,
        // 0xF8000A00-0xF8000A8C, 0xF8000AB0-0xF8000B74.
        private static readonly (uint First, uint Last)[] AllowedRegisterInitRanges =
        {
            (0xE0001000, 0xE0001FFC),
            (0xE000D000, 0xE000DFFC),
            (0xE000E000, 0xE000EFFC),
            (0xE0100004, 0xE0100FFC),
            (0xF8006000, 0xF8006FFC),
            (0xF8000100, 0xF8000234),
            (0xF800024C, 0xF800024C),
            (0xF8000304, 0xF8000834),
            (0xF8000A00, 0xF8000A8C),
            (0xF8000AB0, 0xF8000B74),
        };
    }
}
