# Synchronous trap programs, contract v3 section 4. Built once per TRAP_KIND:
#   1 ecall (mcause 11)            2 ebreak (mcause 3)
#   3 jal x1 to pc+6 (mcause 0)    4 jalr x1, 3(x5): target (x5 + 3) & ~1 = x5 + 2 (mcause 0)
#   5 taken beq to pc+6 (mcause 0) 6 the word 0x00000000 (mcause 2)
#   7 the word 0xFFFFFFFF (mcause 2)
# mstatus.MIE = 1 before the trap (mie.MEIE = 0, so no interrupt can be taken). The handler
# checks mcause, mepc = the trapping pc, mstatus (MIE = 0, MPIE = 1, MPP = 3), mtval = 0 and
# that it runs once, then returns with MRET to mepc + 4. Back in main: the trap had no other
# effect (x1 not linked, PSRAM unchanged), MRET restored MIE = 1, then GPIO_OUT = 0x5A.
#
# The trapping instructions are hand-encoded words, so the assembler can't reject or relax them.
# Failure ids: 0x30-0x37 in main, 0x40-0x44 in the handler.
#include "bonetto.h"

#if TRAP_KIND == 1
#define CAUSE MCAUSE_ECALL
#elif TRAP_KIND == 2
#define CAUSE MCAUSE_BREAKPOINT
#elif TRAP_KIND >= 3 && TRAP_KIND <= 5
#define CAUSE MCAUSE_MISALIGNED_FETCH
#elif TRAP_KIND == 6 || TRAP_KIND == 7
#define CAUSE MCAUSE_ILLEGAL
#else
#error "TRAP_KIND must be 1..7"
#endif

    .macro ABS rd, sym
    lui  \rd, %hi(\sym)
    addi \rd, \rd, %lo(\sym)
    .endm

    # fail with id unless ra == rb
    .macro CHECK_EQ ra, rb, id
    beq  \ra, \rb, .Lok\@
    li   a0, \id
    j    _fail
.Lok\@:
    .endm

    .text
    .global main
main:
    ABS  t0, handler
    csrw mtvec, t0
    csrw mie, zero
    li   t0, MSTATUS_MIE       # MIE = 1, MPIE = 0
    csrw mstatus, t0
    li   s1, 0                 # handler entries
    ABS  s0, buf
    li   t0, 0x11223344
    sw   t0, 0(s0)
    li   x1, 0x55              # a JAL / JALR trap must not link
    li   x6, 0x77
    ABS  x5, target            # 4-byte aligned

trap_pc:
#if TRAP_KIND == 1
    ecall
#elif TRAP_KIND == 2
    ebreak
#elif TRAP_KIND == 3
    .word 0x006000ef           # jal x1, pc+6
#elif TRAP_KIND == 4
    .word 0x003280e7           # jalr x1, 3(x5): (target + 3) & ~1 = target + 2
#elif TRAP_KIND == 5
    .word 0x00630363           # beq x6, x6, pc+6 (taken)
#elif TRAP_KIND == 6
    .word 0x00000000
#else
    .word 0xffffffff
#endif
after:                         # MRET returns here (mepc + 4)
    li   t1, 1
    CHECK_EQ s1, t1, 0x30      # exactly one trap
    li   t1, 0x55
    CHECK_EQ x1, t1, 0x31      # no link
    li   t1, 0x77
    CHECK_EQ x6, t1, 0x32
    ABS  t1, target
    CHECK_EQ x5, t1, 0x33
    lw   t0, 0(s0)
    li   t1, 0x11223344
    CHECK_EQ t0, t1, 0x34      # no memory write
    csrr t0, mstatus
    li   t1, 0x1888            # MRET: MIE = MPIE (1), MPIE = 1
    CHECK_EQ t0, t1, 0x35
    csrw mstatus, zero
    csrr t0, mcause
    li   t1, CAUSE
    CHECK_EQ t0, t1, 0x36
    csrr t0, mepc
    ABS  t1, after
    CHECK_EQ t0, t1, 0x37      # the handler's mepc + 4
    j    _pass

    # Never runs: the targets of the trapping jumps sit 2 bytes into this word.
    .align 2
target:
    li   a0, 0x38
    j    _fail

    .align 2
handler:
    csrr a3, mcause
    li   a4, CAUSE
    CHECK_EQ a3, a4, 0x40
    csrr a3, mepc
    ABS  a4, trap_pc
    CHECK_EQ a3, a4, 0x41
    csrr a3, mstatus
    li   a4, 0x1880            # MIE = 0, MPIE = 1 (the old MIE), MPP = 3
    CHECK_EQ a3, a4, 0x42
    csrr a3, mtval
    CHECK_EQ a3, zero, 0x43
    addi s1, s1, 1
    li   a4, 1
    CHECK_EQ s1, a4, 0x44
    csrr a3, mepc
    addi a3, a3, 4
    csrw mepc, a3
    mret

    .section .bss
    .align 2
buf:
    .space 16
