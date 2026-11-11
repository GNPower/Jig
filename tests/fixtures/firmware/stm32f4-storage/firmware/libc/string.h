// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// The string functions littlefs (lfs.c) and the fixture call. The fixture
// builds freestanding, without a C library, so this header stands in for the
// toolchain's <string.h>; string.c implements it.

#ifndef FIXTURE_STRING_H
#define FIXTURE_STRING_H

#include <stddef.h>

void *memcpy(void *restrict destination, const void *restrict source, size_t count);
void *memset(void *destination, int value, size_t count);
int memcmp(const void *a, const void *b, size_t count);
size_t strlen(const char *text);
char *strcpy(char *restrict destination, const char *restrict source);
char *strchr(const char *text, int character);
size_t strspn(const char *text, const char *accept);
size_t strcspn(const char *text, const char *reject);

#endif
