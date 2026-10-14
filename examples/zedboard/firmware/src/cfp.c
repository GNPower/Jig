// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power

#include "cfp.h"

#include "can.h"
#include "config.h"
#include "irq.h"

#define CFP_SRC_SHIFT       24
#define CFP_DST_SHIFT       19
#define CFP_TYPE_SHIFT      18
#define CFP_REMAIN_SHIFT    10
#define CFP_ADDRESS_MASK    0x1Fu
#define CFP_REMAIN_MASK     0xFFu
#define CFP_IDENT_MASK      0x3FFu
#define CFP_BEGIN           0u
#define CFP_MORE            1u
#define CFP_OVERHEAD        6u      // header and length in the BEGIN frame
#define CFP_TIMEOUT_MS      1000u
#define SLOTS               4u

typedef struct {
    bool used;
    uint8_t src;
    uint8_t dst;
    uint16_t ident;
    uint8_t remain;         // MORE frames still to come
    uint16_t length;
    uint16_t received;
    uint32_t started;
    csp_header header;
    uint8_t data[CSP_MTU];
} transfer;

cfp_counters cfp_stats;
static transfer transfers[SLOTS];
static uint32_t next_ident;

static uint32_t cfp_id(uint32_t src, uint32_t dst, uint32_t type, uint32_t remain, uint32_t ident)
{
    return ((src & CFP_ADDRESS_MASK) << CFP_SRC_SHIFT)
         | ((dst & CFP_ADDRESS_MASK) << CFP_DST_SHIFT)
         | ((type & 1u) << CFP_TYPE_SHIFT)
         | ((remain & CFP_REMAIN_MASK) << CFP_REMAIN_SHIFT)
         | (ident & CFP_IDENT_MASK);
}

// The acceptance filters pass frames addressed to this node, to the CSP
// broadcast address and, on zed-a, to the host computer it relays for.
bool cfp_init(void)
{
    const uint32_t dst_mask = CFP_ADDRESS_MASK << CFP_DST_SHIFT;
    can_filter filters[3] = {
        { (uint32_t)NODE_ADDRESS << CFP_DST_SHIFT, dst_mask },
        { CSP_BROADCAST << CFP_DST_SHIFT, dst_mask },
        { HOST_ADDRESS << CFP_DST_SHIFT, dst_mask },
    };
    return can_init(filters, NODE_HAS_KISS ? 3u : 2u);
}

bool cfp_send(const csp_header *header, const uint8_t *data, uint32_t length)
{
    if (length > CSP_MTU) {
        return false;
    }
    uint32_t remain = (length + CFP_OVERHEAD - 1u) / 8u;
    if (can_tx_space() < remain + 1u) {
        return false;
    }
    uint32_t ident = next_ident;
    next_ident = (next_ident + 1u) & CFP_IDENT_MASK;

    can_frame frame;
    uint32_t first = length < 2u ? length : 2u;
    frame.id = cfp_id(header->src, header->dst, CFP_BEGIN, remain, ident);
    frame.length = (uint8_t)(CFP_OVERHEAD + first);
    put_be32(&frame.data[0], csp_header_pack(header));
    put_be16(&frame.data[4], (uint16_t)length);
    memcpy(&frame.data[6], data, first);
    can_send(&frame);

    for (uint32_t sent = first; sent < length;) {
        uint32_t count = length - sent < 8u ? length - sent : 8u;
        remain--;
        frame.id = cfp_id(header->src, header->dst, CFP_MORE, remain, ident);
        frame.length = (uint8_t)count;
        memcpy(frame.data, data + sent, count);
        can_send(&frame);
        sent += count;
    }
    return true;
}

static transfer *find(uint32_t src, uint32_t dst, uint32_t ident)
{
    for (uint32_t i = 0; i < SLOTS; i++) {
        transfer *t = &transfers[i];
        if (t->used && t->src == src && t->dst == dst && t->ident == ident) {
            return t;
        }
    }
    return NULL;
}

static transfer *allocate(void)
{
    transfer *oldest = &transfers[0];
    for (uint32_t i = 0; i < SLOTS; i++) {
        transfer *t = &transfers[i];
        if (!t->used) {
            return t;
        }
        if (millis() - t->started > millis() - oldest->started) {
            oldest = t;
        }
    }
    cfp_stats.reassembly_drops++;  // the oldest transfer gives way
    return oldest;
}

static void frame_received(const can_frame *frame)
{
    uint32_t src = (frame->id >> CFP_SRC_SHIFT) & CFP_ADDRESS_MASK;
    uint32_t dst = (frame->id >> CFP_DST_SHIFT) & CFP_ADDRESS_MASK;
    uint32_t type = (frame->id >> CFP_TYPE_SHIFT) & 1u;
    uint32_t remain = (frame->id >> CFP_REMAIN_SHIFT) & CFP_REMAIN_MASK;
    uint32_t ident = frame->id & CFP_IDENT_MASK;
    transfer *t = find(src, dst, ident);

    if (type == CFP_BEGIN) {
        if (frame->length < CFP_OVERHEAD || get_be16(&frame->data[4]) > CSP_MTU) {
            cfp_stats.reassembly_drops++;
            return;
        }
        if (t == NULL) {
            t = allocate();
        }
        t->used = true;
        t->src = (uint8_t)src;
        t->dst = (uint8_t)dst;
        t->ident = (uint16_t)ident;
        t->remain = (uint8_t)remain;
        t->length = get_be16(&frame->data[4]);
        t->started = millis();
        t->header = csp_header_unpack(get_be32(&frame->data[0]));
        uint32_t count = frame->length - CFP_OVERHEAD;
        if (count > t->length) {
            count = t->length;
        }
        memcpy(t->data, &frame->data[CFP_OVERHEAD], count);
        t->received = (uint16_t)count;
    } else {
        if (t == NULL || remain + 1u != t->remain) {
            if (t != NULL) {
                t->used = false;
            }
            cfp_stats.reassembly_drops++;
            return;
        }
        uint32_t count = frame->length;
        if (count > (uint32_t)(t->length - t->received)) {
            count = (uint32_t)(t->length - t->received);
        }
        memcpy(&t->data[t->received], frame->data, count);
        t->received = (uint16_t)(t->received + count);
        t->remain = (uint8_t)remain;
    }

    if (t->remain == 0u) {
        t->used = false;
        if (t->received == t->length) {
            csp_route(&t->header, t->data, t->length, LINK_CAN);
        } else {
            cfp_stats.reassembly_drops++;
        }
    }
}

bool cfp_poll(void)
{
    bool any = false;
    can_frame frame;
    while (can_receive(&frame)) {
        frame_received(&frame);
        any = true;
    }
    for (uint32_t i = 0; i < SLOTS; i++) {
        if (transfers[i].used && millis() - transfers[i].started > CFP_TIMEOUT_MS) {
            transfers[i].used = false;
            cfp_stats.reassembly_drops++;
        }
    }
    return any;
}
