# Interrupt stress, contract v3 section 4 (Review Focus 2): interrupts land all over a loop that
# has loads and stores, taken and not-taken branches, JAL, JALR, CSR writes, an MRET that turns MIE
# back on, and MIE windows; mepc must be exactly the next instruction not executed.
#
# The source is the timer's compare match (IRQ_EN.CMP), armed one-shot at the top of every loop
# iteration at an offset from a sweep. Arrival doesn't depend on the program's MIE windows, so
# interrupts land evenly over the loop at any core speed:
#   1. calibration (no interrupt source): one call of the loop (two iterations) timed with
#      TIMER_CNT at P = 8, then at the smallest prescale P that keeps it under 16,384 ticks: T.
#      Its checksum is the reference. Q = T / 4, about half an iteration.
#   2. sweep: call k arms its first iteration Q + Q * vdc(k) ticks after that iteration's top
#      and its second iteration Q * vdc(k) after its top, where vdc(k) = the 7 low bits of k
#      reversed, as a fraction (0, 1/2, 1/4, 3/4, ...): the first iteration samples its second
#      half (where the loop branch is taken), the second iteration its first half, ever finer.
#      Every call must give the reference checksum. The sweep stops when every coverage point
#      below has been hit (and at least CALLS_MIN calls ran); CALLS_MAX calls without full
#      coverage fail.
#   3. WFI with MIE = 1: the interrupt arrives during the stall (WFI may also be a no-op: then
#      the poll loop after it is interrupted). WFI with MIE = 0: it wakes on MEIP & MEIE and
#      nothing is taken.
#
# Exact mepc: every instruction of the loop is a step, `auipc s1, 0` followed by the instruction,
# so s1 holds the pc of the last step marker executed. step_tab has two words per step: the
# control-flow target and the kind. With s1 known, the next instruction not executed is the step's
# instruction (mepc = s1 + 4: not executed yet) or the one after it (E):
#   K_SEQ    E = s1 + 8; either is accepted (re-executing a step changes the checksum)
#   K_CSR    E = s1 + 8; t2 = 0 before the CSR instruction and its rd = t2 gets the CSR's old
#            value, never 0 (mstatus reads MPP = 3, mscratch holds a marker pc), so t2 tells
#            whether it executed: mepc must be E when t2 != 0, s1 + 4 when t2 = 0
#   K_ALW    always taken: E = the target
#   K_LINK   JAL / JALR with rd = ra: E = the target; ra = s1 + 8 tells that it executed
#   K_MRET   MIE = 0 before it, so only E = the target (MRET sets MIE = MPIE = 1)
#   K_NZ_xx  bne xx, zero, target: E = the target when xx != 0, else s1 + 8 (the live register)
# The handler also checks mcause, mstatus = MPP | MPIE (taken with MIE = 1), the pending CMP flag,
# clears it (W1C) and counts in a5. It returns with MPIE = 1.
#
# Coverage (the loop fails without every point): an interrupt right after the store and the load
# behind it, the taken BEQ, the CSRRSI that turns MIE on (interrupts pending during the MIE = 0
# window land there), JAL, the backward BNE (taken), JALR, the CSRRW of mscratch, the MRET (to its
# target) and the loop's BNE (taken). After the CSRRSI and the MRET, landing one instruction later
# also counts: contract 4 lets the core retire one more instruction once MIE turns on.
#
# Registers in the loop: s1 marker, s0 data buffer, a0 checksum, a1 iterations, a2 inner loop,
# a3 this iteration's offset, a4 Q, t0..t2 and ra scratch, gp the caller's return address, tp IO
# base, a5 the handler's interrupt count. Linked with crt0.S. Failure ids: 0x10-0x16 main,
# 0x20-0x26 handler. No stimulus: expect.json is declared (ruling R13).
#include "bonetto.h"

    .option norelax              # every step stays exactly 8 bytes

    .set K_SEQ, 0
    .set K_CSR, 1
    .set K_ALW, 2
    .set K_LINK, 3
    .set K_MRET, 4
    .set K_NZ_A1, 5
    .set K_NZ_A2, 6
    .set K_NZ_A5, 7

    # coverage points: bit number + 1 (0 = none)
    .set C_ST, 1
    .set C_LD, 2
    .set C_BEQ, 3
    .set C_EN, 4
    .set C_JAL, 5
    .set C_INNER, 6
    .set C_JALR, 7
    .set C_CSRW, 8
    .set C_MRET, 9
    .set C_LOOP, 10
    .set COV_ALL, (1 << 10) - 1

    .set SEED, 0x1234
    .set CALLS_MIN, 32
    .set CALLS_MAX, 1024
    .set TICKS_MAX, 16384        # one call stays under this many ticks
    .set POLL_MAX, 1 << 20

    .macro ABS rd, sym
    lui  \rd, %hi(\sym)
    addi \rd, \rd, %lo(\sym)
    .endm

    .macro FAIL id
    li   a0, \id
    j    _fail
    .endm

    # a step: the marker and one instruction (never a pseudo-instruction that expands), with its
    # table entry: target, kind | coverage point when landing after it << 8 | when before << 16
    .macro STEPG kind, target, cove, covn, insn:vararg
    auipc s1, 0
    \insn
    .pushsection .rodata.steps, "a"
    .word \target
    .word \kind | ((\cove) << 8) | ((\covn) << 16)
    .popsection
    .endm

    .macro STEP insn:vararg
    STEPG K_SEQ, 0, 0, 0, \insn
    .endm

    .text
    .global main
