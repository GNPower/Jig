// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Line console on UART1 (the ZedBoard's USB-UART), prompt "zed> ".
// Commands: help, status, stats, uptime, ping <addr> [size].

#ifndef CONSOLE_H
#define CONSOLE_H

#include "uart.h"

extern uart_port console_port;

void console_init(void);
void console_banner(uint32_t clock_faults, bool can_normal);
bool console_poll(void);

void con_printf(const char *format, ...) __attribute__((format(printf, 1, 2)));

#endif
