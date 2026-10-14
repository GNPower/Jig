//
// GICv1 distributor details Renode's ARM_GenericInterruptController does not
// model, applied as bus write hooks on the distributor.
//
// Implemented priority bits (LimitGicPriorityBits). A GIC may implement fewer
// than 8 priority bits; the unimplemented low bits of each GICD_IPRIORITYR byte
// read as zero and ignore writes (ARM IHI 0048, "Interrupt prioritization").
// The Zynq-7000 GIC has 32 levels, 5 bits. FreeRTOS's Cortex-A9 port
// (portable/GCC/ARM_CA9/port.c) asserts the count at scheduler start: it
// writes 0xFF to the first priority byte and checks the levels that read back
// against configUNIQUE_INTERRUPT_PRIORITIES. Renode stores all 8 bits, so the
// hook clears the unimplemented ones before the GIC stores them.
//
// Handling-model bits (IgnoreGicHandlingModelBits). In a GICv1 GICD_ICFGR each
// interrupt has two bits: [1] edge-triggered (1) or level-sensitive (0), [0] the
// handling model (1-N or N-N). The Xilinx XScuGic driver writes 0b01 (level) and
// 0b11 (rising edge) (xscugic.h, XScuGic_SetPriorityTriggerType). Renode only
// accepts 0b00 and 0b10 and leaves an interrupt configured with anything else
// unable to become pending, so the hook clears bit [0] of every field. Reads then
// return 0 there; with one CPU the model bit has no effect.
//
// Usage in a platform file:
//     sysbus:
//         init:
//             LimitGicPriorityBits gic 5
//             IgnoreGicHandlingModelBits gic
//
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Jig.Arm
{
    public static class GicDistributorExtensions
    {
        public static void LimitGicPriorityBits(this IBusController bus, IBusPeripheral gic, int implementedBits)
        {
            if(implementedBits < 4 || implementedBits > 8)
            {
                throw new RecoverableException($"A GIC implements 4 to 8 priority bits, not {implementedBits}");
            }
            var byteMask = (byte)(0xFF << (8 - implementedBits));
            InstallMask(bus, gic, new Range(PriorityRegistersStart, PriorityRegistersSize), byteMask);
        }

        public static void IgnoreGicHandlingModelBits(this IBusController bus, IBusPeripheral gic)
        {
            InstallMask(bus, gic, new Range(ConfigurationRegistersStart, ConfigurationRegistersSize), 0xAA);
        }

        // One mask per register range; a second call for the same range replaces the first.
        private static void InstallMask(IBusController bus, IBusPeripheral gic, Range range, byte byteMask)
        {
            var wordMask = (ushort)(byteMask | byteMask << 8);
            var doubleWordMask = (uint)(wordMask | wordMask << 16);
            lock(Hooks)
            {
                if(!Hooks.TryGetValue(gic, out var masks))
                {
                    masks = new System.Collections.Generic.List<(Range Range, byte Mask)>();
                    Hooks.Add(gic, masks);
                }
                masks.RemoveAll(entry => entry.Range.StartAddress == range.StartAddress);
                masks.Add((range, byteMask));
                // Renode keeps one write hook per access width and peripheral, so a
                // single hook applies every registered range.
                var snapshot = masks.ToArray();
                bus.SetHookBeforePeripheralWrite<byte>(gic, (value, offset) => (byte)(value & MaskFor(snapshot, offset, 1)));
                bus.SetHookBeforePeripheralWrite<ushort>(gic, (value, offset) => (ushort)(value & MaskFor(snapshot, offset, 2)));
                bus.SetHookBeforePeripheralWrite<uint>(gic, (value, offset) => value & MaskFor(snapshot, offset, 4));
            }
        }

        private static uint MaskFor((Range Range, byte Mask)[] masks, long offset, int width)
        {
            foreach(var (range, mask) in masks)
            {
                if(range.Contains((ulong)offset))
                {
                    uint result = 0;
                    for(var i = 0; i < width; i++)
                    {
                        result |= (uint)mask << (8 * i);
                    }
                    return result;
                }
            }
            return 0xFFFFFFFF;
        }

        private static readonly System.Runtime.CompilerServices.ConditionalWeakTable<IBusPeripheral, System.Collections.Generic.List<(Range Range, byte Mask)>> Hooks =
            new System.Runtime.CompilerServices.ConditionalWeakTable<IBusPeripheral, System.Collections.Generic.List<(Range Range, byte Mask)>>();

        private const ulong PriorityRegistersStart = 0x400;
        private const ulong PriorityRegistersSize = 0x400;
        private const ulong ConfigurationRegistersStart = 0xC00;
        private const ulong ConfigurationRegistersSize = 0x100;
    }
}
