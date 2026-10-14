// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power

#ifndef CLOCK_H
#define CLOCK_H

#include <stdint.h>

#define CLOCK_ARM_PLL_UNLOCKED  (1u << 0)
#define CLOCK_IO_PLL_UNLOCKED   (1u << 1)

// Brings the clocks this firmware depends on to the plan in config.h, routes
// UART0 and CAN0 to their MIO pins and resets the controllers it uses.
// Returns 0, or CLOCK_* bits for PLLs that did not report lock.
uint32_t clock_init(void);

#endif
