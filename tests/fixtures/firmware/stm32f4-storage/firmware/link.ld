/*
 * The storage fixture: code in flash at 0x08000000, data in SRAM at
 * 0x20000000 (RM0090 section 2.3, memory map); 128 KiB of SRAM used. The
 * firmware's flash ends at 0x08020000, where the mcu_lfs partition starts
 * (layout.h), so a firmware that grows into it fails to link.
 */
MEMORY
{
    FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 128K
    SRAM (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

ENTRY(reset_handler)

SECTIONS
{
    .vectors : { KEEP(*(.vectors)) } > FLASH
    .text : { *(.text*) *(.rodata*) } > FLASH
    .ARM.exidx : { *(.ARM.exidx*) } > FLASH

    /* Word-aligned at both ends: the reset handler copies and clears words. */
    .data :
    {
        . = ALIGN(4);
        _data_start = .;
        *(.data*)
        . = ALIGN(4);
        _data_end = .;
    } > SRAM AT > FLASH
    _data_load = LOADADDR(.data);

    .bss (NOLOAD) :
    {
        . = ALIGN(4);
        _bss_start = .;
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        _bss_end = .;
    } > SRAM

    _stack_top = ORIGIN(SRAM) + LENGTH(SRAM);

    /DISCARD/ : { *(.note*) *(.comment) }
}
