// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Zynq-7000 registers this firmware touches, with the fields it uses.
//
// Sources:
//   UG585  Xilinx, "Zynq-7000 SoC Technical Reference Manual", v1.7. Chapter
//          numbers are cited per block; appendix B has the register details.
//   DDI0407 Arm, "Cortex-A9 MPCore Technical Reference Manual", section 4.1
//          (private timer).
//   IHI0048 Arm, "Generic Interrupt Controller Architecture Specification"
//          (GICv1, the PL390 the Cortex-A9 MPCore integrates).

#ifndef ZYNQ7000_H
#define ZYNQ7000_H

#include <stdint.h>

static inline uint32_t reg_read(uint32_t address)
{
    return *(volatile const uint32_t *)(uintptr_t)address;
}

static inline void reg_write(uint32_t address, uint32_t value)
{
    *(volatile uint32_t *)(uintptr_t)address = value;
}

static inline void reg_modify(uint32_t address, uint32_t clear, uint32_t set)
{
    reg_write(address, (reg_read(address) & ~clear) | set);
}

// ---------------------------------------------------------------------------
// SLCR, system level control registers (UG585 chapters 4, 25, 26; B.28)

#define SLCR_BASE               0xF8000000u
#define SLCR_LOCK               (SLCR_BASE + 0x004u)
#define SLCR_UNLOCK             (SLCR_BASE + 0x008u)
#define SLCR_ARM_PLL_CTRL       (SLCR_BASE + 0x100u)
#define SLCR_IO_PLL_CTRL        (SLCR_BASE + 0x108u)
#define SLCR_PLL_STATUS         (SLCR_BASE + 0x10Cu)
#define SLCR_ARM_PLL_CFG        (SLCR_BASE + 0x110u)
#define SLCR_IO_PLL_CFG         (SLCR_BASE + 0x118u)
#define SLCR_ARM_CLK_CTRL       (SLCR_BASE + 0x120u)
#define SLCR_APER_CLK_CTRL      (SLCR_BASE + 0x12Cu)
#define SLCR_UART_CLK_CTRL      (SLCR_BASE + 0x154u)
#define SLCR_CAN_CLK_CTRL       (SLCR_BASE + 0x15Cu)
#define SLCR_CAN_MIOCLK_CTRL    (SLCR_BASE + 0x160u)
#define SLCR_CAN_RST_CTRL       (SLCR_BASE + 0x220u)
#define SLCR_UART_RST_CTRL      (SLCR_BASE + 0x228u)
#define SLCR_MIO_PIN(n)         (SLCR_BASE + 0x700u + 4u * (uint32_t)(n))

#define SLCR_LOCK_KEY           0x767Bu
#define SLCR_UNLOCK_KEY         0xDF0Du

// ARM_PLL_CTRL, IO_PLL_CTRL
#define PLL_CTRL_RESET          (1u << 0)
#define PLL_CTRL_PWRDWN         (1u << 1)
#define PLL_CTRL_BYPASS_QUAL    (1u << 3)
#define PLL_CTRL_BYPASS_FORCE   (1u << 4)
#define PLL_CTRL_FDIV_SHIFT     12
#define PLL_CTRL_FDIV_MASK      (0x7Fu << PLL_CTRL_FDIV_SHIFT)

// ARM_PLL_CFG, IO_PLL_CFG: values per multiplier come from UG585 Table 25-6.
#define PLL_CFG(res, cp, lock_cnt) \
    (((uint32_t)(lock_cnt) << 12) | ((uint32_t)(cp) << 8) | ((uint32_t)(res) << 4))

// PLL_STATUS
#define PLL_STATUS_ARM_LOCK     (1u << 0)
#define PLL_STATUS_IO_LOCK      (1u << 2)

// ARM_CLK_CTRL: SRCSEL 0x selects the ARM PLL; bits 28:24 gate the CPU clocks.
#define ARM_CLK_SRCSEL_MASK     (3u << 4)
#define ARM_CLK_DIVISOR_SHIFT   8
#define ARM_CLK_DIVISOR_MASK    (0x3Fu << ARM_CLK_DIVISOR_SHIFT)
#define ARM_CLK_ALL_CLKACT      (0x1Fu << 24)

