// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Byte rings shared between the tick interrupt and the main loop. Each ring
// has one producer and one consumer, and each index has a single writer, so on
// one core no lock is needed. Indices run freely; the size is a power of two.

#ifndef RING_H
#define RING_H

#include "lib.h"

typedef struct {
    uint8_t *data;
    uint32_t mask;              // size - 1
    volatile uint32_t head;     // next slot to write: producer only
    volatile uint32_t tail;     // next slot to read: consumer only
} byte_ring;

#define BYTE_RING(storage) { (storage), sizeof(storage) - 1u, 0u, 0u }

static inline uint32_t ring_used(const byte_ring *ring)
{
    return ring->head - ring->tail;
}

static inline uint32_t ring_space(const byte_ring *ring)
{
    return ring->mask + 1u - ring_used(ring);
}

static inline bool ring_put(byte_ring *ring, uint8_t value)
{
    uint32_t head = ring->head;
    if (head - ring->tail > ring->mask) {
        return false;
    }
    ring->data[head & ring->mask] = value;
    compiler_barrier();
    ring->head = head + 1u;
    return true;
}

static inline bool ring_get(byte_ring *ring, uint8_t *value)
{
    uint32_t tail = ring->tail;
    if (ring->head == tail) {
        return false;
    }
    *value = ring->data[tail & ring->mask];
    compiler_barrier();
    ring->tail = tail + 1u;
    return true;
}

#endif
