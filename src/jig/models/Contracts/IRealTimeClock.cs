//
// Contract for a real-time clock a test sets and reads (a board file's rtc
// setting).
//
namespace Antmicro.Renode.Peripherals.Jig.Contracts
{
    /// <summary>A calendar clock that counts in simulated time while its
    /// oscillator runs. Times carry no time zone: the clock holds whatever local
    /// time it is given. PowerCycle returns the counters to the device's power-up
    /// date and time.</summary>
    public interface IRealTimeClock : IPeripheral
    {
        /// <summary>Loads the date and time into the counters at the current
        /// simulated instant, bypassing the device's write protection, and the
        /// clock counts on from there. The text is ISO 8601,
        /// "yyyy-MM-ddTHH:mm:ss" with an optional fraction of a second
        /// ("2026-10-08T12:34:56.78"), kept to the device's own resolution.
        /// Throws RecoverableException for text it cannot parse or a date outside
        /// the device's range.</summary>
        void SetDateTime(string isoDateTime);

        /// <summary>The counters as software reads them now:
        /// "yyyy-MM-ddTHH:mm:ss", then "." and the sub-second digits the device
        /// keeps (two for hundredths), none for a clock that counts whole
        /// seconds.</summary>
        string GetDateTime();
    }
}
