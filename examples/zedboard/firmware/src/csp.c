// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power

#include "csp.h"

#include "cfp.h"
#include "config.h"
#include "crc32c.h"
#include "kiss.h"

csp_counters csp_stats;

uint32_t csp_header_pack(const csp_header *header)
{
    return ((uint32_t)(header->priority & 0x3u) << 30)
         | ((uint32_t)(header->src & 0x1Fu) << 25)
         | ((uint32_t)(header->dst & 0x1Fu) << 20)
         | ((uint32_t)(header->dport & 0x3Fu) << 14)
         | ((uint32_t)(header->sport & 0x3Fu) << 8)
         | header->flags;
}

csp_header csp_header_unpack(uint32_t word)
{
    csp_header header;
    header.priority = (uint8_t)((word >> 30) & 0x3u);
    header.src = (uint8_t)((word >> 25) & 0x1Fu);
    header.dst = (uint8_t)((word >> 20) & 0x1Fu);
    header.dport = (uint8_t)((word >> 14) & 0x3Fu);
    header.sport = (uint8_t)((word >> 8) & 0x3Fu);
    header.flags = (uint8_t)word;
    return header;
}

// The routing table: zed-a sends the ground station's address to KISS and
// everything else to CAN; zed-b sends everything to CAN.
csp_link csp_route_for(uint8_t dst)
{
    if (dst == NODE_ADDRESS || dst == CSP_BROADCAST) {
        return LINK_LOCAL;
    }
    if (NODE_HAS_KISS && dst == GROUND_ADDRESS) {
        return LINK_KISS;
    }
    return LINK_CAN;
}

const char *csp_link_name(csp_link link)
{
    switch (link) {
    case LINK_CAN:
        return "can0";
    case LINK_KISS:
        return "kiss";
    default:
        return "local";
    }
}

static void deliver(const csp_header *header, const uint8_t *data, uint32_t length)
{
    if ((header->flags & (CSP_FLAG_RDP | CSP_FLAG_XTEA | CSP_FLAG_HMAC)) != 0) {
        csp_stats.unsupported++;
        return;
    }
    if ((header->flags & CSP_FLAG_CRC32) != 0) {
        if (length < 4u || crc32c(data, length - 4u) != get_be32(data + length - 4u)) {
            csp_stats.crc_errors++;
            return;
        }
        length -= 4u;
    }
    csp_stats.delivered++;
    services_deliver(header, data, length);
}

void csp_route(const csp_header *header, const uint8_t *data, uint32_t length, csp_link from)
{
    if (length > CSP_MTU) {
        csp_stats.unsupported++;
        return;
    }
    if (from == LINK_LOCAL) {
        csp_stats.sent++;
    } else if (header->src == NODE_ADDRESS) {
        csp_stats.no_route++;       // a packet from a link cannot come from us
        return;
    }

    csp_link to = csp_route_for(header->dst);
    if (to == LINK_LOCAL) {
        deliver(header, data, length);
        return;
    }
    if (to == from) {
        csp_stats.no_route++;
        return;
    }
    bool queued = false;
    if (to == LINK_CAN) {
        queued = cfp_send(header, data, length);
    } else if (to == LINK_KISS) {
        queued = kiss_send(header, data, length);
    }
    if (!queued) {
        csp_stats.tx_full++;
    } else if (from != LINK_LOCAL) {
        csp_stats.routed++;
    }
}
