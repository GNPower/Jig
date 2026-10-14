// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power

#include "can.h"

#include "config.h"
#include "zynq7000.h"

// A bit is one sync quantum plus TS1 and TS2 quanta, each quantum BRPR + 1
// cycles of CAN_REF_CLK; BTR holds TS1 - 1, TS2 - 1 and SJW - 1 (UG585 18.2.6
// and B.5). 100 MHz / 10 gives 10 MHz quanta and 10 quanta per bit: 1 Mbit/s,
// sampled after 8 quanta (80 %).
#define CAN_QUANTA      10u
#define CAN_TS1         7u
#define CAN_TS2         2u
#define CAN_SJW         1u
#define CAN_PRESCALER   (CAN_REF_HZ / (CAN_BITRATE * CAN_QUANTA))

_Static_assert(1u + CAN_TS1 + CAN_TS2 == CAN_QUANTA, "bit time segments");
_Static_assert(CAN_PRESCALER * CAN_QUANTA * CAN_BITRATE == CAN_REF_HZ, "CAN bit rate");

#define CAN_MODE_POLLS  1000000u
#define FIFO_DEPTH      64u
#define RING_SIZE       128u    // power of two

typedef struct {
    can_frame slot[RING_SIZE];
    volatile uint32_t head;
    volatile uint32_t tail;
} frame_ring;

can_counters can_stats;
static frame_ring rx_ring;
static frame_ring tx_ring;

static bool frame_put(frame_ring *ring, const can_frame *frame)
{
    uint32_t head = ring->head;
    if (head - ring->tail >= RING_SIZE) {
        return false;
    }
    ring->slot[head & (RING_SIZE - 1u)] = *frame;
    compiler_barrier();
    ring->head = head + 1u;
    return true;
}

static bool frame_get(frame_ring *ring, can_frame *frame)
{
    uint32_t tail = ring->tail;
    if (ring->head == tail) {
        return false;
    }
    *frame = ring->slot[tail & (RING_SIZE - 1u)];
    compiler_barrier();
    ring->tail = tail + 1u;
    return true;
}

// 29-bit identifier <-> identifier word (UG585 Table 18-3).
static uint32_t idr_from_id(uint32_t id)
{
    return (((id >> 18) & 0x7FFu) << CAN_IDR_ID_HIGH_SHIFT)
         | ((id & 0x3FFFFu) << CAN_IDR_ID_LOW_SHIFT);
}

static uint32_t id_from_idr(uint32_t idr)
{
    return (((idr >> CAN_IDR_ID_HIGH_SHIFT) & 0x7FFu) << 18)
         | ((idr >> CAN_IDR_ID_LOW_SHIFT) & 0x3FFFFu);
}

static bool wait_status(uint32_t bit, bool set)
{
    for (uint32_t i = 0; i < CAN_MODE_POLLS; i++) {
        if (((reg_read(CAN0_BASE + CAN_SR) & bit) != 0) == set) {
            return true;
        }
    }
    return false;
}

// UG585 18.3.3, "Start-up Sequence". Returns true once the controller reports
// normal mode. Filters match extended frames only (IDE compared).
bool can_init(const can_filter *filters, uint32_t count)
{
    reg_write(CAN0_BASE + CAN_SRR, CAN_SRR_SRST);
    wait_status(CAN_SR_CONFIG, true);

    reg_write(CAN0_BASE + CAN_BRPR, CAN_PRESCALER - 1u);
    reg_write(CAN0_BASE + CAN_BTR, ((CAN_SJW - 1u) << 7) | ((CAN_TS2 - 1u) << 4) | (CAN_TS1 - 1u));
    reg_write(CAN0_BASE + CAN_IER, 0);
    reg_write(CAN0_BASE + CAN_ICR, 0x7FFFu);

    // UG585 18.2.5, "Example: Program Acceptance Filter".
    reg_write(CAN0_BASE + CAN_AFR, 0);
    if (count > 4u) {
        count = 4u;
    }
    for (uint32_t i = 0; i < count; i++) {
        wait_status(CAN_SR_ACFBSY, false);
        reg_write(CAN0_BASE + CAN_AFMR(i), idr_from_id(filters[i].mask) | CAN_IDR_IDE);
        reg_write(CAN0_BASE + CAN_AFIR(i), idr_from_id(filters[i].match) | CAN_IDR_IDE | CAN_IDR_SRR);
    }
    reg_write(CAN0_BASE + CAN_AFR, (1u << count) - 1u);

    reg_write(CAN0_BASE + CAN_MSR, 0);              // normal mode
    reg_write(CAN0_BASE + CAN_SRR, CAN_SRR_CEN);
    return wait_status(CAN_SR_NORMAL, true);
}