main:
    addi sp, sp, -4
    sw   ra, 0(sp)
    lui  tp, 0x80000
    ABS  t0, irq_handler
    csrw mtvec, t0
    sw   zero, OFS_IRQ_EN(tp)
    li   t0, MSTATUS_MPIE
    csrs mstatus, t0             # MPIE = 1: the loop's MRET turns MIE on
    csrw mscratch, tp            # never 0 (the loop's CSRRW reads it back)
    li   a5, 0

# 1. calibration
    li   t0, TMR_P(8) | TMR_EN
    sw   t0, OFS_TIMER_CTRL(tp)
    call measure
    ABS  t0, ref
    sw   a0, 0(t0)
    addi t1, t1, 1               # P: the smallest with ((T8 + 1) << 8) >> P < TICKS_MAX
    slli t1, t1, 8
    li   t2, 0
    li   t0, TICKS_MAX
1:  srl  a2, t1, t2
    bltu a2, t0, 2f
    addi t2, t2, 1
    j    1b
2:  ori  t2, t2, TMR_EN
    sw   t2, OFS_TIMER_CTRL(tp)
    call measure
    ABS  t0, ref
    lw   t0, 0(t0)
    beq  a0, t0, 3f
    FAIL 0x10
3:  srli t1, t1, 2               # Q = T / 4
    bnez t1, 4f
    li   t1, 1
4:  ABS  t2, qtab                # qtab[i] = Q >> i, i = 0..7
    li   t0, 8
5:  sw   t1, 0(t2)
    srli t1, t1, 1
    addi t2, t2, 4
    addi t0, t0, -1
    bnez t0, 5b

# 2. the sweep, with the compare interrupt
    li   t0, IRQ_CMP | IRQ_CAP
    sw   t0, OFS_IRQ_PEND(tp)
    li   t0, IRQ_CMP
    sw   t0, OFS_IRQ_EN(tp)
    li   t0, MIE_MEIE
    csrs mie, t0
sweep:
    ABS  t2, calls
    lw   a2, 0(t2)               # k
    ABS  t2, qtab
    lw   a4, 0(t2)               # Q
    mv   a3, a4                  # a3 = Q + the sum over set bits i of k of Q >> (i + 1)
    li   t0, 1
1:  addi t2, t2, 4
    and  t1, a2, t0
    beqz t1, 2f
    lw   t1, 0(t2)
    add  a3, a3, t1
