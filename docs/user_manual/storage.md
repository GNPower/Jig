# Storage and device memories

A run can start with bytes in place: a boot image in a NOR flash, a
configuration block at an offset, a LittleFS or FAT volume built from a
folder, a table in an STM32's own flash or in a Zynq-7000's block RAM. The
board file describes the regions the firmware uses as `partition` lines. A
system picks what goes in each. Jig composes the bytes before the device
boots, and a plan reads them back while the run goes and after it ends.

Jig writes into two kinds of target:

- a **storage device**, a peripheral on the board's `storage` list. Jig
  composes its whole contents into `<machine>/<storage>.img` in the run
  directory, once, after the platform description loads and before the
  device's family configures the machine, and the peripheral's model
  attaches that file (`AttachImage`). From then on the model writes each
  program and erase to the file as it completes, and the contents survive
  resets and power cycles. The model implements Jig's storage contract
  ({doc}`../how_to/write_a_model`). Renode's own flash models, such as
  `SPI.Micron_MT25Q`, don't, so a board needs a model of its own; the test
  fixtures' `JigFixtures.SpiNorFlash` is one.
- a **device memory**, a memory of the device itself: an STM32's flash
  and SRAM, a Zynq-7000's OCM and the block RAM of its programmable logic.
  Jig writes its regions into the machine at the points its kind says:
  once before the first boot, at every power-on, or each time the
  programmable logic is configured.

A storage device that nothing writes, with no partition contents, no file
from the system and no boot image from its family, gets no composed
image: its model makes the file at `AttachImage` and starts from its own
contents, as in Jig 0.2.

## The storage test fixture

The board in `tests/fixtures/firmware/stm32f4-storage` runs firmware on
Renode's STM32F4 platform that mounts a LittleFS volume in the MCU's own
flash and, on a SPI NOR flash, a LittleFS volume, a FAT volume and a raw
region. The firmware prints what it finds in each and writes a file to
the LittleFS volume on the NOR flash. Jig's Renode tests run it:

```{literalinclude} ../../tests/fixtures/firmware/stm32f4-storage/board.jig
:language: jig
```

`mcu_lfs` sits in the device's own flash at 0x08020000, 128 KiB past its
first byte. The other three share `flash0`, whose image Jig composes from
two folders and a raw file. The plan beside the board checks the console,
then reads the volume the firmware wrote to and the raw region:

```{literalinclude} ../../tests/fixtures/firmware/stm32f4-storage/storage.jig
:language: jig
:start-at: test "the plan reads /written.txt
```

## Partition lines

```jig
partition NAME: FORMAT(on: TARGET, at: OFFSET, size: SIZE, contents: PATH,
                       ...)
```

```{list-table}
:header-rows: 1
:widths: 15 85

* - Argument
  - Meaning
* - `on`
  - The target: a peripheral on the board's `storage` list (`flash0`), or
    a memory of the board's device as `DEVICE.MEMORY` (`mcu.flash`,
    `zynq.ocm`). A peripheral missing from `storage` is an error, since
    Jig attaches no image to it.
* - `at`
  - The region's first byte, counted in bytes from the target's first
    byte: an offset, not a bus address. `at: 128 KiB` on an STM32's
    `flash` is bus address 0x08020000.
* - `size`
  - The region's length in bytes, 1 or more.
* - `contents`
  - What the region holds unless a system gives something else: a file,
    placed as it is, or a folder, which the format builds into a volume.
    The path is relative to the board file, as `repl` fragments are.
    Without `contents` the region keeps what the base image puts there:
    erased bytes on a new device.
* - The format's arguments
  - Everything else goes to the format: the settings the firmware mounts
    the region with.
```

Rules for partitions:

- They sit at the board's top level, or under `if`, `elif` and `else`
  there. A device body holds none.
