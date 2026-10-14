//
// Reset and clock control (RCC) of the STM32U5 series, written from ST RM0456
// (chapter "Reset and clock control (RCC)"). Register offsets and bit
// positions follow that chapter and ST's CMSIS device headers stm32u575xx.h
// and stm32u595xx.h.
//
// What the model does:
//  - Every oscillator and PLL enable bit is mirrored into its ready bit: MSIS,
//    MSIK, HSI16, HSI48, SHSI, HSE, PLL1, PLL2 and PLL3 in RCC_CR; LSE, LSESYS
//    (LSESYSEN together with LSEON) and LSI in RCC_BDCR.
//  - CFGR1.SWS mirrors CFGR1.SW for all four values (MSIS, HSI16, HSE, PLL1).
//  - Every other register stores what software writes. No clock tree is
//    simulated: peripherals take their frequencies from the platform file.
//  - RCC_BDCR lives in the backup domain: a system reset leaves it as it is,
//    a power-on reset clears it, and so does BDCR.BDRST, which holds the
//    backup domain in reset while it is set. Both drive the BackupDomainReset
//    output, which the platform connects to the other backup-domain blocks
//    (the RTC and TAMP): a power-on reset pulses it; BDRST holds it high.
//  - The reset flags of RCC_CSR survive system resets, as on silicon, until
//    software sets RMVF. Each machine reset adds the flags of its cause
//    (RM0456 "Reset": an internal reset also drives NRST, so PINRSTF comes
//    with it). The cause is, in order of precedence: the one announced with
//    NextResetCause; Software, when the NVIC's SystemResetRequest line is
//    connected to input 0 and the core sets AIRCR.SYSRESETREQ; otherwise
//    UnannouncedResetCause. The first reset after the model is created is a
//    power-on reset (BORRSTF and PINRSTF only).
//
// Not modelled: clock security system, clock interrupts (CIFR reads 0), the
// MSI PLL mode, peripheral resets through the *RSTR registers (stored only),
// and the reset values of the clock-enable registers (they read 0 until
// written).
//
using System;

