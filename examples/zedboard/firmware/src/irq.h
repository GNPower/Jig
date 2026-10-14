// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// The one interrupt this firmware takes: the Cortex-A9 private timer (PPI 29)
// through the GIC, once per millisecond.

#ifndef IRQ_H
#define IRQ_H

#include <stdint.h>

extern volatile uint32_t tick_ms;

static inline uint32_t millis(void)
{
    return tick_ms;
}

void gic_init(void);
void tick_init(void);

// Supplied by the application; runs in the tick interrupt.
void tick_service(void);

// Entry points start.S branches to.
void irq_handler(void);
_Noreturn void fault_handler(uint32_t kind, uint32_t address);

#endif
