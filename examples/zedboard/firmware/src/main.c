// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Two-node CSP example for the ZedBoard. Interrupt use is limited to the 1 ms
// private timer tick, which also services the UART and CAN FIFOs; the main
// loop handles everything else and sleeps in WFI between ticks.

#include "can.h"
#include "cfp.h"
#include "clock.h"
#include "config.h"
#include "console.h"
#include "crc32c.h"
#include "irq.h"
#include "kiss.h"

void tick_service(void)
{
    uart_service(&console_port);
    if (NODE_HAS_KISS) {
        uart_service(&kiss_port);
    }
    can_service();
}

int main(void)
{
    uint32_t clock_faults = clock_init();
    crc32c_init();
    console_init();
    if (NODE_HAS_KISS) {
        kiss_init();
    }
    bool can_normal = cfp_init();
    gic_init();
    tick_init();
    irq_enable();
    console_banner(clock_faults, can_normal);

    for (;;) {
        bool busy = false;
        if (NODE_HAS_KISS) {
            busy |= kiss_poll();
        }
        busy |= cfp_poll();
        busy |= console_poll();
        if (!busy) {
            wait_for_interrupt();
        }
    }
}
