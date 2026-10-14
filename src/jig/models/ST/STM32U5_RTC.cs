//
// Real-time clock (RTC) of the STM32U5 series, written from ST RM0456 (chapter
// "Real-time clock (RTC)"). Register offsets and bit positions follow that
// chapter and ST's CMSIS device headers stm32u575xx.h and stm32u595xx.h.
//
// The RTC lives in the backup domain, and the model keeps its two resets apart:
//  - a system reset (Reset) clears ICSR.INIT, INITF and RSF and locks the
//    write protection; the calendar keeps counting, and the prescalers, CR,
//    the alarms, the wakeup timer and the status flags keep their values;
//  - a backup domain reset (a rising edge on input 0, which the RCC drives on
//    a power-on reset and when RCC_BDCR.BDRST is set) and PowerCycle return
//    every register to its reset value: the calendar starts again at
//    2000-01-01 00:00:00 (DR 0x00002101, TR 0).
//
// Behaviour modelled:
//  - BCD calendar (ICSR.BIN = 00) counting periods of RTCCLK (the frequency
//    parameter, 32768 Hz by default): a second every (PREDIV_A + 1) x
//    (PREDIV_S + 1) periods, SSR counting down from PREDIV_S once every
//    PREDIV_A + 1 periods. 24-hour and AM/PM formats (CR.FMT). A leap year
//    every fourth year, which holds for 2000 to 2099. The week day counts on
//    from whatever software wrote to DR.WDU.
//  - Initialization mode: ICSR.INIT stops the calendar and INITF follows it at
//    once. TR, DR and PRER take writes only in it; ICSR.BIN and BCDU only
//    while INITF is set. Entering and leaving it restart the sub-second count
//    at PREDIV_S. INITS reads 1 while the year is not 00.
//  - Write protection: the WPR keys 0xCA then 0x53 unlock TR, DR, ICSR, PRER,
//    WUTR, CR, CALR, SHIFTR and the alarm registers; any other key locks them
//    again. A write to them while locked is dropped with a warning.
//  - RSF: cleared by software (writing 0), by entering or leaving
//    initialization mode and by a system reset. After a clear it reads 0 once
//    and 1 from then on (on silicon the shadow copy completes two RTCCLK
//    periods later). With CR.BYPSHAD set it reads 0. TR, DR and SSR always
//    read the counters, so a read never sees a stale shadow copy.
//  - CR.ADD1H and SUB1H add and subtract an hour outside initialization mode
//    (SUB1H does nothing at hour 0).
//  - Alarms A and B: ALRMxR with its masks MSK1 to MSK4, WDSEL and PM, compared
//    with the calendar each time its seconds change; ALRAF and ALRBF in SR.
//    ALRMxR and ALRMxSSR take writes while their alarm is disabled or in
//    initialization mode.
//  - Wakeup timer: WUT + 1 periods of the clock CR.WUCKSEL selects (RTCCLK
//    divided by 16, 8, 4 or 2, or ck_spre, with 2^16 added to WUT for 11x),
//    counted from the write that sets WUTE; WUTF in SR. WUTR takes writes only
//    while WUTE is clear, and ICSR.WUTWF reads 1 then.
//  - SR, MISR and SCR for ALRAF, ALRBF and WUTF. IRQ is high while one of them
//    is set together with its enable (ALRAIE, ALRBIE, WUTIE).
//  - IRealTimeClock: SetDateTime and GetDateTime work on the calendar
//    counters; GetDateTime prints as many sub-second digits as the
//    synchronous prescaler resolves (3 for PREDIV_S = 255).
//
// Not modelled: binary and mixed modes (ICSR.BIN other than 00, ALRxBINR and
// SSRUF; those registers store what software writes), sub-second alarm
// comparison (with ALRMxSSR.MASKSS above 0 the alarm fires at the second
// boundary), automatic flag clearing (WUTOCLR, ALRAFCLR, ALRBFCLR),
// timestamps, tamper events, smooth calibration (CALR stored, RECALPF reads
// 0), shift operations (SHIFTR), the calibration and alarm outputs, the secure
// and privileged access rules (SECCFGR and PRIVCFGR stored, SMISR reads 0),
// the hold of the backup domain reset (writes while the RCC holds BDRST are
// taken), and the RCC clock gate: the calendar counts whatever RCC_BDCR.RTCEN
// and RTCSEL hold.
//
using System;
using System.Globalization;

