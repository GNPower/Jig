//
// Zynq-7000 System Level Control Registers (SLCR, 0xF8000000), including the
// reset controller and the boot-mode straps.
//
// References: UG585 v1.7 chapter 4 "System Addresses", chapter 26 "Reset
// System", register reference B.28 (slcr). Power-on values: those listed in
// PowerOnValues, all as B.28 gives them (the PLL and clock values are also
// those of Renode's own Zynq7000_SystemLevelControlRegisters model). Every
// other register resets to 0, although B.28 gives many of them other values
// (MIO_PIN_xx, most clock controls). The lock starts engaged.
//
// Behaviour modelled:
//  - Write protection: SLCR_LOCK (0x767B) / SLCR_UNLOCK (0xDF0D) / SLCR_LOCKSTA.
//    While locked, writes to other registers are ignored and logged.
//  - Every other register in the 4 KB block is storage, so ps7_init's
//    read-modify-writes and later reads behave.
//  - Clocks are not generated: PLL and divider writes are stored, and each
//    peripheral's clock frequency is a fixed parameter of the board's platform
//    description (jig check compares them with the design's ps7_init).
//  - PLL_STATUS: a PLL reports locked & stable while it is neither held in
//    reset nor powered down.
//  - DDRIOB_DCI_STATUS.DONE follows DDRIOB_DCI_CTRL.ENABLE (calibration is
//    instantaneous).
//  - BOOT_MODE returns the strap value set by the board / test harness.
//  - Resets: PSS_RST_CTRL.SOFT_RST and the watchdogs call RequestReset(cause).
//    REBOOT_STATUS records the cause (bits 16..22) and survives every reset but
//    power-on; software clears it. The whole machine is then reset and the
//    BootROM model runs the boot path.
//  - FPGA_RST_CTRL resets to 0x01F33F0F (B.28) and drives FCLK_RESET0_N..
//    FCLK_RESET3_N from bits 0-3, numbered GPIO outputs 0-3 (high = reset
//    released). FPGA0..3_OUT_RST therefore start asserted; ps7_post_config and
//    the FSBL's fabric hook release them by writing 0.
//  - Peripheral software resets (0x204-0x248): every write is published
//    (ResetControlWritten), and a peripheral registered with AddResetTarget
//    resets when a write sets one of its bits, as UART_RST_CTRL bit 0 resets
//    UART0. Holding a bit set does not hold the peripheral in reset: register
//    accesses keep working.
//  - Reset pins as GPIO inputs: 0 = PS_POR_B asserted (power-on reset),
//    1 = PS_SRST_B asserted (external system reset, REBOOT_STATUS[21]). The
//    reset happens when the input becomes asserted.
//  - A9_CPU_RST_CTRL is storage: only CPU0 is modelled (CPU1 stays parked as
//    the BootROM leaves it), so a CPU0 reset request through it is reported.
//
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Jig.Xilinx
{
    public enum ZynqResetCause
    {
        SystemWatchdog = 16,
        Cpu0Watchdog = 17,
        Cpu1Watchdog = 18,
        SoftwareReset = 19,
        DebugReset = 20,
        ExternalReset = 21,
        PowerOn = 22,
    }

    public sealed class Zynq7000_SLCR : IDoubleWordPeripheral, IKnownSize, IGPIOReceiver, INumberedGPIOOutput
    {
        public Zynq7000_SLCR(IMachine machine, uint bootMode = 0)
        {
            this.machine = machine;
            var fclkResets = new Dictionary<int, IGPIO>();
            for(var i = 0; i < 4; i++)
            {
                fclkResets[i] = new GPIO();
            }
            Connections = fclkResets;
            BootMode = bootMode;
            storage = new uint[Size / 4];
            rebootStatus = 1u << (int)ZynqResetCause.PowerOn;
            Reset();
        }

        // System reset: everything returns to its reset value except
        // REBOOT_STATUS, which only a power-on reset clears.
        public void Reset()
        {
            Array.Clear(storage, 0, storage.Length);
            foreach(var entry in PowerOnValues)
            {
                storage[entry.Key / 4] = entry.Value;
            }
            locked = true;
            DriveFclkResets();
        }

        public long Size => 0x1000;

        /// <summary>FCLK_RESET0_N..FCLK_RESET3_N: high while FPGAn_OUT_RST is clear.</summary>
        public IReadOnlyDictionary<int, IGPIO> Connections { get; }

        /// <summary>BOOT_MODE strap pins [3:0] (0 JTAG, 1 Quad-SPI, 2 NOR, 5 NAND, 6 SD) and bit 4 PLL bypass.</summary>
        public uint BootMode { get; set; }

        public bool Locked => locked;

        public uint RebootStatus
        {
            get => rebootStatus;
            set => rebootStatus = value;
        }

        public uint FpgaResetControl => storage[(int)Registers.FpgaResetControl / 4];

        public event Action<uint> FpgaResetChanged;

        /// <summary>Raised after a write to one of the peripheral reset-control
        /// registers (0x204-0x248, e.g. LQSPI_RST_CTRL at 0x230) with the offset and
        /// the value written, so peripherals reset when software pulses their bits.</summary>
        public event Action<long, uint> ResetControlWritten;

        /// <summary>Raised just before a requested reset is handed to the machine.</summary>
        public event Action<ZynqResetCause> ResetRequested;

        /// <summary>The cause of the last reset (the monitor cannot read a nullable property).</summary>
        public ZynqResetCause LastResetCause { get; private set; } = ZynqResetCause.PowerOn;

        public void OnGPIO(int number, bool value)
        {
            if(number < 0 || number >= resetPins.Length)
            {
                this.Log(LogLevel.Error, "Reset input {0} does not exist (0 = PS_POR_B, 1 = PS_SRST_B)", number);
                return;
            }
            var wasAsserted = resetPins[number];
            resetPins[number] = value;
            if(value && !wasAsserted)
            {
                RequestReset(number == PowerOnResetPin ? ZynqResetCause.PowerOn : ZynqResetCause.ExternalReset);
            }
        }

        /// <summary>Makes a write to the reset-control register at
        /// <paramref name="offset"/> (0x204-0x248) that sets any bit of
        /// <paramref name="mask"/> reset <paramref name="peripheral"/>; the
        /// platform description wires each controller to its bits (UG585 B.28,
        /// e.g. UART_RST_CTRL 0x228 bits 0 and 2 for UART0).</summary>
        public void AddResetTarget(IPeripheral peripheral, long offset, uint mask)
        {
            if(peripheral == null)
            {
                throw new RecoverableException("AddResetTarget needs a peripheral");
            }
            if(offset < FirstPeripheralResetControl || offset > LastPeripheralResetControl || (offset & 3) != 0)
            {
                throw new RecoverableException($"0x{offset:X} is not a reset-control register (0x{FirstPeripheralResetControl:X}-0x{LastPeripheralResetControl:X})");
            }
            if(mask == 0)
            {
                throw new RecoverableException("AddResetTarget needs at least one reset bit");
            }
            resetTargets.Add((peripheral, offset, mask));
        }

        public void RequestReset(ZynqResetCause cause)
        {
            this.Log(LogLevel.Info, "Reset requested: {0}", cause);
            if(cause == ZynqResetCause.PowerOn)
            {
                rebootStatus = 1u << (int)ZynqResetCause.PowerOn;
            }
            else
            {
                rebootStatus |= 1u << (int)cause;
            }
            LastResetCause = cause;
            ResetRequested?.Invoke(cause);
            machine.RequestReset();
        }

        public uint ReadDoubleWord(long offset)
        {
            switch((Registers)offset)
            {
            case Registers.Lock:
            case Registers.Unlock:
                return 0;
            case Registers.LockStatus:
                return locked ? 1u : 0u;
            case Registers.PllStatus:
                return PllStatus();
            case Registers.RebootStatus:
                return rebootStatus;
            case Registers.BootMode:
                return BootMode;
            case Registers.DciStatus:
                return (storage[(int)Registers.DciControl / 4] & DciEnable) != 0 ? DciDone : 0u;
            default:
                return storage[offset / 4];
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch((Registers)offset)
            {
            case Registers.Lock:
                if((value & 0xFFFF) == LockKey) locked = true;
                else this.Log(LogLevel.Warning, "Invalid SLCR lock key 0x{0:X}", value);
                return;
            case Registers.Unlock:
                if((value & 0xFFFF) == UnlockKey) locked = false;
                else this.Log(LogLevel.Warning, "Invalid SLCR unlock key 0x{0:X}", value);
                return;
            case Registers.LockStatus:
            case Registers.PllStatus:
            case Registers.BootMode:
            case Registers.DciStatus:
                this.Log(LogLevel.Debug, "Write to read-only register 0x{0:X3} ignored", offset);
                return;
            }

            if(locked)
            {
                this.Log(LogLevel.Warning, "Write 0x{0:X8} to 0x{1:X3} ignored: SLCR is locked", value, offset);
                return;
            }

            switch((Registers)offset)
            {
            case Registers.RebootStatus:
                rebootStatus = value;
                return;
            case Registers.SoftResetControl:
                storage[offset / 4] = value & 1;
                if((value & 1) != 0)
                {
                    RequestReset(ZynqResetCause.SoftwareReset);
                }
                return;
            case Registers.FpgaResetControl:
                storage[offset / 4] = value;
                DriveFclkResets();
                FpgaResetChanged?.Invoke(value);
                return;
            case Registers.A9CpuResetControl:
                storage[offset / 4] = value;
                if((value & A9Cpu0Reset) != 0)
                {
                    this.Log(LogLevel.Warning, "A9_CPU_RST_CTRL 0x{0:X}: CPU0 reset through the SLCR is not modelled", value);
                }
                return;
            case Registers.OcmConfig:
                if((value & 0xF) != (storage[offset / 4] & 0xF))
                {
                    this.Log(LogLevel.Warning, "OCM_CFG remap 0x{0:X} requested; the OCM layout is fixed at boot in this model", value & 0xF);
                }
                storage[offset / 4] = value;
                return;
            default:
                storage[offset / 4] = value;
                if(offset >= FirstPeripheralResetControl && offset <= LastPeripheralResetControl)
                {
                    ResetControlWritten?.Invoke(offset, value);
                    ResetTargets(offset, value);
                }
                return;
            }
        }

        private void ResetTargets(long offset, uint value)
        {
            foreach(var (peripheral, targetOffset, mask) in resetTargets)
            {
                if(targetOffset == offset && (value & mask) != 0)
                {
                    this.Log(LogLevel.Debug, "Software reset of {0} (0x{1:X3} = 0x{2:X})", peripheral.GetName(), offset, value);
                    peripheral.Reset();
                }
            }
        }

        private void DriveFclkResets()
        {
            var control = storage[(int)Registers.FpgaResetControl / 4];
            for(var i = 0; i < Connections.Count; i++)
            {
                Connections[i].Set(((control >> i) & 1) == 0);
            }
        }

        private uint PllStatus()
        {
            uint status = 0;
            var plls = new[] { Registers.ArmPllControl, Registers.DdrPllControl, Registers.IoPllControl };
            for(var i = 0; i < plls.Length; i++)
            {
                var control = storage[(int)plls[i] / 4];
                var running = (control & (PllReset | PllPowerDown)) == 0;
                if(running)
                {
                    status |= 1u << i;        // ARM/DDR/IO_PLL_LOCK
                    status |= 1u << (i + 3);  // ARM/DDR/IO_PLL_STABLE
                }
            }
            return status;
        }

        private static readonly (long Key, uint Value)[] PowerOnValues =
        {
            ((long)Registers.ArmPllControl, 0x0001A008),
            ((long)Registers.DdrPllControl, 0x0001A008),
            ((long)Registers.IoPllControl, 0x0001A008),
            ((long)Registers.ArmClockControl, 0x1F000400),
            ((long)Registers.DdrClockControl, 0x18400003),
            ((long)Registers.AperClockControl, 0x01FFCCCD),
            ((long)Registers.Gem0ClockControl, 0x00003C01),
            ((long)Registers.SdioClockControl, 0x00001E03),
            ((long)Registers.UartClockControl, 0x00003F03),
            ((long)Registers.Clock621True, 0x00000001),
            ((long)Registers.FpgaResetControl, 0x01F33F0F),
        };

        private readonly IMachine machine;
        private readonly uint[] storage;
        private readonly List<(IPeripheral Peripheral, long Offset, uint Mask)> resetTargets = new List<(IPeripheral, long, uint)>();
        private readonly bool[] resetPins = new bool[2];
        private bool locked;
        private uint rebootStatus;

        private const int PowerOnResetPin = 0;
        private const uint A9Cpu0Reset = 1u << 0;

        private const uint LockKey = 0x767B;
        private const uint UnlockKey = 0xDF0D;
        private const uint PllReset = 1u << 0;
        private const uint PllPowerDown = 1u << 1;
        private const uint DciEnable = 1u << 1;
        private const uint DciDone = 1u << 13;
        private const long FirstPeripheralResetControl = 0x204;
        private const long LastPeripheralResetControl = 0x248;

        private enum Registers : long
        {
            Lock = 0x004,
            Unlock = 0x008,
            LockStatus = 0x00C,
            ArmPllControl = 0x100,
            DdrPllControl = 0x104,
            IoPllControl = 0x108,
            PllStatus = 0x10C,
            ArmClockControl = 0x120,
            DdrClockControl = 0x124,
            AperClockControl = 0x12C,
            Gem0ClockControl = 0x140,
            SdioClockControl = 0x150,
            UartClockControl = 0x154,
            Clock621True = 0x1C4,
            SoftResetControl = 0x200,
            FpgaResetControl = 0x240,
            A9CpuResetControl = 0x244,
            AwdtResetControl = 0x24C,
            RebootStatus = 0x258,
            BootMode = 0x25C,
            WdtClockSelect = 0x304,
            OcmConfig = 0x910,
            DciControl = 0xB70,
            DciStatus = 0xB74,
        }
    }
}
