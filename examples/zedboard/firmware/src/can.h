// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Zynq-7000 CAN controller 0 (UG585 chapter 18), polled, extended (29-bit)
// data frames only. The tick interrupt moves frames between the 64-message
// hardware FIFOs and the software rings; at 1 Mbit/s the bus carries at most
// about 8 frames per millisecond.

#ifndef CAN_H
#define CAN_H

#include "lib.h"

typedef struct {
    uint32_t id;            // 29-bit identifier
    uint8_t length;         // 0 to 8
    uint8_t data[8];
} can_frame;

// One acceptance filter: a frame passes when (id & mask) == (match & mask).
typedef struct {
    uint32_t match;
    uint32_t mask;
} can_filter;

typedef struct {
    volatile uint32_t tx_frames;    // frames handed to the TX FIFO
    volatile uint32_t rx_frames;    // frames read from the RX FIFO
    volatile uint32_t rx_overflows; // RX FIFO overflow events
    volatile uint32_t rx_dropped;   // frames lost to a full RX ring
    volatile uint32_t bus_off;      // bus-off events
    volatile uint32_t errors;       // error events (ESR)
} can_counters;

extern can_counters can_stats;

bool can_init(const can_filter *filters, uint32_t count);
void can_service(void);
bool can_receive(can_frame *frame);
uint32_t can_tx_space(void);
bool can_send(const can_frame *frame);
uint32_t can_status(void);          // SR
uint32_t can_error_counters(void);  // ECR: REC in 15:8, TEC in 7:0

#endif
