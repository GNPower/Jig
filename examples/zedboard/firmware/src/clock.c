// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// PS clocks, MIO routing and controller resets (UG585 chapters 2, 18, 19, 25
// and 26). On hardware the debugger's ps7_init has usually started the PLLs
// already; a PLL that runs at the planned multiplier is left alone.

#include "clock.h"

#include "config.h"
#include "lib.h"
#include "zynq7000.h"

// UG585 Table 25-6: PLL_RES, PLL_CP and LOCK_CNT for each multiplier.
#define ARM_PLL_CFG_VALUE   PLL_CFG(2u, 2u, 250u)   // multipliers 37 to 40
#define IO_PLL_CFG_VALUE    PLL_CFG(12u, 2u, 325u)  // multipliers 29 and 30

// A PLL locks within 60 us (UG585 25.10.4); this bound is far longer.
#define PLL_LOCK_POLLS      100000u

// MIO pins (UG585 appendix B, MIO_PIN_xx): CAN0 and UART0 reach the ZedBoard's
// MIO Pmod connector; UART1 on MIO 48/49 is the USB-UART.
#define MIO_CAN0_RX         10u
#define MIO_CAN0_TX         11u
#define MIO_UART0_RX        14u
#define MIO_UART0_TX        15u
#define MIO_UART1_TX        48u
#define MIO_UART1_RX        49u
#define MIO_L3_UART1        7u

static bool pll_running_at(uint32_t ctrl_reg, uint32_t lock_bit, uint32_t fdiv)
{
    uint32_t ctrl = reg_read(ctrl_reg);
    if (((ctrl & PLL_CTRL_FDIV_MASK) >> PLL_CTRL_FDIV_SHIFT) != fdiv) {
        return false;
    }
    if ((ctrl & (PLL_CTRL_RESET | PLL_CTRL_PWRDWN | PLL_CTRL_BYPASS_FORCE)) != 0) {
        return false;
    }
    return (reg_read(SLCR_PLL_STATUS) & lock_bit) != 0;
}

// UG585 25.10.4, "Software-Controlled PLL Update". The PLL is bypassed (its
// output follows PS_CLK) while it relocks, so the clocks it feeds keep running.
static bool pll_program(uint32_t ctrl_reg, uint32_t cfg_reg, uint32_t lock_bit,
                        uint32_t fdiv, uint32_t cfg)
{
    reg_write(cfg_reg, cfg);
    reg_modify(ctrl_reg, PLL_CTRL_FDIV_MASK, fdiv << PLL_CTRL_FDIV_SHIFT);
    reg_modify(ctrl_reg, PLL_CTRL_BYPASS_QUAL, PLL_CTRL_BYPASS_FORCE);
    reg_modify(ctrl_reg, PLL_CTRL_PWRDWN, PLL_CTRL_RESET);
    reg_modify(ctrl_reg, PLL_CTRL_RESET, 0);
    for (uint32_t i = 0; i < PLL_LOCK_POLLS; i++) {
        if ((reg_read(SLCR_PLL_STATUS) & lock_bit) != 0) {
            reg_modify(ctrl_reg, PLL_CTRL_BYPASS_FORCE, 0);
            return true;
        }
    }
    return false;
}

// Changes only the pin's multiplexer and output enable; the I/O standard and
// pull-up belong to the board's bank voltages, which its ps7_init sets.
static void mio_route(uint32_t pin, uint32_t l3_sel, bool input)
{
    reg_modify(SLCR_MIO_PIN(pin), MIO_MUX_MASK | MIO_TRI_ENABLE,
               MIO_L3_SEL(l3_sel) | (input ? MIO_TRI_ENABLE : 0u));
}

