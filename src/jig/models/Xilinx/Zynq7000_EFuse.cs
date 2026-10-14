//
// Zynq-7000 eFuse status block (read-only view at 0xF800D000).
//
// Only EFUSE_STATUS (offset 0x10) is read by software on this platform:
//  - bit 7: single-CPU device. The standalone BSP's boot.S parks CPU1 when set
//    (standalone/src/arm/cortexa9/gcc/boot.S, CheckEFUSE).
//  - bit 10: RSA authentication enabled. The Xilinx FSBL then demands
//    authenticated partitions (fsbl.h, EFUSE_STATUS_RSA_ENABLE_MASK = 0x400).
// Both default to 0, matching an unfused dual-core XC7Z020.
//
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure.Registers;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Jig.Xilinx
{
    public sealed class Zynq7000_EFuse : BasicDoubleWordPeripheral, IKnownSize
    {
        public Zynq7000_EFuse(IMachine machine, bool singleCpu = false, bool rsaEnabled = false)
            : base(machine)
        {
            Registers.Status.Define(this)
                .WithReservedBits(0, 7)
                .WithFlag(7, FieldMode.Read, valueProviderCallback: _ => singleCpu, name: "SINGLE_CPU")
                .WithReservedBits(8, 2)
                .WithFlag(10, FieldMode.Read, valueProviderCallback: _ => rsaEnabled, name: "RSA_ENABLE")
                .WithReservedBits(11, 21);
        }

        public long Size => 0x1000;

        private enum Registers
        {
            Status = 0x10,
        }
    }
}
