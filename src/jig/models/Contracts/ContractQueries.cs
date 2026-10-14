//
// The monitor query behind jig.contracts.verify: which board contracts a
// peripheral implements.
//
//   machine JigContracts "qspi.flash0"
//
// answers one line: "missing" when no peripheral has that name, else "found",
// the peripheral's class (without the Antmicro.Renode.Peripherals. prefix) and
// each contract interface it implements. IStorage and IAnalogInputs carry the
// value board.yaml must agree with: "IStorage=<Size>", "IAnalogInputs=<ChannelCount>".
// A name resolves as the monitor resolves it: as written, then with "sysbus."
// in front.
//
using System;
using System.Collections.Generic;
using System.Globalization;
using Antmicro.Renode.Core;

namespace Antmicro.Renode.Peripherals.Jig.Contracts
{
    public static class ContractQueries
    {
        public static string JigContracts(this IMachine machine, string peripheral)
        {
            if(!machine.TryGetByName(peripheral, out IPeripheral found)
               && !machine.TryGetByName(SystemBusPrefix + peripheral, out found))
            {
                return "missing";
            }
            var words = new List<string> { "found", TypeName(found) };
            if(found is IStorage storage)
            {
                words.Add("IStorage=" + storage.Size.ToString(CultureInfo.InvariantCulture));
            }
            if(found is IAnalogInputs inputs)
            {
                words.Add("IAnalogInputs=" + inputs.ChannelCount.ToString(CultureInfo.InvariantCulture));
            }
            if(found is IRealTimeClock)
            {
                words.Add("IRealTimeClock");
            }
            if(found is IPowerCycled)
            {
                words.Add("IPowerCycled");
            }
            if(found is ISpiNorFlash)
            {
                words.Add("ISpiNorFlash");
            }
            return string.Join(" ", words);
        }

        private static string TypeName(IPeripheral peripheral)
        {
            var name = peripheral.GetType().FullName ?? peripheral.GetType().Name;
            return name.StartsWith(PeripheralsNamespace, StringComparison.Ordinal) ? name.Substring(PeripheralsNamespace.Length) : name;
        }

        private const string SystemBusPrefix = "sysbus.";
        private const string PeripheralsNamespace = "Antmicro.Renode.Peripherals.";
    }
}
