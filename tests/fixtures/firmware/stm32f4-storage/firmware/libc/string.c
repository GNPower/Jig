// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Byte-at-a-time versions of the functions string.h declares. build.py
// passes -fno-tree-loop-distribute-patterns, which keeps GCC from turning
// these loops into calls to themselves.

#include <stdint.h>
#include <string.h>

void *memcpy(void *restrict destination, const void *restrict source, size_t count)
{
    uint8_t *to = destination;
    const uint8_t *from = source;
    while(count-- > 0u)
    {
        *to++ = *from++;
    }
    return destination;
}

void *memset(void *destination, int value, size_t count)
{
    uint8_t *to = destination;
    while(count-- > 0u)
    {
        *to++ = (uint8_t)value;
    }
    return destination;
}

int memcmp(const void *a, const void *b, size_t count)
{
    const uint8_t *x = a;
    const uint8_t *y = b;
    for(size_t i = 0u; i < count; i++)
    {
        if(x[i] != y[i])
        {
            return x[i] < y[i] ? -1 : 1;
        }
    }
    return 0;
}

size_t strlen(const char *text)
{
    size_t length = 0u;
    while(text[length] != '\0')
    {
        length++;
    }
    return length;
}

char *strcpy(char *restrict destination, const char *restrict source)
{
    char *to = destination;
    while((*to++ = *source++) != '\0')
    {
    }
    return destination;
}

char *strchr(const char *text, int character)
{
    for(;; text++)
    {
        if(*text == (char)character)
        {
            return (char *)text;
        }
        if(*text == '\0')
        {
            return NULL;
        }
    }
}

// The length of the start of text made only of characters in set (accept
// true) or only of characters not in set (accept false).
static size_t span(const char *text, const char *set, int accept)
{
    size_t length = 0u;
    for(; text[length] != '\0'; length++)
    {
        int found = strchr(set, text[length]) != NULL;
        if(found != accept)
        {
            break;
        }
    }
    return length;
}

size_t strspn(const char *text, const char *accept)
{
    return span(text, accept, 1);
}

size_t strcspn(const char *text, const char *reject)
{
    return span(text, reject, 0);
}
