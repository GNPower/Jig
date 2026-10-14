//
// Contract between the Jig QSPI controller models and serial NOR flash
// die models.
//
using Antmicro.Renode.Peripherals.SPI;

namespace Antmicro.Renode.Peripherals.Jig.Contracts
{
    /// <summary>A serial NOR flash die. Through ISPIPeripheral it answers one
    /// byte per Transmit and ends a command at FinishTransmission (chip select
    /// high); PowerCycle (IPowerCycled) clears its volatile state.</summary>
    public interface ISpiNorFlash : ISPIPeripheral, IPowerCycled
    {
        /// <summary>Array size in bytes.</summary>
        long Size { get; }

        /// <summary>Data lines used by the byte currently being clocked (1, 2 or
        /// 4), so a controller can time quad data phases correctly.</summary>
        int CurrentDataLines { get; }

        /// <summary>One complete read transaction as a linear (memory-mapped)
        /// controller issues it: <paramref name="opcode"/> with the 24-bit
        /// <paramref name="address"/>, the device's dummy cycles, then
        /// <paramref name="count"/> data bytes.</summary>
        void LinearRead(byte opcode, long address, byte[] destination, int offset, int count);
    }
}
