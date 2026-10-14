/*
 * STM32F4 smoke test for Jig's stm32 SoC driver: prints a greeting on USART2,
 * sends one frame on CAN1, reports the transmit status, then sleeps.
 *
 * Register addresses and bits come from RM0090 (STM32F405/407 reference
 * manual: RCC 7.3, GPIO 8.4, USART 30.6, bxCAN 32.9) and the vector table
 * layout from PM0214 (Cortex-M4 programming manual, 2.3.4). The core runs on
 * the reset clock, the 16 MHz HSI, which also clocks APB1.
 */
#include <stdint.h>

#define REG(address) (*(volatile uint32_t *)(address))

#define RCC_AHB1ENR REG(0x40023830u)
#define RCC_APB1ENR REG(0x40023840u)
#define RCC_AHB1ENR_GPIOAEN (1u << 0)
#define RCC_APB1ENR_USART2EN (1u << 17)
#define RCC_APB1ENR_CAN1EN (1u << 25)

#define GPIOA_MODER REG(0x40020000u)
#define GPIOA_AFRL REG(0x40020020u)
#define GPIOA_AFRH REG(0x40020024u)

#define USART2_SR REG(0x40004400u)
#define USART2_DR REG(0x40004404u)
#define USART2_BRR REG(0x40004408u)
#define USART2_CR1 REG(0x4000440Cu)
#define USART_SR_TC (1u << 6)
#define USART_SR_TXE (1u << 7)
#define USART_CR1_TE (1u << 3)
#define USART_CR1_UE (1u << 13)

#define CAN1_MCR REG(0x40006400u)
#define CAN1_MSR REG(0x40006404u)
#define CAN1_TSR REG(0x40006408u)
#define CAN1_BTR REG(0x4000641Cu)
#define CAN1_TI0R REG(0x40006580u)
#define CAN1_TDT0R REG(0x40006584u)
#define CAN1_TDL0R REG(0x40006588u)
#define CAN1_TDH0R REG(0x4000658Cu)
#define CAN_MCR_INRQ (1u << 0)
#define CAN_MSR_INAK (1u << 0)
#define CAN_TSR_RQCP0 (1u << 0)
#define CAN_TSR_TXOK0 (1u << 1)
#define CAN_TIR_TXRQ (1u << 0)

#define CLOCK_HZ 16000000u
#define BAUD 115200u
#define FRAME_ID 0x123u

extern uint32_t _stack_top;
extern uint32_t _data_start, _data_end, _data_load, _bss_start, _bss_end;

void reset_handler(void);
void fault_handler(void);

/* Initial stack pointer, reset, then the 14 system exceptions (PM0214 2.3.4). */
__attribute__((section(".vectors"), used)) static const uint32_t vectors[16] = {
    (uint32_t)&_stack_top,
    (uint32_t)reset_handler,
    (uint32_t)fault_handler, /* NMI */
    (uint32_t)fault_handler, /* HardFault */
    (uint32_t)fault_handler, /* MemManage */
    (uint32_t)fault_handler, /* BusFault */
    (uint32_t)fault_handler, /* UsageFault */
    0u, 0u, 0u, 0u,
    (uint32_t)fault_handler, /* SVCall */
    (uint32_t)fault_handler, /* DebugMonitor */
    0u,
    (uint32_t)fault_handler, /* PendSV */
    (uint32_t)fault_handler, /* SysTick */
};

void fault_handler(void)
{
    for(;;)
    {
    }
}

static void usart2_start(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_APB1ENR |= RCC_APB1ENR_USART2EN;
    /* PA2 (TX) and PA3 (RX): alternate function mode, AF7. */
    GPIOA_MODER = (GPIOA_MODER & ~(0xFu << 4)) | (0xAu << 4);
    GPIOA_AFRL = (GPIOA_AFRL & ~(0xFFu << 8)) | (0x77u << 8);
    /* With 16x oversampling BRR holds the divider in 1/16 steps: clock / baud. */
    USART2_BRR = (CLOCK_HZ + BAUD / 2u) / BAUD;
    USART2_CR1 = USART_CR1_UE | USART_CR1_TE;
}

static void usart2_print(const char *text)
{
    for(; *text != '\0'; text++)
    {
        while((USART2_SR & USART_SR_TXE) == 0u)
        {
        }
        USART2_DR = (uint8_t)*text;
    }
    while((USART2_SR & USART_SR_TC) == 0u)
    {
    }
}

/* Sends one standard frame with three data bytes; returns TXOK0. */
static int can1_send(void)
{
    RCC_APB1ENR |= RCC_APB1ENR_CAN1EN;
    /* PA11 (RX) and PA12 (TX): alternate function mode, AF9. */
    GPIOA_MODER = (GPIOA_MODER & ~(0xFu << 22)) | (0xAu << 22);
    GPIOA_AFRH = (GPIOA_AFRH & ~(0xFFu << 12)) | (0x99u << 12);
    CAN1_MCR = CAN_MCR_INRQ; /* initialisation mode, out of sleep */
    while((CAN1_MSR & CAN_MSR_INAK) == 0u)
    {
    }
    /* 1 Mbit/s from 16 MHz: prescaler 2, then 1 + 6 + 1 time quanta a bit. */
    CAN1_BTR = (0u << 20) | (5u << 16) | (2u - 1u);
    CAN1_MCR = 0u; /* normal mode */
    while((CAN1_MSR & CAN_MSR_INAK) != 0u)
    {
    }
    CAN1_TDT0R = 3u;
    CAN1_TDL0R = (uint32_t)'J' | (uint32_t)'I' << 8 | (uint32_t)'G' << 16;
    CAN1_TDH0R = 0u;
    CAN1_TI0R = FRAME_ID << 21 | CAN_TIR_TXRQ;
    while((CAN1_TSR & CAN_TSR_RQCP0) == 0u)
    {
    }
    return (CAN1_TSR & CAN_TSR_TXOK0) != 0u;
}

void reset_handler(void)
{
    const uint32_t *from = &_data_load;
    for(uint32_t *to = &_data_start; to < &_data_end;)
    {
        *to++ = *from++;
    }
    for(uint32_t *to = &_bss_start; to < &_bss_end;)
    {
        *to++ = 0u;
    }
    usart2_start();
    usart2_print("hello from stm32f4\r\n");
    usart2_print(can1_send() ? "can frame sent\r\n" : "can frame failed\r\n");
    for(;;)
    {
        __asm__ volatile("wfi");
    }
}
