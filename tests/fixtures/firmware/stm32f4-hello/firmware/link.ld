/*
 * STM32F4 smoke test: code in flash at 0x08000000, data in SRAM at
 * 0x20000000 (RM0090 section 2.3, memory map); 128 KiB of SRAM used. The
 * vector table comes first, where the driver points the core.
 */
MEMORY
{
    FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 1024K
    SRAM (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

ENTRY(reset_handler)

SECTIONS
{
    .vectors : { KEEP(*(.vectors)) } > FLASH
    .text : { *(.text*) *(.rodata*) } > FLASH
    .ARM.exidx : { *(.ARM.exidx*) } > FLASH

    .data :
    {
        _data_start = .;
        *(.data*)
        _data_end = .;
    } > SRAM AT > FLASH
    _data_load = LOADADDR(.data);

    .bss (NOLOAD) :
    {
        _bss_start = .;
        *(.bss*)
        *(COMMON)
        _bss_end = .;
    } > SRAM

    _stack_top = ORIGIN(SRAM) + LENGTH(SRAM);

    /DISCARD/ : { *(.note*) *(.comment) }
}