using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Jig.Contracts;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.ST
{
    [AllowedTranslations(AllowedTranslation.ByteToDoubleWord | AllowedTranslation.WordToDoubleWord)]
    public class STM32U5_RTC : IDoubleWordPeripheral, IKnownSize, IGPIOReceiver, IRealTimeClock, IPowerCycled
    {
        public STM32U5_RTC(IMachine machine, ulong frequency = 32768)
        {
            if(frequency == 0)
            {
                throw new ConstructionException("RTCCLK frequency must be greater than 0");
            }
            IRQ = new GPIO();
            alarms = new[] { new Alarm("A", 0), new Alarm("B", 1) };
            // LimitTimer.Value checks a write against the limit the timer was
            // built with, so both are built with the largest limit they can take.
            calendarTimer = new LimitTimer(machine.ClockSource, frequency, this, "calendar", limit: LongestSecond,
                direction: Direction.Ascending, enabled: false, workMode: WorkMode.Periodic, eventEnabled: true);
            calendarTimer.LimitReached += OnSecond;
            wakeupTimer = new LimitTimer(machine.ClockSource, frequency, this, "wakeup", limit: LongestWakeupPeriod,
                direction: Direction.Ascending, enabled: false, workMode: WorkMode.Periodic, eventEnabled: true);
            wakeupTimer.LimitReached += OnWakeup;
            ResetBackupDomain();
        }

        // System reset: the backup domain keeps its state.
        public void Reset()
        {
            if(initMode)
            {
                SetInitMode(false);
            }
            synchronised = false;
            unlocked = false;
            firstKeyWritten = false;
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
            switch((Registers)offset)
            {
            case Registers.Time:
                return ReadTime();
            case Registers.Date:
                return ReadDate();
            case Registers.SubSecond:
                return SubSecond;
            case Registers.InitializationControlAndStatus:
                return ReadInitializationControlAndStatus();
            case Registers.Prescaler:
                return (asynchronousPrescaler << 16) | synchronousPrescaler;
            case Registers.WakeupTimer:
                return wakeupTimerRegister;
            case Registers.Control:
                return control;
            case Registers.PrivilegeConfiguration:
                return privilegeConfiguration;
            case Registers.SecureConfiguration:
                return secureConfiguration;
            case Registers.Calibration:
                return calibration;
            case Registers.WriteProtection:
            case Registers.ShiftControl:
            case Registers.TimestampTime:
            case Registers.TimestampDate:
            case Registers.TimestampSubSecond:
            case Registers.SecureMaskedInterruptStatus:
            case Registers.StatusClear:
                return 0;
            case Registers.AlarmA:
                return alarms[0].Value;
            case Registers.AlarmASubSecond:
                return alarms[0].SubSecondValue;
            case Registers.AlarmB:
                return alarms[1].Value;
            case Registers.AlarmBSubSecond:
                return alarms[1].SubSecondValue;
            case Registers.Status:
                return status;
            case Registers.MaskedInterruptStatus:
                return MaskedStatus;
            case Registers.AlarmABinary:
                return alarms[0].BinaryValue;
            case Registers.AlarmBBinary:
                return alarms[1].BinaryValue;
            default:
                this.LogUnhandledRead(offset);
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch((Registers)offset)
            {
            case Registers.Time:
                if(CanWriteCalendar(Registers.Time))
                {
                    WriteTime(value);
                }
                break;
            case Registers.Date:
                if(CanWriteCalendar(Registers.Date))
                {
                    WriteDate(value);
                }
                break;
            case Registers.InitializationControlAndStatus:
                WriteInitializationControlAndStatus(value);
                break;
            case Registers.Prescaler:
                if(CanWriteCalendar(Registers.Prescaler))
                {
                    asynchronousPrescaler = (value >> 16) & 0x7F;
                    synchronousPrescaler = value & 0x7FFF;
                    calendarTimer.Limit = SecondLength;
                }
                break;
            case Registers.WakeupTimer:
                if(!CheckUnlocked(Registers.WakeupTimer))
                {
                    break;
                }
                if(WakeupTimerEnabled)
                {
                    this.Log(LogLevel.Warning, "WUTR written while the wakeup timer runs (WUTWF = 0); write dropped");
                    break;
                }
                wakeupTimerRegister = value;
                if((value >> 16) != 0)
                {
                    WarnOnce(ref warnedAutoClear, "WUTR.WUTOCLR is not modelled: WUTF stays set until software clears it");
                }
                break;
            case Registers.Control:
                if(CheckUnlocked(Registers.Control))
                {
                    WriteControl(value);
                }
                break;
            case Registers.PrivilegeConfiguration:
                privilegeConfiguration = value;
                break;
            case Registers.SecureConfiguration:
                secureConfiguration = value;
                break;
            case Registers.WriteProtection:
                WriteKey(value & 0xFF);
                break;
            case Registers.Calibration:
                if(CheckUnlocked(Registers.Calibration))
                {
                    calibration = value;
                }
                break;
            case Registers.ShiftControl:
                if(CheckUnlocked(Registers.ShiftControl))
                {
                    WarnOnce(ref warnedShift, "SHIFTR: shift operations are not modelled; write ignored");
                }
                break;
            case Registers.AlarmA:
                WriteAlarm(alarms[0], value, false);
                break;
            case Registers.AlarmASubSecond:
                WriteAlarm(alarms[0], value, true);
                break;
            case Registers.AlarmB:
                WriteAlarm(alarms[1], value, false);
                break;
            case Registers.AlarmBSubSecond:
                WriteAlarm(alarms[1], value, true);
                break;
            case Registers.StatusClear:
                status &= ~(value & StatusMask);
                UpdateInterrupts();
                break;
            case Registers.AlarmABinary:
                if(CheckUnlocked(Registers.AlarmABinary))
                {
                    alarms[0].BinaryValue = value;
                }
                break;
            case Registers.AlarmBBinary:
                if(CheckUnlocked(Registers.AlarmBBinary))
                {
                    alarms[1].BinaryValue = value;
                }
                break;
            default:
                this.LogUnhandledWrite(offset, value);
                break;
            }
        }

        public void SetDateTime(string isoDateTime)
        {
            if(!DateTimeOffset.TryParse(isoDateTime, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var parsed))
            {
                throw new RecoverableException($"'{isoDateTime}' is not an ISO 8601 date and time");
            }
            var value = parsed.DateTime;
            if(value.Year < 2000 || value.Year > 2099)
            {
                throw new RecoverableException($"{isoDateTime} is outside 2000-01-01 to 2099-12-31");
            }
            year = value.Year - 2000;
            month = value.Month;
            day = value.Day;
            hours = value.Hour;
            minutes = value.Minute;
            seconds = value.Second;
            weekDay = value.DayOfWeek == DayOfWeek.Sunday ? 7 : (int)value.DayOfWeek;
            // The fraction, kept to the synchronous prescaler's resolution
            var fraction = (double)(value.Ticks % TimeSpan.TicksPerSecond) / TimeSpan.TicksPerSecond;
            var counts = Math.Min((ulong)Math.Floor(fraction * (synchronousPrescaler + 1)), synchronousPrescaler);
            calendarTimer.Value = counts * (asynchronousPrescaler + 1);
        }

        public string GetDateTime()
        {
            var text = string.Format(CultureInfo.InvariantCulture, "{0:D4}-{1:D2}-{2:D2}T{3:D2}:{4:D2}:{5:D2}",
                2000 + year, month, day, hours, minutes, seconds);
            var steps = (ulong)synchronousPrescaler + 1;
            var digits = 0;
            for(var scale = 1UL; scale < steps; scale *= 10)
            {
                digits++;
            }
            if(digits == 0)
            {
                return text;
            }
            var counts = (ulong)(synchronousPrescaler - SubSecond);
            var fraction = counts * (ulong)Math.Pow(10, digits) / steps;
            return text + "." + fraction.ToString("D" + digits, CultureInfo.InvariantCulture);
        }

        public long Size => 0x400;

        public GPIO IRQ { get; }

        private bool TwelveHourFormat => (control & HourFormat) != 0;

        private bool BypassShadow => (control & BypassShadowRegisters) != 0;

        private bool WakeupTimerEnabled => (control & WakeupTimerEnable) != 0;

        private ulong SecondLength => (ulong)(asynchronousPrescaler + 1) * (synchronousPrescaler + 1);

        private uint SubSecond
        {
            get
            {
                var elapsed = calendarTimer.Value / (asynchronousPrescaler + 1);
                return synchronousPrescaler - (uint)Math.Min(elapsed, synchronousPrescaler);
            }
        }

        private uint MaskedStatus
        {
            get
            {
                var masked = 0u;
                if((status & AlarmAFlag) != 0 && (control & AlarmAInterruptEnable) != 0)
                {
                    masked |= AlarmAFlag;
                }
                if((status & AlarmBFlag) != 0 && (control & AlarmBInterruptEnable) != 0)
                {
                    masked |= AlarmBFlag;
                }
                if((status & WakeupFlag) != 0 && (control & WakeupInterruptEnable) != 0)
                {
                    masked |= WakeupFlag;
                }
                return masked;
            }
        }

        private void ResetBackupDomain()
        {
            seconds = 0;
            minutes = 0;
            hours = 0;
            weekDay = 1;
            day = 1;
            month = 1;
            year = 0;
            asynchronousPrescaler = DefaultAsynchronousPrescaler;
            synchronousPrescaler = DefaultSynchronousPrescaler;
            wakeupTimerRegister = 0xFFFF;
            control = 0;
            privilegeConfiguration = 0;
            secureConfiguration = 0;
            calibration = 0;
            status = 0;
            binaryMode = 0;
            binaryUpdate = 0;
            foreach(var alarm in alarms)
            {
                alarm.Reset();
            }
            initMode = false;
            synchronised = false;
            unlocked = false;
            firstKeyWritten = false;
            wakeupTimer.Enabled = false;
            wakeupTimer.ResetValue();
            calendarTimer.Enabled = false;
            calendarTimer.Limit = SecondLength;
            calendarTimer.ResetValue();
            calendarTimer.Enabled = true;
            UpdateInterrupts();
        }

        private uint ReadTime()
        {
            var hour = hours;
            var afternoon = false;
            if(TwelveHourFormat)
            {
                afternoon = hour >= 12;
                hour %= 12;
                if(hour == 0)
                {
                    hour = 12;
                }
            }
            return (afternoon ? PostMeridiem : 0) | (ToBcd(hour) << 16) | (ToBcd(minutes) << 8) | ToBcd(seconds);
        }

        private void WriteTime(uint value)
        {
            seconds = FromBcd(value & 0x7F);
            minutes = FromBcd((value >> 8) & 0x7F);
            hours = HourFromRegister(value);
        }

        private uint ReadDate()
        {
            return (ToBcd(year) << 16) | ((uint)weekDay << 13) | (ToBcd(month) << 8) | ToBcd(day);
        }

        private void WriteDate(uint value)
        {
            day = FromBcd(value & 0x3F);
            month = FromBcd((value >> 8) & 0x1F);
            weekDay = (int)((value >> 13) & 0x7);
            year = FromBcd((value >> 16) & 0xFF);
        }

        // HT and HU (bits 21:16) and PM (bit 22) of TR or ALRMxR, as a 0-23 hour.
        private int HourFromRegister(uint value)
        {
            var hour = FromBcd((value >> 16) & 0x3F);
            if(TwelveHourFormat)
            {
                hour = hour % 12 + ((value & PostMeridiem) != 0 ? 12 : 0);
            }
            return hour;
        }

        private uint ReadInitializationControlAndStatus()
        {
            var value = (binaryMode << 8) | (binaryUpdate << 10);
            if(!WakeupTimerEnabled)
            {
                value |= WakeupTimerWriteFlag;
            }
            if(year != 0)
            {
                value |= InitializationStatus;
            }
            if(ReadSynchronisationFlag())
            {
                value |= RegistersSynchronised;
            }
            if(initMode)
            {
                value |= InitializationFlag | Initialization;
            }
            return value;
        }

        // RSF: 0 in initialization mode and with BYPSHAD; after a clear, 0 once and then 1.
        private bool ReadSynchronisationFlag()
        {
            if(initMode || BypassShadow)
            {
                return false;
            }
            if(!synchronised)
            {
                synchronised = true;
                return false;
            }
            return true;
        }

        private void WriteInitializationControlAndStatus(uint value)
        {
            // RSF is cleared by writing 0 (rc_w0)
            if((value & RegistersSynchronised) == 0)
            {
                synchronised = false;
            }
            var enterInit = (value & Initialization) != 0;
            var newBinaryMode = (value >> 8) & 0x3;
            var newBinaryUpdate = (value >> 10) & 0x7;
            // BIN and BCDU take writes only while INITF is set
            var binaryChange = initMode && (newBinaryMode != binaryMode || newBinaryUpdate != binaryUpdate);
            if(enterInit == initMode && !binaryChange)
            {
                return;
            }
            if(!CheckUnlocked(Registers.InitializationControlAndStatus))
            {
                return;
            }
            if(binaryChange)
            {
                binaryMode = newBinaryMode;
                binaryUpdate = newBinaryUpdate;
                if(binaryMode != 0)
                {
                    WarnOnce(ref warnedBinaryMode, "ICSR.BIN selects a binary mode, which is not modelled: the calendar stays in BCD");
                }
            }
            if(enterInit != initMode)
            {
                SetInitMode(enterInit);
            }
        }

        private void SetInitMode(bool value)
        {
            initMode = value;
            synchronised = false;
            calendarTimer.Enabled = false;
            calendarTimer.ResetValue();
            if(!value)
            {
                calendarTimer.Enabled = true;
            }
            this.Log(LogLevel.Debug, value ? "Initialization mode" : "Calendar running");
        }

        private void WriteControl(uint value)
        {
            var wasEnabled = WakeupTimerEnabled;
            // ADD1H and SUB1H always read 0
            control = value & ~(AddOneHour | SubtractOneHour);
            if(!initMode)
            {
                if((value & AddOneHour) != 0)
                {
                    AdvanceHour();
                }
                if((value & SubtractOneHour) != 0 && hours > 0)
                {
                    hours--;
                }
            }
            if(WakeupTimerEnabled && !wasEnabled)
            {
                StartWakeupTimer();
            }
            else if(!WakeupTimerEnabled && wasEnabled)
            {
                wakeupTimer.Enabled = false;
            }
            UpdateInterrupts();
        }

        private void StartWakeupTimer()
        {
            var selection = control & 0x7;
            ulong periods = (wakeupTimerRegister & 0xFFFF) + 1UL;
            ulong clockDivider;
            if(selection < 4)
            {
                // 000: RTCCLK / 16, 001: / 8, 010: / 4, 011: / 2
                clockDivider = 16UL >> (int)selection;
            }
            else
            {
                // 10x: ck_spre; 11x: ck_spre with 2^16 added to WUT
                clockDivider = SecondLength;
                if(selection >= 6)
                {
                    periods += 0x10000;
                }
            }
            wakeupTimer.Enabled = false;
            wakeupTimer.Limit = periods * clockDivider;
            wakeupTimer.ResetValue();
            wakeupTimer.Enabled = true;
        }

        private void WriteAlarm(Alarm alarm, uint value, bool subSecond)
        {
            var register = subSecond
                ? (alarm.Index == 0 ? Registers.AlarmASubSecond : Registers.AlarmBSubSecond)
                : (alarm.Index == 0 ? Registers.AlarmA : Registers.AlarmB);
            if(!CheckUnlocked(register))
            {
                return;
            }
            if((control & alarm.EnableBit) != 0 && !initMode)
            {
                this.Log(LogLevel.Warning, "{0} written while alarm {1} is enabled; write dropped", register, alarm.Name);
                return;
            }
            if(subSecond)
            {
                alarm.SubSecondValue = value;
                if(((value >> 24) & 0xF) != 0)
                {
                    WarnOnce(ref warnedSubSecondAlarm, "ALRMxSSR.MASKSS > 0: sub-second alarm comparison is not modelled; the alarm fires at the second boundary");
                }
            }
            else
            {
                alarm.Value = value;
            }
        }

        private void WriteKey(uint key)
        {
            if(key == FirstKey && !firstKeyWritten)
            {
                firstKeyWritten = true;
            }
            else if(key == SecondKey && firstKeyWritten)
            {
                unlocked = true;
                firstKeyWritten = false;
            }
            else
            {
                unlocked = false;
                firstKeyWritten = false;
            }
        }

        private bool CheckUnlocked(Registers register)
        {
            if(unlocked)
            {
                return true;
            }
            this.Log(LogLevel.Warning, "{0} is write protected (unlock it with WPR keys 0xCA, 0x53); write dropped", register);
            return false;
        }

        private bool CanWriteCalendar(Registers register)
        {
            if(!CheckUnlocked(register))
            {
                return false;
            }
            if(!initMode)
            {
                this.Log(LogLevel.Warning, "{0} takes writes only in initialization mode (ICSR.INIT); write dropped", register);
                return false;
            }
            return true;
        }

        private void OnSecond()
        {
            if(++seconds >= 60)
            {
                seconds = 0;
                if(++minutes >= 60)
                {
                    minutes = 0;
                    AdvanceHour();
                }
            }
            CheckAlarms();
        }

        private void AdvanceHour()
        {
            if(++hours < 24)
            {
                return;
            }
            hours = 0;
            weekDay = weekDay % 7 + 1;
            if(++day <= DaysInMonth(month, year))
            {
                return;
            }
            day = 1;
            if(++month <= 12)
            {
                return;
            }
            month = 1;
            year = (year + 1) % 100;
        }

        private void CheckAlarms()
        {
            foreach(var alarm in alarms)
            {
                if((control & alarm.EnableBit) != 0 && AlarmMatches(alarm.Value))
                {
                    status |= alarm.FlagBit;
                }
            }
            UpdateInterrupts();
        }

        private bool AlarmMatches(uint alarm)
        {
            if((alarm & MaskSeconds) == 0 && FromBcd(alarm & 0x7F) != seconds)
            {
                return false;
            }
            if((alarm & MaskMinutes) == 0 && FromBcd((alarm >> 8) & 0x7F) != minutes)
            {
                return false;
            }
            if((alarm & MaskHours) == 0 && HourFromRegister(alarm) != hours)
            {
                return false;
            }
            if((alarm & MaskDate) == 0)
            {
                if((alarm & WeekDaySelection) != 0)
                {
                    return (int)((alarm >> 24) & 0xF) == weekDay;
                }
                return FromBcd((alarm >> 24) & 0x3F) == day;
            }
            return true;
        }

        private void OnWakeup()
        {
            status |= WakeupFlag;
            UpdateInterrupts();
        }

        private void UpdateInterrupts()
        {
            IRQ.Set(MaskedStatus != 0);
        }

        private void WarnOnce(ref bool warned, string message)
        {
            if(!warned)
            {
                this.Log(LogLevel.Warning, message);
                warned = true;
            }
        }

        private static int DaysInMonth(int month, int year)
        {
            switch(month)
            {
            case 2:
                return year % 4 == 0 ? 29 : 28;
            case 4:
            case 6:
            case 9:
            case 11:
                return 30;
            default:
                return 31;
            }
        }

        private static uint ToBcd(int value)
        {
            return (uint)((value / 10) << 4 | value % 10);
        }

        private static int FromBcd(uint value)
        {
            return (int)((value >> 4) * 10 + (value & 0xF));
        }

        private int seconds;
        private int minutes;
        private int hours;
        private int weekDay;
        private int day;
        private int month;
        private int year;
        private uint asynchronousPrescaler;
        private uint synchronousPrescaler;
        private uint wakeupTimerRegister;
        private uint control;
        private uint privilegeConfiguration;
        private uint secureConfiguration;
        private uint calibration;
        private uint status;
        private uint binaryMode;
        private uint binaryUpdate;
        private bool initMode;
        private bool synchronised;
        private bool unlocked;
        private bool firstKeyWritten;
        private bool backupDomainResetLine;
        private bool warnedAutoClear;
        private bool warnedShift;
        private bool warnedBinaryMode;
        private bool warnedSubSecondAlarm;

        private readonly Alarm[] alarms;
        private readonly LimitTimer calendarTimer;
        private readonly LimitTimer wakeupTimer;

        private const uint DefaultAsynchronousPrescaler = 0x7F;
        private const uint DefaultSynchronousPrescaler = 0xFF;
        // (PREDIV_A + 1) x (PREDIV_S + 1) at most: 2^7 x 2^15 RTCCLK periods
        private const ulong LongestSecond = 1UL << 22;
        // (WUT + 1 + 2^16) ck_spre periods at most
        private const ulong LongestWakeupPeriod = (1UL << 17) * LongestSecond;
        private const uint FirstKey = 0xCA;
        private const uint SecondKey = 0x53;

        private const uint PostMeridiem = 1u << 22;              // TR.PM, ALRMxR.PM

        private const uint WakeupTimerWriteFlag = 1u << 2;       // ICSR.WUTWF
        private const uint InitializationStatus = 1u << 4;       // ICSR.INITS
        private const uint RegistersSynchronised = 1u << 5;      // ICSR.RSF
        private const uint InitializationFlag = 1u << 6;         // ICSR.INITF
        private const uint Initialization = 1u << 7;             // ICSR.INIT

        private const uint BypassShadowRegisters = 1u << 5;      // CR.BYPSHAD
        private const uint HourFormat = 1u << 6;                 // CR.FMT
        private const uint WakeupTimerEnable = 1u << 10;         // CR.WUTE
        private const uint AlarmAInterruptEnable = 1u << 12;     // CR.ALRAIE
        private const uint AlarmBInterruptEnable = 1u << 13;     // CR.ALRBIE
        private const uint WakeupInterruptEnable = 1u << 14;     // CR.WUTIE
        private const uint AddOneHour = 1u << 16;                // CR.ADD1H
        private const uint SubtractOneHour = 1u << 17;           // CR.SUB1H

        private const uint MaskSeconds = 1u << 7;                // ALRMxR.MSK1
        private const uint MaskMinutes = 1u << 15;               // ALRMxR.MSK2
        private const uint MaskHours = 1u << 23;                 // ALRMxR.MSK3
        private const uint WeekDaySelection = 1u << 30;          // ALRMxR.WDSEL
        private const uint MaskDate = 1u << 31;                  // ALRMxR.MSK4

        private const uint AlarmAFlag = 1u << 0;                 // SR.ALRAF
        private const uint AlarmBFlag = 1u << 1;                 // SR.ALRBF
        private const uint WakeupFlag = 1u << 2;                 // SR.WUTF
        private const uint StatusMask = AlarmAFlag | AlarmBFlag | WakeupFlag;

        private sealed class Alarm
        {
            public Alarm(string name, int index)
            {
                Name = name;
                Index = index;
                EnableBit = 1u << (8 + index);  // CR.ALRAE, ALRBE
                FlagBit = 1u << index;          // SR.ALRAF, ALRBF
            }

            public void Reset()
            {
                Value = 0;
                SubSecondValue = 0;
                BinaryValue = 0;
            }

            public string Name { get; }
            public int Index { get; }
            public uint EnableBit { get; }
            public uint FlagBit { get; }
            public uint Value { get; set; }
            public uint SubSecondValue { get; set; }
            public uint BinaryValue { get; set; }
        }

        private enum Registers
        {
            Time = 0x00,                        // RTC_TR
            Date = 0x04,                        // RTC_DR
            SubSecond = 0x08,                   // RTC_SSR
            InitializationControlAndStatus = 0x0C, // RTC_ICSR
            Prescaler = 0x10,                   // RTC_PRER
            WakeupTimer = 0x14,                 // RTC_WUTR
            Control = 0x18,                     // RTC_CR
            PrivilegeConfiguration = 0x1C,      // RTC_PRIVCFGR
            SecureConfiguration = 0x20,         // RTC_SECCFGR
            WriteProtection = 0x24,             // RTC_WPR
            Calibration = 0x28,                 // RTC_CALR
            ShiftControl = 0x2C,                // RTC_SHIFTR
            TimestampTime = 0x30,               // RTC_TSTR
            TimestampDate = 0x34,               // RTC_TSDR
            TimestampSubSecond = 0x38,          // RTC_TSSSR
            AlarmA = 0x40,                      // RTC_ALRMAR
            AlarmASubSecond = 0x44,             // RTC_ALRMASSR
            AlarmB = 0x48,                      // RTC_ALRMBR
            AlarmBSubSecond = 0x4C,             // RTC_ALRMBSSR
            Status = 0x50,                      // RTC_SR
            MaskedInterruptStatus = 0x54,       // RTC_MISR
            SecureMaskedInterruptStatus = 0x58, // RTC_SMISR
            StatusClear = 0x5C,                 // RTC_SCR
            AlarmABinary = 0x70,                // RTC_ALRABINR
            AlarmBBinary = 0x74,                // RTC_ALRBBINR
        }
    }
}
