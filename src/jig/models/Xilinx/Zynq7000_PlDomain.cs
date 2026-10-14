//
// The programmable logic as a reset domain: the state that disappears when the
// PL is reset or reconfigured, and the AXI slaves that stop answering while
// their FCLK reset is asserted.
//
// Block RAM. Every system reset resets the PL and the FSBL has to configure it
// again (UG585 26.2, Table 26-1). Block RAM therefore loses its contents on a
// reset, when PROG_B clears the configuration, and when a new configuration
// loads (block RAM takes the bitstream's initial values; this model reads 0,
// as for a design that sets none). Renode keeps RAM across a machine reset, so
// the PL memories are registered here (AddMemory) and cleared at those points.
//
// AXI slaves held in reset. A slave behind an interconnect whose reset is
// asserted (SLCR FPGA_RST_CTRL FPGAn_OUT_RST drives FCLK_RESETn_N into
// proc_sys_reset) never completes a transaction, and the CPU that issued it
// stalls inside the access; only a reset ends that. Slaves are registered with
// AddPeripheral and the FCLK reset that holds them; an access
// while that reset is asserted halts the CPU until the next machine reset.
// The halt takes effect where Renode next leaves the CPU's translation block:
// the access completes and the instructions after it up to the next branch
// still run (a precise stop would need a sysbus watchpoint per address).
// Memories are not covered: Renode maps them directly into the CPU.
//
// PL peripheral models reset with the machine like any other peripheral.
//
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CPU;
using Antmicro.Renode.Peripherals.Memory;

namespace Antmicro.Renode.Peripherals.Jig.Xilinx
{
    public sealed class Zynq7000_PlDomain : IPeripheral
    {
        public Zynq7000_PlDomain(IMachine machine, Zynq7000_DevC devc = null, Zynq7000_SLCR slcr = null,
                                 ICPU cpu = null)
        {
            this.machine = machine;
            this.slcr = slcr;
            this.cpu = cpu;
            if(devc != null)
            {
                devc.PlConfigurationChanged += configured =>
                {
                    this.Log(LogLevel.Debug, configured ? "PL configured: block RAM takes its initial values" : "PL configuration cleared");
                    ClearMemories();
                };
            }
        }

        /// <summary>Adds a block-RAM region that belongs to the PL.</summary>
        public void AddMemory(MappedMemory memory)
        {
            if(!memories.Contains(memory))
            {
                memories.Add(memory);
            }
        }

        /// <summary>Adds an AXI slave that hangs while FCLK_RESET[fclkReset]_N is asserted.</summary>
        public void AddPeripheral(IBusPeripheral peripheral, int fclkReset)
        {
            if(slcr == null || cpu == null)
            {
                throw new RecoverableException("The PL domain needs the slcr and cpu references to model AXI slaves held in reset");
            }
            if(fclkReset < 0 || fclkReset > 3)
            {
                throw new RecoverableException($"FCLK reset {fclkReset} does not exist (0-3)");
            }
            var bus = machine.GetSystemBus(peripheral);
            var name = machine.GetLocalName(peripheral);
            bus.SetHookAfterPeripheralRead<byte>(peripheral, (value, offset) => { Check(name, fclkReset, offset); return value; });
            bus.SetHookAfterPeripheralRead<ushort>(peripheral, (value, offset) => { Check(name, fclkReset, offset); return value; });
            bus.SetHookAfterPeripheralRead<uint>(peripheral, (value, offset) => { Check(name, fclkReset, offset); return value; });
            bus.SetHookBeforePeripheralWrite<byte>(peripheral, (value, offset) => { Check(name, fclkReset, offset); return value; });
            bus.SetHookBeforePeripheralWrite<ushort>(peripheral, (value, offset) => { Check(name, fclkReset, offset); return value; });
            bus.SetHookBeforePeripheralWrite<uint>(peripheral, (value, offset) => { Check(name, fclkReset, offset); return value; });
        }

        public void Reset()
        {
            ClearMemories();
        }

        private void Check(string name, int fclkReset, long offset)
        {
            if(((slcr.FpgaResetControl >> fclkReset) & 1) == 0)
            {
                return;
            }
            this.Log(LogLevel.Warning, "AXI access to {0} at +0x{1:X} while FCLK_RESET{2}_N is asserted: the transaction never completes, CPU stalled",
                     name, offset, fclkReset);
            cpu.IsHalted = true;
        }

        private void ClearMemories()
        {
            foreach(var memory in memories)
            {
                memory.ZeroAll();
            }
        }

        private readonly IMachine machine;
        private readonly Zynq7000_SLCR slcr;
        private readonly ICPU cpu;
        private readonly List<MappedMemory> memories = new List<MappedMemory>();
    }
}
