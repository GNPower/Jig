// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// The storage fixture's firmware. On Renode's STM32F4 platform it prints on
// USART2 what it finds in the partitions board.jig declares (layout.h), one
// volume after the other, then "storage fixture done":
//
//   mcu_lfs  LittleFS in the MCU's own flash, read in place with memcpy
//   nor_lfs  LittleFS on the SPI NOR flash: listed, /written.txt written,
//            listed again
//   nor_fat  FAT on the SPI NOR flash, read-only (fat.c)
//   nor_raw  the first 32 bytes of a raw region of the SPI NOR flash, in hex
//
// Each volume prints what its superblock or boot sector says, then a line
// for every directory and file, depth first:
//
//   nor_lfs: littlefs 2.1, 256 blocks of 4096 bytes
//   nor_lfs: dir /config
//   nor_lfs: file /config/settings.txt 30 "mode = \"fixture\"\nrate = 10 Hz\n"
//
// A file's line gives its path, its size in bytes and its contents as
// print_escaped writes them (console.h). A step that fails prints a line
// with "failed" and the error code, and the firmware goes on.
//
// The vector table follows PM0214 (Cortex-M4 programming manual) 2.3.4.

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "console.h"
#include "fat.h"
#include "layout.h"
#include "lfs.h"
#include "spi_nor.h"

#define LFS_CACHE_SIZE 256u
#define LFS_LOOKAHEAD_SIZE 16u
#define LFS_BLOCK_CYCLES 500
#define PATH_BYTES 512u
#define DEPTH_MAX 8u

static const char WRITTEN_PATH[] = "/written.txt";
// Longer than LFS_CACHE_SIZE, so littlefs can't inline it in the directory
// and the write erases and programs a block of its own.
static const char WRITTEN_TEXT[] =
    "Written by the storage fixture's firmware at boot.\n"
    "At more than 256 bytes, the cache size, the file is too long for littlefs\n"
    "to keep in its directory. littlefs gives it a block of its own: the\n"
    "firmware erases the block with command 0x20, then programs it with 0x02.\n";

extern uint32_t _stack_top;
extern uint32_t _data_start, _data_end, _data_load, _bss_start, _bss_end;

void reset_handler(void);
void fault_handler(void);

// Initial stack pointer, reset, then the 14 system exceptions.
__attribute__((section(".vectors"), used)) static const uint32_t vectors[16] = {
    (uint32_t)&_stack_top,
    (uint32_t)reset_handler,
    (uint32_t)fault_handler, // NMI
    (uint32_t)fault_handler, // HardFault
    (uint32_t)fault_handler, // MemManage
    (uint32_t)fault_handler, // BusFault
    (uint32_t)fault_handler, // UsageFault
    0u, 0u, 0u, 0u,
    (uint32_t)fault_handler, // SVCall
    (uint32_t)fault_handler, // DebugMonitor
    0u,
    (uint32_t)fault_handler, // PendSV
    (uint32_t)fault_handler, // SysTick
};

void fault_handler(void)
{
    print("fault\r\n");
    for(;;)
    {
    }
}

static void failed(const char *volume, const char *what, int32_t error)
{
    print(volume);
    print(": ");
    print(what);
    print(" failed (");
    print_signed(error);
    print(")\r\n");
}

// ------------------------------------------------------------ block devices

static int mcu_flash_read(const struct lfs_config *config, lfs_block_t block, lfs_off_t offset,
                          void *buffer, lfs_size_t size)
{
    memcpy(buffer, (const uint8_t *)MCU_LFS_ADDRESS + block * config->block_size + offset, size);
    return LFS_ERR_OK;
}

// The firmware only reads mcu_lfs, so littlefs never programs or erases it.
static int mcu_flash_prog(const struct lfs_config *config, lfs_block_t block, lfs_off_t offset,
                          const void *buffer, lfs_size_t size)
{
    (void)config;
    (void)block;
    (void)offset;
    (void)buffer;
    (void)size;
    return LFS_ERR_IO;
}

static int mcu_flash_erase(const struct lfs_config *config, lfs_block_t block)
{
    (void)config;
    (void)block;
    return LFS_ERR_IO;
}

static int nor_read(const struct lfs_config *config, lfs_block_t block, lfs_off_t offset,
                    void *buffer, lfs_size_t size)
{
    spi_nor_read(NOR_LFS_OFFSET + block * config->block_size + offset, buffer, size);
    return LFS_ERR_OK;
}

static int nor_prog(const struct lfs_config *config, lfs_block_t block, lfs_off_t offset,
                    const void *buffer, lfs_size_t size)
{
    uint32_t address = NOR_LFS_OFFSET + block * config->block_size + offset;
    return spi_nor_program(address, buffer, size) == 0 ? LFS_ERR_OK : LFS_ERR_IO;
}

static int nor_erase(const struct lfs_config *config, lfs_block_t block)
{
    uint32_t address = NOR_LFS_OFFSET + block * config->block_size;
    return spi_nor_erase_sector(address) == 0 ? LFS_ERR_OK : LFS_ERR_IO;
}

