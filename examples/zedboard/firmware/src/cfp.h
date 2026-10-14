// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// CSP over CAN with libcsp 1.6's CAN fragmentation protocol (CFP). Each
// 29-bit identifier holds, most significant first: source 5 bits, destination
// 5, type 1 (0 BEGIN, 1 MORE), frames remaining 8, transfer identifier 10. A
// BEGIN frame carries the CSP header, the payload length (16 bits,
// big-endian) and the first 2 payload bytes; each MORE frame carries up to 8.

#ifndef CFP_H
#define CFP_H

#include "csp.h"

typedef struct {
    uint32_t reassembly_drops;  // frames out of sequence, unknown transfers, timeouts, oversize
} cfp_counters;

extern cfp_counters cfp_stats;

bool cfp_init(void);
bool cfp_poll(void);
bool cfp_send(const csp_header *header, const uint8_t *data, uint32_t length);

#endif