2:  slli t0, t0, 1
    li   t1, 128
    bltu t0, t1, 1b
    li   a0, SEED
    li   a1, 2
    ABS  s0, buf
    ABS  gp, 3f
    j    entry
3:  ABS  t0, ref
    lw   t0, 0(t0)
    beq  a0, t0, 4f
    FAIL 0x11
4:  ABS  t2, calls
    lw   a2, 0(t2)
    addi a2, a2, 1
    sw   a2, 0(t2)
    ABS  t0, cov
    lw   t0, 0(t0)
    li   t1, COV_ALL
    bne  t0, t1, 5f
    li   t1, CALLS_MIN
    bgeu a2, t1, wfi_tests
5:  li   t1, CALLS_MAX
    bltu a2, t1, sweep
    FAIL 0x12                    # coverage incomplete (cov holds the points hit)

# 3. WFI
wfi_tests:
    ABS  t0, qtab
    lw   a3, 0(t0)
    slli a3, a3, 1               # about one iteration from the arming
    li   a5, 0
    li   a1, POLL_MAX
    ABS  gp, 1f
    j    wfi_blk                 # MIE = 1: an interrupt during the stall
1:  mv   a2, a5                  # MIE = 0: WFI wakes on MEIP & MEIE, nothing is taken
    li   t0, IRQ_CMP
    sw   t0, OFS_IRQ_PEND(tp)
    lw   t0, OFS_TIMER_CNT(tp)
    add  t0, t0, a3
    sw   t0, OFS_TIMER_CMP(tp)
    wfi
    li   t1, POLL_MAX
2:  lw   t0, OFS_IRQ_PEND(tp)
    andi t0, t0, IRQ_CMP
    bnez t0, 3f
    addi t1, t1, -1
    bnez t1, 2b
    FAIL 0x13
3:  beq  a5, a2, 4f
    FAIL 0x14                    # an interrupt with MIE = 0
4:  csrr t0, mip
    li   t1, MIP_MEIP
    beq  t0, t1, 5f
    FAIL 0x15
5:  sw   zero, OFS_IRQ_EN(tp)
    sw   zero, OFS_TIMER_CTRL(tp)
    li   t0, IRQ_CMP | IRQ_CAP
    sw   t0, OFS_IRQ_PEND(tp)
    csrw mie, zero
    lw   ra, 0(sp)
    addi sp, sp, 4
    li   a0, 0
    ret

# One call of the loop timed with TIMER_CNT: t1 = ticks, a0 = checksum (no interrupt source).
measure:
    addi sp, sp, -4
    sw   ra, 0(sp)
    lw   t0, OFS_TIMER_CNT(tp)
    ABS  t1, t_start
    sw   t0, 0(t1)
    li   a0, SEED
    li   a1, 2
    li   a3, 0
    li   a4, 0
    ABS  s0, buf
    ABS  gp, 1f
    j    entry
1:  lw   t0, OFS_TIMER_CNT(tp)
    ABS  t1, t_start
    lw   t1, 0(t1)
    sub  t1, t0, t1
    slli t1, t1, 16
    srli t1, t1, 16
    lw   ra, 0(sp)
    addi sp, sp, 4
    ret

bad:
    FAIL 0x16

# ---- the steps: every 8 bytes from `steps` to `steps_end` is one, with one step_tab entry -------
    .pushsection .rodata.steps, "a"
    .align 2
step_tab:
    .popsection
    .align 3
steps:
entry:                                    # MIE = 0; a0, a1 = 2, a3, a4, s0, gp from the caller
    STEP  addi t2, zero, 0
    STEPG K_CSR, 0, 0, 0, csrrsi t2, mstatus, 8      # MIE = 1
