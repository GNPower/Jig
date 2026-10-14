// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Cadence UART (UG585 chapter 19), polled. The 1 ms tick interrupt moves bytes
// between the 64-byte hardware FIFOs and the software rings; at 115200 baud a
// FIFO holds 5.5 ms of data, so nothing is lost between two ticks.

#ifndef UART_H
#define UART_H

#include "ring.h"

typedef struct {
    uint32_t base;
    byte_ring rx;
    byte_ring tx;
    volatile uint32_t rx_overruns;  // RX FIFO overruns the controller reported
    volatile uint32_t rx_dropped;   // bytes lost to a full RX ring
} uart_port;

void uart_init(uart_port *port);
void uart_service(uart_port *port);
bool uart_read(uart_port *port, uint8_t *byte);
uint32_t uart_tx_space(const uart_port *port);
void uart_write(uart_port *port, const void *data, uint32_t length);
void uart_write_direct(uint32_t base, const char *text);

#endif
