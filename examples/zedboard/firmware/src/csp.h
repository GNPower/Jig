// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// CubeSat Space Protocol, wire-compatible with libcsp 1.6: one 32-bit header
// word in network byte order, then the payload. Header fields, most
// significant first: priority 2 bits, source 5, destination 5, destination
// port 6, source port 6, flags 8.

#ifndef CSP_H
#define CSP_H

#include "lib.h"

#define CSP_FLAG_CRC32      0x01u   // CRC-32C of the payload follows it
#define CSP_FLAG_RDP        0x02u
#define CSP_FLAG_XTEA       0x04u
#define CSP_FLAG_HMAC       0x08u
#define CSP_PRIORITY_NORMAL 2u

typedef struct {
    uint8_t priority;
    uint8_t src;
    uint8_t dst;
    uint8_t dport;
    uint8_t sport;
    uint8_t flags;
} csp_header;

typedef enum {
    LINK_LOCAL,     // this node
    LINK_CAN,       // CAN0, CSP fragmentation protocol
    LINK_KISS,      // UART0, KISS framing (zed-a only)
} csp_link;

typedef struct {
    uint32_t routed;        // forwarded from one link to the other
    uint32_t delivered;     // addressed to this node
    uint32_t sent;          // created on this node
    uint32_t crc_errors;    // KISS CRC-32C and CSP CRC32 option failures
    uint32_t no_route;      // would leave on the link it arrived on, or claims our address
    uint32_t no_port;       // no service on the destination port
    uint32_t unsupported;   // RDP, XTEA or HMAC requested, or a payload over CSP_MTU
    uint32_t tx_full;       // the outgoing link's buffer had no room
} csp_counters;

extern csp_counters csp_stats;

uint32_t csp_header_pack(const csp_header *header);
csp_header csp_header_unpack(uint32_t word);

csp_link csp_route_for(uint8_t dst);
const char *csp_link_name(csp_link link);

// A packet that arrived on `from`, or that this node created (LINK_LOCAL):
// deliver it here or send it on.
void csp_route(const csp_header *header, const uint8_t *data, uint32_t length, csp_link from);

// Implemented by the services and the console.
void services_deliver(const csp_header *header, const uint8_t *data, uint32_t length);

#endif
