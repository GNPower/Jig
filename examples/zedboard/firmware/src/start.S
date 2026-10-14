// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Start-up for Cortex-A9 core 0 in ARM state: vector table, mode stacks, BSS
// clear, then main(). The MMU and the caches stay off, so every data access is
// strongly ordered (Arm DDI 0406 B3.2.1) and the C code is built with
// -mno-unaligned-access.
//
// Coprocessor registers (Arm DDI 0406 "ARM Architecture Reference Manual
// ARMv7-A and ARMv7-R edition"): MPIDR c0,c0,5; SCTLR c1,c0,0; VBAR c12,c0,0.

    .syntax unified
    .arm

    .equ    MODE_IRQ, 0x12
    .equ    MODE_SVC, 0x13
    .equ    MODE_ABT, 0x17
    .equ    MODE_UND, 0x1B

    .equ    SCTLR_M,  (1 << 0)      // MMU
    .equ    SCTLR_C,  (1 << 2)      // data cache
    .equ    SCTLR_Z,  (1 << 11)     // branch prediction
    .equ    SCTLR_I,  (1 << 12)     // instruction cache
    .equ    SCTLR_V,  (1 << 13)     // high vectors (0 = use VBAR)
    .equ    SCTLR_TE, (1 << 30)     // Thumb exceptions

    .section .vectors, "ax"
    .align  5                       // VBAR needs 32-byte alignment
    .global vector_table
vector_table:
    b       reset_entry
    b       undef_entry
    b       svc_entry
    b       pabort_entry
    b       dabort_entry
    b       .                       // reserved
    b       irq_entry
    b       fiq_entry

    .text
    .global _start
    .type   _start, %function
_start:
reset_entry:
    cpsid   if, #MODE_SVC

    // Core 0 runs the firmware; any other core waits here.
    mrc     p15, 0, r0, c0, c0, 5
    ands    r0, r0, #3
    beq     1f
0:  wfe
    b       0b
1:
    mrc     p15, 0, r0, c1, c0, 0
    bic     r0, r0, #(SCTLR_M | SCTLR_C)
    bic     r0, r0, #(SCTLR_Z | SCTLR_I)
    bic     r0, r0, #SCTLR_V
    bic     r0, r0, #SCTLR_TE
    mcr     p15, 0, r0, c1, c0, 0
    isb

    ldr     r0, =vector_table
    mcr     p15, 0, r0, c12, c0, 0
    isb

    cps     #MODE_IRQ
    ldr     sp, =__irq_stack_top
    cps     #MODE_ABT
    ldr     sp, =__abt_stack_top
    cps     #MODE_UND
    ldr     sp, =__und_stack_top
    cps     #MODE_SVC
    ldr     sp, =__svc_stack_top

    ldr     r0, =__bss_start
    ldr     r1, =__bss_end
    mov     r2, #0
2:  cmp     r0, r1
    strlo   r2, [r0], #4
    blo     2b

    bl      main
3:  wfi
    b       3b
    .size   _start, . - _start

// IRQ: the handler runs on the IRQ stack with IRQs masked, then returns to the
// interrupted instruction (LR_irq - 4) and restores CPSR from SPSR.
irq_entry:
    sub     lr, lr, #4
    push    {r0-r3, r12, lr}
    bl      irq_handler
    ldmfd   sp!, {r0-r3, r12, pc}^

// Everything else is fatal: report the kind and the faulting address.
undef_entry:
    mov     r0, #1
    sub     r1, lr, #4
    b       fault_entry
svc_entry:
    mov     r0, #2
    sub     r1, lr, #4
    b       fault_entry
pabort_entry:
    mov     r0, #3
    sub     r1, lr, #4
    b       fault_entry
dabort_entry:
    mov     r0, #4
    sub     r1, lr, #8
    b       fault_entry
fiq_entry:
    mov     r0, #5
    sub     r1, lr, #4
fault_entry:
    cpsid   if, #MODE_ABT
    ldr     sp, =__abt_stack_top
    b       fault_handler
