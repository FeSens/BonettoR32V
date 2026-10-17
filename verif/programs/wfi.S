# WFI, contract v3 section 4: "WFI retires and may stall until mip.MEIP & mie.MEIE (whatever MIE
# says). Treating it as a no-op is also legal." The harness pulses ui4 (WFI_PIN) every WFI_PERIOD
# cycles from WFI_T0 (expect.json gpio_script); GPIO_EDGE flags its rising edge, IRQ_EN.GPIO and
# mie.MEIE make it the interrupt line.
#   1. MIE = 1: WFI, then the edge is taken as an interrupt. The handler checks mcause and the
#      source, clears the flag (W1C) and records mepc: the instruction after the WFI, or one of
#      the poll loop behind it on a core whose WFI is a no-op. Exactly one entry.
#   2. MIE = 0: WFI wakes on the next edge, and nothing is taken: the flag and mip.MEIP stay set.
#   3. MIE = 0 with mip.MEIP & mie.MEIE already 1: WFI falls through at once.
# Linked with crt0.S. Main uses t0, t1, a2 (IO base), s0 (handler entries), s1 (the last mepc);
# the handler only a3..a5. Failure ids 0x10-0x1a (main), 0x20-0x21 (handler).
# Its outcome depends on the stimulus timing: expect.json is declared (ruling R13).
#include "bonetto.h"
#include "stim.h"

    .set POLL_MAX, 1 << 22

    .macro ABS rd, sym
    lui  \rd, %hi(\sym)
    addi \rd, \rd, %lo(\sym)
    .endm

    .macro FAIL id
    li   a0, \id
    j    _fail
    .endm

    .macro DELAY4                # more than 3 cycles: mip.MEIP follows its sources
    nop
    nop
    nop
    nop
    .endm

    .text
    .global main
main:
    ABS  t0, wfi_handler
    csrw mtvec, t0
    li   s0, 0
    li   s1, 0
    lui  a2, 0x80000
    li   t0, EDGE_EN(1 << WFI_PIN)     # rising
    sw   t0, OFS_GPIO_EDGE(a2)
    li   t0, 0xff
    sw   t0, OFS_GPIO_EPEND(a2)
    li   t0, IRQ_GPIO
    sw   t0, OFS_IRQ_EN(a2)
    li   t0, MIE_MEIE
    csrs mie, t0

# 1. MIE = 1
    csrsi mstatus, 8
    wfi
wfi1_next:
    li   t1, POLL_MAX
1:  bnez s0, 2f                  # the handler ran
    addi t1, t1, -1
    bnez t1, 1b
    FAIL 0x10
2:  csrci mstatus, 8
    li   t0, 1
    beq  s0, t0, 3f
    FAIL 0x11
3:  ABS  t0, wfi1_next           # wfi1_next <= mepc <= the poll loop's last instruction
    bgeu s1, t0, 4f
    FAIL 0x12
4:  ABS  t0, 2b
    bltu s1, t0, 5f
    FAIL 0x13

# 2. MIE = 0: WFI still wakes on the edge
5:  li   t0, 0xff
    sw   t0, OFS_GPIO_EPEND(a2)
    DELAY4
    csrr t0, mip
    beqz t0, 6f                  # the next edge is WFI_PERIOD cycles away
    FAIL 0x14
6:  wfi
    li   t1, POLL_MAX
7:  lw   t0, OFS_GPIO_EPEND(a2)
    andi t0, t0, 1 << WFI_PIN
    bnez t0, 8f
    addi t1, t1, -1
    bnez t1, 7b
    FAIL 0x15
8:  li   t0, 1
    beq  s0, t0, 9f              # nothing taken
    FAIL 0x16
9:  csrr t0, mip
    li   t1, MIP_MEIP
    beq  t0, t1, 10f             # pending, masked by MIE
    FAIL 0x17

# 3. MIE = 0, mip.MEIP & mie.MEIE = 1: WFI falls through
10: wfi
    csrr t0, mip
    beq  t0, t1, 11f
    FAIL 0x18
11: li   t0, 1
    beq  s0, t0, 12f
    FAIL 0x19
12: li   t0, 0xff
    sw   t0, OFS_GPIO_EPEND(a2)
    DELAY4
    csrr t0, mip
    beqz t0, 13f
    FAIL 0x1a
13: sw   zero, OFS_IRQ_EN(a2)
    sw   zero, OFS_GPIO_EDGE(a2)
    csrw mie, zero
    li   a0, 0
    ret

    .align 2
wfi_handler:
    csrr a3, mcause
    li   a4, MCAUSE_EXT_IRQ
    beq  a3, a4, 1f
    FAIL 0x20
1:  lw   a3, OFS_IRQ_PEND(a2)
    andi a3, a3, IRQ_GPIO
    bnez a3, 2f
    FAIL 0x21
2:  lw   a3, OFS_GPIO_EPEND(a2)
    sw   a3, OFS_GPIO_EPEND(a2)  # W1C the flags seen
    csrr s1, mepc
    addi s0, s0, 1
    mret