static int sync_nothing(const struct lfs_config *config)
{
    (void)config;
    return LFS_ERR_OK;
}

static int nor_fat_read(void *context, uint32_t offset, void *buffer, uint32_t size)
{
    (void)context;
    if(offset > NOR_FAT_SIZE || size > NOR_FAT_SIZE - offset)
    {
        return -1; // past the partition
    }
    spi_nor_read(NOR_FAT_OFFSET + offset, buffer, size);
    return 0;
}

// ------------------------------------------------------------ littlefs

static uint8_t mcu_read_cache[LFS_CACHE_SIZE];
static uint8_t mcu_prog_cache[LFS_CACHE_SIZE];
static uint8_t mcu_lookahead[LFS_LOOKAHEAD_SIZE];
static uint8_t nor_read_cache[LFS_CACHE_SIZE];
static uint8_t nor_prog_cache[LFS_CACHE_SIZE];
static uint8_t nor_lookahead[LFS_LOOKAHEAD_SIZE];
static uint8_t file_cache[LFS_CACHE_SIZE];

// Zero for name_max, file_max and attr_max: the limits the superblock records.
static const struct lfs_config MCU_LFS = {
    .read = mcu_flash_read,
    .prog = mcu_flash_prog,
    .erase = mcu_flash_erase,
    .sync = sync_nothing,
    .read_size = LFS_READ_SIZE,
    .prog_size = LFS_PROG_SIZE,
    .block_size = MCU_LFS_BLOCK_SIZE,
    .block_count = MCU_LFS_BLOCK_COUNT,
    .block_cycles = LFS_BLOCK_CYCLES,
    .cache_size = LFS_CACHE_SIZE,
    .lookahead_size = LFS_LOOKAHEAD_SIZE,
    .read_buffer = mcu_read_cache,
    .prog_buffer = mcu_prog_cache,
    .lookahead_buffer = mcu_lookahead,
};

static const struct lfs_config NOR_LFS = {
    .read = nor_read,
    .prog = nor_prog,
    .erase = nor_erase,
    .sync = sync_nothing,
    .read_size = LFS_READ_SIZE,
    .prog_size = LFS_PROG_SIZE,
    .block_size = NOR_LFS_BLOCK_SIZE,
    .block_count = NOR_LFS_BLOCK_COUNT,
    .block_cycles = LFS_BLOCK_CYCLES,
    .cache_size = LFS_CACHE_SIZE,
    .lookahead_size = LFS_LOOKAHEAD_SIZE,
    .read_buffer = nor_read_cache,
    .prog_buffer = nor_prog_cache,
    .lookahead_buffer = nor_lookahead,
};

// One file is open at a time, so the volumes share its cache.
static const struct lfs_file_config FILE_CONFIG = {.buffer = file_cache};

static char path[PATH_BYTES];

static void print_lfs_file(const char *volume, lfs_t *lfs, lfs_size_t size)
{
    lfs_file_t file;
    uint8_t data[64];
    lfs_ssize_t count;
    print(volume);
    print(": file ");
    print(path);
    print(" ");
    print_unsigned(size);
    print(" \"");
    int error = lfs_file_opencfg(lfs, &file, path, LFS_O_RDONLY, &FILE_CONFIG);
    if(error < 0)
    {
        print("\"\r\n");
        failed(volume, "open", error);
        return;
    }
    while((count = lfs_file_read(lfs, &file, data, sizeof data)) > 0)
    {
        print_escaped(data, (uint32_t)count);
    }
    lfs_file_close(lfs, &file);
    print("\"\r\n");
    if(count < 0)
    {
        failed(volume, "read", count);
    }
}

// Lists the directory at path (the root when length is 0) and everything
// below it.
static void walk_lfs(const char *volume, lfs_t *lfs, uint32_t length, unsigned depth)
{
    lfs_dir_t dir;
    struct lfs_info info;
    int error = lfs_dir_open(lfs, &dir, length == 0u ? "/" : path);
    if(error < 0)
    {
        failed(volume, "open dir", error);
        return;
    }
    while((error = lfs_dir_read(lfs, &dir, &info)) > 0)
    {
        if(info.name[0] == '.' &&
           (info.name[1] == '\0' || (info.name[1] == '.' && info.name[2] == '\0')))
        {
            continue;
        }
        uint32_t name_length = strlen(info.name);
        if(length + 1u + name_length >= PATH_BYTES)
        {
            failed(volume, "path", LFS_ERR_NAMETOOLONG);
            continue;
        }
        path[length] = '/';
        memcpy(path + length + 1u, info.name, name_length + 1u);
        if(info.type == LFS_TYPE_DIR)
        {
            print(volume);
            print(": dir ");
            print(path);
            print("\r\n");
            if(depth + 1u < DEPTH_MAX)
            {
                walk_lfs(volume, lfs, length + 1u + name_length, depth + 1u);
            }
        }
        else
        {
            print_lfs_file(volume, lfs, info.size);
        }
        path[length] = '\0';
    }
    lfs_dir_close(lfs, &dir);
    if(error < 0)
    {
        failed(volume, "read dir", error);
    }
}

