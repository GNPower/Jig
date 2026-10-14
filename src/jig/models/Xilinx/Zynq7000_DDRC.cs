//
// Zynq-7000 DDR memory controller register block (0xF8006000).
//
// A board may fit no DDR, yet ps7_init still programs the controller and waits
// for it to leave initialisation, so the register block has to answer.
// Reference: UG585 chapter 10 "DDR Memory Controller", register reference B.10
// (ddrc).
//
// Behaviour modelled:
//  - All registers are storage (ps7_init read-modify-writes them) and reset to
//    0, although UG585 v1.7 B.6 gives many of them other values (ddrc_ctrl
//    0x00000200, for one).
//  - ddrc_ctrl.reg_ddrc_soft_rstb (bit 0) releases the controller; the
//    read-only mode_sts_reg (0x054) then reports operating mode 1 ("normal
//    operation") in bits [2:0], else 0 ("init"); its queue depths and stall
//    bit read 0.
//  - No memory is attached; the memory region itself is absent from the bus,
//    as on a board without DDR.
//
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Jig.Xilinx
{
    public sealed class Zynq7000_DDRC : IDoubleWordPeripheral, IKnownSize
    {
        public Zynq7000_DDRC(IMachine machine)
        {
            storage = new uint[Size / 4];
        }

        public void Reset()
        {
            Array.Clear(storage, 0, storage.Length);
        }

        public long Size => 0x1000;

        public uint ReadDoubleWord(long offset)
        {
            if(offset == ModeStatus)
            {
                return (storage[Control / 4] & SoftResetReleased) != 0 ? NormalOperation : 0u;
            }
            return storage[offset / 4];
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(offset == ModeStatus)
            {
                return; // read-only
            }
            storage[offset / 4] = value;
        }

        private readonly uint[] storage;

        private const long Control = 0x000;
        private const long ModeStatus = 0x054;
        private const uint SoftResetReleased = 1u << 0;
        private const uint NormalOperation = 1u;
    }
}
