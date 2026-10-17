# trap_*: one synchronous trap each (TRAP_KIND): 1 ecall, 2 ebreak, 3 jal to pc+6, 4 jalr with
# target[1] = 1, 5 taken branch to pc+6, 6 the word 0x00000000, 7 the word 0xFFFFFFFF.
# Before the trap MIE = 1 (MEIE = 0). The handler checks mcause, mepc = the trapping pc,
# MIE = 0, MPIE = 1, sets mepc to `resume` and returns with MRET; then MIE = 1 and MPIE = 1 and
# the registers the trapping instruction names are unchanged. A not-taken branch to a
# misaligned target must not trap.
#define EXPECT(r, v) li x15, v; bne r, x15, fail

    .section .text.start, "ax"
    .global _start
_start:
    li    x1, 0
    li    x2, 0
    li    x3, 0
    li    x4, 0
    li    x5, 0
    li    x6, 0
    li    x7, 0
    li    x8, 0
    li    x9, 0
    li    x10, 0
    li    x11, 0
    li    x12, 0
    li    x13, 0
    li    x14, 0
    li    x15, 0
    la    x2, handler
    csrw  mtvec, x2
    .word 0x00631363            # bne x6, x6, pc+6: not taken, must not trap
    li    x1, 0x55
    li    x6, 0x77
    la    x5, target            # 4-byte aligned
    la    x4, tpc
    li    x3, 8
    csrs  mstatus, x3           # MIE = 1, MEIE = 0
    .align 2
tpc:
#if TRAP_KIND == 1
    ecall
#elif TRAP_KIND == 2
    ebreak
#elif TRAP_KIND == 3
    .word 0x006000ef            # jal x1, pc+6
#elif TRAP_KIND == 4
    .word 0x003280e7            # jalr x1, 3(x5): (target + 3) & ~1 = target + 2
#elif TRAP_KIND == 5
    .word 0x00630363            # beq x6, x6, pc+6 (taken)
#elif TRAP_KIND == 6
    .word 0x00000000
#elif TRAP_KIND == 7
    .word 0xffffffff
#else
#error "TRAP_KIND must be 1..7"
#endif
    j     fail                  # never: the handler resumes at `resume`
    .align 2
target:
    j     fail
    nop
    j     fail
resume:
    EXPECT(x1, 0x55)
    EXPECT(x6, 0x77)
    li    x9, 1
    bne   x8, x9, fail          # the handler ran once
    csrr  x2, mstatus
    EXPECT(x2, 0x1888)          # MRET: MIE = MPIE (1), MPIE = 1
    li    x13, 0x5a
    j     done
fail:
    li    x13, 0xee
done:
    csrw  mstatus, x0
    lui   x14, 0x80000
    sw    x0, 8(x14)
    sw    x13, 0(x14)
1:  jal   x0, 1b

    .align 2
handler:
    addi  x8, x8, 1
    csrr  x2, mcause
#if TRAP_KIND == 1
    EXPECT(x2, 11)
#elif TRAP_KIND == 2
    EXPECT(x2, 3)
#elif TRAP_KIND >= 3 && TRAP_KIND <= 5
    EXPECT(x2, 0)
#else
    EXPECT(x2, 2)
#endif
    csrr  x2, mepc
    bne   x2, x4, fail
    csrr  x2, mstatus
    EXPECT(x2, 0x1880)          # MIE = 0, MPIE = 1
    la    x2, resume
    csrw  mepc, x2
    mret
