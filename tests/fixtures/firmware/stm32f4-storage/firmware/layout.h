// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Where the firmware finds its storage: the partitions of board.jig, which
// builds each one with the arguments here.

#ifndef LAYOUT_H
#define LAYOUT_H

// mcu_lfs: LittleFS in the MCU's own flash, 128 KiB from 128 KiB in, which
// is sector 5 of the STM32F407's flash (RM0090 3.3). The linker script ends
// the firmware's flash where it starts.
#define MCU_LFS_ADDRESS 0x08020000u
#define MCU_LFS_BLOCK_SIZE 4096u
#define MCU_LFS_BLOCK_COUNT 32u

// nor_lfs: LittleFS on the SPI NOR flash, 1 MiB from 1 MiB in. A block is
// the flash's 4 KiB erase sector.
#define NOR_LFS_OFFSET 0x100000u
#define NOR_LFS_BLOCK_SIZE 4096u
#define NOR_LFS_BLOCK_COUNT 256u

// Both LittleFS volumes read and program in units of 16 bytes.
#define LFS_READ_SIZE 16u
#define LFS_PROG_SIZE 16u

// nor_fat: FAT on the SPI NOR flash, 1 MiB from 2 MiB in.
#define NOR_FAT_OFFSET 0x200000u
#define NOR_FAT_SIZE 0x100000u

// nor_raw: a raw region of the SPI NOR flash, from 3 MiB in.
#define NOR_RAW_OFFSET 0x300000u
#define NOR_RAW_SHOWN 32u

#endif
