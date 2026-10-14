//
// Tamper and backup registers (TAMP) of the STM32U5 series, written from ST
// RM0456 (chapter "Tamper and backup registers (TAMP)"). Register offsets and
// bit positions follow that chapter and ST's CMSIS device headers
// stm32u575xx.h and stm32u595xx.h.
//
// TAMP lives in the backup domain: a system reset (Reset) leaves every
// register as it is; a backup domain reset (a rising edge on input 0, which
// the RCC drives on a power-on reset and when RCC_BDCR.BDRST is set) and
// PowerCycle clear them all.
//
// Behaviour modelled:
//  - The 32 backup registers TAMP_BKP0R to BKP31R (0x100 to 0x17C) and the
//    configuration registers CR1 to CR3, FLTCR, ATCR1, ATCR2, SECCFGR,
//    PRIVCFGR, IER and ERCFGR store what software writes; they read 0 after a
//    backup domain reset.
//  - COUNTR, the monotonic counter: each write adds one, up to 0xFFFFFFFF.
//  - SR, MISR and SMISR read 0: no tamper event is ever detected. SCR and
//    ATSEEDR take writes without effect; ATOR reads 0.
//
// Not modelled: tamper detection (external, internal and active tampers) and
// the erase of the backup registers it causes, the secure and privileged
// access rules, and the reset values of the configuration registers other
// than 0.
//
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Jig.Contracts;

namespace Antmicro.Renode.Peripherals.Jig.ST
{
    [AllowedTranslations(AllowedTranslation.ByteToDoubleWord | AllowedTranslation.WordToDoubleWord)]
    public class STM32U5_TAMP : IDoubleWordPeripheral, IKnownSize, IGPIOReceiver, IPowerCycled
    {
        public STM32U5_TAMP()
        {
            words = new uint[Size / 4];
        }

        // System reset: the backup domain keeps its state.
        public void Reset()
        {
        }

        // Supply loss and return without a backup battery: a backup domain reset.
        public void PowerCycle()
        {
            ResetBackupDomain();
        }

        // Input 0: the backup domain reset from the RCC (power-on reset, BDRST).
        public void OnGPIO(int number, bool value)
        {
            if(number != 0)
            {
                return;
            }
            if(value && !backupDomainResetLine)
            {
                this.Log(LogLevel.Debug, "Backup domain reset");
                ResetBackupDomain();
            }
            backupDomainResetLine = value;
        }

        public uint ReadDoubleWord(long offset)
        {
            if(IsBackupRegister(offset) || IsStored(offset))
            {
                return words[offset / 4];
            }
            switch((Registers)offset)
            {
            case Registers.MonotonicCounter:
                return words[offset / 4];
            case Registers.ActiveTamperSeed:
            case Registers.ActiveTamperOutput:
            case Registers.Status:
            case Registers.MaskedInterruptStatus:
            case Registers.SecureMaskedInterruptStatus:
            case Registers.StatusClear:
                return 0;
            default:
                this.LogUnhandledRead(offset);
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(IsBackupRegister(offset) || IsStored(offset))
            {
                words[offset / 4] = value;
                return;
            }
            switch((Registers)offset)
            {
            case Registers.MonotonicCounter:
                if(words[offset / 4] != uint.MaxValue)
                {
                    words[offset / 4]++;
                }
                break;
            case Registers.ActiveTamperSeed:
            case Registers.StatusClear:
                break;
            default:
                this.LogUnhandledWrite(offset, value);
                break;
            }
        }

        public long Size => 0x400;

        private static bool IsBackupRegister(long offset)
        {
            return offset >= (long)Registers.Backup0 && offset <= (long)Registers.Backup31 && offset % 4 == 0;
        }

        private static bool IsStored(long offset)
        {
            switch((Registers)offset)
            {
            case Registers.Control1:
            case Registers.Control2:
            case Registers.Control3:
            case Registers.FilterControl:
            case Registers.ActiveTamperControl1:
            case Registers.ActiveTamperControl2:
            case Registers.SecureConfiguration:
            case Registers.PrivilegeConfiguration:
            case Registers.InterruptEnable:
            case Registers.EraseConfiguration:
                return true;
            default:
                return false;
            }
        }

        private void ResetBackupDomain()
        {
            for(var i = 0; i < words.Length; i++)
            {
                words[i] = 0;
            }
        }

        private bool backupDomainResetLine;

        private readonly uint[] words;

        private enum Registers
        {
            Control1 = 0x00,                    // TAMP_CR1
            Control2 = 0x04,                    // TAMP_CR2
            Control3 = 0x08,                    // TAMP_CR3
            FilterControl = 0x0C,               // TAMP_FLTCR
            ActiveTamperControl1 = 0x10,        // TAMP_ATCR1
            ActiveTamperSeed = 0x14,            // TAMP_ATSEEDR
            ActiveTamperOutput = 0x18,          // TAMP_ATOR
            ActiveTamperControl2 = 0x1C,        // TAMP_ATCR2
            SecureConfiguration = 0x20,         // TAMP_SECCFGR
            PrivilegeConfiguration = 0x24,      // TAMP_PRIVCFGR
            InterruptEnable = 0x2C,             // TAMP_IER
            Status = 0x30,                      // TAMP_SR
            MaskedInterruptStatus = 0x34,       // TAMP_MISR
            SecureMaskedInterruptStatus = 0x38, // TAMP_SMISR
            StatusClear = 0x3C,                 // TAMP_SCR
            MonotonicCounter = 0x40,            // TAMP_COUNTR
            EraseConfiguration = 0x54,          // TAMP_ERCFGR
            Backup0 = 0x100,                    // TAMP_BKP0R
            Backup31 = 0x17C,                   // TAMP_BKP31R
        }
    }
}