body:
    STEP  lw   t0, OFS_TIMER_CNT(tp)      # this iteration's interrupt: CMP = CNT + a3
    STEP  add  t0, t0, a3
    STEP  sw   t0, OFS_TIMER_CMP(tp)
    STEPG K_SEQ, 0, C_ST, 0, sw a0, 4(s0)
    STEPG K_SEQ, 0, C_LD, 0, lw t1, 4(s0)            # the load right behind the store
    STEP  add  a0, a0, t1
    STEPG K_ALW, 1f, C_BEQ, 0, beq zero, zero, 1f    # taken over one step
    STEP  addi a0, a0, 0x7ff              # never runs
1:  STEP  csrrci zero, mstatus, 8         # MIE = 0 ...
    STEP  xori a0, a0, 0x55
    STEP  addi t2, zero, 0
    STEPG K_CSR, 0, C_EN, 0, csrrsi t2, mstatus, 8   # ... MIE = 1: pending interrupts land here
    STEPG K_SEQ, 0, 0, C_EN, sh a0, 8(s0)            # (or one instruction later)
    STEP  lb   t1, 9(s0)
    STEP  xor  a0, a0, t1
    STEPG K_LINK, 3f, C_JAL, 0, jal ra, 3f
    STEP  addi a0, a0, 5                  # never runs
3:  STEP  add  a0, a0, ra                 # ra = the skipped step's marker
    STEP  addi a2, zero, 2                # an inner loop of 2
4:  STEP  addi a0, a0, 7
    STEP  addi a2, a2, -1
    STEPG K_NZ_A2, 4b, C_INNER, 0, bne a2, zero, 4b  # backward, taken once
    STEP  lui  t0, %hi(5f)
    STEPG K_LINK, 5f, C_JALR, 0, jalr ra, %lo(5f)(t0)
    STEP  addi a0, a0, 9                  # never runs
5:  STEP  add  a0, a0, ra
    STEP  addi t2, zero, 0
    STEPG K_CSR, 0, C_CSRW, 0, csrrw t2, mscratch, s1  # the old value is a marker pc: never 0
    STEP  csrrs t1, mscratch, zero
    STEP  add  a0, a0, t1
    STEP  csrrci zero, mstatus, 8         # MIE = 0 until the MRET
    STEP  lui  t0, %hi(2f)
    STEP  addi t0, t0, %lo(2f)
    STEP  csrrw zero, mepc, t0
    STEPG K_MRET, 2f, C_MRET, 0, mret     # MIE = MPIE = 1 at 2f: pending interrupts land there
    STEP  addi a0, a0, 1                  # never runs
2:  STEPG K_SEQ, 0, 0, C_MRET, sub a3, a3, a4      # (or one instruction later); the second
    STEP  addi a1, a1, -1                 #   iteration arms Q ticks earlier
    STEPG K_NZ_A1, body, C_LOOP, 0, bne a1, zero, body
    STEP  csrrci zero, mstatus, 8         # MIE = 0 for the caller
    STEP  jalr zero, 0(gp)

wfi_blk:                                  # MIE = 0; a3 = the delay, a5 = 0, a1 = the poll bound
    STEP  lw   t0, OFS_TIMER_CNT(tp)
    STEP  add  t0, t0, a3
    STEP  sw   t0, OFS_TIMER_CMP(tp)
    STEP  addi t2, zero, 0
    STEPG K_CSR, 0, 0, 0, csrrsi t2, mstatus, 8      # MIE = 1
    STEP  wfi                             # the interrupt comes during the stall
wfi_poll:                                 # right after the WFI
6:  STEPG K_NZ_A5, 7f, 0, 0, bne a5, zero, 7f        # the handler ran (WFI may be a no-op)
    STEP  addi a1, a1, -1
    STEPG K_NZ_A1, 6b, 0, 0, bne a1, zero, 6b
    STEP  jal  zero, bad                  # no interrupt came
7:  STEP  csrrci zero, mstatus, 8         # MIE = 0
    STEP  jalr zero, 0(gp)
steps_end:

# ---- the handler -------------------------------------------------------------------------------
    .align 2