// APER_CLK_CTRL: AMBA clock enables (UG585 Table 25-2)
#define APER_CAN0_CLKACT        (1u << 16)
#define APER_UART0_CLKACT       (1u << 20)
#define APER_UART1_CLKACT       (1u << 21)

// UART_CLK_CTRL, CAN_CLK_CTRL: SRCSEL 0x selects the IO PLL.
#define REFCLK_CLKACT0          (1u << 0)
#define REFCLK_CLKACT1          (1u << 1)
#define REFCLK_SRCSEL_MASK      (3u << 4)
#define REFCLK_DIVISOR0_SHIFT   8
#define REFCLK_DIVISOR1_SHIFT   20

// CAN_MIOCLK_CTRL: 0 takes CAN0_REF_CLK from the PS clock generator.
#define CAN0_REF_SEL            (1u << 6)

// CAN_RST_CTRL, UART_RST_CTRL
#define CAN0_CPU1X_RST          (1u << 0)
#define UART0_CPU1X_RST         (1u << 0)
#define UART1_CPU1X_RST         (1u << 1)
#define UART0_REF_RST           (1u << 2)
#define UART1_REF_RST           (1u << 3)

// MIO_PIN_xx: the mux fields and TRI_ENABLE. IO_Type, PULLUP, Speed and
// DisableRcvr (bits 13:8) describe the board's I/O bank and are left alone.
#define MIO_TRI_ENABLE          (1u << 0)
#define MIO_MUX_MASK            0xFEu
#define MIO_L3_SEL(v)           ((uint32_t)(v) << 5)
#define MIO_L3_CAN0             1u      // MIO 10/14 CAN0 Rx, 11/15 CAN0 Tx
#define MIO_L3_UART0            7u      // MIO 10/14 UART0 RxD, 11/15 UART0 TxD

// ---------------------------------------------------------------------------
// UART, Cadence UART controller (UG585 chapter 19; B.33)

#define UART0_BASE              0xE0000000u
#define UART1_BASE              0xE0001000u

#define UART_CR                 0x00u   // Control_reg0
#define UART_MR                 0x04u   // mode_reg0
#define UART_IDR                0x0Cu   // Intrpt_dis_reg0
#define UART_ISR                0x14u   // Chnl_int_sts_reg0
#define UART_BAUDGEN            0x18u   // Baud_rate_gen_reg0
#define UART_RXTOUT             0x1Cu   // Rcvr_timeout_reg0
#define UART_SR                 0x2Cu   // Channel_sts_reg0
#define UART_FIFO               0x30u   // TX_RX_FIFO0
#define UART_BAUDDIV            0x34u   // Baud_rate_divider_reg0

#define UART_CR_RXRST           (1u << 0)
#define UART_CR_TXRST           (1u << 1)
#define UART_CR_RXEN            (1u << 2)
#define UART_CR_RXDIS           (1u << 3)
#define UART_CR_TXEN            (1u << 4)
#define UART_CR_TXDIS           (1u << 5)
#define UART_CR_STPBRK          (1u << 8)

#define UART_MR_8N1             (4u << 3)   // CHRL 8 bits, PAR 1xx none, NBSTOP 1

#define UART_SR_REMPTY          (1u << 1)
#define UART_SR_TFUL            (1u << 4)

#define UART_ISR_ROVR           (1u << 5)
#define UART_ISR_ALL            0x1FFFu

// ---------------------------------------------------------------------------
// CAN, Zynq-7000 CAN controller (UG585 chapter 18; B.5)

#define CAN0_BASE               0xE0008000u

