//
// Simulated-time stepping in whole nanoseconds, and one-call collection of
// everything the harness ports saw during a step.
//
// The monitor's "emulation RunFor" parses decimal seconds through a double, so
// a step such as 0.3 s can come out one nanosecond short and a long run drifts.
// The harness keeps time as an integer count of nanoseconds and steps with
// these instead:
//     emulation RunForNanoseconds 10000000
//     emulation RunForNanosecondsAndCollect 10000000
//     emulation ElapsedNanoseconds
//
// RunForNanosecondsAndCollect returns, after the step, one line per harness
// port that has pending data (see IHarnessPort), so a step costs the harness
// one monitor round trip whatever the number of UARTs and buses.
//
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Jig.Harness
{
    /// <summary>An external the harness reads after every step.</summary>
    public interface IHarnessPort : IExternal
    {
        /// <summary>Appends one line describing the data seen since the last
        /// collection (the line starts with a kind letter and the port's name)
        /// and clears it; appends nothing when there is no data.</summary>
        void Collect(string name, StringBuilder output);
    }

    public static class SteppingExtensions
    {
        public static void RunForNanoseconds(this Emulation emulation, ulong nanoseconds)
        {
            if(nanoseconds == 0)
            {
                throw new RecoverableException("A step must be at least 1 ns");
            }
            emulation.RunFor(TimeInterval.FromNanoseconds(nanoseconds));
        }

        public static string RunForNanosecondsAndCollect(this Emulation emulation, ulong nanoseconds)
        {
            emulation.RunForNanoseconds(nanoseconds);
            return emulation.CollectHarnessPorts();
        }

        public static string CollectHarnessPorts(this Emulation emulation)
        {
            var output = new StringBuilder();
            foreach(var port in emulation.ExternalsManager.GetExternalsOfType<IHarnessPort>())
            {
                if(emulation.ExternalsManager.TryGetName(port, out var name))
                {
                    port.Collect(name, output);
                }
            }
            return output.ToString();
        }

        public static ulong ElapsedNanoseconds(this Emulation emulation)
        {
            return emulation.MasterTimeSource.ElapsedVirtualTime.Ticks;
        }
    }
}