irq_handler:
    addi sp, sp, -20
    sw   t0, 0(sp)
    sw   t1, 4(sp)
    sw   t2, 8(sp)
    sw   a4, 12(sp)
    sw   gp, 16(sp)
    csrr t0, mcause
    li   t1, MCAUSE_EXT_IRQ
    beq  t0, t1, 1f
    FAIL 0x20
1:  csrr t0, mstatus
    li   t1, (3 << 11) | MSTATUS_MPIE
    beq  t0, t1, 1f
    FAIL 0x21                    # taken with MIE = 0 (MPIE = 0), or MIE left on
1:  lw   t0, OFS_IRQ_PEND(tp)
    andi t0, t0, IRQ_CMP
    bnez t0, 1f
    FAIL 0x22
1:  li   t0, IRQ_CMP
    sw   t0, OFS_IRQ_PEND(tp)    # W1C
    ABS  t1, steps               # s1 must be a step marker
    sub  t2, s1, t1
    ABS  t0, steps_end
    sub  t0, t0, t1
    bgeu t2, t0, 9f
    andi t0, t2, 7
    bnez t0, 9f
    ABS  t0, step_tab
    add  t2, t0, t2
    lw   t0, 4(t2)               # kind word
    lw   gp, 0(t2)               # target
    andi a4, t0, 0xff
    li   t1, K_NZ_A1             # conditional branches: taken from the live register
    bltu a4, t1, 4f
    mv   t2, a1
    beq  a4, t1, 3f
    mv   t2, a2
    li   t1, K_NZ_A2
    beq  a4, t1, 3f
    mv   t2, a5
3:  li   a4, K_ALW
    bnez t2, 4f                  # taken: like K_ALW
    li   a4, K_SEQ               # not taken: the next step, no coverage point when after it
    li   t1, 0xff00
    not  t1, t1
    and  t0, t0, t1
4:  csrr t1, mepc
    li   t2, K_ALW
    beq  a4, t2, 20f             # accept s1 + 4 or the target
    li   t2, K_MRET
    beq  a4, t2, 21f             # only the target
    li   t2, K_LINK
    bne  a4, t2, 5f
    addi t2, s1, 8
    beq  ra, t2, 21f             # ra = the link: it executed, so the target
    j    22f                     # not executed: s1 + 4
5:  addi gp, s1, 8               # K_SEQ, K_CSR: E = s1 + 8
    beqz a4, 20f                 # K_SEQ: accept s1 + 4 or E
    lw   t2, 8(sp)               # K_CSR: the interrupted t2, the CSR's old value if it executed
    bnez t2, 21f
    j    22f
20: addi t2, s1, 4               # s1 + 4 or E
    beq  t1, t2, 23f
    beq  t1, gp, 24f
    FAIL 0x23
21: beq  t1, gp, 24f             # E only
    FAIL 0x24
22: addi t2, s1, 4               # s1 + 4 only
    beq  t1, t2, 23f
    FAIL 0x25
23: srli t0, t0, 16              # landed before the step's instruction
    j    25f
24: srli t0, t0, 8               # landed after it
25: andi t0, t0, 0xff            # the coverage point, if any
    beqz t0, 26f
    addi t0, t0, -1
    li   t2, 1
    sll  t2, t2, t0
    ABS  t1, cov
    lw   t0, 0(t1)
    or   t0, t0, t2
    sw   t0, 0(t1)
26: addi a5, a5, 1               # the interrupt count (a5 belongs to the handler)
    lw   t0, 0(sp)
    lw   t1, 4(sp)
    lw   t2, 8(sp)
    lw   a4, 12(sp)
    lw   gp, 16(sp)
    addi sp, sp, 20
    mret
9:  FAIL 0x26                    # s1 is not a step marker

    .section .bss
    .align 2
buf:
    .space 16
ref:
    .space 4
t_start:
    .space 4
calls:
    .space 4
cov:
    .space 4
qtab:
    .space 4 * 8
