# Startup for the contract-v3 test programs (docs/contract.md sections 3-4). Execution starts at
# pc = 0 (flash) after reset.
#
#   1. x1..x15 start unknown on the core (contract 1), so clear them all: otherwise prologues
#      would store unknown callee-saved values to PSRAM.
#   2. mtvec = _default_trap. mstatus.MIE, mstatus.MPIE and mie.MEIE reset to 0 (contract 4):
#      read them before anything writes them, and fail with id 0x5F unless all three are 0.
#   3. PSRAM starts arbitrary (contract 3): copy .data from its flash image, clear .bss.
#   4. call main. main returns 0 on pass, or a nonzero failure id.
#   5. _pass: ALTFN = 0 (every uo pin back to GPIO), GPIO_OUT = 0x5A, then the program end
#      (`jal x0, 0` retired twice at the same pc, contract 4).
#
# _fail (a0 = failure id < 0x60): ALTFN = 0, GPIO_OUT = id, then GPIO_OUT = 0xEE, self-loop.
# 0x5F: the interrupt enables were not 0 after reset.
# _default_trap (any trap a program doesn't handle): fails with id 0x60 + mcause[3:0], plus 0x10
# for an interrupt.
#include "bonetto.h"

    .section .text.start, "ax"
    .global _start
_start:
    li   x1, 0
    li   x2, 0
    li   x3, 0
    li   x4, 0
    li   x5, 0
    li   x6, 0
    li   x7, 0
    li   x8, 0
    li   x9, 0
    li   x10, 0
    li   x11, 0
    li   x12, 0
    li   x13, 0
    li   x14, 0
    li   x15, 0
    lui  t0, %hi(_default_trap)
    addi t0, t0, %lo(_default_trap)
    csrw mtvec, t0
    csrr t0, mstatus
    andi t0, t0, MSTATUS_MIE | MSTATUS_MPIE
    csrr t1, mie                 # MEIE is its only bit that can be 1
    or   t0, t0, t1
    beqz t0, 0f
    li   a0, 0x5F
    j    _fail
0:
    lui  sp, %hi(_stack_top)
    addi sp, sp, %lo(_stack_top)
    # .data: words from the flash image (both ends are 4-aligned, see link.ld)
    lui  t0, %hi(_data_lma)
    addi t0, t0, %lo(_data_lma)
    lui  t1, %hi(_data_start)
    addi t1, t1, %lo(_data_start)
    lui  t2, %hi(_data_end)
    addi t2, t2, %lo(_data_end)
1:  bgeu t1, t2, 2f
    lw   a0, 0(t0)
    sw   a0, 0(t1)
    addi t0, t0, 4
    addi t1, t1, 4
    j    1b
2:  # .bss
    lui  t1, %hi(_bss_start)
    addi t1, t1, %lo(_bss_start)
    lui  t2, %hi(_bss_end)
    addi t2, t2, %lo(_bss_end)
3:  bgeu t1, t2, 4f
    sw   zero, 0(t1)
    addi t1, t1, 4
    j    3b
4:  li   a0, 0
    call main
    bnez a0, _fail

    .global _pass
_pass:
    lui  t0, 0x80000
    sw   zero, OFS_ALTFN(t0)
    li   t1, RESULT_PASS
    sw   t1, OFS_GPIO_OUT(t0)
    .global _end_ok
_end_ok:
    j    _end_ok

    .global _fail
_fail:
    lui  t0, 0x80000
    sw   zero, OFS_ALTFN(t0)
    sb   a0, OFS_GPIO_OUT(t0)
    li   t1, RESULT_FAIL
    sw   t1, OFS_GPIO_OUT(t0)
5:  j    5b

    .align 2
    .global _default_trap
_default_trap:
    csrr a0, mcause
    srli t0, a0, 31
    slli t0, t0, 4
    andi a0, a0, 15
    or   a0, a0, t0
    addi a0, a0, 0x60
    j    _fail
