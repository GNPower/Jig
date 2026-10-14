//
// Test fixture: a generic serial NOR flash with 3-byte addresses, for Jig's
// tests of its SPI and Quad-SPI controller models. Written from the JEDEC SFDP
// standard (JESD216, first edition: the SFDP header, one parameter header and
// the 9-word Basic Flash Parameter Table) and the commands that table
// describes. It models no particular device.
//
// Commands (an opcode byte, then a 3-byte address where shown, most
// significant byte first):
//   0x9F READ ID           manufacturer, memory type, capacity code (log2 of
//                          the size in bytes), then 0x00
//   0x5A READ SFDP         address, 8 dummy clocks, the SFDP tables
//   0x05 READ STATUS       the status register, repeated while chip select
//                          stays low: bit 0 BUSY, bit 1 WEL, bits 4:2 BP
//   0x01 WRITE STATUS      needs WEL; stores BP (non-volatile), clears WEL
//   0x06 / 0x04            WRITE ENABLE / WRITE DISABLE
//   0x03 READ              address, data
//   0x0B FAST READ         address, 8 dummy clocks, data
//   0x3B DUAL OUTPUT READ  address, 8 dummy clocks, data on 2 lines (1-1-2)
//   0x6B QUAD OUTPUT READ  address, 8 dummy clocks, data on 4 lines (1-1-4)
//   0x02 PAGE PROGRAM      address, then data: the last 256 bytes sent are
//                          ANDed into the addressed 256-byte page, wrapping
//                          inside it
//   0x20 / 0xD8            4 KB / 64 KB ERASE of the block holding the address
//   0x60 / 0xC7            CHIP ERASE
// Reads continue across the array and wrap at its end. Program and erase start
// when chip select rises after a complete command, need WEL, keep BUSY set for
// the time the constructor gives (fixture values, not a datasheet's) and clear
// WEL when done. While BUSY only READ STATUS is answered: other commands and
// linear reads see 0xFF. BP is stored but does not protect anything.
//
// Contracts: ISpiNorFlash (LinearRead runs one array read at once), IStorage
// (AttachImage, Size) and, through ISpiNorFlash, IPowerCycled: PowerCycle
// clears WEL and BUSY and drops a program or erase in progress without its
// effect; the array and BP stay. Reset stands for the board's reset line,
// which this device lacks: it ends the command in progress without running it.
//
//   flash0: JigFixtures.SpiNorFlash @ qspi 0
//       size: 0x1000000
//
using System;
using System.Collections.Generic;
using System.IO;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Jig.Contracts;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.JigFixtures
{
    public sealed class SpiNorFlash : ISpiNorFlash, IStorage
    {
        public SpiNorFlash(IMachine machine, long size = 0x1000000, byte manufacturerId = 0xA5, byte memoryType = 0x5A,
                           ulong pageProgramMicroseconds = 500, ulong sectorEraseMicroseconds = 30000,
                           ulong blockEraseMicroseconds = 120000, ulong chipEraseMicroseconds = 2000000)
        {
            if(size < MinimumSize || size > MaximumSize || (size & (size - 1)) != 0)
            {
                throw new ConstructionException("size must be a power of two from 64 KiB to 16 MiB (3-byte addresses)");
            }
            this.machine = machine;
            this.manufacturerId = manufacturerId;
            this.memoryType = memoryType;
            this.pageProgramMicroseconds = pageProgramMicroseconds;
            this.sectorEraseMicroseconds = sectorEraseMicroseconds;
            this.blockEraseMicroseconds = blockEraseMicroseconds;
            this.chipEraseMicroseconds = chipEraseMicroseconds;
            Size = size;
            capacityCode = (byte)Math.Round(Math.Log(size, 2));
            array = new byte[size];
            for(var i = 0; i < array.Length; i++)
            {
                array[i] = Erased;
            }
            sfdp = BuildSfdp(size);
            PowerCycle();
        }

        public long Size { get; }

        public int CurrentDataLines
        {
            get
            {
                lock(sync)
                {
                    return phase == Phase.Data ? dataLines : 1;
                }
            }
        }

        /// <summary>The attached image file, or null.</summary>
        public string ImageFile => imagePath;

        /// <summary>One byte of the array, without a command (for tests).</summary>
        public byte Peek(long address)
        {
            lock(sync)
            {
                return array[address & (Size - 1)];
            }
        }

        public void Reset()
        {
            lock(sync)
            {
                EndCommand();
            }
        }

        public void PowerCycle()
        {
            lock(sync)
            {
                generation++;
                busy = false;
                writeEnable = false;
                EndCommand();
            }
        }

        public void AttachImage(string path)
        {
            lock(sync)
            {
                var fullPath = Path.GetFullPath(path);
                var file = new FileInfo(fullPath);
                if(file.Exists && file.Length > Size)
                {
                    throw new RecoverableException($"Image {fullPath} ({file.Length} bytes) is larger than the flash ({Size} bytes)");
                }
                var stream = new FileStream(fullPath, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.Read);
                if(file.Exists && file.Length > 0)
                {
                    var length = (int)stream.Length;
                    var read = 0;
                    while(read < length)
                    {
                        var n = stream.Read(array, read, length - read);
                        if(n == 0)
                        {
                            break;
                        }
                        read += n;
                    }
                    for(var i = read; i < array.Length; i++)
                    {
                        array[i] = Erased;
                    }
                }
                stream.SetLength(Size);
                stream.Position = 0;
                stream.Write(array, 0, array.Length);
                stream.Flush();
                imageStream?.Dispose();
                imageStream = stream;
                imagePath = fullPath;
            }
        }

        // ------------------------------------------------------------ SPI wire

        public byte Transmit(byte data)
        {
            lock(sync)
            {
                switch(phase)
                {
                case Phase.Opcode:
                    StartCommand(data);
                    return Undriven;
                case Phase.Address:
                    address = (address << 8) | data;
                    addressBytesLeft--;
                    if(addressBytesLeft == 0)
                    {
                        AddressComplete();
                    }
                    return Undriven;
                case Phase.Dummy:
                    dummyBytesLeft--;
                    if(dummyBytesLeft == 0)
                    {
                        phase = Phase.Data;
                    }
                    return Undriven;
                case Phase.Data:
                    return DataByte(data);
                default:
                    return Undriven;
                }
            }
        }

        public void FinishTransmission()
        {
            lock(sync)
            {
                switch(command)
                {
                case WriteStatus:
                    if(dataIndex > 0 && writeEnable)
                    {
                        blockProtect = (byte)((pendingStatus >> 2) & 0x7);
                        writeEnable = false;
                    }
                    break;
                case PageProgram:
                    if(addressComplete && programBuffer.Count > 0)
                    {
                        Program();
                    }
                    break;
                case SectorErase:
                    if(addressComplete)
                    {
                        Erase(SectorSize, sectorEraseMicroseconds);
                    }
                    break;
                case BlockErase:
                    if(addressComplete)
                    {
                        Erase(BlockSize, blockEraseMicroseconds);
                    }
                    break;
                case ChipErase:
                case ChipEraseAlternate:
                    address = 0;
                    Erase(Size, chipEraseMicroseconds);
                    break;
                }
                EndCommand();
            }
        }

        public void LinearRead(byte opcode, long linearAddress, byte[] destination, int offset, int count)
        {
            lock(sync)
            {
                if(busy || !IsArrayRead(opcode))
                {
                    this.Log(LogLevel.Warning, busy ? "Linear read while busy: 0xFF" : "Linear read with instruction 0x{0:X2}, which is no array read: 0xFF", opcode);
                    for(var i = 0; i < count; i++)
                    {
                        destination[offset + i] = Undriven;
                    }
                    return;
                }
                var start = linearAddress & 0xFFFFFF & (Size - 1);
                for(var i = 0; i < count; i++)
                {
                    destination[offset + i] = array[(start + i) & (Size - 1)];
                }
            }
        }

        // ------------------------------------------------------------ commands

        private void StartCommand(byte opcode)
        {
            command = opcode;
            address = 0;
            dataIndex = 0;
            dataLines = 1;
            addressComplete = false;
            programBuffer.Clear();
            if(busy && opcode != ReadStatus)
            {
                this.Log(LogLevel.Debug, "Command 0x{0:X2} ignored while busy", opcode);
                command = 0;
                phase = Phase.Ignore;
                return;
            }
            switch(opcode)
            {
            case WriteEnable:
                writeEnable = true;
                phase = Phase.Ignore;
                break;
            case WriteDisable:
                writeEnable = false;
                phase = Phase.Ignore;
                break;
            case ReadId:
            case ReadStatus:
            case WriteStatus:
                phase = Phase.Data;
                break;
            case Read:
            case FastRead:
            case DualOutputRead:
            case QuadOutputRead:
            case ReadSfdp:
            case PageProgram:
            case SectorErase:
            case BlockErase:
                phase = Phase.Address;
                addressBytesLeft = 3;
                break;
            case ChipErase:
            case ChipEraseAlternate:
                phase = Phase.Ignore;
                break;
            default:
                this.Log(LogLevel.Warning, "Unsupported command 0x{0:X2}", opcode);
                command = 0;
                phase = Phase.Ignore;
                break;
            }
        }

        private void AddressComplete()
        {
            addressComplete = true;
            switch(command)
            {
            case Read:
            case PageProgram:
                phase = Phase.Data;
                break;
            case FastRead:
            case DualOutputRead:
            case QuadOutputRead:
            case ReadSfdp:
                dataLines = command == QuadOutputRead ? 4 : command == DualOutputRead ? 2 : 1;
                dummyBytesLeft = DummyClocks / 8;
                phase = Phase.Dummy;
                break;
            default:
                phase = Phase.Ignore; // erases run when chip select rises
                break;
            }
        }

        private byte DataByte(byte input)
        {
            var index = dataIndex++;
            switch(command)
            {
            case ReadId:
                return index == 0 ? manufacturerId : index == 1 ? memoryType : index == 2 ? capacityCode : (byte)0x00;
            case ReadStatus:
                return StatusRegister;
            case WriteStatus:
                if(index == 0)
                {
                    pendingStatus = input;
                }
                return Undriven;
            case Read:
            case FastRead:
            case DualOutputRead:
            case QuadOutputRead:
                return array[(address + index) & (Size - 1)];
            case ReadSfdp:
            {
                var at = address + index;
                return at < sfdp.Length ? sfdp[at] : Undriven;
            }
            case PageProgram:
                programBuffer.Add(input);
                return Undriven;
            default:
                return Undriven;
            }
        }

        private void EndCommand()
        {
            phase = Phase.Opcode;
            command = 0;
            dataIndex = 0;
            dataLines = 1;
            addressComplete = false;
            programBuffer.Clear();
        }

        private void Program()
        {
            if(!writeEnable)
            {
                this.Log(LogLevel.Debug, "Page program ignored: WEL clear");
                return;
            }
            var pageBase = (address & (Size - 1)) & ~(long)(PageSize - 1);
            var start = (int)(address & (PageSize - 1));
            var kept = Math.Min(programBuffer.Count, PageSize);
            var data = programBuffer.GetRange(programBuffer.Count - kept, kept).ToArray();
            StartBusy(pageProgramMicroseconds, () =>
            {
                for(var i = 0; i < data.Length; i++)
                {
                    array[pageBase + ((start + i) % PageSize)] &= data[i];
                }
                Persist(pageBase, PageSize);
            });
        }

        private void Erase(long blockSize, ulong microseconds)
        {
            if(!writeEnable)
            {
                this.Log(LogLevel.Debug, "Erase ignored: WEL clear");
                return;
            }
            var blockBase = (address & (Size - 1)) & ~(blockSize - 1);
            StartBusy(microseconds, () =>
            {
                for(var i = 0L; i < blockSize; i++)
                {
                    array[blockBase + i] = Erased;
                }
                Persist(blockBase, blockSize);
            });
        }

        private void StartBusy(ulong microseconds, Action done)
        {
            busy = true;
            var token = ++generation;
            machine.ScheduleAction(TimeInterval.FromMicroseconds(microseconds), _ =>
            {
                lock(sync)
                {
                    if(token != generation)
                    {
                        return; // a power cycle dropped this operation
                    }
                    done();
                    busy = false;
                    writeEnable = false;
                }
            }, "spi-nor-busy");
        }

        private void Persist(long start, long length)
        {
            if(imageStream == null)
            {
                return;
            }
            imageStream.Position = start;
            imageStream.Write(array, (int)start, (int)length);
            imageStream.Flush();
        }

        private byte StatusRegister => (byte)((busy ? 0x01 : 0x00) | (writeEnable ? 0x02 : 0x00) | (blockProtect << 2));

        private static bool IsArrayRead(byte opcode)
        {
            return opcode == Read || opcode == FastRead || opcode == DualOutputRead || opcode == QuadOutputRead;
        }

        // JESD216 SFDP: header, one parameter header, then the Basic Flash
        // Parameter Table (BFPT) of 9 DWORDs at 0x10.
        private static byte[] BuildSfdp(long size)
        {
            var dwords = new uint[]
            {
                // 1: 4 KB erase supported (bits 1:0 = 01), page writes (bit 2),
                // non-volatile BP (bit 3 = 0), bits 7:5 unused (111), 4 KB erase
                // instruction 0x20, 1-1-2 (bit 16) and 1-1-4 (bit 22) fast reads,
                // 3-byte addresses only (bits 18:17 = 00), bit 23 and 31:24 unused (1s).
                0xFF000000u | (1u << 23) | (1u << 22) | (1u << 16) | ((uint)SectorErase << 8) | (0x7u << 5) | (1u << 2) | 0x1u,
                // 2: density in bits minus one.
                (uint)(size * 8 - 1),
                // 3: 1-4-4 unsupported (0); 1-1-4: 8 wait states, no mode clocks, instruction 0x6B.
                ((uint)QuadOutputRead << 24) | (8u << 16),
                // 4: 1-1-2: 8 wait states, no mode clocks, instruction 0x3B; 1-2-2 unsupported.
                ((uint)DualOutputRead << 8) | 8u,
                // 5: 2-2-2 and 4-4-4 unsupported (bits 0 and 4 clear); reserved bits set.
                0xFFFFFFEEu,
                // 6, 7: reserved halves set; 2-2-2 and 4-4-4 fields unused.
                0x0000FFFFu,
                0x0000FFFFu,
                // 8: erase type 1 = 4 KB (2^12) with 0x20, type 2 = 64 KB (2^16) with 0xD8.
                ((uint)BlockErase << 24) | (16u << 16) | ((uint)SectorErase << 8) | 12u,
                // 9: erase types 3 and 4 absent.
                0x00000000u,
            };
            var table = new byte[BfptOffset + dwords.Length * 4];
            // SFDP header: signature "SFDP", revision 1.0, one parameter header
            // (the count is stored minus one), unused byte 0xFF.
            table[0] = (byte)'S';
            table[1] = (byte)'F';
            table[2] = (byte)'D';
            table[3] = (byte)'P';
            table[4] = 0x00;
            table[5] = 0x01;
            table[6] = 0x00;
            table[7] = 0xFF;
            // Parameter header 0: JEDEC ID 0xFF00 (LSB 0x00, MSB 0xFF), table
            // revision 1.0, length in DWORDs, 24-bit pointer.
            table[8] = 0x00;
            table[9] = 0x00;
            table[10] = 0x01;
            table[11] = (byte)dwords.Length;
            table[12] = BfptOffset;
            table[13] = 0x00;
            table[14] = 0x00;
            table[15] = 0xFF;
            for(var i = 0; i < dwords.Length; i++)
            {
                var value = dwords[i];
                for(var b = 0; b < 4; b++)
                {
                    table[BfptOffset + 4 * i + b] = (byte)(value >> (8 * b));
                }
            }
            return table;
        }

        private readonly object sync = new object();
        private readonly IMachine machine;
        private readonly byte manufacturerId;
        private readonly byte memoryType;
        private readonly byte capacityCode;
        private readonly ulong pageProgramMicroseconds;
        private readonly ulong sectorEraseMicroseconds;
        private readonly ulong blockEraseMicroseconds;
        private readonly ulong chipEraseMicroseconds;
        private readonly byte[] array;
        private readonly byte[] sfdp;
        private readonly List<byte> programBuffer = new List<byte>();

        private FileStream imageStream;
        private string imagePath;

        private Phase phase;
        private byte command;
        private long address;
        private int addressBytesLeft;
        private int dummyBytesLeft;
        private int dataIndex;
        private int dataLines;
        private bool addressComplete;
        private byte pendingStatus;
        private ulong generation;
        private bool busy;
        private bool writeEnable;
        private byte blockProtect;

        private enum Phase
        {
            Opcode,
            Address,
            Dummy,
            Data,
            Ignore,
        }

        private const long MinimumSize = 0x10000;
        private const long MaximumSize = 0x1000000;
        private const int PageSize = 256;
        private const long SectorSize = 0x1000;
        private const long BlockSize = 0x10000;
        private const int DummyClocks = 8;
        private const byte BfptOffset = 0x10;
        private const byte Erased = 0xFF;
        private const byte Undriven = 0xFF;

        private const byte WriteStatus = 0x01;
        private const byte PageProgram = 0x02;
        private const byte Read = 0x03;
        private const byte WriteDisable = 0x04;
        private const byte ReadStatus = 0x05;
        private const byte WriteEnable = 0x06;
        private const byte FastRead = 0x0B;
        private const byte SectorErase = 0x20;
        private const byte DualOutputRead = 0x3B;
        private const byte ReadSfdp = 0x5A;
        private const byte ChipEraseAlternate = 0x60;
        private const byte QuadOutputRead = 0x6B;
        private const byte ReadId = 0x9F;
        private const byte ChipErase = 0xC7;
        private const byte BlockErase = 0xD8;
    }
}
