// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power

#include "kiss.h"

#include "config.h"
#include "crc32c.h"
#include "zynq7000.h"

#define FEND        0xC0u
#define FESC        0xDBu
#define TFEND       0xDCu
#define TFESC       0xDDu
#define TNC_DATA    0x00u

// Command byte, header, payload and the KISS CRC. A longer frame is dropped
// whole: the router would refuse its payload anyway.
#define FRAME_MAX   (1u + 4u + CSP_MTU + 4u)

kiss_counters kiss_stats;

static uint8_t rx_storage[1024];
static uint8_t tx_storage[2048];
uart_port kiss_port = { UART0_BASE, BYTE_RING(rx_storage), BYTE_RING(tx_storage), 0u, 0u };

static uint8_t frame[FRAME_MAX];
static uint32_t frame_length;
static bool in_frame;
static bool escaped;
static bool too_long;

void kiss_init(void)
{
    uart_init(&kiss_port);
}

static void frame_done(void)
{
    if (too_long || frame[0] != TNC_DATA || frame_length < 1u + 8u) {
        kiss_stats.bad_frames++;
        return;
    }
    const uint8_t *body = &frame[1];
    const uint8_t *payload = body + 4;
    uint32_t length = frame_length - 1u - 8u;
    if (crc32c(payload, length) != get_be32(payload + length)) {
        csp_stats.crc_errors++;
        return;
    }
    kiss_stats.rx_frames++;
    csp_header header = csp_header_unpack(get_be32(body));
    csp_route(&header, payload, length, LINK_KISS);
}

static void receive(uint8_t byte)
{
    if (byte == FEND) {
        if (in_frame && frame_length > 0u) {
            frame_done();
        }
        in_frame = true;
        frame_length = 0;
        escaped = false;
        too_long = false;
        return;
    }
    if (!in_frame) {
        return;
    }
    if (escaped) {
        escaped = false;
        if (byte == TFEND) {
            byte = FEND;
        } else if (byte == TFESC) {
            byte = FESC;
        }
    } else if (byte == FESC) {
        escaped = true;
        return;
    }
    if (frame_length < FRAME_MAX) {
        frame[frame_length++] = byte;
    } else {
        too_long = true;
    }
}

bool kiss_poll(void)
{
    bool any = false;
    uint8_t byte;
    while (uart_read(&kiss_port, &byte)) {
        receive(byte);
        any = true;
    }
    return any;
}

static void put_escaped(const uint8_t *data, uint32_t length)
{
    for (uint32_t i = 0; i < length; i++) {
        uint8_t byte = data[i];
        if (byte == FEND) {
            ring_put(&kiss_port.tx, FESC);
            ring_put(&kiss_port.tx, TFEND);
        } else if (byte == FESC) {
            ring_put(&kiss_port.tx, FESC);
            ring_put(&kiss_port.tx, TFESC);
        } else {
            ring_put(&kiss_port.tx, byte);
        }
    }
}

bool kiss_send(const csp_header *header, const uint8_t *data, uint32_t length)
{
    // Worst case every byte needs an escape.
    if (!NODE_HAS_KISS || uart_tx_space(&kiss_port) < 3u + 2u * (4u + length + 4u)) {
        return false;
    }
    uint8_t word[4];
    ring_put(&kiss_port.tx, FEND);
    ring_put(&kiss_port.tx, TNC_DATA);
    put_be32(word, csp_header_pack(header));
    put_escaped(word, 4u);
    put_escaped(data, length);
    put_be32(word, crc32c(data, length));
    put_escaped(word, 4u);
    ring_put(&kiss_port.tx, FEND);
    kiss_stats.tx_frames++;
    return true;
}
