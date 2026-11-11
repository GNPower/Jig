// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Output on USART2 (PA2), 115200 baud from the 16 MHz reset clock.

#ifndef CONSOLE_H
#define CONSOLE_H

#include <stdint.h>

void console_start(void);
void print(const char *text);
void print_unsigned(uint32_t value);
void print_signed(int32_t value);
// Lower-case hexadecimal, zero-padded to digits, without a 0x prefix.
void print_hex(uint32_t value, unsigned digits);
// Bytes as text: printable ASCII as it is, except backslash and the double
// quote, which get a backslash in front; \n, \r and \t for those control
// characters; \xHH for any other byte. A file's contents stays on one line.
void print_escaped(const uint8_t *data, uint32_t size);

#endif
