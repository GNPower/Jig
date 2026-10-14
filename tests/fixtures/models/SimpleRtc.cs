//
// Test fixture: a calendar clock that counts whole seconds in simulated time
// and implements the IRealTimeClock and IPowerCycled contracts. Its range is
// 2000-01-01 to 2099-12-31; it powers up at 2000-01-01T00:00:00. It has no
// registers: register it on the system bus without an address.
//
//   rtc: JigFixtures.SimpleRtc @ sysbus
//
using System;
using System.Globalization;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Peripherals.Jig.Contracts;

namespace Antmicro.Renode.Peripherals.JigFixtures
{
    public sealed class SimpleRtc : IRealTimeClock, IPowerCycled
    {
        public SimpleRtc(IMachine machine)
        {
            this.machine = machine;
            PowerCycle();
        }

        public void Reset()
        {
        }

        public void PowerCycle()
        {
            Load(PowerUp);
        }

        public void SetDateTime(string isoDateTime)
        {
            if(!DateTimeOffset.TryParse(isoDateTime, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var parsed))
            {
                throw new RecoverableException($"'{isoDateTime}' is not an ISO 8601 date and time");
            }
            var value = parsed.DateTime;
            if(value < PowerUp || value >= End)
            {
                throw new RecoverableException($"{isoDateTime} is outside 2000-01-01 to 2099-12-31");
            }
            Load(value);
        }

        public string GetDateTime()
        {
            var now = Now();
            var seconds = (long)Math.Floor(origin + now);
            return PowerUp.AddSeconds(seconds).ToString("yyyy-MM-dd'T'HH:mm:ss", CultureInfo.InvariantCulture);
        }

        private void Load(DateTime value)
        {
            // The counters hold whole seconds; the fraction is dropped.
            origin = Math.Floor((value - PowerUp).TotalSeconds) - Now();
        }

        private double Now() => machine.ElapsedVirtualTime.TimeElapsed.TotalSeconds;

        private readonly IMachine machine;
        private double origin;

        private static readonly DateTime PowerUp = new DateTime(2000, 1, 1);
        private static readonly DateTime End = new DateTime(2100, 1, 1);
    }
}
