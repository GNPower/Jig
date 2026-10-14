//
// ARM L2C-310 (PL310) level-2 cache controller register block (Zynq-7000:
// 0xF8F02000).
//
// The CPU model does not simulate an L2 cache, so this is a register-accurate
// controller whose maintenance operations complete instantly. Reference: ARM
// CoreLink Level 2 Cache Controller L2C-310 TRM (DDI 0246), register summary.
// Reset values are UG585 v1.7 B.23's (L2Cpl310): cache ID 0x410000C8 (r3p2),
// cache type 0x9E300300, auxiliary control 0x02060000, tag and data RAM
// latency 0x777, everything else 0. UG585 is not consistent on the auxiliary
// control value: B.23's description names 0x02020000, and Table 6-20 lists
// 0x02050000 at reset and 0x02060000 after the BootROM; the model takes the
// field table's 0x02060000.
//
// Behaviour modelled:
//  - Control, auxiliary control, tag/data RAM latency, event counter, interrupt,
//    lockdown, address filtering, debug, prefetch and power registers are
//    read/write storage with the values software wrote.
//  - Background maintenance by way (INV_WAY, CLEAN_WAY, CLEAN_INV_WAY) and the
//    cache sync register read back 0: every operation has already completed.
//  - Interrupt raw status never sets (no ECC, parity or decode errors occur);
//    INT_CLEAR is accepted.
//  - The auxiliary control and RAM latency registers are only writable while
//    the cache is disabled. Silicon answers such a write with SLVERR; the model
//    drops it and logs a warning.
//
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Jig.Arm
{
    public sealed class PL310 : IDoubleWordPeripheral, IKnownSize
    {
        public PL310(IMachine machine, uint cacheId = 0x410000C8, uint cacheType = 0x9E300300)
        {
            this.cacheId = cacheId;
            this.cacheType = cacheType;
            storage = new uint[Size / 4];
            Reset();
        }

        public void Reset()
        {
            Array.Clear(storage, 0, storage.Length);
            storage[AuxiliaryControl / 4] = AuxiliaryControlResetValue;
            storage[TagRamControl / 4] = RamLatencyResetValue;
            storage[DataRamControl / 4] = RamLatencyResetValue;
        }

        public long Size => 0x1000;

        public bool Enabled => (storage[Control / 4] & 1) != 0;

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case CacheId: return cacheId;
            case CacheType: return cacheType;
            case InterruptMaskedStatus:
            case InterruptRawStatus:
                return 0;
            case CacheSync:
            case InvalidateByWay:
            case CleanByWay:
            case CleanInvalidateByWay:
            case InvalidateByPa:
            case CleanByPa:
            case CleanByIndex:
            case CleanInvalidateByPa:
            case CleanInvalidateByIndex:
                return 0; // operation complete
            default:
                return storage[offset / 4];
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case CacheId:
            case CacheType:
            case InterruptMaskedStatus:
            case InterruptRawStatus:
                this.Log(LogLevel.Debug, "Write to read-only register 0x{0:X3} ignored", offset);
                return;
            case InterruptClear:
                return;
            case AuxiliaryControl:
            case TagRamControl:
            case DataRamControl:
                if(Enabled)
                {
                    this.Log(LogLevel.Warning, "Write 0x{0:X8} to 0x{1:X3} ignored: the L2 cache is enabled", value, offset);
                    return;
                }
                storage[offset / 4] = value;
                return;
            default:
                storage[offset / 4] = value;
                return;
            }
        }

        private readonly uint cacheId;
        private readonly uint cacheType;
        private readonly uint[] storage;

        private const long CacheId = 0x000;
        private const long CacheType = 0x004;
        private const long Control = 0x100;
        private const long AuxiliaryControl = 0x104;
        private const long TagRamControl = 0x108;
        private const long DataRamControl = 0x10C;
        private const long InterruptMaskedStatus = 0x218;
        private const long InterruptRawStatus = 0x21C;
        private const long InterruptClear = 0x220;
        private const long CacheSync = 0x730;
        private const long InvalidateByPa = 0x770;
        private const long InvalidateByWay = 0x77C;
        private const long CleanByPa = 0x7B0;
        private const long CleanByIndex = 0x7B8;
        private const long CleanByWay = 0x7BC;
        private const long CleanInvalidateByPa = 0x7F0;
        private const long CleanInvalidateByIndex = 0x7F8;
        private const long CleanInvalidateByWay = 0x7FC;

        private const uint AuxiliaryControlResetValue = 0x02060000;
        private const uint RamLatencyResetValue = 0x777;
    }
}