- A partition's name follows the rules of the other declarations: no two
  devices, peripherals, links, sensors or partitions share a name, and
  none takes the name of a member of the board's handle: `name`, `board`,
  `boot_mode`, `boot`, `cli`, `send`, `expect`, `set_sensor`,
  `sensor_code`, `sensors`, `drive`, `set_time`, `time`, `reset`,
  `power_on`, `power_off`, `power_cycle`, `powered`, `image`, `monitor`,
  and the members the board's plugin adds. A region the firmware calls
  `boot` takes another name in the board file, such as `boot_image`. A
  partition is
  no value in the board file's expressions, so it may share its name with
  an option. `option data: "storage/empty"` with `partition data:
  littlefs(..., contents: data)` lets every system that uses the board
  pick the folder with `(data: "fixtures/full")`, a path relative to the
  board file, as `contents` is.
- Partitions on one target never share a byte, whether or not they have
  contents, and none runs past the target's end. A storage peripheral
  with partitions therefore needs a `size` property: `peripheral flash0:
  "JigFixtures.SpiNorFlash" (size: 4 MiB)`.
- When the format's arguments, with their defaults filled in, hold a
  `block_size` (littlefs) or a `sector_size` (fat), `at` and `size` are
  multiples of it.
- A format Jig ships has its arguments checked when the board file loads,
  and an error names the line. A format from the board's plugin is
  checked when a run, `jig check` or `jig image` resolves the board's
  storage, since the plugin loads after the board file.

## Formats

### raw

`raw(on:, at:, size:, contents:)` takes no other argument. Its contents
are a file, placed at the partition's first byte, with the rest of the
partition erased; a folder is an error. A raw partition has no files: a
plan reads its bytes.

### littlefs

Jig builds and reads littlefs volumes with littlefs-python 0.19, which
bundles littlefs 2.11 (on-disk versions 2.0 and 2.1). The arguments repeat
the firmware's `lfs_config`:

```{list-table}
:header-rows: 1
:widths: 22 22 56

* - Argument
  - Default
  - Accepted
* - `block_size`
  - none: required
  - 128 bytes or more. The partition's `size` is a whole number of
    blocks.
* - `block_count`
  - `size / block_size`
  - 2 or more, and at most `size / block_size`.
* - `read_size`, `prog_size`
  - `block_size`
  - 1 or more; `block_size` is a multiple of both.
* - `name_max`
  - 255
  - 1 to 255
* - `file_max`
  - 2147483647
  - 1 to 2147483647
* - `attr_max`
  - 1022
  - 1 to 1022
* - `disk_version`
  - `"2.1"`
  - `"2.0"` or `"2.1"`, or the number 2.0 or 2.1. littlefs 2.6 and
    later write 2.1; a firmware on an older littlefs reads only 2.0.
```

The three limits default to littlefs's own maximums. A firmware built
with smaller ones refuses a volume that records larger ones, so give the
firmware's values. Jig writes the folder's directories and files in name
order, so one folder and one set of arguments give the same bytes on
every host. A name longer than `name_max` bytes of UTF-8, or a file
larger than `file_max`, is an error before the build starts, and a folder
that doesn't fit is an error that says how many blocks it takes.

A file given as a littlefs partition's contents is a prebuilt volume. Its
superblock must agree with the arguments on the on-disk version, the
block size and count and the three limits; `read_size` and `prog_size`
aren't recorded on disk. Reading a littlefs partition back mounts it with
its arguments, as the firmware does, and a superblock that disagrees is an
error that lists each difference. Paths are case-sensitive.

### fat

Jig writes FAT12, FAT16 and FAT32 volumes itself, from Microsoft's FAT32
File System Specification (version 1.03), and reads them back from their
own boot sector.

