// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power

#include "console.h"

#include <stdarg.h>

#include "can.h"
#include "cfp.h"
#include "clock.h"
#include "config.h"
#include "csp.h"
#include "irq.h"
#include "kiss.h"
#include "services.h"
#include "zynq7000.h"

#define LINE_MAX            80u
#define PING_TIMEOUT_MS     1000u
#define PING_DEFAULT_SIZE   32u
#define PING_FIRST_PORT     48u     // the console's pings use source ports 48 to 63
#define PROMPT              "zed> "

static uint8_t rx_storage[256];
static uint8_t tx_storage[4096];
uart_port console_port = { UART1_BASE, BYTE_RING(rx_storage), BYTE_RING(tx_storage), 0u, 0u };

static char line[LINE_MAX + 1u];
static uint32_t line_length;
static bool last_was_cr;
static bool prompt_due;

static struct {
    bool active;
    uint8_t dst;
    uint8_t sport;
    uint8_t sequence;
    uint32_t size;
    uint32_t started;
} ping;
static uint8_t ping_data[CSP_MTU];

// ---------------------------------------------------------------- output

static void put_char(char c)
{
    uart_write(&console_port, &c, 1u);
}

static uint32_t format_decimal(char *out, unsigned long value)
{
    char digits[10];
    uint32_t count = 0;
    do {
        digits[count++] = (char)('0' + value % 10u);
        value /= 10u;
    } while (value != 0u);
    for (uint32_t i = 0; i < count; i++) {
        out[i] = digits[count - 1u - i];
    }
    return count;
}

static uint32_t format_hex(char *out, unsigned long value, bool upper)
{
    const char *set = upper ? "0123456789ABCDEF" : "0123456789abcdef";
    char digits[8];
    uint32_t count = 0;
    do {
        digits[count++] = set[value & 0xFu];
        value >>= 4;
    } while (value != 0u);
    for (uint32_t i = 0; i < count; i++) {
        out[i] = digits[count - 1u - i];
    }
    return count;
}

// printf subset: %c %s %d %u %x %X %%, the flags '-' and '0', a width, and the
// 'l' length modifier (uint32_t is unsigned long on arm-none-eabi).
void con_printf(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    for (const char *p = format; *p != '\0'; p++) {
        if (*p != '%') {
            put_char(*p);
            continue;
        }
        p++;
        bool left = false;
        bool zero = false;
        bool is_long = false;
        uint32_t width = 0;
        if (*p == '-') {
            left = true;
            p++;
        }
        if (*p == '0') {
            zero = true;
            p++;
        }
        while (*p >= '0' && *p <= '9') {
            width = width * 10u + (uint32_t)(*p - '0');
            p++;
        }
        if (*p == 'l') {
            is_long = true;
            p++;
        }
        char text[12];
        const char *out = text;
        uint32_t length;
        switch (*p) {
        case 'c':
            text[0] = (char)va_arg(args, int);
            length = 1;
            break;
        case 's':
            out = va_arg(args, const char *);
            length = strlen(out);
            break;
        case 'u':
            length = format_decimal(text, is_long ? va_arg(args, unsigned long) : va_arg(args, unsigned int));
            break;
        case 'd': {
            long value = is_long ? va_arg(args, long) : va_arg(args, int);
            if (value < 0) {
                text[0] = '-';
                length = 1u + format_decimal(&text[1], 0ul - (unsigned long)value);
            } else {
                length = format_decimal(text, (unsigned long)value);
            }
            break;
        }
        case 'x':
        case 'X':
            length = format_hex(text, is_long ? va_arg(args, unsigned long) : va_arg(args, unsigned int),
                                *p == 'X');
            break;
        case '\0':
            p--;            // a lone '%' ends the format
            length = 0;
            break;
        default:
            text[0] = *p;   // '%%' and anything unknown print as themselves
            length = 1;
            break;
        }
        for (uint32_t i = length; !left && i < width; i++) {
            put_char(zero ? '0' : ' ');
        }
        uart_write(&console_port, out, length);
        for (uint32_t i = length; left && i < width; i++) {
            put_char(' ');
        }
    }
    va_end(args);
}

// --------------------------------------------------------------- commands

static bool parse_number(const char *text, uint32_t *value)
{
    uint32_t result = 0;
    if (*text == '\0') {
        return false;
    }
    for (; *text != '\0'; text++) {
        if (*text < '0' || *text > '9' || result > 99999u) {
            return false;
        }
        result = result * 10u + (uint32_t)(*text - '0');
    }
    *value = result;
    return true;
}

