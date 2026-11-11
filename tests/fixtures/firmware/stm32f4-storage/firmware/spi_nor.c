// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power

#include "spi_nor.h"

#include "stm32f4.h"

#define READ 0x03u
#define PAGE_PROGRAM 0x02u
#define SECTOR_ERASE 0x20u
#define READ_STATUS 0x05u
#define WRITE_ENABLE 0x06u

#define STATUS_BUSY (1u << 0)
#define PAGE_SIZE 256u
// The bound on a wait for the busy bit to clear. It is far more polls than
// the 30 ms sector erase of the fixture flash model (SpiNorFlash.cs) takes.
#define READY_POLLS 1000000u

#define CHIP_SELECT 4u // PA4

void spi_nor_start(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB2ENR |= RCC_APB2ENR_SPI1EN;
    // Chip select high (deselected) before PA4 becomes an output.
    GPIOA_BSRR = 1u << CHIP_SELECT;
    GPIOA_MODER = (GPIOA_MODER & ~(0x3u << (2u * CHIP_SELECT))) |
                  (GPIO_MODE_OUTPUT << (2u * CHIP_SELECT));
    // PA5 (SCK), PA6 (MISO), PA7 (MOSI): alternate function mode, AF5.
    GPIOA_MODER = (GPIOA_MODER & ~(0x3Fu << 10)) |
                  (GPIO_MODE_ALTERNATE << 10) | (GPIO_MODE_ALTERNATE << 12) |
                  (GPIO_MODE_ALTERNATE << 14);
    GPIOA_AFRL = (GPIOA_AFRL & ~(0xFFFu << 20)) | (0x555u << 20);
    // Master, mode 0, the clock at APB2 / 2; software slave management with
    // SSI set keeps the controller in master mode without an NSS pin.
    SPI1_CR1 = SPI_CR1_MSTR | SPI_CR1_SSM | SPI_CR1_SSI;
    SPI1_CR1 |= SPI_CR1_SPE;
}

static uint8_t transfer(uint8_t out)
{
    while((SPI1_SR & SPI_SR_TXE) == 0u)
    {
    }
    SPI1_DR = out;
    while((SPI1_SR & SPI_SR_RXNE) == 0u)
    {
    }
    return (uint8_t)SPI1_DR;
}

static void select(void)
{
    GPIOA_BSRR = 1u << (CHIP_SELECT + 16u);
}

// Chip select rising ends the command; a program or erase starts then.
static void deselect(void)
{
    GPIOA_BSRR = 1u << CHIP_SELECT;
}

static void send_command(uint8_t opcode, uint32_t address)
{
    transfer(opcode);
    transfer((uint8_t)(address >> 16));
    transfer((uint8_t)(address >> 8));
    transfer((uint8_t)address);
}

static void write_enable(void)
{
    select();
    transfer(WRITE_ENABLE);
    deselect();
}

static int wait_ready(void)
{
    for(uint32_t poll = 0u; poll < READY_POLLS; poll++)
    {
        select();
        transfer(READ_STATUS);
        uint8_t status = transfer(0xFFu);
        deselect();
        if((status & STATUS_BUSY) == 0u)
        {
            return 0;
        }
    }
    return -1;
}

void spi_nor_read(uint32_t address, void *buffer, uint32_t size)
{
    uint8_t *to = buffer;
    select();
    send_command(READ, address);
    while(size-- > 0u)
    {
        *to++ = transfer(0xFFu);
    }
    deselect();
}

int spi_nor_program(uint32_t address, const void *data, uint32_t size)
{
    const uint8_t *from = data;
    while(size > 0u)
    {
        // A page program wraps inside its 256-byte page, so no command
        // crosses a page boundary.
        uint32_t room = PAGE_SIZE - (address % PAGE_SIZE);
        uint32_t count = size < room ? size : room;
        write_enable();
        select();
        send_command(PAGE_PROGRAM, address);
        for(uint32_t i = 0u; i < count; i++)
        {
            transfer(from[i]);
        }
        deselect();
        if(wait_ready() != 0)
        {
            return -1;
        }
        address += count;
        from += count;
        size -= count;
    }
    return 0;
}

int spi_nor_erase_sector(uint32_t address)
{
    write_enable();
    select();
    send_command(SECTOR_ERASE, address);
    deselect();
    return wait_ready();
}