using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure.Registers;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Jig.ST
{
    [AllowedTranslations(AllowedTranslation.ByteToDoubleWord | AllowedTranslation.WordToDoubleWord)]
    public class STM32U5_RCC : BasicDoubleWordPeripheral, IKnownSize, IGPIOReceiver
    {
        public STM32U5_RCC(IMachine machine) : base(machine)
        {
            BackupDomainReset = new GPIO();
            DefineRegisters();
            Reset();
        }

        public override void Reset()
        {
            var cause = poweredOn ? (announcedCause ?? UnannouncedResetCause) : ResetCause.PowerOn;
            poweredOn = true;
            announcedCause = null;

            base.Reset();

            if(cause == ResetCause.PowerOn)
            {
                resetFlags = PinResetFlag | BrownoutResetFlag;
                ResetBackupDomain();
            }
            else
            {
                resetFlags |= PinResetFlag | FlagFor(cause);
            }
            LastResetCause = cause;
            this.Log(LogLevel.Debug, "Reset cause {0}, CSR reset flags 0x{1:X8}", cause, resetFlags);
        }

        // Input 0: the NVIC's SystemResetRequest line (AIRCR.SYSRESETREQ).
        public void OnGPIO(int number, bool value)
        {
            if(number != 0 || !value)
            {
                return;
            }
            this.Log(LogLevel.Info, "Software reset requested through AIRCR.SYSRESETREQ");
            announcedCause = ResetCause.Software;
            machine.RequestReset();
        }

        // The cause the next machine reset reports in RCC_CSR. Cleared by that reset.
        public ResetCause NextResetCause
        {
            get => announcedCause ?? UnannouncedResetCause;
            set => announcedCause = value;
        }

        // The cause reported for a machine reset nobody announced. With the
        // NVIC's reset line connected here, the only peripheral left that resets
        // a machine on its own is the independent watchdog.
        public ResetCause UnannouncedResetCause { get; set; } = ResetCause.IndependentWatchdog;

        public ResetCause LastResetCause { get; private set; }

        // RCC_CSR bits 31:25 as they read now.
        public uint ResetFlags
        {
            get => resetFlags;
            set => resetFlags = value & ResetFlagsMask;
        }

        // High while the backup domain is in reset: pulsed by a power-on reset,
        // held by RCC_BDCR.BDRST.
        public GPIO BackupDomainReset { get; }

        public long Size => 0x400;

        private static uint FlagFor(ResetCause cause)
        {
            switch(cause)
            {
            case ResetCause.PowerOn:
                return BrownoutResetFlag;
            case ResetCause.Pin:
                return 0;
            case ResetCause.Software:
                return SoftwareResetFlag;
            case ResetCause.IndependentWatchdog:
                return IndependentWatchdogResetFlag;
            case ResetCause.WindowWatchdog:
                return WindowWatchdogResetFlag;
            case ResetCause.LowPower:
                return LowPowerResetFlag;
            case ResetCause.OptionByteLoad:
                return OptionByteLoaderResetFlag;
            default:
                throw new ArgumentException($"Unknown reset cause {cause}");
            }
        }

        // Power-on: RCC_BDCR returns to 0 and the other backup-domain blocks
        // get a reset pulse.
        private void ResetBackupDomain()
        {
            backupDomainControl = 0;
            BackupDomainReset.Set(false);
            BackupDomainReset.Set(true);
            BackupDomainReset.Set(false);
        }

        private uint ReadBackupDomainControl()
        {
            var value = backupDomainControl;
            if((value & LseOn) != 0)
            {
                value |= LseReady;
                if((value & LseSystemEnable) != 0)
                {
                    value |= LseSystemReady;
                }
            }
            if((value & LsiOn) != 0)
            {
                value |= LsiReady;
            }
            return value;
        }

        private void WriteBackupDomainControl(uint value)
        {
            if((value & BackupDomainSoftwareReset) != 0)
            {
                // BDRST resets the whole backup domain, this register included,
                // and holds it in reset until software clears the bit again.
                if((backupDomainControl & BackupDomainSoftwareReset) == 0)
                {
                    this.Log(LogLevel.Debug, "Backup domain reset (BDCR.BDRST)");
                }
                backupDomainControl = BackupDomainSoftwareReset;
                BackupDomainReset.Set(true);
                return;
            }
            backupDomainControl = value & BackupDomainControlWritable;
            BackupDomainReset.Set(false);
        }

        private void DefineRegisters()
        {
            // RCC_CR: reset value 0x00000035 (MSIS and MSIK on and ready)
            Registers.ClockControl.Define(this, 0x00000035)
                .WithFlag(0, out var msisOn, name: "MSISON")
                .WithFlag(1, name: "MSIKERON")
                .WithFlag(2, FieldMode.Read, valueProviderCallback: _ => msisOn.Value, name: "MSISRDY")
                .WithFlag(3, name: "MSIPLLEN")
                .WithFlag(4, out var msikOn, name: "MSIKON")
                .WithFlag(5, FieldMode.Read, valueProviderCallback: _ => msikOn.Value, name: "MSIKRDY")
                .WithFlag(6, name: "MSIPLLSEL")
                .WithFlag(7, name: "MSIPLLFAST")
                .WithFlag(8, out var hsiOn, name: "HSION")
                .WithFlag(9, name: "HSIKERON")
                .WithFlag(10, FieldMode.Read, valueProviderCallback: _ => hsiOn.Value, name: "HSIRDY")
                .WithReservedBits(11, 1)
                .WithFlag(12, out var hsi48On, name: "HSI48ON")
                .WithFlag(13, FieldMode.Read, valueProviderCallback: _ => hsi48On.Value, name: "HSI48RDY")
                .WithFlag(14, out var shsiOn, name: "SHSION")
                .WithFlag(15, FieldMode.Read, valueProviderCallback: _ => shsiOn.Value, name: "SHSIRDY")
                .WithFlag(16, out var hseOn, name: "HSEON")
                .WithFlag(17, FieldMode.Read, valueProviderCallback: _ => hseOn.Value, name: "HSERDY")
                .WithFlag(18, name: "HSEBYP")
                .WithFlag(19, name: "CSSON")
                .WithFlag(20, name: "HSEEXT")
                .WithReservedBits(21, 3)
                .WithFlag(24, out var pll1On, name: "PLL1ON")
                .WithFlag(25, FieldMode.Read, valueProviderCallback: _ => pll1On.Value, name: "PLL1RDY")
                .WithFlag(26, out var pll2On, name: "PLL2ON")
                .WithFlag(27, FieldMode.Read, valueProviderCallback: _ => pll2On.Value, name: "PLL2RDY")
                .WithFlag(28, out var pll3On, name: "PLL3ON")
                .WithFlag(29, FieldMode.Read, valueProviderCallback: _ => pll3On.Value, name: "PLL3RDY")
                .WithReservedBits(30, 2);

            // Calibration and trimming registers: stored. ICSCR1 reset selects
            // range 4 for MSIS and MSIK; ICSCR2 holds the MSI trims at mid-scale;
            // ICSCR3 holds HSITRIM = 16.
            Registers.InternalClockSourcesCalibration1.Define(this, 0x44000000)
                .WithValueField(0, 32, name: "ICSCR1");
            Registers.InternalClockSourcesCalibration2.Define(this, 0x00084210)
                .WithValueField(0, 32, name: "ICSCR2");
            Registers.InternalClockSourcesCalibration3.Define(this, 0x00100000)
                .WithValueField(0, 32, name: "ICSCR3");
            Registers.ClockRecoveryRc.Define(this)
                .WithValueField(0, 32, name: "CRRCR");

            // RCC_CFGR1: SWS follows SW
            Registers.ClockConfiguration1.Define(this)
                .WithValueField(0, 2, out var systemClockSwitch, name: "SW")
                .WithValueField(2, 2, FieldMode.Read, valueProviderCallback: _ => systemClockSwitch.Value, name: "SWS")
                .WithFlag(4, name: "STOPWUCK")
                .WithFlag(5, name: "STOPKERWUCK")
                .WithReservedBits(6, 18)
                .WithValueField(24, 4, name: "MCOSEL")
                .WithValueField(28, 3, name: "MCOPRE")
                .WithReservedBits(31, 1);

            Registers.ClockConfiguration2.Define(this)
                .WithValueField(0, 32, name: "CFGR2");
            Registers.ClockConfiguration3.Define(this)
                .WithValueField(0, 32, name: "CFGR3");

            Registers.Pll1Configuration.Define(this)
                .WithValueField(0, 32, name: "PLL1CFGR");
            Registers.Pll2Configuration.Define(this)
                .WithValueField(0, 32, name: "PLL2CFGR");
            Registers.Pll3Configuration.Define(this)
                .WithValueField(0, 32, name: "PLL3CFGR");
            Registers.Pll1Dividers.Define(this, 0x01010280)
                .WithValueField(0, 32, name: "PLL1DIVR");
            Registers.Pll1FractionalDivider.Define(this)
                .WithValueField(0, 32, name: "PLL1FRACR");
            Registers.Pll2Dividers.Define(this, 0x01010280)
                .WithValueField(0, 32, name: "PLL2DIVR");
            Registers.Pll2FractionalDivider.Define(this)
                .WithValueField(0, 32, name: "PLL2FRACR");
            Registers.Pll3Dividers.Define(this, 0x01010280)
                .WithValueField(0, 32, name: "PLL3DIVR");
            Registers.Pll3FractionalDivider.Define(this)
                .WithValueField(0, 32, name: "PLL3FRACR");

            // Clock interrupts: enables stored, no flag is ever raised
            Registers.ClockInterruptEnable.Define(this)
                .WithValueField(0, 32, name: "CIER");
            Registers.ClockInterruptFlag.Define(this)
                .WithValueField(0, 32, FieldMode.Read, valueProviderCallback: _ => 0, name: "CIFR");
            Registers.ClockInterruptClear.Define(this)
                .WithValueField(0, 32, FieldMode.Write, name: "CICR");

            // Peripheral resets, clock enables, sleep/stop clock enables,
            // autonomous mode and kernel clock selection: stored
            foreach(var register in StoredRegisters)
            {
                register.Define(this)
                    .WithValueField(0, 32, name: register.ToString());
            }

            // RCC_BDCR: kept in backupDomainControl, which Reset leaves alone.
            // LSEON (0), LSEBYP (2), LSEDRV (4:3), LSECSSON (5), LSESYSEN (7),
            // RTCSEL (9:8), LSEGFON (12), RTCEN (15), BDRST (16), LSCOEN (24),
            // LSCOSEL (25), LSION (26) and LSIPREDIV (28) store what software
            // writes; LSERDY (1), LSESYSRDY (11) and LSIRDY (27) follow their
            // enables; LSECSSD (6) reads 0.
            Registers.BackupDomainControl.Define(this)
                .WithValueField(0, 32, valueProviderCallback: _ => ReadBackupDomainControl(),
                    writeCallback: (_, value) => WriteBackupDomainControl((uint)value), name: "BDCR");

            // RCC_CSR: MSI ranges after standby stored, reset flags kept in
            // resetFlags, RMVF clears them
            Registers.ControlStatus.Define(this, 0x00004400)
                .WithReservedBits(0, 8)
                .WithValueField(8, 4, name: "MSIKSRANGE")
                .WithValueField(12, 4, name: "MSISSRANGE")
                .WithReservedBits(16, 7)
                .WithFlag(23, FieldMode.Read | FieldMode.Write, valueProviderCallback: _ => false,
                    writeCallback: (_, value) =>
                    {
                        if(value)
                        {
                            resetFlags = 0;
                        }
                    }, name: "RMVF")
                .WithReservedBits(24, 1)
                .WithValueField(25, 7, FieldMode.Read, valueProviderCallback: _ => resetFlags >> 25,
                    name: "OBLRSTF/PINRSTF/BORRSTF/SFTRSTF/IWDGRSTF/WWDGRSTF/LPWRRSTF");

            Registers.SecureConfiguration.Define(this)
                .WithValueField(0, 32, name: "SECCFGR");
            Registers.PrivilegeConfiguration.Define(this)
                .WithValueField(0, 32, name: "PRIVCFGR");
        }

        private bool poweredOn;
        private ResetCause? announcedCause;
        private uint resetFlags;
        private uint backupDomainControl;

        private const uint OptionByteLoaderResetFlag = 1u << 25;
        private const uint PinResetFlag = 1u << 26;
        private const uint BrownoutResetFlag = 1u << 27;
        private const uint SoftwareResetFlag = 1u << 28;
        private const uint IndependentWatchdogResetFlag = 1u << 29;
        private const uint WindowWatchdogResetFlag = 1u << 30;
        private const uint LowPowerResetFlag = 1u << 31;
        private const uint ResetFlagsMask = 0xFE000000;

        private const uint LseOn = 1u << 0;
        private const uint LseReady = 1u << 1;
        private const uint LseSystemEnable = 1u << 7;
        private const uint LseSystemReady = 1u << 11;
        private const uint BackupDomainSoftwareReset = 1u << 16;
        private const uint LsiOn = 1u << 26;
        private const uint LsiReady = 1u << 27;
        // LSEON, LSEBYP, LSEDRV, LSECSSON, LSESYSEN, RTCSEL, LSEGFON, bits 14:13,
        // RTCEN, LSCOEN, LSCOSEL, LSION, LSIPREDIV
        private const uint BackupDomainControlWritable = 0x1700F3BD;

        private static readonly Registers[] StoredRegisters =
        {
            Registers.Ahb1Reset, Registers.Ahb2Reset1, Registers.Ahb2Reset2, Registers.Ahb3Reset,
            Registers.Apb1Reset1, Registers.Apb1Reset2, Registers.Apb2Reset, Registers.Apb3Reset,
            Registers.Ahb1Enable, Registers.Ahb2Enable1, Registers.Ahb2Enable2, Registers.Ahb3Enable,
            Registers.Apb1Enable1, Registers.Apb1Enable2, Registers.Apb2Enable, Registers.Apb3Enable,
            Registers.Ahb1SleepEnable, Registers.Ahb2SleepEnable1, Registers.Ahb2SleepEnable2, Registers.Ahb3SleepEnable,
            Registers.Apb1SleepEnable1, Registers.Apb1SleepEnable2, Registers.Apb2SleepEnable, Registers.Apb3SleepEnable,
            Registers.AutonomousMode,
            Registers.PeripheralsIndependentClockConfiguration1,
            Registers.PeripheralsIndependentClockConfiguration2,
            Registers.PeripheralsIndependentClockConfiguration3,
        };

        public enum ResetCause
        {
            PowerOn,
            Pin,
            Software,
            IndependentWatchdog,
            WindowWatchdog,
            LowPower,
            OptionByteLoad,
        }

        private enum Registers
        {
            ClockControl = 0x00,                              // RCC_CR
            InternalClockSourcesCalibration1 = 0x08,          // RCC_ICSCR1
            InternalClockSourcesCalibration2 = 0x0C,          // RCC_ICSCR2
            InternalClockSourcesCalibration3 = 0x10,          // RCC_ICSCR3
            ClockRecoveryRc = 0x14,                           // RCC_CRRCR
            ClockConfiguration1 = 0x1C,                       // RCC_CFGR1
            ClockConfiguration2 = 0x20,                       // RCC_CFGR2
            ClockConfiguration3 = 0x24,                       // RCC_CFGR3
            Pll1Configuration = 0x28,                         // RCC_PLL1CFGR
            Pll2Configuration = 0x2C,                         // RCC_PLL2CFGR
            Pll3Configuration = 0x30,                         // RCC_PLL3CFGR
            Pll1Dividers = 0x34,                              // RCC_PLL1DIVR
            Pll1FractionalDivider = 0x38,                     // RCC_PLL1FRACR
            Pll2Dividers = 0x3C,                              // RCC_PLL2DIVR
            Pll2FractionalDivider = 0x40,                     // RCC_PLL2FRACR
            Pll3Dividers = 0x44,                              // RCC_PLL3DIVR
            Pll3FractionalDivider = 0x48,                     // RCC_PLL3FRACR
            ClockInterruptEnable = 0x50,                      // RCC_CIER
            ClockInterruptFlag = 0x54,                        // RCC_CIFR
            ClockInterruptClear = 0x58,                       // RCC_CICR
            Ahb1Reset = 0x60,                                 // RCC_AHB1RSTR
            Ahb2Reset1 = 0x64,                                // RCC_AHB2RSTR1
            Ahb2Reset2 = 0x68,                                // RCC_AHB2RSTR2
            Ahb3Reset = 0x6C,                                 // RCC_AHB3RSTR
            Apb1Reset1 = 0x74,                                // RCC_APB1RSTR1
            Apb1Reset2 = 0x78,                                // RCC_APB1RSTR2
            Apb2Reset = 0x7C,                                 // RCC_APB2RSTR
            Apb3Reset = 0x80,                                 // RCC_APB3RSTR
            Ahb1Enable = 0x88,                                // RCC_AHB1ENR
            Ahb2Enable1 = 0x8C,                               // RCC_AHB2ENR1
            Ahb2Enable2 = 0x90,                               // RCC_AHB2ENR2
            Ahb3Enable = 0x94,                                // RCC_AHB3ENR
            Apb1Enable1 = 0x9C,                               // RCC_APB1ENR1
            Apb1Enable2 = 0xA0,                               // RCC_APB1ENR2
            Apb2Enable = 0xA4,                                // RCC_APB2ENR
            Apb3Enable = 0xA8,                                // RCC_APB3ENR
            Ahb1SleepEnable = 0xB0,                           // RCC_AHB1SMENR
            Ahb2SleepEnable1 = 0xB4,                          // RCC_AHB2SMENR1
            Ahb2SleepEnable2 = 0xB8,                          // RCC_AHB2SMENR2
            Ahb3SleepEnable = 0xBC,                           // RCC_AHB3SMENR
            Apb1SleepEnable1 = 0xC4,                          // RCC_APB1SMENR1
            Apb1SleepEnable2 = 0xC8,                          // RCC_APB1SMENR2
            Apb2SleepEnable = 0xCC,                           // RCC_APB2SMENR
            Apb3SleepEnable = 0xD0,                           // RCC_APB3SMENR
            AutonomousMode = 0xD8,                            // RCC_SRDAMR
            PeripheralsIndependentClockConfiguration1 = 0xE0, // RCC_CCIPR1
            PeripheralsIndependentClockConfiguration2 = 0xE4, // RCC_CCIPR2
            PeripheralsIndependentClockConfiguration3 = 0xE8, // RCC_CCIPR3
            BackupDomainControl = 0xF0,                       // RCC_BDCR
            ControlStatus = 0xF4,                             // RCC_CSR
            SecureConfiguration = 0x110,                      // RCC_SECCFGR
            PrivilegeConfiguration = 0x114,                   // RCC_PRIVCFGR
        }
    }
}