static void cmd_help(void)
{
    con_printf("commands:\r\n"
               "  help                 this list\r\n"
               "  status               node, links and routes\r\n"
               "  stats                frame and packet counters\r\n"
               "  uptime               time since boot\r\n"
               "  ping <addr> [size]   CSP ping (port %u), size 0 to %u bytes, default %u\r\n",
               PORT_PING, CSP_MTU, PING_DEFAULT_SIZE);
}

static const char *can_error_state(uint32_t sr)
{
    if ((sr & CAN_SR_CONFIG) != 0) {
        return "configuration mode";
    }
    switch ((sr & CAN_SR_ESTAT_MASK) >> CAN_SR_ESTAT_SHIFT) {
    case 1u:
        return "error active";
    case 2u:
        return "bus off";
    case 3u:
        return "error passive";
    default:
        return "error state unknown";
    }
}

static void cmd_status(void)
{
    uint32_t now = millis();
    uint32_t sr = can_status();
    uint32_t ecr = can_error_counters();
    con_printf("node     %s, CSP address %u\r\n", NODE_NAME, (unsigned)NODE_ADDRESS);
    con_printf("uptime   %lu.%03lu s\r\n", now / 1000u, now % 1000u);
    con_printf("can0     %u kbit/s, %s, %s, TEC %lu, REC %lu\r\n", CAN_BITRATE / 1000u,
               (sr & CAN_SR_NORMAL) != 0 ? "normal mode" : "not in normal mode", can_error_state(sr),
               ecr & 0xFFu, (ecr >> 8) & 0xFFu);
    if (NODE_HAS_KISS) {
        con_printf("kiss     uart0, %u baud\r\n", UART_BAUD);
        con_printf("routes   %u -> kiss, others -> can0\r\n", GROUND_ADDRESS);
    } else {
        con_printf("routes   all -> can0\r\n");
    }
    con_printf("services ping %u, uptime %u, status %u\r\n", PORT_PING, PORT_UPTIME, PORT_STATUS);
}

static void cmd_stats(void)
{
    con_printf("can0     sent %lu, received %lu frames; rx overflows %lu, rx dropped %lu, "
               "bus-off %lu, errors %lu\r\n",
               can_stats.tx_frames, can_stats.rx_frames, can_stats.rx_overflows, can_stats.rx_dropped,
               can_stats.bus_off, can_stats.errors);
    if (NODE_HAS_KISS) {
        con_printf("kiss     sent %lu, received %lu frames; bad frames %lu\r\n", kiss_stats.tx_frames,
                   kiss_stats.rx_frames, kiss_stats.bad_frames);
    }
    con_printf("csp      routed %lu, delivered %lu, sent %lu, crc errors %lu\r\n", csp_stats.routed,
               csp_stats.delivered, csp_stats.sent, csp_stats.crc_errors);
    con_printf("dropped  no route %lu, no port %lu, unsupported %lu, link full %lu, reassembly %lu\r\n",
               csp_stats.no_route, csp_stats.no_port, csp_stats.unsupported, csp_stats.tx_full,
               cfp_stats.reassembly_drops);
    con_printf("uart     console overruns %lu", console_port.rx_overruns);
    if (NODE_HAS_KISS) {
        con_printf(", kiss overruns %lu", kiss_port.rx_overruns);
    }
    con_printf("\r\n");
}

static void cmd_ping(uint32_t argc, char *const argv[])
{
    uint32_t address;
    uint32_t size = PING_DEFAULT_SIZE;
    if (argc < 2u || argc > 3u || !parse_number(argv[1], &address) || address > CSP_BROADCAST
        || (argc == 3u && (!parse_number(argv[2], &size) || size > CSP_MTU))) {
        con_printf("usage: ping <addr 0-31> [size 0-%u]\r\n", CSP_MTU);
        return;
    }
    ping.sequence++;
    ping.dst = (uint8_t)address;
    ping.sport = (uint8_t)(PING_FIRST_PORT + (ping.sequence & 0xFu));
    ping.size = size;
    for (uint32_t i = 0; i < size; i++) {
        ping_data[i] = (uint8_t)(ping.sequence + i);
    }
    ping.started = millis();
    ping.active = true;
    csp_header header = {
        .priority = CSP_PRIORITY_NORMAL,
        .src = NODE_ADDRESS,
        .dst = (uint8_t)address,
        .dport = PORT_PING,
        .sport = ping.sport,
        .flags = 0,
    };
    csp_route(&header, ping_data, size, LINK_LOCAL);
}

