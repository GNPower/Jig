// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power

#include "services.h"

#include "can.h"
#include "config.h"
#include "crc32c.h"
#include "irq.h"
#include "kiss.h"

static uint8_t reply[CSP_MTU + 4u];     // an answer and its CRC32 option

uint32_t services_status(uint8_t *out)
{
    uint32_t ecr = can_error_counters();
    put_be32(&out[0], STATUS_MAGIC);
    out[4] = STATUS_VERSION;
    out[5] = NODE_ADDRESS;
    out[6] = (uint8_t)ecr;
    out[7] = (uint8_t)(ecr >> 8);
    put_be32(&out[8], millis());
    put_be32(&out[12], can_stats.tx_frames);
    put_be32(&out[16], can_stats.rx_frames);
    put_be32(&out[20], kiss_stats.tx_frames);
    put_be32(&out[24], kiss_stats.rx_frames);
    put_be32(&out[28], csp_stats.routed);
    put_be32(&out[32], csp_stats.crc_errors);
    return STATUS_LENGTH;
}

// Sends `length` bytes of `reply` back to the requester. The answer keeps the
// request's priority and CRC32 option, as a libcsp server connection does.
static void answer(const csp_header *request, uint32_t length)
{
    csp_header header = {
        .priority = request->priority,
        .src = NODE_ADDRESS,
        .dst = request->src,
        .dport = request->sport,
        .sport = request->dport,
        .flags = request->flags & CSP_FLAG_CRC32,
    };
    if ((header.flags & CSP_FLAG_CRC32) != 0) {
        put_be32(&reply[length], crc32c(reply, length));
        length += 4u;
    }
    csp_route(&header, reply, length, LINK_LOCAL);
}

void services_deliver(const csp_header *header, const uint8_t *data, uint32_t length)
{
    switch (header->dport) {
    case PORT_PING:
        memmove(reply, data, length);
        answer(header, length);
        return;
    case PORT_UPTIME:
        put_be32(reply, millis() / 1000u);
        answer(header, 4u);
        return;
    case PORT_STATUS:
        if (length == 1u && data[0] == STATUS_REQUEST) {
            answer(header, services_status(reply));
        } else {
            csp_stats.no_port++;
        }
        return;
    default:
        if (!console_ping_reply(header, data, length)) {
            csp_stats.no_port++;
        }
        return;
    }
}
