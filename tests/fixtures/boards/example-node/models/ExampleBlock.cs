// A board model of the example-node fixture. The unit tests only resolve its
// path into a model set; they do not compile it.
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Example
{
    public class ExampleBlock : IDoubleWordPeripheral
    {
        public ExampleBlock(IMachine machine)
        {
        }

        public uint ReadDoubleWord(long offset)
        {
            return 0;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
        }

        public void Reset()
        {
        }
    }
}
