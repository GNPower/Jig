//
// Zynq-7000 PS GPIO controller (UG585 ch. 14): 54 MIO pins in banks 0-1 and 64
// EMIO pins in banks 2-3, with the per-bank interrupt block.
//
// Register map, bank widths and reset values follow the standalone driver
// (xgpiops_hw.h offsets; xgpiops_hw.c XGpioPs_ResetHw writes 0 to every
// register except INT_TYPE, which resets to 0xFFFFFFFF, 0x003FFFFF (bank 1,
// 22 pins), 0xFFFFFFFF, 0xFFFFFFFF).
//
//  - MASK_DATA_n_LSW/MSW: bits [31:16] protect the matching data bit; only
//    unprotected bits update DATA. Reads return the data half.
//  - DATA_n: the output data register.
//  - DATA_n_RO: the pin levels. A pin with DIRM and OEN set is driven by DATA;
//    any other pin shows its external level (OnGPIO, or its pull setting).
//  - INT_EN / INT_DIS (write-only) clear / set INT_MASK bits. INT_STAT records
//    events whether or not the bit is masked and is write-1-to-clear; the
//    level-sensitive bits re-assert while the level persists. INT_TYPE 1 =
//    edge, INT_POLARITY 1 = rising/high, INT_ANY 1 = both edges. IRQ is the
//    OR of INT_STAT & ~INT_MASK over all banks.
// Connections[n] carries pin n's level (driven or external), so a device on
// the other end sees what the pin shows, including a released line's pull.
// External levels are not cleared by a reset: other devices drive them.
// MIO routing in the SLCR is not modelled: every pin behaves as a GPIO.
//
using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Jig.Xilinx
{
    public sealed class Zynq7000_GPIO : IDoubleWordPeripheral, IKnownSize, INumberedGPIOOutput, IGPIOReceiver
    {
        public Zynq7000_GPIO(IMachine machine)
        {
            var connections = new Dictionary<int, IGPIO>();
            for(var pin = 0; pin < PinCount; pin++)
            {
                connections[pin] = new GPIO();
            }
            Connections = new ReadOnlyDictionary<int, IGPIO>(connections);
            IRQ = new GPIO();
            Reset();
        }

        public IReadOnlyDictionary<int, IGPIO> Connections { get; }

        public GPIO IRQ { get; }

        public long Size => 0x1000;

        public void Reset()
        {
            lock(sync)
            {
                for(var bank = 0; bank < BankCount; bank++)
                {
                    data[bank] = 0;
                    direction[bank] = 0;
                    outputEnable[bank] = 0;
                    interruptMask[bank] = 0;
                    interruptStatus[bank] = 0;
                    interruptType[bank] = InterruptTypeReset[bank];
                    interruptPolarity[bank] = 0;
                    interruptAny[bank] = 0;
                }
                Update(fromReset: true);
            }
        }

        /// <summary>External level on a pin (another device driving it).</summary>
        public void OnGPIO(int number, bool value)
        {
            if(number < 0 || number >= PinCount)
            {
                this.Log(LogLevel.Error, "GPIO input {0} does not exist (0-{1})", number, PinCount - 1);
                return;
            }
            lock(sync)
            {
                external[number] = value;
                Update(fromReset: false);
            }
        }

        /// <summary>Level a pin settles to when nothing drives it (board pull-up or pull-down).</summary>
        public void SetPull(int pin, bool high)
        {
            if(pin < 0 || pin >= PinCount)
            {
                throw new RecoverableException($"GPIO pin {pin} does not exist (0-{PinCount - 1})");
            }
            lock(sync)
            {
                external[pin] = high;
                Update(fromReset: false);
            }
        }

        /// <summary>Current level of a pin as DATA_RO shows it.</summary>
        public bool GetPinLevel(int pin)
        {
            if(pin < 0 || pin >= PinCount)
            {
                throw new RecoverableException($"GPIO pin {pin} does not exist (0-{PinCount - 1})");
            }
            lock(sync)
            {
                return level[pin];
            }
        }

        /// <summary>True when the pin is an enabled output.</summary>
        public bool IsOutput(int pin)
        {
            if(pin < 0 || pin >= PinCount)
            {
                throw new RecoverableException($"GPIO pin {pin} does not exist (0-{PinCount - 1})");
            }
            var (bank, bit) = Locate(pin);
            lock(sync)
            {
                return ((direction[bank] & outputEnable[bank]) >> bit & 1) != 0;
            }
        }

        public uint ReadDoubleWord(long offset)
        {
            lock(sync)
            {
                if(offset < 0x040)
                {
                    var bank = (int)(offset / 8);
                    if(bank >= BankCount)
                    {
                        return Unhandled(offset);
                    }
                    return (offset & 4) == 0 ? data[bank] & 0xFFFF : data[bank] >> 16;
                }
                if(offset >= 0x040 && offset < 0x050)
                {
                    return data[(offset - 0x040) / 4];
                }
                if(offset >= 0x060 && offset < 0x070)
                {
                    return PinLevels((int)((offset - 0x060) / 4));
                }
                if(offset >= 0x204 && offset < 0x204 + BankCount * 0x40)
                {
                    var bank = (int)((offset - 0x204) / 0x40);
                    switch((offset - 0x204) % 0x40)
                    {
                    case 0x00: return direction[bank];
                    case 0x04: return outputEnable[bank];
                    case 0x08: return interruptMask[bank];
                    case 0x0C: return 0; // INT_EN, write-only
                    case 0x10: return 0; // INT_DIS, write-only
                    case 0x14: return interruptStatus[bank];
                    case 0x18: return interruptType[bank];
                    case 0x1C: return interruptPolarity[bank];
                    case 0x20: return interruptAny[bank];
                    }
                }
                return Unhandled(offset);
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            lock(sync)
            {
                if(offset < 0x040)
                {
                    var bank = (int)(offset / 8);
                    if(bank >= BankCount)
                    {
                        UnhandledWrite(offset, value);
                        return;
                    }
                    var shift = (offset & 4) == 0 ? 0 : 16;
                    var writable = (~value >> 16) & 0xFFFF;
                    var mask = (writable << shift) & BankMask[bank];
                    data[bank] = (data[bank] & ~mask) | (((value & 0xFFFF) << shift) & mask);
                }
                else if(offset >= 0x040 && offset < 0x050)
                {
                    var bank = (int)((offset - 0x040) / 4);
                    data[bank] = value & BankMask[bank];
                }
                else if(offset >= 0x060 && offset < 0x070)
                {
                    this.Log(LogLevel.Warning, "Write 0x{0:X8} to read-only DATA_{1}_RO ignored", value, (offset - 0x060) / 4);
                    return;
                }
                else if(offset >= 0x204 && offset < 0x204 + BankCount * 0x40)
                {
                    var bank = (int)((offset - 0x204) / 0x40);
                    value &= BankMask[bank];
                    switch((offset - 0x204) % 0x40)
                    {
                    case 0x00: direction[bank] = value; break;
                    case 0x04: outputEnable[bank] = value; break;
                    case 0x08:
                        this.Log(LogLevel.Warning, "Write to read-only INT_MASK_{0} ignored (use INT_EN / INT_DIS)", bank);
                        return;
                    case 0x0C: interruptMask[bank] &= ~value; break;
                    case 0x10: interruptMask[bank] |= value; break;
                    case 0x14: interruptStatus[bank] &= ~value; break;
                    case 0x18: interruptType[bank] = value; break;
                    case 0x1C: interruptPolarity[bank] = value; break;
                    case 0x20: interruptAny[bank] = value; break;
                    default:
                        UnhandledWrite(offset, value);
                        return;
                    }
                }
                else
                {
                    UnhandledWrite(offset, value);
                    return;
                }
                Update(fromReset: false);
            }
        }

        private void Update(bool fromReset)
        {
            for(var pin = 0; pin < PinCount; pin++)
            {
                var (bank, bit) = Locate(pin);
                var driven = ((direction[bank] & outputEnable[bank]) >> bit & 1) != 0;
                var newLevel = driven ? ((data[bank] >> bit) & 1) != 0 : external[pin];
                var oldLevel = level[pin];
                level[pin] = newLevel;
                if(!fromReset && newLevel != oldLevel && ((interruptType[bank] >> bit) & 1) != 0)
                {
                    var anyEdge = ((interruptAny[bank] >> bit) & 1) != 0;
                    var rising = ((interruptPolarity[bank] >> bit) & 1) != 0;
                    if(anyEdge || newLevel == rising)
                    {
                        interruptStatus[bank] |= 1u << bit;
                    }
                }
                if(newLevel != oldLevel || fromReset)
                {
                    Connections[pin].Set(newLevel);
                }
            }
            // Level-sensitive bits follow the pin while it stays active.
            for(var bank = 0; bank < BankCount; bank++)
            {
                var levelBits = ~interruptType[bank] & BankMask[bank];
                if(levelBits == 0)
                {
                    continue;
                }
                var active = ~(PinLevels(bank) ^ interruptPolarity[bank]) & levelBits;
                interruptStatus[bank] |= active;
            }
            var pending = false;
            for(var bank = 0; bank < BankCount; bank++)
            {
                pending |= (interruptStatus[bank] & ~interruptMask[bank] & BankMask[bank]) != 0;
            }
            IRQ.Set(pending);
        }

        private uint PinLevels(int bank)
        {
            var value = 0u;
            for(var bit = 0; bit < BankWidth[bank]; bit++)
            {
                if(level[BankBase[bank] + bit])
                {
                    value |= 1u << bit;
                }
            }
            return value;
        }

        private static (int Bank, int Bit) Locate(int pin)
        {
            for(var bank = BankCount - 1; bank >= 0; bank--)
            {
                if(pin >= BankBase[bank])
                {
                    return (bank, pin - BankBase[bank]);
                }
            }
            throw new ArgumentOutOfRangeException(nameof(pin));
        }

        private uint Unhandled(long offset)
        {
            this.Log(LogLevel.Warning, "Read from unhandled offset 0x{0:X3}", offset);
            return 0;
        }

        private void UnhandledWrite(long offset, uint value)
        {
            this.Log(LogLevel.Warning, "Write 0x{0:X8} to unhandled offset 0x{1:X3} ignored", value, offset);
        }

        private readonly object sync = new object();
        private readonly uint[] data = new uint[BankCount];
        private readonly uint[] direction = new uint[BankCount];
        private readonly uint[] outputEnable = new uint[BankCount];
        private readonly uint[] interruptMask = new uint[BankCount];
        private readonly uint[] interruptStatus = new uint[BankCount];
        private readonly uint[] interruptType = new uint[BankCount];
        private readonly uint[] interruptPolarity = new uint[BankCount];
        private readonly uint[] interruptAny = new uint[BankCount];
        private readonly bool[] external = new bool[PinCount];
        private readonly bool[] level = new bool[PinCount];

        private const int BankCount = 4;
        private const int PinCount = 118;
        private static readonly int[] BankBase = { 0, 32, 54, 86 };
        private static readonly int[] BankWidth = { 32, 22, 32, 32 };
        private static readonly uint[] BankMask = { 0xFFFFFFFF, 0x003FFFFF, 0xFFFFFFFF, 0xFFFFFFFF };
        private static readonly uint[] InterruptTypeReset = { 0xFFFFFFFF, 0x003FFFFF, 0xFFFFFFFF, 0xFFFFFFFF };
    }
}
