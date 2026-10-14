// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power

#include "irq.h"

#include "config.h"
#include "lib.h"
#include "uart.h"
#include "zynq7000.h"

#define TICK_PRIORITY       0xA0u   // lower value = higher priority
#define PRIORITY_MASK       0xF0u   // signal everything above the lowest levels
#define GIC_ID_REGISTERS    3u      // interrupt IDs 0 to 95 (UG585 7.2)

volatile uint32_t tick_ms;

// IHI0048 4.3 and 4.4: everything off and cleared, then the timer's PPI on.
void gic_init(void)
{
    reg_write(ICDDCR, 0);
    for (uint32_t n = 0; n < GIC_ID_REGISTERS; n++) {
        reg_write(ICDICER(n), 0xFFFFFFFFu);
        reg_write(ICDICPR(n), 0xFFFFFFFFu);
    }
    uint32_t shift = 8u * (IRQ_ID_PRIVATE_TIMER % 4u);
    reg_modify(ICDIPR(IRQ_ID_PRIVATE_TIMER / 4u), 0xFFu << shift, TICK_PRIORITY << shift);
    reg_write(ICDISER(IRQ_ID_PRIVATE_TIMER / 32u), 1u << (IRQ_ID_PRIVATE_TIMER % 32u));
    reg_write(ICCPMR, PRIORITY_MASK);
    reg_write(ICCICR, 1u);      // EnableS: signal secure (group 0) interrupts as IRQ
    reg_write(ICDDCR, 1u);
}

// DDI0407 4.1: the period is (PRESCALER + 1) x (LOAD + 1) / CPU_3x2x.
void tick_init(void)
{
    reg_write(PTIMER_CONTROL, 0);
    reg_write(PTIMER_ISR, PTIMER_ISR_EVENT);
    reg_write(PTIMER_LOAD, CPU_3X2X_HZ / TICK_HZ - 1u);
    reg_write(PTIMER_CONTROL, PTIMER_CTRL_ENABLE | PTIMER_CTRL_AUTO_RELOAD | PTIMER_CTRL_IRQ_ENABLE);
}

void irq_handler(void)
{
    uint32_t iar = reg_read(ICCIAR);
    uint32_t id = iar & 0x3FFu;
    if (id == GIC_SPURIOUS_ID) {
        return;
    }
    if (id == IRQ_ID_PRIVATE_TIMER) {
        reg_write(PTIMER_ISR, PTIMER_ISR_EVENT);
        tick_ms++;
        tick_service();
    }
    reg_write(ICCEOIR, iar);
}

static void hex32(char *out, uint32_t value)
{
    static const char digits[] = "0123456789abcdef";
    out[0] = '0';
    out[1] = 'x';
    for (int i = 0; i < 8; i++) {
        out[2 + i] = digits[(value >> (28 - 4 * i)) & 0xFu];
    }
    out[10] = '\0';
}

// Reached from start.S with IRQ and FIQ masked, on the abort stack.
_Noreturn void fault_handler(uint32_t kind, uint32_t address)
{
    static const char *const names[] = {
        "exception", "undefined instruction", "SVC", "prefetch abort", "data abort", "FIQ",
    };
    char text[11];
    uart_write_direct(UART1_BASE, "\r\nfault: ");
    uart_write_direct(UART1_BASE, kind < sizeof names / sizeof names[0] ? names[kind] : names[0]);
    uart_write_direct(UART1_BASE, " at ");
    hex32(text, address);
    uart_write_direct(UART1_BASE, text);
    if (kind == 4u) {
        uint32_t dfar;
        uint32_t dfsr;
        __asm__ volatile("mrc p15, 0, %0, c6, c0, 0" : "=r"(dfar));
        __asm__ volatile("mrc p15, 0, %0, c5, c0, 0" : "=r"(dfsr));
        uart_write_direct(UART1_BASE, ", DFAR ");
        hex32(text, dfar);
        uart_write_direct(UART1_BASE, text);
        uart_write_direct(UART1_BASE, ", DFSR ");
        hex32(text, dfsr);
        uart_write_direct(UART1_BASE, text);
    }
    uart_write_direct(UART1_BASE, "\r\n");
    for (;;) {
        wait_for_interrupt();
    }
}