uint32_t clock_init(void)
{
    uint32_t faults = 0;
    uint32_t uarts = REFCLK_CLKACT1;                    // UART1: console
    uint32_t amba = APER_CAN0_CLKACT | APER_UART1_CLKACT;
    if (NODE_HAS_KISS) {
        uarts |= REFCLK_CLKACT0;                        // UART0: KISS
        amba |= APER_UART0_CLKACT;
    }

    reg_write(SLCR_UNLOCK, SLCR_UNLOCK_KEY);

    // CPU_6x4x = ARM PLL / 2 = 666.7 MHz; the private timer runs at half that.
    if (!pll_running_at(SLCR_ARM_PLL_CTRL, PLL_STATUS_ARM_LOCK, ARM_PLL_FDIV)
        && !pll_program(SLCR_ARM_PLL_CTRL, SLCR_ARM_PLL_CFG, PLL_STATUS_ARM_LOCK,
                        ARM_PLL_FDIV, ARM_PLL_CFG_VALUE)) {
        faults |= CLOCK_ARM_PLL_UNLOCKED;
    }
    // UG585 25.10.1: select the source first, then set the divisor.
    reg_modify(SLCR_ARM_CLK_CTRL, ARM_CLK_SRCSEL_MASK, ARM_CLK_ALL_CLKACT);
    reg_modify(SLCR_ARM_CLK_CTRL, ARM_CLK_DIVISOR_MASK,
               CPU_6X4X_DIVISOR << ARM_CLK_DIVISOR_SHIFT);

    // IO PLL 1000 MHz feeds the UART and CAN reference clocks.
    if (!pll_running_at(SLCR_IO_PLL_CTRL, PLL_STATUS_IO_LOCK, IO_PLL_FDIV)
        && !pll_program(SLCR_IO_PLL_CTRL, SLCR_IO_PLL_CFG, PLL_STATUS_IO_LOCK,
                        IO_PLL_FDIV, IO_PLL_CFG_VALUE)) {
        faults |= CLOCK_IO_PLL_UNLOCKED;
    }
    reg_write(SLCR_UART_CLK_CTRL, (UART_REF_DIVISOR << REFCLK_DIVISOR0_SHIFT) | uarts);
    reg_modify(SLCR_CAN_CLK_CTRL,
               (0x3Fu << REFCLK_DIVISOR1_SHIFT) | (0x3Fu << REFCLK_DIVISOR0_SHIFT) | REFCLK_SRCSEL_MASK,
               (CAN_REF_DIVISOR1 << REFCLK_DIVISOR1_SHIFT) | (CAN_REF_DIVISOR0 << REFCLK_DIVISOR0_SHIFT)
                   | REFCLK_CLKACT0);
    reg_modify(SLCR_CAN_MIOCLK_CTRL, CAN0_REF_SEL, 0);
    reg_modify(SLCR_APER_CLK_CTRL, 0, amba);

    mio_route(MIO_CAN0_RX, MIO_L3_CAN0, true);
    mio_route(MIO_CAN0_TX, MIO_L3_CAN0, false);
    mio_route(MIO_UART1_RX, MIO_L3_UART1, true);
    mio_route(MIO_UART1_TX, MIO_L3_UART1, false);
    if (NODE_HAS_KISS) {
        mio_route(MIO_UART0_RX, MIO_L3_UART0, true);
        mio_route(MIO_UART0_TX, MIO_L3_UART0, false);
    }

    // Reset the controllers this firmware drives (UG585 18.4.2, 19.4.2).
    uint32_t uart_resets = UART1_CPU1X_RST | UART1_REF_RST;
    if (NODE_HAS_KISS) {
        uart_resets |= UART0_CPU1X_RST | UART0_REF_RST;
    }
    reg_modify(SLCR_UART_RST_CTRL, 0, uart_resets);
    reg_modify(SLCR_UART_RST_CTRL, uart_resets, 0);
    reg_modify(SLCR_CAN_RST_CTRL, 0, CAN0_CPU1X_RST);
    reg_modify(SLCR_CAN_RST_CTRL, CAN0_CPU1X_RST, 0);

    reg_write(SLCR_LOCK, SLCR_LOCK_KEY);
    return faults;
}
