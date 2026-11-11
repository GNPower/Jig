// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// The STM32F407 registers the fixture uses, from RM0090 (STM32F405/407
// reference manual): RCC 7.3, GPIO 8.4, SPI 28.5, USART 30.6. The core runs
// on the reset clock, the 16 MHz HSI, which also clocks APB1 and APB2.

#ifndef STM32F4_H
#define STM32F4_H

#include <stdint.h>

#define REG(address) (*(volatile uint32_t *)(address))

#define CLOCK_HZ 16000000u

#define RCC_AHB1ENR REG(0x40023830u)
#define RCC_APB1ENR REG(0x40023840u)
#define RCC_APB2ENR REG(0x40023844u)
#define RCC_AHB1ENR_GPIOAEN (1u << 0)
#define RCC_APB1ENR_USART2EN (1u << 17)
#define RCC_APB2ENR_SPI1EN (1u << 12)

#define GPIOA_MODER REG(0x40020000u)
#define GPIOA_BSRR REG(0x40020018u)
#define GPIOA_AFRL REG(0x40020020u)
#define GPIO_MODE_OUTPUT 0x1u
#define GPIO_MODE_ALTERNATE 0x2u

#define USART2_SR REG(0x40004400u)
#define USART2_DR REG(0x40004404u)
#define USART2_BRR REG(0x40004408u)
#define USART2_CR1 REG(0x4000440Cu)
#define USART_SR_TC (1u << 6)
#define USART_SR_TXE (1u << 7)
#define USART_CR1_TE (1u << 3)
#define USART_CR1_UE (1u << 13)

#define SPI1_CR1 REG(0x40013000u)
#define SPI1_SR REG(0x40013008u)
#define SPI1_DR REG(0x4001300Cu)
#define SPI_CR1_MSTR (1u << 2)
#define SPI_CR1_SPE (1u << 6)
#define SPI_CR1_SSI (1u << 8)
#define SPI_CR1_SSM (1u << 9)
#define SPI_SR_RXNE (1u << 0)
#define SPI_SR_TXE (1u << 1)

#endif
