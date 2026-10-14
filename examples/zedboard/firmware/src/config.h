// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Build-time configuration. build.py compiles the same sources twice, with
// -DNODE_ADDRESS=1 for zed-a and -DNODE_ADDRESS=2 for zed-b.

#ifndef CONFIG_H
#define CONFIG_H

#ifndef NODE_ADDRESS
#error "build with -DNODE_ADDRESS=1 (zed-a) or -DNODE_ADDRESS=2 (zed-b)"
#endif

#if NODE_ADDRESS == 1
#define NODE_NAME           "zed-a"
#define NODE_HAS_KISS       1       // UART0 carries the KISS link to the ground station
#elif NODE_ADDRESS == 2
#define NODE_NAME           "zed-b"
#define NODE_HAS_KISS       0
#else
#error "NODE_ADDRESS must be 1 or 2"
#endif

// CSP addresses and ports
#define GROUND_ADDRESS      10u     // the ground station, reached over KISS through zed-a
#define CSP_BROADCAST       31u
#define PORT_PING           1u
#define PORT_UPTIME         6u
#define PORT_STATUS         10u
#define CSP_MTU             256u    // largest payload sent or accepted, CRC32 option included

// Clock plan for PS_CLK = 33.333 MHz, from UG585 Table 25-3 example 1, with the
// UART reference clock of UG585 19.4.1. The firmware programs these itself.
#define ARM_PLL_FDIV        40u     // ARM PLL 1333.3 MHz
#define IO_PLL_FDIV         30u     // IO PLL 1000 MHz
#define CPU_6X4X_DIVISOR    2u      // CPU_6x4x 666.7 MHz, 6:2:1 mode
#define CPU_3X2X_HZ         333333333u  // CPU_6x4x / 2: private timer clock (UG585 8.2.1)
#define UART_REF_DIVISOR    20u     // UART_REF_CLK 50 MHz
#define UART_REF_HZ         50000000u
#define CAN_REF_DIVISOR0    10u     // CAN_REF_CLK 100 MHz
#define CAN_REF_DIVISOR1    1u
#define CAN_REF_HZ          100000000u

// Links
#define UART_BAUD           115200u
#define CAN_BITRATE         1000000u
#define TICK_HZ             1000u

#endif