```{list-table}
:header-rows: 1
:widths: 18 40 42

* - Argument
  - Default
  - Accepted
* - `fat_type`
  - The specification's choice for the volume's size: up to 8,400 sectors
    of 512 bytes FAT12, up to 1,048,576 (512 MiB) FAT16, above that
    FAT32. With `cluster_size` given, the type its cluster count makes.
  - 12, 16 or 32
* - `sector_size`
  - 512
  - 512, 1024, 2048 or 4096
* - `sector_count`
  - `size / sector_size`
  - 1 to 4294967295, at most `size / sector_size`
* - `cluster_size`
  - FAT16 and FAT32: the specification's tables for the volume's size,
    else the smallest size that gives a valid cluster count. FAT12: the
    smallest such size.
  - `sector_size` times 1, 2, 4 and so on up to 128
* - `label`
  - none (the boot sector says `NO NAME`)
  - 1 to 11 of A to Z, 0 to 9, space and ``!#$%&'()-@^_`{}~``, not
    starting with a space; lower case becomes upper case
* - `volume_id`
  - 0
  - 0 to 0xFFFFFFFF
* - `time`
  - `"1980-01-01T00:00:00"`
  - ISO 8601 date and time from 1980-01-01 to 2107-12-31T23:59:59; a time
    zone is dropped and fractions of a second cut
```

Every volume has two FATs and media byte 0xF8. FAT12 and FAT16 have one
reserved sector and 512 root directory entries; FAT32 has 32 reserved
sectors, its root directory at cluster 2, FSInfo at sector 1, and copies
of the boot sector and FSInfo at sectors 6 and 7. Every directory entry,
labels and `.` and `..` included, carries `time`. With `time` and
`volume_id` fixed, as their defaults are, one folder gives the same bytes
every time.

A name in 8.3 form and upper case is its own short name. Every other name
gets long-name entries, up to 255 UTF-16 units, and a short name built
from it with a `~N` tail. A folder holding two names that differ only in
case is an error, and so is a name holding `"*/:<>?\|` or a control
character, one that starts or ends with a space or ends with a period,
more than 512 entries in a FAT12 or FAT16 root directory, and a folder of
more than 65,536 entries. Jig makes no volume of 4085 or 65525 clusters,
the two counts that Microsoft's specification and FatFs put in different
FAT types.

The erased value fills only the data clusters no file uses, the bytes
after each file's end in its last cluster, and the bytes past
`sector_count`. The reserved sectors, both FATs and a FAT12 or FAT16 root
directory are written in full with zeros where nothing else goes: a FAT
entry of 0 marks a free cluster and a directory entry starting with 0 ends
the directory, so 0xFF there would read as a full root directory and no
free cluster.

A prebuilt FAT volume must agree with the arguments on the sector size
and count, the cluster size and the FAT type. Reading one back takes its
geometry from its boot sector, so a volume the firmware formatted again
still reads. Names compare as FAT compares them, ignoring case; an
entry's `path` keeps the case it was stored with.

## A system's storage map

A system gives each board instance its own contents with the `storage`
setting of its board line:

```jig
board a: "logger" (storage: {data: "fixtures/full-disk"})
board b: "logger" (storage: {config: "fixtures/config.bin"})
board c: "logger" (storage: {flash0: {0: "boot.bin", 0x100000: "app.bin"}})
board d: "logger" (storage: {flash0: "dump.bin", "mcu.flash": "cal.bin"})
```

```{list-table}
:header-rows: 1
:widths: 26 74

* - Key
  - Value
* - A partition (`data`)
  - A folder, which the partition's format builds; a file, placed as it
    is with the rest of the partition erased; or a map from offset to
    file, each offset counted from the partition's first byte. The value
    replaces the board file's `contents` for that partition.
* - A storage device (`flash0`)
  - A file, the base image of the whole device from byte 0, as in Jig
    0.2; a shorter file leaves the rest erased. Or a map from offset to
    file, each placed at that offset of the device.
* - A device memory in quotes (`"mcu.flash"`)
  - A file, the memory's base image from its first byte, or a map from
    offset to file.
```

Paths are relative to the plan's directory. A folder fills only a
partition: a folder for a whole storage device or device memory is an
error. An offset is a whole number of bytes, and one past the end of its
partition, device or memory is an error. A key that names nothing on the
board is an error that lists what the board has. `jig boot --storage
NAME=PATH` takes the same keys, with paths relative to the current
directory ({doc}`cli`).

