// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power

#include "console.h"

#include "stm32f4.h"

#define BAUD 115200u

static const char HEX[] = "0123456789abcdef";

void console_start(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB1ENR |= RCC_APB1ENR_USART2EN;
    // PA2 (TX) and PA3 (RX): alternate function mode, AF7.
    GPIOA_MODER = (GPIOA_MODER & ~(0xFu << 4)) | (0xAu << 4);
    GPIOA_AFRL = (GPIOA_AFRL & ~(0xFFu << 8)) | (0x77u << 8);
    // With 16x oversampling BRR holds the divider in 1/16 steps: clock / baud.
    USART2_BRR = (CLOCK_HZ + BAUD / 2u) / BAUD;
    USART2_CR1 = USART_CR1_UE | USART_CR1_TE;
}

static void put(char character)
{
    while((USART2_SR & USART_SR_TXE) == 0u)
    {
    }
    USART2_DR = (uint8_t)character;
}

void print(const char *text)
{
    for(; *text != '\0'; text++)
    {
        put(*text);
    }
    while((USART2_SR & USART_SR_TC) == 0u)
    {
    }
}

void print_unsigned(uint32_t value)
{
    char digits[10];
    unsigned count = 0u;
    do
    {
        digits[count++] = (char)('0' + value % 10u);
        value /= 10u;
    } while(value != 0u);
    while(count > 0u)
    {
        put(digits[--count]);
    }
}

void print_signed(int32_t value)
{
    if(value < 0)
    {
        put('-');
        print_unsigned(0u - (uint32_t)value);
        return;
    }
    print_unsigned((uint32_t)value);
}

void print_hex(uint32_t value, unsigned digits)
{
    while(digits-- > 0u)
    {
        put(HEX[(value >> (4u * digits)) & 0xFu]);
    }
}

void print_escaped(const uint8_t *data, uint32_t size)
{
    for(uint32_t i = 0u; i < size; i++)
    {
        uint8_t byte = data[i];
        if(byte == '\\' || byte == '"')
        {
            put('\\');
            put((char)byte);
        }
        else if(byte == '\n')
        {
            print("\\n");
        }
        else if(byte == '\r')
        {
            print("\\r");
        }
        else if(byte == '\t')
        {
            print("\\t");
        }
        else if(byte >= 0x20u && byte < 0x7Fu)
        {
            put((char)byte);
        }
        else
        {
            print("\\x");
            print_hex(byte, 2u);
        }
    }
}