#define CAN_SRR                 0x00u
#define CAN_MSR                 0x04u
#define CAN_BRPR                0x08u
#define CAN_BTR                 0x0Cu
#define CAN_ECR                 0x10u
#define CAN_ESR                 0x14u
#define CAN_SR                  0x18u
#define CAN_ISR                 0x1Cu
#define CAN_IER                 0x20u
#define CAN_ICR                 0x24u
#define CAN_TXFIFO_ID           0x30u
#define CAN_TXFIFO_DLC          0x34u
#define CAN_TXFIFO_DATA1        0x38u
#define CAN_TXFIFO_DATA2        0x3Cu
#define CAN_RXFIFO_ID           0x50u
#define CAN_RXFIFO_DLC          0x54u
#define CAN_RXFIFO_DATA1        0x58u
#define CAN_RXFIFO_DATA2        0x5Cu
#define CAN_AFR                 0x60u
#define CAN_AFMR(n)             (0x64u + 8u * (uint32_t)(n))   // n = 0..3 for AFMR1..4
#define CAN_AFIR(n)             (0x68u + 8u * (uint32_t)(n))

#define CAN_SRR_SRST            (1u << 0)
#define CAN_SRR_CEN             (1u << 1)

#define CAN_SR_CONFIG           (1u << 0)
#define CAN_SR_NORMAL           (1u << 3)
#define CAN_SR_ESTAT_SHIFT      7
#define CAN_SR_ESTAT_MASK       (3u << CAN_SR_ESTAT_SHIFT)
#define CAN_SR_TXFLL            (1u << 10)
#define CAN_SR_ACFBSY           (1u << 11)

// ISR, IER, ICR (UG585 Table 18-4)
#define CAN_IXR_TXOK            (1u << 1)
#define CAN_IXR_RXOK            (1u << 4)
#define CAN_IXR_RXOFLW          (1u << 6)
#define CAN_IXR_RXNEMP          (1u << 7)
#define CAN_IXR_ERROR           (1u << 8)
#define CAN_IXR_BSOFF           (1u << 9)

// Message identifier word (UG585 Table 18-3)
#define CAN_IDR_ID_HIGH_SHIFT   21      // ID[28:18]
#define CAN_IDR_SRR             (1u << 20)
#define CAN_IDR_IDE             (1u << 19)
#define CAN_IDR_ID_LOW_SHIFT    1       // ID[17:0]
#define CAN_IDR_RTR             (1u << 0)
#define CAN_DLC_SHIFT           28

// ---------------------------------------------------------------------------
// Cortex-A9 MPCore private memory region (UG585 Table 4-4; B.24)

// GIC CPU interface (IHI0048 4.4; UG585 chapter 7)
#define GIC_CPU_BASE            0xF8F00100u
#define ICCICR                  (GIC_CPU_BASE + 0x00u)
#define ICCPMR                  (GIC_CPU_BASE + 0x04u)
#define ICCIAR                  (GIC_CPU_BASE + 0x0Cu)
#define ICCEOIR                 (GIC_CPU_BASE + 0x10u)

// GIC distributor (IHI0048 4.3)
#define GIC_DIST_BASE           0xF8F01000u
#define ICDDCR                  (GIC_DIST_BASE + 0x000u)
#define ICDISER(n)              (GIC_DIST_BASE + 0x100u + 4u * (uint32_t)(n))
#define ICDICER(n)              (GIC_DIST_BASE + 0x180u + 4u * (uint32_t)(n))
#define ICDICPR(n)              (GIC_DIST_BASE + 0x280u + 4u * (uint32_t)(n))
#define ICDIPR(n)               (GIC_DIST_BASE + 0x400u + 4u * (uint32_t)(n))

#define GIC_SPURIOUS_ID         1023u
#define IRQ_ID_PRIVATE_TIMER    29u     // PPI, UG585 Table 7-2

// Private timer (DDI0407 4.1; UG585 8.2), clocked by CPU_3x2x
#define PTIMER_BASE             0xF8F00600u
#define PTIMER_LOAD             (PTIMER_BASE + 0x00u)
#define PTIMER_CONTROL          (PTIMER_BASE + 0x08u)
#define PTIMER_ISR              (PTIMER_BASE + 0x0Cu)

#define PTIMER_CTRL_ENABLE      (1u << 0)
#define PTIMER_CTRL_AUTO_RELOAD (1u << 1)
#define PTIMER_CTRL_IRQ_ENABLE  (1u << 2)
#define PTIMER_ISR_EVENT        (1u << 0)

#endif
