// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// A serial NOR flash with 3-byte addresses on SPI1: SCK, MISO and MOSI on
// PA5 to PA7, chip select driven as a GPIO output on PA4. It sends five
// commands, as the fixture flash model (SpiNorFlash.cs) takes them: 0x03
// READ, 0x02 PAGE PROGRAM (256-byte pages), 0x20 4 KiB SECTOR ERASE, 0x05
// READ STATUS (bit 0, busy) and 0x06 WRITE ENABLE.

#ifndef SPI_NOR_H
#define SPI_NOR_H

#include <stdint.h>

#define SPI_NOR_SECTOR_SIZE 4096u

void spi_nor_start(void);
void spi_nor_read(uint32_t address, void *buffer, uint32_t size);
// 0, or -1 when the flash stays busy past the polling limit.
int spi_nor_program(uint32_t address, const void *data, uint32_t size);
int spi_nor_erase_sector(uint32_t address);

#endif
