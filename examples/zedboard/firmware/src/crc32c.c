// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power

#include "crc32c.h"

static uint32_t table[256];

void crc32c_init(void)
{
    for (uint32_t i = 0; i < 256u; i++) {
        uint32_t crc = i;
        for (int bit = 0; bit < 8; bit++) {
            crc = (crc & 1u) != 0 ? (crc >> 1) ^ 0x82F63B78u : crc >> 1;
        }
        table[i] = crc;
    }
}

uint32_t crc32c(const uint8_t *data, uint32_t length)
{
    uint32_t crc = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < length; i++) {
        crc = table[(crc ^ data[i]) & 0xFFu] ^ (crc >> 8);
    }
    return crc ^ 0xFFFFFFFFu;
}
