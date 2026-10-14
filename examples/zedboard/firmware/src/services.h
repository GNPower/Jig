// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// CSP services on this node:
//   port 1  ping: the request comes back unchanged
//   port 6  uptime: 4 bytes, big-endian, whole seconds since boot (libcsp 1.6
//           answers CSP_UPTIME in seconds)
//   port 10 status: request 0x01, answer STATUS_LENGTH bytes, big-endian:
//
//   offset size field
//    0     4   magic 0x5A454453 ("ZEDS")
//    4     1   layout version, 1
//    5     1   node CSP address
//    6     1   CAN transmit error counter (ECR.TEC)
//    7     1   CAN receive error counter (ECR.REC)
//    8     4   uptime, milliseconds
//   12     4   CAN frames sent
//   16     4   CAN frames received
//   20     4   KISS frames sent
//   24     4   KISS frames received (good CRC)
//   28     4   CSP packets routed between CAN and KISS
//   32     4   CRC errors (KISS CRC-32C and CSP CRC32 option)

#ifndef SERVICES_H
#define SERVICES_H

#include "csp.h"

#define STATUS_REQUEST      0x01u
#define STATUS_MAGIC        0x5A454453u
#define STATUS_VERSION      1u
#define STATUS_LENGTH       36u

uint32_t services_status(uint8_t *out);

// The console's ping client: true when the packet answered its request.
bool console_ping_reply(const csp_header *header, const uint8_t *data, uint32_t length);

#endif