## Composition

For each storage device and each device memory that something writes,
before the device boots:

1. Every byte starts at the target's erased value. For a storage device
   that is its model's `ErasedValue` (0xFF unless the model says
   otherwise); for a device memory, its family's fill, 0xFF for an STM32's
   flash and 0 for RAM.
2. The base image goes in at byte 0: the system's file for the whole
   target, else the boot image the device's family builds for it. Only a
   Zynq-7000 booting from Quad-SPI has one: the board plugin's
   `qspi_image`, for the flash the boot reads, when the system gives no
   file for that flash. A board whose plugin has no `qspi_image` gets no
   base image, so its partitions can hold the whole boot image.
3. Each partition's contents go in, in the order the board file declares
   the partitions, then each file the system places at an offset.
4. A storage device's bytes go to `<machine>/<storage>.img`, which its
   model attaches; a device memory's regions go to
   `<machine>/memories/`, and the family writes them when the memory's
   kind says.

A folder is built once per content: Jig keeps the volume in its cache,
keyed by the folder's paths and file contents, the format and its
arguments, the partition's size, the erased value, and the versions of
Jig and of the library that writes the format.

Composition stops the run before the device boots, with exit code 2. It
refuses:

- a byte written twice: by a partition and a file the system places at
  an offset, or by two such files;
- contents that land on bytes of the base image that aren't erased, so a
  boot image and a file system never overwrite each other unnoticed;
- a file larger than its partition, a base image larger than its target,
  and a file at an offset that runs past the end of its partition, device
  or memory;
- a missing file or folder, and a folder for a whole target;
- a prebuilt volume the partition's format refuses, and a folder it
  can't build;
- a storage device whose `size` property disagrees with its model's.

One error names every problem with the target, one problem a line
(wrapped here):

```text
storage flash0: 2 problems composing it:
    the storage map's config: config.bin (5000 bytes) is larger than
    partition config (4096 bytes)
    the storage map's data: prebuilt.bin as littlefs: no littlefs
    superblock: neither of the first two blocks starts with one (the name
    "littlefs" at byte 8)
```

`jig check` composes each target the board's own partitions write, with
0xFF for every storage device and without the family's boot image, and
reports the same problems; `jig check --contracts` builds the machine in
Renode and composes with each model's own erased value.

## A device's own memories

A partition's `on` can name a memory of the device instead of a storage
peripheral. These memories aren't on the `storage` list; the family names
them and says when Jig writes them:

```{list-table}
:header-rows: 1
:widths: 14 32 16 38

* - Family
  - Memory
  - Kind
  - When Jig writes its partitions
* - `stm32`
  - `flash`
  - non-volatile
  - Once, before the first boot, a board that starts without power
    included. What the firmware programs stays, as on silicon.
* - `stm32`
  - `sram`, or `sram1` to `sram5`
  - volatile
  - At the first boot, before the ELF loads, and at every power-on.
* - `zynq7000`
  - `ocm`, 256 KiB: `ocmLow` holds the first 192 KiB at 0x0, `ocmHigh`
    the last 64 KiB at 0xFFFF0000
  - volatile
  - At every power-on: the BootROM model writes it after it clears OCM
    and before it reads the boot device.
* - `zynq7000`
  - Block RAM of the programmable logic, by its block-design name
    (`axi_bram_ctrl_0`) or `ip` line
  - PL block RAM
  - Each time the programmable logic is configured
    ({doc}`programmable_logic`).
```

A memory's partitions and the system's files for it are composed as a
storage device's are, with the memory's fill as the erased value. A
memory nothing writes is left alone.

### STM32

The memories come from the part's table entry, or from the platform file
for a part Jig reads one for:

```{list-table}
:header-rows: 1
:widths: 22 78

* - Part
  - Memories
* - `STM32F407...`
  - `flash`, 2 MiB at 0x08000000, as Renode's STM32F4 platform maps it;
    `sram`, 128 KiB at 0x20000000, as Jig maps SRAM1 and SRAM2
* - `STM32U575...`
  - `flash`, 2 MiB at 0x08000000; `sram1` (192 KiB), `sram2` (64 KiB),
    `sram3` (512 KiB) and `sram4` (16 KiB)
* - `STM32U595...`
  - `flash`, 4 MiB at 0x08000000; `sram1` (768 KiB), `sram2` (64 KiB),
    `sram3` (832 KiB), `sram4` (16 KiB) and `sram5` (832 KiB)
* - A `platform` file
  - Its `Memory.MappedMemory` entries named `flash`, `sram` and `sram1`
    to `sram5`
```

Renode's platform maps 2 MiB of flash for every STM32F407, while an
STM32F407VG has 1 MiB (the G of its part number, in the datasheet's
ordering information): a partition between 1 MiB and 2 MiB works in the
simulation and not on that part.

`board.boot()`, which runs the boot flow again without a power-on, writes
neither memory. `board.power_cycle()` writes SRAM again and leaves flash
as the firmware left it.

An STM32 boot loads the ELF's segments at their physical addresses.
Renode 1.17.0's `LoadELF` writes `p_memsz` bytes of each load segment at
its `p_paddr`: the segment's bytes from the file, then zeros. That reaches
past the code when a `.bss` has a physical address in flash. GNU ld gives
the storage fixture's `.bss`, which has no bytes in the file, the physical
address just past the code, so Renode writes as many zeros there as
`.bss` is long. Nothing is programmed there on silicon.

Before each boot, Jig compares each load segment's `p_memsz` bytes at
`p_paddr` with every partition in flash, with or without contents, and
with every file the system places in flash. One that overlaps stops the
boot with an error that names both, and says how many of the segment's
bytes are zeros (one line, wrapped here):

```text
dut: firmware.elf's segment at 0x08000000-0x080001F8 lands on partition
vectors on mcu.flash (0x08000000-0x080003FF)
```

`jig check` reports the same for a prebuilt ELF.

### Zynq-7000

OCM's partitions load at the first boot, at a power-on after
`power_off()`, and at `board.boot()`, which replays power-on through the
BootROM in both boot modes. A system reset clears OCM, as the BootROM
model's `clearOcmOnReset` says, and writes nothing. A Quad-SPI boot's
BootROM then copies the first-stage boot loader into OCM over whatever
the partitions put there.

The block RAM a partition can name is each AXI BRAM controller of the
block design that keeps Jig's model or that an `ip` line gives
`memory(...)`, and each `ip` line's own `memory(...)` block. A block RAM
named `ocm` is an error.

## Reading back

Every partition is a member of its board's handle: `dut.nor_lfs`.
`bytes()` gives the region as it stands; a partition whose format holds
files also has `files()`, `walk()`, `read(path)`, `exists(path)` and
`stat(path)` ({ref}`partition-members`). On a storage device the members
read the device's image file, which the model writes as each program or
erase completes; on a device memory they read the machine. Plans read
between steps, while Renode is paused, and every call reads the bytes
again.

littlefs commits atomically, so a littlefs partition always reads as the
last commit left it. FAT doesn't: a read can catch a file the firmware is
halfway through writing, so read a FAT partition once the firmware is
idle.

## Images without a run

`jig image build BOARD` writes each storage device's composed image to a
directory without starting Renode: the same bytes a run attaches, which
can also go onto a board. `--storage NAME=PATH` gives it what a system's
storage map would, such as a folder for a partition the board file leaves
empty. `jig image ls` lists the files in a partition of an image, such as
a run's `<machine>/flash0.img`, and `jig image extract` copies them out
({doc}`cli`).

## Formats from plugins

A board's plugin can add a format of its own, such as a firmware's key-value
store kept in redundant copies, with `api.storage_format(name, format)`.
Only that board's partitions see it ({doc}`../how_to/write_a_plugin`).
