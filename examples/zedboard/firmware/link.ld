/* SPDX-License-Identifier: MIT */
/* Copyright (c) 2026 Graham Power */

/*
 * The firmware runs from ZedBoard DDR at 0x00100000, the first address every
 * interconnect master reaches as DDR (UG585 Table 4-1). On hardware the DDR
 * controller must be set up before the ELF is loaded: run the board's
 * ps7_init from the debugger first, as in any Zynq JTAG flow.
 */

OUTPUT_FORMAT("elf32-littlearm")
OUTPUT_ARCH(arm)
ENTRY(_start)

MEMORY
{
    DDR (rwx) : ORIGIN = 0x00100000, LENGTH = 16M
}

PHDRS
{
    text PT_LOAD FLAGS(5);      /* read, execute */
    data PT_LOAD FLAGS(6);      /* read, write */
}

UND_STACK_SIZE = 1K;
ABT_STACK_SIZE = 1K;
IRQ_STACK_SIZE = 4K;
SVC_STACK_SIZE = 16K;

SECTIONS
{
    .text : ALIGN(32)
    {
        KEEP(*(.vectors))
        *(.text .text.*)
    } > DDR :text

    .rodata : ALIGN(4)
    {
        *(.rodata .rodata.*)
    } > DDR :text

    .data : ALIGN(4)
    {
        *(.data .data.*)
    } > DDR :data

    .bss (NOLOAD) : ALIGN(4)
    {
        __bss_start = .;
        *(.bss .bss.* COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } > DDR :data

    .stacks (NOLOAD) : ALIGN(8)
    {
        . += UND_STACK_SIZE;
        __und_stack_top = .;
        . += ABT_STACK_SIZE;
        __abt_stack_top = .;
        . += IRQ_STACK_SIZE;
        __irq_stack_top = .;
        . += SVC_STACK_SIZE;
        __svc_stack_top = .;
    } > DDR :data

    /DISCARD/ :
    {
        *(.ARM.exidx*)
        *(.ARM.extab*)
        *(.comment)
        *(.note*)
    }
}