bool console_ping_reply(const csp_header *header, const uint8_t *data, uint32_t length)
{
    if (!ping.active || header->dport != ping.sport || header->src != ping.dst || header->sport != PORT_PING) {
        return false;
    }
    ping.active = false;
    uint32_t elapsed = millis() - ping.started;
    bool intact = length == ping.size && memcmp(data, ping_data, length) == 0;
    con_printf("reply from %u: %lu bytes in %lu ms%s\r\n", (unsigned)header->src, length, elapsed,
               intact ? "" : ", payload differs");
    prompt_due = true;
    return true;
}

static void execute(char *text)
{
    char *argv[4];
    uint32_t argc = 0;
    while (*text != '\0' && argc < 4u) {
        while (*text == ' ') {
            text++;
        }
        if (*text == '\0') {
            break;
        }
        argv[argc++] = text;
        while (*text != '\0' && *text != ' ') {
            text++;
        }
        if (*text != '\0') {
            *text++ = '\0';
        }
    }
    if (argc == 0u) {
        return;
    }
    if (str_equal(argv[0], "help")) {
        cmd_help();
    } else if (str_equal(argv[0], "status")) {
        cmd_status();
    } else if (str_equal(argv[0], "stats")) {
        cmd_stats();
    } else if (str_equal(argv[0], "uptime")) {
        con_printf("uptime %lu ms\r\n", millis());
    } else if (str_equal(argv[0], "ping")) {
        cmd_ping(argc, argv);
    } else {
        con_printf("unknown command '%s', type help\r\n", argv[0]);
    }
}

// ------------------------------------------------------------ line input

static void receive_char(uint8_t c)
{
    if (c == '\n' && last_was_cr) {
        last_was_cr = false;    // CR LF ends one line, not two
        return;
    }
    last_was_cr = c == '\r';
    if (c == '\r' || c == '\n') {
        con_printf("\r\n");
        line[line_length] = '\0';
        execute(line);
        line_length = 0;
        prompt_due = true;
    } else if (c == 0x08u || c == 0x7Fu) {
        if (line_length > 0u) {
            line_length--;
            con_printf("\b \b");
        }
    } else if (c >= 0x20u && c < 0x7Fu && line_length < LINE_MAX) {
        line[line_length++] = (char)c;
        put_char((char)c);
    }
}

void console_init(void)
{
    uart_init(&console_port);
}

void console_banner(uint32_t clock_faults, bool can_normal)
{
    con_printf("\r\n%s: CSP address %u on a ZedBoard (Zynq-7020, Cortex-A9 core 0)\r\n", NODE_NAME,
               (unsigned)NODE_ADDRESS);
    con_printf("links: can0 %u kbit/s, CSP with 29-bit CFP identifiers", CAN_BITRATE / 1000u);
    if (NODE_HAS_KISS) {
        con_printf("; kiss on uart0 %u baud to ground station %u", UART_BAUD, GROUND_ADDRESS);
    }
    con_printf("\r\nconsole: uart1 %u baud, type help\r\n", UART_BAUD);
    if ((clock_faults & CLOCK_ARM_PLL_UNLOCKED) != 0) {
        con_printf("warning: the ARM PLL did not lock, the CPU runs from PS_CLK\r\n");
    }
    if ((clock_faults & CLOCK_IO_PLL_UNLOCKED) != 0) {
        con_printf("warning: the IO PLL did not lock, UART and CAN clocks are wrong\r\n");
    }
    if (!can_normal) {
        con_printf("warning: can0 did not enter normal mode\r\n");
    }
    prompt_due = true;
}

// Main loop: prints a due prompt and handles typed characters, except while a
// ping waits for its reply.
bool console_poll(void)
{
    bool any = false;
    if (ping.active && millis() - ping.started >= PING_TIMEOUT_MS) {
        ping.active = false;
        con_printf("no reply from %u in %u ms\r\n", (unsigned)ping.dst, PING_TIMEOUT_MS);
        prompt_due = true;
        any = true;
    }
    uint8_t byte;
    while (!ping.active) {
        if (prompt_due) {
            con_printf(PROMPT);
            prompt_due = false;
        }
        if (!uart_read(&console_port, &byte)) {
            break;
        }
        receive_char(byte);
        any = true;
    }
    return any;
}
