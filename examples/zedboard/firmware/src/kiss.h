// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// KISS framing of CSP packets on UART0, as libcsp 1.6 frames them: FEND, the
// data command byte 0x00, the CSP header, the payload and a CRC-32C of the
// payload (big-endian), with FEND and FESC escaped, then FEND.

#ifndef KISS_H
#define KISS_H

#include "csp.h"
#include "uart.h"

typedef struct {
    uint32_t tx_frames;
    uint32_t rx_frames;     // frames with a good CRC
    uint32_t bad_frames;    // not a data frame, too short or too long
} kiss_counters;

extern kiss_counters kiss_stats;
extern uart_port kiss_port;

void kiss_init(void);
bool kiss_poll(void);
bool kiss_send(const csp_header *header, const uint8_t *data, uint32_t length);

#endif