static void list_lfs(const char *volume, lfs_t *lfs)
{
    path[0] = '\0';
    walk_lfs(volume, lfs, 0u, 0u);
}

// Mounts a volume and prints what its superblock says; 0 when mounted.
static int mount_lfs(const char *volume, lfs_t *lfs, const struct lfs_config *config)
{
    struct lfs_fsinfo info;
    int error = lfs_mount(lfs, config);
    if(error < 0)
    {
        failed(volume, "mount", error);
        return error;
    }
    error = lfs_fs_stat(lfs, &info);
    if(error < 0)
    {
        failed(volume, "stat", error);
        return 0;
    }
    print(volume);
    print(": littlefs ");
    print_unsigned(info.disk_version >> 16);
    print(".");
    print_unsigned(info.disk_version & 0xFFFFu);
    print(", ");
    print_unsigned(info.block_count);
    print(" blocks of ");
    print_unsigned(info.block_size);
    print(" bytes\r\n");
    return 0;
}

static int write_file(lfs_t *lfs)
{
    lfs_file_t file;
    lfs_size_t size = sizeof WRITTEN_TEXT - 1u;
    int error = lfs_file_opencfg(lfs, &file, WRITTEN_PATH, LFS_O_WRONLY | LFS_O_CREAT | LFS_O_TRUNC,
                                 &FILE_CONFIG);
    if(error < 0)
    {
        return error;
    }
    lfs_ssize_t written = lfs_file_write(lfs, &file, WRITTEN_TEXT, size);
    error = lfs_file_close(lfs, &file);
    if(written < 0)
    {
        return written;
    }
    if((lfs_size_t)written != size)
    {
        return LFS_ERR_NOSPC;
    }
    return error;
}

static void show_mcu_lfs(void)
{
    static lfs_t lfs;
    if(mount_lfs("mcu_lfs", &lfs, &MCU_LFS) != 0)
    {
        return;
    }
    list_lfs("mcu_lfs", &lfs);
    lfs_unmount(&lfs);
}

static void show_nor_lfs(void)
{
    static lfs_t lfs;
    if(mount_lfs("nor_lfs", &lfs, &NOR_LFS) != 0)
    {
        return;
    }
    list_lfs("nor_lfs", &lfs);
    int error = write_file(&lfs);
    if(error < 0)
    {
        failed("nor_lfs", "write", error);
    }
    else
    {
        print("nor_lfs: wrote ");
        print(WRITTEN_PATH);
        print(" ");
        print_unsigned(sizeof WRITTEN_TEXT - 1u);
        print("\r\n");
    }
    list_lfs("nor_lfs", &lfs);
    lfs_unmount(&lfs);
}

// ------------------------------------------------------------ FAT and raw

static void print_piece(const uint8_t *data, uint32_t size, void *argument)
{
    (void)argument;
    print_escaped(data, size);
}

static int print_fat_entry(struct fat_volume *volume, const struct fat_entry *entry,
                           void *argument)
{
    (void)argument;
    if(entry->directory)
    {
        print("nor_fat: dir ");
        print(entry->path);
        print("\r\n");
        return 0;
    }
    print("nor_fat: file ");
    print(entry->path);
    print(" ");
    print_unsigned(entry->size);
    print(" \"");
    int error = fat_read_file(volume, entry, print_piece, NULL);
    print("\"\r\n");
    if(error != 0)
    {
        failed("nor_fat", "read", error);
    }
    return 0;
}

static void show_nor_fat(void)
{
    static struct fat_volume volume;
    int error = fat_mount(&volume, nor_fat_read, NULL);
    if(error != 0)
    {
        failed("nor_fat", "mount", error);
        return;
    }
    print("nor_fat: FAT");
    print_unsigned(volume.type);
    print(", ");
    print_unsigned(volume.total_sectors);
    print(" sectors of ");
    print_unsigned(volume.bytes_per_sector);
    print(" bytes, label \"");
    print(volume.label);
    print("\"\r\n");
    error = fat_walk(&volume, print_fat_entry, NULL);
    if(error != 0)
    {
        failed("nor_fat", "walk", error);
    }
}

static void show_nor_raw(void)
{
    uint8_t data[NOR_RAW_SHOWN];
    spi_nor_read(NOR_RAW_OFFSET, data, sizeof data);
    print("nor_raw:");
    for(uint32_t i = 0u; i < sizeof data; i++)
    {
        print(" ");
        print_hex(data[i], 2u);
    }
    print("\r\n");
}

void reset_handler(void)
{
    const uint32_t *from = &_data_load;
    for(uint32_t *to = &_data_start; to < &_data_end;)
    {
        *to++ = *from++;
    }
    for(uint32_t *to = &_bss_start; to < &_bss_end;)
    {
        *to++ = 0u;
    }
    console_start();
    spi_nor_start();
    show_mcu_lfs();
    show_nor_lfs();
    show_nor_fat();
    show_nor_raw();
    print("storage fixture done\r\n");
    for(;;)
    {
        __asm__ volatile("wfi");
    }
}