// Tick interrupt.
void can_service(void)
{
    uint32_t isr = reg_read(CAN0_BASE + CAN_ISR);
    uint32_t events = isr & (CAN_IXR_RXOFLW | CAN_IXR_BSOFF | CAN_IXR_ERROR | CAN_IXR_TXOK | CAN_IXR_RXOK);
    if ((isr & CAN_IXR_RXOFLW) != 0) {
        can_stats.rx_overflows++;
    }
    if ((isr & CAN_IXR_BSOFF) != 0) {
        can_stats.bus_off++;
    }
    if ((isr & CAN_IXR_ERROR) != 0) {
        can_stats.errors++;
        reg_write(CAN0_BASE + CAN_ESR, reg_read(CAN0_BASE + CAN_ESR));
    }
    if (events != 0) {
        reg_write(CAN0_BASE + CAN_ICR, events);
    }

    // UG585 18.3.7: read all four words of each message; RXNEMP sets again
    // while messages remain.
    for (uint32_t n = 0; n < FIFO_DEPTH; n++) {
        if ((reg_read(CAN0_BASE + CAN_ISR) & CAN_IXR_RXNEMP) == 0) {
            break;
        }
        uint32_t idr = reg_read(CAN0_BASE + CAN_RXFIFO_ID);
        uint32_t dlc = reg_read(CAN0_BASE + CAN_RXFIFO_DLC);
        uint32_t data1 = reg_read(CAN0_BASE + CAN_RXFIFO_DATA1);
        uint32_t data2 = reg_read(CAN0_BASE + CAN_RXFIFO_DATA2);
        reg_write(CAN0_BASE + CAN_ICR, CAN_IXR_RXNEMP);
        can_stats.rx_frames++;
        if ((idr & CAN_IDR_IDE) == 0 || (idr & CAN_IDR_RTR) != 0) {
            continue;   // CSP uses extended data frames only
        }
        can_frame frame;
        frame.id = id_from_idr(idr);
        frame.length = (uint8_t)(dlc >> CAN_DLC_SHIFT);
        if (frame.length > 8u) {
            frame.length = 8u;
        }
        put_be32(&frame.data[0], data1);
        put_be32(&frame.data[4], data2);
        if (!frame_put(&rx_ring, &frame)) {
            can_stats.rx_dropped++;
        }
    }

    // UG585 18.3.5: one message per four writes, committed by DATA2.
    can_frame frame;
    while ((reg_read(CAN0_BASE + CAN_SR) & CAN_SR_TXFLL) == 0 && frame_get(&tx_ring, &frame)) {
        reg_write(CAN0_BASE + CAN_TXFIFO_ID, idr_from_id(frame.id) | CAN_IDR_IDE | CAN_IDR_SRR);
        reg_write(CAN0_BASE + CAN_TXFIFO_DLC, (uint32_t)frame.length << CAN_DLC_SHIFT);
        reg_write(CAN0_BASE + CAN_TXFIFO_DATA1, get_be32(&frame.data[0]));
        reg_write(CAN0_BASE + CAN_TXFIFO_DATA2, get_be32(&frame.data[4]));
        can_stats.tx_frames++;
    }
}

bool can_receive(can_frame *frame)
{
    return frame_get(&rx_ring, frame);
}

uint32_t can_tx_space(void)
{
    return RING_SIZE - (tx_ring.head - tx_ring.tail);
}

// Data bytes past the length go to the controller as zeros (UG585 18.2.2).
bool can_send(const can_frame *frame)
{
    can_frame clean = *frame;
    if (clean.length > 8u) {
        clean.length = 8u;
    }
    for (uint32_t i = clean.length; i < 8u; i++) {
        clean.data[i] = 0;
    }
    return frame_put(&tx_ring, &clean);
}

uint32_t can_status(void)
{
    return reg_read(CAN0_BASE + CAN_SR);
}

uint32_t can_error_counters(void)
{
    return reg_read(CAN0_BASE + CAN_ECR);
}
