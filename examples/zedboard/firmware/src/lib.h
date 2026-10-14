// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Freestanding helpers: the memory functions GCC may call even in a
// freestanding build, big-endian field access and a few CPU instructions.

#ifndef LIB_H
#define LIB_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

void *memcpy(void *restrict dst, const void *restrict src, size_t n);
void *memmove(void *dst, const void *src, size_t n);
void *memset(void *dst, int value, size_t n);
int memcmp(const void *a, const void *b, size_t n);
size_t strlen(const char *s);
bool str_equal(const char *a, const char *b);

static inline uint16_t get_be16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] << 8 | p[1]);
}

static inline uint32_t get_be32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static inline void put_be16(uint8_t *p, uint16_t value)
{
    p[0] = (uint8_t)(value >> 8);
    p[1] = (uint8_t)value;
}

static inline void put_be32(uint8_t *p, uint32_t value)
{
    p[0] = (uint8_t)(value >> 24);
    p[1] = (uint8_t)(value >> 16);
    p[2] = (uint8_t)(value >> 8);
    p[3] = (uint8_t)value;
}

// Keeps the compiler from moving memory accesses across this point.
#define compiler_barrier() __asm__ volatile("" ::: "memory")

static inline void irq_enable(void)
{
    __asm__ volatile("cpsie i" ::: "memory");
}

static inline void wait_for_interrupt(void)
{
    __asm__ volatile("wfi" ::: "memory");
}

#endif
