//
// Zynq-7000 device configuration interface (DevC / PCAP, 0xF8007000-0xF80070FF).
//
// References: UG585 chapter 6 "Boot and Configuration" (6.4 PCAP) and the
// devcfg register summary (CTRL resets to 0x0C006000, STATUS to 0x40000820);
// register and bit names follow the BSP header xdevcfg_hw.h (CONFIG resets to
// XDCFG_CONFIG_RESET_VALUE 0x508). The behaviour below is what the Xilinx
// FSBL's PCAP code (pcap.c) relies on.
//
// Behaviour modelled:
//  - CTRL.PCFG_PROG_B (bit 30) is low after reset. Driving it low clears the PL
//    configuration: STATUS.PCFG_INIT reads 0 and INT_STS.PCFG_INIT_NE sets.
//    Driving it high finishes PL initialisation: STATUS.PCFG_INIT reads 1 and
//    INT_STS.PCFG_INIT_PE sets (housecleaning takes no simulated time).
//  - MCTRL: PS_VERSION in [31:28], PCFG_POR_B (bit 8, PL powered) reads 1,
//    PCAP_LPBK (bit 4) is read/write.
//  - DMA: SRC_ADDR, DEST_ADDR, SRC_LEN, DEST_LEN (lengths in 32-bit words);
//    writing DEST_LEN queues the command. Address bit 0 is the drivers' "last
//    transfer" marker and is ignored for addressing. A destination of
//    0xFFFFFFFF streams the source into the PL through PCAP (CTRL.PCAP_MODE must
//    be set) at 32 bits per PCAP clock, a quarter of that with
//    CTRL.PCAP_RATE_EN; the transfer then raises DMA_DONE and D_P_DONE and, if
//    the PL was initialised, PCFG_DONE. The SHA-256 of the data is kept so tests
//    can check which bitstream reached the PL. The time counts only the PCAP
//    clock, not the source's read bandwidth.
//  - STATUS: DMA_CMD_Q_E, DMA_DONE_CNT [29:28] (written 1s clear it),
//    PCFG_INIT, PSS_CFG_RESET_B and PSS_GTS_USR_B as after reset; once the PL
//    is configured PSS_GTS_CFG_B, PSS_GPWRDWN_B and PSS_FST_CFG_B read 1.
//  - INT_STS is write-one-to-clear, INT_MASK (reset all ones) masks the IRQ.
//  - MULTIBOOT_ADDR [12:0] survives every reset except a power-on reset; the
//    BootROM's golden search starts from it and leaves the booted index in it.
//  - JTAG configuration (the debugger's "fpga -f") through ConfigureFromJtag.
// Not modelled: AES/HMAC decryption, PCAP readback, SEU, the XADC interface
// (0x100 and up, a separate model).
//
using System;
using System.IO;
using System.Security.Cryptography;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.Xilinx
{
    public sealed class Zynq7000_DevC : IDoubleWordPeripheral, IKnownSize
    {
        public Zynq7000_DevC(IMachine machine, uint psVersion = 3, ulong pcapClockFrequency = 100000000,
                             Zynq7000_SLCR slcr = null)
        {
            this.machine = machine;
            this.psVersion = psVersion & 0xF;
            this.pcapClockFrequency = pcapClockFrequency;
            IRQ = new GPIO();
            if(slcr != null)
            {
                slcr.ResetRequested += cause =>
                {
                    if(cause == ZynqResetCause.PowerOn)
                    {
                        PowerOnReset();
                    }
                };
            }
            Reset();
        }

        /// <summary>System reset: the PL loses its configuration; MULTIBOOT_ADDR survives.</summary>
        public void Reset()
        {
            generation++;
            control = ControlResetValue;
            lockBits = 0;
            config = ConfigResetValue;
            interruptStatus = 0;
            interruptMask = 0xFFFFFFFF;
            srcAddress = 0;
            dstAddress = 0;
            srcLength = 0;
            dstLength = 0;
            dmaBusy = false;
            dmaDoneCount = 0;
            romShadow = 0;
            softwareId = 0;
            miscControl = 0;
            plInitialised = false;     // PROG_B is low after reset
            if(PlConfigured)
            {
                PlConfigured = false;
                PlConfigurationChanged?.Invoke(false);
            }
            UpdateIrq();
        }

        /// <summary>Power-on reset (PS_POR_B): also clears MULTIBOOT_ADDR.</summary>
        public void PowerOnReset()
        {
            multiBootAddress = 0;
            ConfiguredBitstreamSha256 = null;
            Reset();
        }

        public long Size => 0x100;

        public GPIO IRQ { get; }

        public bool PlConfigured { get; private set; }

        public string ConfiguredBitstreamSha256 { get; private set; }

        /// <summary>Raised with true when the PL becomes configured, false when it loses its configuration.</summary>
        public event Action<bool> PlConfigurationChanged;

        /// <summary>MULTIBOOT_ADDR[12:0]: the BootROM's search start, in 32 KB units.</summary>
        public uint MultiBootAddress
        {
            get => multiBootAddress;
            set => multiBootAddress = value & MultiBootMask;
        }

        /// <summary>Configures the PL the way a JTAG debugger does ("fpga -f"): the
        /// file's SHA-256 is recorded and the PL reports configured.</summary>
        public void ConfigureFromJtag(string bitstreamPath)
        {
            if(!File.Exists(bitstreamPath))
            {
                throw new RecoverableException($"Bitstream not found: {bitstreamPath}");
            }
            byte[] hash;
            using(var sha = SHA256.Create())
            using(var stream = File.OpenRead(bitstreamPath))
            {
                hash = sha.ComputeHash(stream);
            }
            ConfiguredBitstreamSha256 = BitConverter.ToString(hash).Replace("-", "").ToLowerInvariant();
            plInitialised = true;
            MarkConfigured("JTAG", new FileInfo(bitstreamPath).Length);
            UpdateIrq();
        }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case ControlOffset: return control;
            case LockOffset: return lockBits;
            case ConfigOffset: return config;
            case InterruptStatusOffset: return interruptStatus;
            case InterruptMaskOffset: return interruptMask;
            case StatusOffset: return Status();
            case DmaSourceAddressOffset: return srcAddress;
            case DmaDestinationAddressOffset: return dstAddress;
            case DmaSourceLengthOffset: return srcLength;
            case DmaDestinationLengthOffset: return dstLength;
            case RomShadowOffset: return romShadow;
            case MultiBootAddressOffset: return multiBootAddress;
            case SoftwareIdOffset: return softwareId;
            case UnlockOffset: return 0;
            case MiscControlOffset: return (psVersion << 28) | MiscControlPlPowered | (miscControl & MiscControlLoopback);
            default:
                this.Log(LogLevel.Warning, "Read from unhandled offset 0x{0:X2}", offset);
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case ControlOffset:
                WriteControl(value);
                break;
            case LockOffset:
                lockBits |= value & 0x1F; // lock bits are sticky until reset
                break;
            case ConfigOffset:
                config = value;
                break;
            case InterruptStatusOffset:
                interruptStatus &= ~value;
                break;
            case InterruptMaskOffset:
                interruptMask = value;
                break;
            case StatusOffset:
                if((value & StatusDmaDoneCountMask) != 0)
                {
                    dmaDoneCount = 0;
                }
                break;
            case DmaSourceAddressOffset:
                srcAddress = value;
                break;
            case DmaDestinationAddressOffset:
                dstAddress = value;
                break;
            case DmaSourceLengthOffset:
                srcLength = value & DmaLengthMask;
                break;
            case DmaDestinationLengthOffset:
                dstLength = value & DmaLengthMask;
                StartDma();
                break;
            case RomShadowOffset:
                romShadow = value;
                break;
            case MultiBootAddressOffset:
                multiBootAddress = value & MultiBootMask;
                break;
            case SoftwareIdOffset:
                softwareId = value;
                break;
            case UnlockOffset:
                if(value != UnlockKey)
                {
                    this.Log(LogLevel.Warning, "Invalid DevC unlock key 0x{0:X8}", value);
                }
                break;
            case MiscControlOffset:
                miscControl = value & MiscControlLoopback;
                break;
            default:
                this.Log(LogLevel.Warning, "Write 0x{0:X8} to unhandled offset 0x{1:X2}", value, offset);
                break;
            }
            UpdateIrq();
        }

        private void WriteControl(uint value)
        {
            var oldProgB = (control & ControlProgB) != 0;
            control = value;
            var newProgB = (value & ControlProgB) != 0;
            if(oldProgB && !newProgB)
            {
                plInitialised = false;
                interruptStatus |= IntInitNegEdge;
                if(PlConfigured)
                {
                    PlConfigured = false;
                    this.Log(LogLevel.Info, "PROG_B low: PL configuration cleared");
                    PlConfigurationChanged?.Invoke(false);
                }
            }
            else if(!oldProgB && newProgB)
            {
                plInitialised = true;
                interruptStatus |= IntInitPosEdge;
            }
        }

        private uint Status()
        {
            uint status = StatusGtsUsrB | StatusCfgResetB;
            if(!dmaBusy) status |= StatusDmaQueueEmpty;
            status |= (uint)(dmaDoneCount & 0x3) << 28;
            if(plInitialised) status |= StatusPcfgInit;
            if(PlConfigured) status |= StatusGtsCfgB | StatusGpwrdwnB | StatusFirstConfigDone;
            return status;
        }

        private void StartDma()
        {
            if(dmaBusy)
            {
                interruptStatus |= IntDmaQueueOverflow;
                this.Log(LogLevel.Warning, "DMA command issued while another is in flight");
                return;
            }
            var source = srcAddress & ~1u;
            var destination = dstAddress & ~1u;
            if((dstAddress | 1u) == PcapAddress)
            {
                if((control & ControlPcapMode) == 0)
                {
                    this.Log(LogLevel.Error, "PCAP write with CTRL.PCAP_MODE clear");
                    interruptStatus |= IntDmaCommandError;
                    return;
                }
                StartPcapWrite(source, srcLength);
            }
            else if((srcAddress | 1u) == PcapAddress)
            {
                this.Log(LogLevel.Error, "PCAP readback (source 0xFFFFFFFF) is not supported");
                interruptStatus |= IntDmaCommandError;
            }
            else
            {
                StartMemoryCopy(source, destination, srcLength);
            }
        }

        private ulong TransferNanoseconds(uint words)
        {
            var rate = (control & ControlPcapRateEnable) != 0 ? pcapClockFrequency / 4.0 : pcapClockFrequency;
            return (ulong)Math.Max(1.0, words * 1e9 / rate);
        }

        private void StartPcapWrite(uint source, uint words)
        {
            var bytes = (int)(words * 4);
            var data = machine.SystemBus.ReadBytes(source, bytes);
            dmaBusy = true;
            var token = generation;
            machine.ScheduleAction(TimeInterval.FromNanoseconds(TransferNanoseconds(words)), _ =>
            {
                if(token != generation)
                {
                    return;
                }
                dmaBusy = false;
                dmaDoneCount++;
                interruptStatus |= IntDmaDone | IntDmaPcapDone;
                if(plInitialised)
                {
                    using(var sha = SHA256.Create())
                    {
                        ConfiguredBitstreamSha256 = BitConverter.ToString(sha.ComputeHash(data)).Replace("-", "").ToLowerInvariant();
                    }
                    MarkConfigured("PCAP", bytes);
                }
                else
                {
                    this.Log(LogLevel.Warning, "PCAP data sent while the PL was not initialised (PROG_B low): ignored");
                }
                UpdateIrq();
            }, "devc-pcap");
        }

        private void MarkConfigured(string path, long bytes)
        {
            PlConfigured = true;
            interruptStatus |= IntPcfgDone;
            this.Log(LogLevel.Info, "PL configured through {0} ({1} bytes, sha256 {2})", path, bytes, ConfiguredBitstreamSha256);
            PlConfigurationChanged?.Invoke(true);
        }

        private void StartMemoryCopy(uint source, uint destination, uint words)
        {
            var bytes = (int)(words * 4);
            var data = machine.SystemBus.ReadBytes(source, bytes);
            dmaBusy = true;
            var token = generation;
            machine.ScheduleAction(TimeInterval.FromNanoseconds(TransferNanoseconds(words)), _ =>
            {
                if(token != generation)
                {
                    return;
                }
                machine.SystemBus.WriteBytes(data, destination);
                dmaBusy = false;
                dmaDoneCount++;
                interruptStatus |= IntDmaDone;
                UpdateIrq();
            }, "devc-dma");
        }

        private void UpdateIrq()
        {
            IRQ.Set((interruptStatus & ~interruptMask) != 0);
        }

        private readonly IMachine machine;
        private readonly uint psVersion;
        private readonly ulong pcapClockFrequency;

        private ulong generation;
        private uint control;
        private uint lockBits;
        private uint config;
        private uint interruptStatus;
        private uint interruptMask;
        private uint srcAddress;
        private uint dstAddress;
        private uint srcLength;
        private uint dstLength;
        private bool dmaBusy;
        private int dmaDoneCount;
        private uint romShadow;
        private uint multiBootAddress;
        private uint softwareId;
        private uint miscControl;
        private bool plInitialised;

        private const long ControlOffset = 0x00;
        private const long LockOffset = 0x04;
        private const long ConfigOffset = 0x08;
        private const long InterruptStatusOffset = 0x0C;
        private const long InterruptMaskOffset = 0x10;
        private const long StatusOffset = 0x14;
        private const long DmaSourceAddressOffset = 0x18;
        private const long DmaDestinationAddressOffset = 0x1C;
        private const long DmaSourceLengthOffset = 0x20;
        private const long DmaDestinationLengthOffset = 0x24;
        private const long RomShadowOffset = 0x28;
        private const long MultiBootAddressOffset = 0x2C;
        private const long SoftwareIdOffset = 0x30;
        private const long UnlockOffset = 0x34;
        private const long MiscControlOffset = 0x80;

        private const uint ControlResetValue = 0x0C006000;
        private const uint ControlProgB = 0x40000000;          // XDCFG_CTRL_PCFG_PROG_B_MASK
        private const uint ControlPcapMode = 0x04000000;       // XDCFG_CTRL_PCAP_MODE_MASK
        private const uint ControlPcapRateEnable = 0x02000000; // XDCFG_CTRL_PCAP_RATE_EN_MASK
        private const uint ConfigResetValue = 0x508;           // XDCFG_CONFIG_RESET_VALUE
        private const uint UnlockKey = 0x757BDF0D;             // XDCFG_UNLOCK_DATA
        private const uint PcapAddress = 0xFFFFFFFF;
        private const uint DmaLengthMask = 0x07FFFFFF;         // XDCFG_DMA_LEN_MASK
        private const uint MultiBootMask = 0x00001FFF;
        private const uint MiscControlPlPowered = 0x00000100;  // XDCFG_MCTRL_PCAP_PCFG_POR_B_MASK (FSBL pcap.h)
        private const uint MiscControlLoopback = 0x00000010;   // XDCFG_MCTRL_PCAP_LPBK_MASK

        private const uint IntDmaCommandError = 0x00008000;
        private const uint IntDmaQueueOverflow = 0x00004000;
        private const uint IntDmaDone = 0x00002000;
        private const uint IntDmaPcapDone = 0x00001000;
        private const uint IntPcfgDone = 0x00000004;
        private const uint IntInitPosEdge = 0x00000002;
        private const uint IntInitNegEdge = 0x00000001;

        private const uint StatusDmaQueueEmpty = 0x40000000;
        private const uint StatusDmaDoneCountMask = 0x30000000;
        private const uint StatusGtsUsrB = 0x00000800;
        private const uint StatusFirstConfigDone = 0x00000400;
        private const uint StatusGpwrdwnB = 0x00000200;
        private const uint StatusGtsCfgB = 0x00000100;
        private const uint StatusCfgResetB = 0x00000020;
        private const uint StatusPcfgInit = 0x00000010;
    }
}
