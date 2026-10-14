// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// CRC-32C (Castagnoli): polynomial 0x1EDC6F41, processed LSB first (reflected
// 0x82F63B78), initial value and final XOR 0xFFFFFFFF. The check value for the
// ASCII string "123456789" is 0xE3069283.

#ifndef CRC32C_H
#define CRC32C_H

#include <stdint.h>

void crc32c_init(void);
uint32_t crc32c(const uint8_t *data, uint32_t length);

#endif
