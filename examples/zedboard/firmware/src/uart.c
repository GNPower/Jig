// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power

#include "uart.h"

#include "config.h"
#include "zynq7000.h"

// Baud rate = UART_REF_CLK / (CD x (BDIV + 1)) (UG585 19.2.3): 50 MHz / (62 x 7)
// = 115207 baud, 0.006 % fast (UG585 Table 19-1).
#define UART_BDIV   6u
#define UART_CD     ((UART_REF_HZ + UART_BAUD * (UART_BDIV + 1u) / 2u) / (UART_BAUD * (UART_BDIV + 1u)))

_Static_assert(UART_CD == 62u, "UART divisor differs from UG585 Table 19-1");

// UG585 19.3.2: 8 data bits, no parity, 1 stop bit; interrupts stay masked.
void uart_init(uart_port *port)
{
    uint32_t base = port->base;
    reg_write(base + UART_IDR, UART_ISR_ALL);
    reg_write(base + UART_CR, UART_CR_RXDIS | UART_CR_TXDIS);
    reg_write(base + UART_MR, UART_MR_8N1);
    reg_write(base + UART_BAUDGEN, UART_CD);
    reg_write(base + UART_BAUDDIV, UART_BDIV);
    reg_write(base + UART_RXTOUT, 0);
    reg_write(base + UART_CR, UART_CR_RXRST | UART_CR_TXRST | UART_CR_RXEN | UART_CR_TXEN
                                  | UART_CR_STPBRK);
    reg_write(base + UART_ISR, UART_ISR_ALL);
}

// Tick interrupt: empty the RX FIFO into the RX ring, refill the TX FIFO.
void uart_service(uart_port *port)
{
    uint32_t base = port->base;
    if ((reg_read(base + UART_ISR) & UART_ISR_ROVR) != 0) {
        reg_write(base + UART_ISR, UART_ISR_ROVR);
        port->rx_overruns++;
    }
    while ((reg_read(base + UART_SR) & UART_SR_REMPTY) == 0) {
        uint8_t byte = (uint8_t)reg_read(base + UART_FIFO);
        if (!ring_put(&port->rx, byte)) {
            port->rx_dropped++;
        }
    }
    uint8_t byte;
    while ((reg_read(base + UART_SR) & UART_SR_TFUL) == 0 && ring_get(&port->tx, &byte)) {
        reg_write(base + UART_FIFO, byte);
    }
}

bool uart_read(uart_port *port, uint8_t *byte)
{
    return ring_get(&port->rx, byte);
}

uint32_t uart_tx_space(const uart_port *port)
{
    return ring_space(&port->tx);
}

// Waits for room in the TX ring; the tick interrupt drains it.
void uart_write(uart_port *port, const void *data, uint32_t length)
{
    const uint8_t *bytes = data;
    for (uint32_t i = 0; i < length; i++) {
        while (!ring_put(&port->tx, bytes[i])) {
            wait_for_interrupt();
        }
    }
}

// Writes straight to the TX FIFO with interrupts masked: the fault path.
void uart_write_direct(uint32_t base, const char *text)
{
    for (; *text != '\0'; text++) {
        while ((reg_read(base + UART_SR) & UART_SR_TFUL) != 0) {
        }
        reg_write(base + UART_FIFO, (uint8_t)*text);
    }
}
