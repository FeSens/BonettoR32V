# Interrupt stress, contract v3 section 4 (Review Focus 2): interrupts land all over a loop that
# has loads and stores, taken and not-taken branches, JAL, JALR, CSR writes, an MRET that turns MIE
# back on, and MIE windows; mepc must be exactly the next instruction not executed.
#
# The source is the timer's compare match (IRQ_EN.CMP), armed one-shot at the top of every loop
# iteration at an offset from a sweep. Arrival doesn't depend on the program's MIE windows, so
# interrupts land evenly over the loop at any core speed:
#   1. calibration (no interrupt source): each iteration arms at its top by writing CMP, then
#      CNT = 0, so after one call (two iterations) CNT holds the second iteration's length, from
#      its arm to the return: L, measured at P = 8, then (the longest of CAL_CALLS calls) at the
#      smallest prescale P that keeps it under 8,192 ticks. The checksum is the reference.
#      Q = L / 2. (The arm writes CNT after CMP, so an interrupt landing inside the arm, from the
#      previous iteration's match, can't leave a stale compare.)
#   2. sweep: call j arms its first iteration at Q + w and its second at w, w = v + v / 8,
#      v = Q * vdc(j), where vdc(j) = the VDC_BITS (10) low bits of j reversed, as a fraction
#      (0, 1/2, 1/4, 3/4, ...): the first iteration samples its second half (where the loop
#      branch is taken; the 1/8 stretch reaches it even when the calibration ran short under
#      random timing), the second iteration its first half, ever finer. From FOCUS_FROM calls on,
#      one call in four instead aims at a coverage point still missing: at the arm offset that
#      last landed on its step (off_tab, written by the handler), jittered by -4..+3 x Q / 32.
#      Every call must give the reference checksum. The sweep stops when every coverage point
#      below has been hit (and at least CALLS_MIN calls ran); CALLS_MAX calls without full
#      coverage fail (0x12), inside expect.json's max_cycles (ruling R35) at 2,048 cycles per
#      instruction.
#   3. A forced pending interrupt (CMP = CNT = 0 with MIE = 0) when a CSRRSI, then an MRET,
#      turn MIE on: it must land right after them or one instruction later, never later.
#   4. WFI with MIE = 1: the interrupt arrives during the stall (WFI may also be a no-op: then
#      the poll loop after it is interrupted). WFI with MIE = 0: it wakes on MEIP & MEIE and
#      nothing is taken.
#
# Exact mepc: every instruction of the loop is a step, `auipc s1, 0` followed by the instruction,
# so s1 holds the pc of the last step marker executed. step_tab has two words per step: the
# control-flow target and the kind. With s1 known, the next instruction not executed is the step's
# instruction (mepc = s1 + 4: not executed yet) or the one after it (E):
#   K_SEQ    E = s1 + 8; either is accepted. Re-running a step that folds into a0 or counts the
#            loop changes the checksum; re-running an idempotent one (the t2 clears, the word
#            clear, LUI, the arming stores, CSRRCI) or the arm-offset SUB goes unseen
#   K_DET    E = s1 + 8; the step before sets t2 = 0 and the instruction's rd = t2 gets a value
#            that is never 0 (a CSR's old value: mstatus reads MPP = 3, mscratch holds a marker
#            pc; TIMER_CTRL with EN = 1; the word the store before wrote, a marker pc), so t2
#            tells whether it executed: mepc must be E when t2 != 0, s1 + 4 when t2 = 0
#   K_STDET  E = s1 + 8; a store of a marker pc to a word cleared by the step before: it
#            executed when the word isn't 0
#   K_ALW    always taken: E = the target
#   K_LINK   JAL / JALR with rd = ra: E = the target; ra = s1 + 8 tells that it executed
#   K_MRET   only E = the target (MRET sets MIE = MPIE = 1)
#   K_NZ_xx  bne xx, zero, target: E = the target when xx != 0, else s1 + 8 (the live register)
# and two flags: NOE, no landing after the instruction (CSRRCI turning MIE off), NON, no landing
# before it (MIE = 0 there: the CSRRSI or MRET that turns MIE on); OFF = both (strictly inside an
# MIE = 0 window). The handler also checks mcause, mstatus = MPP | MPIE (taken with MIE = 1), the
# pending CMP flag, clears it (W1C) and counts in a5. It returns with MPIE = 1.
#
# Coverage (the loop fails without every point): an interrupt right after the IO load, the store
# and the load behind it, the taken BEQ, the CSRRSI that turns MIE on (interrupts pending during
# the MIE = 0 window land there), JAL, the backward BNE (taken), JALR, the CSRRW of mscratch, the
# MRET (to its target) and the loop's BNE (taken). After the CSRRSI and the MRET, landing one
# instruction later also counts: contract 4 lets the core retire one more instruction once MIE
# turns on.
#
# Registers in the loop: s1 marker, s0 data buffer (the handler's off_tab follows it), a0
# checksum, a1 iterations, a2 inner loop, a3 this iteration's offset, a4 Q, t0, t2 and ra scratch,
# t1 unused (the handler doesn't keep it), gp the caller's return address, tp IO base, a5 the
# handler's interrupt count. Linked with crt0.S. Failure ids: 0x10-0x17 main,
# 0x20-0x28 handler. No stimulus: expect.json is declared (ruling R13), with max_cycles.
#include "bonetto.h"

    .option norelax              # every step stays exactly 8 bytes

    .set K_SEQ, 0
    .set K_DET, 1
    .set K_ALW, 2
    .set K_LINK, 3
    .set K_MRET, 4
    .set K_STDET, 5
    .set K_NZ_A1, 6              # the conditional kinds come last
    .set K_NZ_A2, 7
    .set K_NZ_A5, 8
    .set NOE, 1 << 24            # no landing after the instruction
    .set NON, 1 << 25            # no landing before it
    .set OFF, NOE | NON

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
    .set C_IOLD, 11
    .set NCOV, 11
    .set COV_ALL, (1 << NCOV) - 1

    .set SEED, 0x1234
    .set CALLS_MIN, 64
    .set FOCUS_FROM, 64          # odd calls from here aim at an uncovered point (see the sweep)
    .set CALLS_MAX, 1024         # ruling R35: a coverage miss fails inside max_cycles
    .set VDC_BITS, 10
    .if CALLS_MAX > (1 << VDC_BITS)
    .error "CALLS_MAX needs more VDC_BITS"
    .endif
    .set TICKS_MAX, 8192         # one iteration stays under this many ticks
    .set CAL_CALLS, 4            # calibration: L is the longest of this many calls
    .set POLL_MAX, 1 << 20
    .set STORM, 64               # interrupts in one call of the loop at most
    .set OFF_TAB, 16             # off_tab - buf

    .macro ABS rd, sym
    lui  \rd, %hi(\sym)
    addi \rd, \rd, %lo(\sym)
    .endm

    .macro FAIL id
    li   a0, \id
    j    _fail
    .endm

    # a step: the marker and one instruction (never a pseudo-instruction that expands), with its
    # table entry: target, kind | flags | coverage point when landing after it << 8 | when
    # before << 16
    .macro STEPG kind, target, cove, covn, insn:vararg
    .if \cove
    .set covstep_\cove, .
    .endif
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

    # strictly inside an MIE = 0 window: no interrupt may land before or after it
    .macro STEPOFF insn:vararg
    STEPG K_SEQ | OFF, 0, 0, 0, \insn
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
    addi t1, t1, 1               # P: the smallest with ((L8 + 1) << 8) >> P < TICKS_MAX
    slli t1, t1, 8
    li   t2, 0
    li   t0, TICKS_MAX
1:  srl  a2, t1, t2
    bltu a2, t0, 2f
    addi t2, t2, 1
    j    1b
2:  ori  t2, t2, TMR_EN
    sw   t2, OFS_TIMER_CTRL(tp)
    li   t0, CAL_CALLS           # L: the longest of CAL_CALLS calls at that P
    ABS  t1, cal_left
    sw   t0, 0(t1)
3:  call measure
    ABS  t0, ref
    lw   t0, 0(t0)
    beq  a0, t0, 4f
    FAIL 0x10
4:  ABS  t2, lmax
    lw   t0, 0(t2)
    bgeu t0, t1, 5f
    sw   t1, 0(t2)
5:  ABS  t2, cal_left
    lw   t0, 0(t2)
    addi t0, t0, -1
    sw   t0, 0(t2)
    bnez t0, 3b
    ABS  t2, lmax
    lw   t1, 0(t2)
    srli t1, t1, 1               # Q = L / 2
    bnez t1, 6f
    li   t1, 1
6:  ABS  t2, qtab                # qtab[i] = Q >> i, i = 0..VDC_BITS
    li   t0, VDC_BITS + 1
7:  sw   t1, 0(t2)
    srli t1, t1, 1
    addi t2, t2, 4
    addi t0, t0, -1
    bnez t0, 7b

# 2. the sweep, with the compare interrupt
    li   t0, IRQ_CMP | IRQ_CAP
    sw   t0, OFS_IRQ_PEND(tp)
    li   t0, IRQ_CMP
    sw   t0, OFS_IRQ_EN(tp)
    li   t0, MIE_MEIE
    csrs mie, t0
sweep:
    ABS  t2, qtab
    lw   a4, 0(t2)               # Q
    ABS  t2, voff
    lw   a3, 0(t2)               # v = Q * vdc(j), j = the sweep calls so far
    srli t0, a3, 3               # stretched by 1/8: the second half reaches past the loop
    add  a3, a3, t0              # branch even when calibration ran short (random timing)
    li   gp, 0                   # gp = 1: this call aims at a missing coverage point instead
    ABS  t2, calls
    lw   a2, 0(t2)
    li   t0, FOCUS_FROM          # from FOCUS_FROM on, one call in four aims
    bltu a2, t0, 18f
    andi t0, a2, 3
    addi t0, t0, -3
    bnez t0, 18f
    ABS  t0, cov                 # the missing points, scanned from r = (k >> 2) & 15 on
    lw   s1, 0(t0)
    not  s1, s1
    li   t0, COV_ALL
    and  s1, s1, t0
    beqz s1, 18f
    srli a0, a2, 2
    andi a0, a0, 15
    srl  t0, s1, a0              # the mask rotated right by r (16 bits)
    li   t1, 16
    sub  t1, t1, a0
    sll  t1, s1, t1
    or   s1, t0, t1
    slli s1, s1, 16
    srli s1, s1, 16
11: andi t0, s1, 1
    beqz t0, 12f
    andi t0, a0, 15              # point (r + i) & 15 is missing: the last arm offset that
    slli t0, t0, 2               # landed on its step
    ABS  t1, cov_step
    add  t1, t1, t0
    lw   t1, 0(t1)
    ABS  t0, off_tab
    add  t1, t1, t0
    lw   t0, 0(t1)
    bnez t0, 13f
12: srli s1, s1, 1
    addi a0, a0, 1
    bnez s1, 11b
    j    18f                     # none has a landing on record: the sweep
13: addi t0, t0, -1              # X (off_tab holds it + 1), jittered by (i - 4) * Q / 32 with
    srli t1, a4, 5               # i = (k >> 2) & 7
    srli a0, a2, 2
    andi a0, a0, 7
14: beqz a0, 15f
    add  t0, t0, t1
    addi a0, a0, -1
    j    14b
15: slli a0, t1, 2               # - 4 * Q / 32
    sub  t0, t0, a0
    bgez t0, 16f
    li   t0, 0
16: mv   a3, t0                  # X < Q: the second iteration arms at X, else the first
    bltu t0, a4, 17f
    sub  a3, t0, a4
17: li   gp, 1
18: ABS  t0, aimed
    sw   gp, 0(t0)
    add  a3, a3, a4              # the first iteration arms at Q + v, the second at v
    li   a0, SEED
    li   a1, 2
    li   a5, 0
    ABS  s0, buf
    ABS  gp, 3f
    j    entry
3:  ABS  t0, ref
    lw   t0, 0(t0)
    beq  a0, t0, 4f
    FAIL 0x11
4:  ABS  t2, calls
    lw   gp, 0(t2)
    addi gp, gp, 1               # gp = k + 1
    sw   gp, 0(t2)
    ABS  t0, aimed
    lw   t0, 0(t0)
    bnez t0, 7f                  # an aimed call leaves the sweep where it was
    ABS  t2, swept               # j + 1, and vdc(j + 1) from vdc(j): the trailing ones of j
    lw   a2, 0(t2)               # clear (bit i takes off Q >> (i + 1)), the next bit sets
    addi t0, a2, 1
    sw   t0, 0(t2)
    ABS  t2, voff
    lw   a3, 0(t2)
    ABS  a4, qtab + 4
5:  andi t1, a2, 1
    beqz t1, 6f
    lw   t1, 0(a4)
    sub  a3, a3, t1
    srli a2, a2, 1
    addi a4, a4, 4
    j    5b
6:  lw   t1, 0(a4)
    add  a3, a3, t1
    sw   a3, 0(t2)
7:  ABS  t0, cov
    lw   t0, 0(t0)
    li   t1, COV_ALL
    bne  t0, t1, 8f
    li   t1, CALLS_MIN
    bgeu gp, t1, forced
8:  li   t1, CALLS_MAX
    bltu gp, t1, sweep
    FAIL 0x12                    # coverage incomplete (cov holds the points hit)

# 3. forced pending, 4. WFI
forced:
    li   a5, 0                   # a pending interrupt meets the CSRRSI, then the MRET
    ABS  gp, 1f
    j    force_blk
1:  li   t0, 2
    beq  a5, t0, wfi_tests
    FAIL 0x17                    # the forced interrupts weren't both taken
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

# One call of the loop (no interrupt source): t1 = CNT at the return, the second iteration's
# length from its arm (L, in ticks), a0 = the checksum.
measure:
    addi sp, sp, -4
    sw   ra, 0(sp)
    li   a0, SEED
    li   a1, 2
    li   a3, 0
    li   a4, 0
    ABS  s0, buf
    ABS  gp, 1f
    j    entry
1:  lw   t1, OFS_TIMER_CNT(tp)
    lw   ra, 0(sp)
    addi sp, sp, 4
    ret

bad:
    FAIL 0x16

# ---- the steps: every 8 bytes from `steps` to `steps_end` is one, with one step_tab entry -------
    .pushsection .rodata.steps, "a"
    .align 2
    .word steps, steps_end - steps
step_tab:
    .popsection
    .align 3
steps:
entry:                                    # MIE = 0; a0, a1 = 2, a3, a4, s0, gp from the caller
    STEPOFF addi t2, zero, 0
    STEPG K_DET | NON, 0, 0, 0, csrrsi t2, mstatus, 8      # MIE = 1
body:
    STEP  sw   a3, OFS_TIMER_CMP(tp)      # this iteration's interrupt: CMP = a3, then CNT = 0
    STEP  sw   zero, OFS_TIMER_CNT(tp)
    STEP  sub  a3, a3, a4                 # the next iteration arms Q ticks earlier
    STEP  addi t2, zero, 0
    STEPG K_DET, 0, C_IOLD, 0, lw t2, OFS_TIMER_CTRL(tp)   # an IO load (EN = 1: never 0)
    STEP  sw   zero, 4(s0)
    STEP  addi t2, zero, 0
    STEPG K_STDET, 0, C_ST, 0, sw s1, 4(s0)          # a marker pc: never 0
    STEPG K_DET, 0, C_LD, 0, lw t2, 4(s0)            # the load right behind the store
    STEP  add  a0, a0, t2
    STEPG K_ALW, 1f, C_BEQ, 0, beq zero, zero, 1f    # taken over one step
    STEP  addi a0, a0, 0x7ff              # never runs
1:  STEPG K_SEQ | NOE, 0, 0, 0, csrrci zero, mstatus, 8   # MIE = 0 ...
    STEPOFF addi t2, zero, 0
    STEPG K_DET | NON, 0, C_EN, 0, csrrsi t2, mstatus, 8  # ... MIE = 1: pending interrupts land here
    STEPG K_SEQ, 0, 0, C_EN, xor a0, a0, t2          # (or one instruction later); t2 = 0x1880
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
    STEPG K_DET, 0, C_CSRW, 0, csrrw t2, mscratch, s1  # the old value is a marker pc: never 0
    STEPG K_SEQ | NOE, 0, 0, 0, csrrci zero, mstatus, 8   # MIE = 0 until the MRET
    STEPOFF lui  t0, %hi(2f)
    STEPOFF addi t0, t0, %lo(2f)
    STEPOFF csrrw zero, mepc, t0
    STEPG K_MRET | NON, 2f, C_MRET, 0, mret     # MIE = MPIE = 1 at 2f: pending interrupts land there
    STEP  addi a0, a0, 1                  # never runs
2:  STEPG K_SEQ, 0, 0, C_MRET, addi a1, a1, -1    # (or one instruction later)
    STEPG K_NZ_A1, body, C_LOOP, 0, bne a1, zero, body
    STEPG K_SEQ | NOE, 0, 0, 0, csrrci zero, mstatus, 8   # MIE = 0 for the caller
    STEPOFF jalr zero, 0(gp)

wfi_blk:                                  # MIE = 0; a3 = the delay, a5 = 0, a1 = the poll bound
    STEPOFF lw   t0, OFS_TIMER_CNT(tp)
    STEPOFF add  t0, t0, a3
    STEPOFF sw   t0, OFS_TIMER_CMP(tp)
    STEPOFF addi t2, zero, 0
    STEPG K_DET | NON, 0, 0, 0, csrrsi t2, mstatus, 8      # MIE = 1
    STEP  wfi                             # the interrupt comes during the stall
wfi_poll:                                 # right after the WFI
6:  STEPG K_NZ_A5, 7f, 0, 0, bne a5, zero, 7f        # the handler ran (WFI may be a no-op)
    STEP  addi a1, a1, -1
    STEPG K_NZ_A1, 6b, 0, 0, bne a1, zero, 6b
    STEP  jal  zero, bad                  # no interrupt came
7:  STEPG K_SEQ | NOE, 0, 0, 0, csrrci zero, mstatus, 8   # MIE = 0
    STEPOFF jalr zero, 0(gp)
force_blk:                                # MIE = 0: CMP = CNT = 0 matches within a tick or two,
    STEPOFF sw   zero, OFS_TIMER_CMP(tp)  # so the interrupt is pending when the CSRRSI and later
    STEPOFF sw   zero, OFS_TIMER_CNT(tp)  # the MRET turn MIE on: it lands right after them or
    STEPOFF addi t2, zero, 0              # one instruction later (contract 4), never later
    STEPOFF addi t2, zero, 0
    STEPOFF addi t2, zero, 0
    STEPG K_DET | NON, 0, 0, 0, csrrsi t2, mstatus, 8
force_en:                                 # right after the CSRRSI
    STEPG K_SEQ | NOE, 0, 0, 0, addi t2, zero, 0
    STEPG K_SEQ | NOE, 0, 0, 0, csrrci zero, mstatus, 8   # MIE = 0
    STEPOFF sw   zero, OFS_TIMER_CMP(tp)
    STEPOFF sw   zero, OFS_TIMER_CNT(tp)
    STEPOFF lui  t0, %hi(8f)
    STEPOFF addi t0, t0, %lo(8f)
    STEPOFF csrrw zero, mepc, t0
    STEPG K_MRET | NON, 8f, 0, 0, mret
    STEP  addi a0, a0, 1                  # never runs
force_mret:                               # right after the MRET (its target)
8:  STEPG K_SEQ | NOE, 0, 0, 0, addi t2, zero, 0
    STEPG K_SEQ | NOE, 0, 0, 0, csrrci zero, mstatus, 8   # MIE = 0
    STEPOFF jalr zero, 0(gp)
steps_end:

    .section .rodata
    .align 2
cov_step:                        # per coverage point: its step's byte offset / 2 (off_tab index)
    .word (covstep_C_ST - steps) / 2, (covstep_C_LD - steps) / 2, (covstep_C_BEQ - steps) / 2
    .word (covstep_C_EN - steps) / 2, (covstep_C_JAL - steps) / 2, (covstep_C_INNER - steps) / 2
    .word (covstep_C_JALR - steps) / 2, (covstep_C_CSRW - steps) / 2
    .word (covstep_C_MRET - steps) / 2, (covstep_C_LOOP - steps) / 2, (covstep_C_IOLD - steps) / 2
    .text

# ---- the handler -------------------------------------------------------------------------------
    .align 2
irq_handler:                     # t1 is free: no step uses it
    addi sp, sp, -16
    sw   t0, 0(sp)
    sw   t2, 4(sp)
    sw   a4, 8(sp)
    sw   gp, 12(sp)
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
    ABS  gp, step_tab
    lw   t1, -8(gp)              # steps
    sub  t2, s1, t1              # s1 must be a step marker: steps <= s1 < steps_end, 8-aligned
    lw   t0, -4(gp)              # steps_end - steps
    bgeu t2, t0, 9f
    andi t0, t2, 7
    bnez t0, 9f
    add  gp, gp, t2              # the step's entry
    srli t0, t2, 1               # off_tab[step] = this iteration's arm offset + 1 (0: none
    add  t1, s0, t0              # yet); off_tab follows buf (s0)
    add  t0, a3, a4
    addi t0, t0, 1
    sw   t0, OFF_TAB(t1)
    lw   t0, 4(gp)               # kind word: kind | flags | coverage points
    lw   gp, 0(gp)               # target
    csrr t1, mepc
    andi a4, t0, 0xff
    beqz a4, 10f                 # K_SEQ, the most common
    addi t2, a4, -K_NZ_A1        # conditional branches: taken from the live register
    bltz t2, 4f
    beqz t2, 2f
    addi t2, t2, -1
    beqz t2, 3f
    mv   t2, a5                  # K_NZ_A5
    j    1f
2:  mv   t2, a1                  # K_NZ_A1
    j    1f
3:  mv   t2, a2                  # K_NZ_A2
1:  li   a4, K_ALW
    bnez t2, 4f                  # taken: like K_ALW
    lui  t2, 0xffff0             # not taken: like K_SEQ, with no coverage point after it
    addi t2, t2, 0xff
    and  t0, t0, t2
10: addi gp, s1, 8               # K_SEQ: s1 + 4 or E = s1 + 8
    j    20f
4:  li   t2, K_ALW
    beq  a4, t2, 20f             # s1 + 4 or the target
    li   t2, K_MRET
    beq  a4, t2, 21f             # only the target
    li   t2, K_LINK
    bne  a4, t2, 5f
    addi t2, s1, 8
    beq  ra, t2, 21f             # ra = the link: it executed, so the target
    j    22f                     # not executed: s1 + 4
5:  addi gp, s1, 8               # K_DET, K_STDET: E = s1 + 8
    li   t2, K_STDET
    beq  a4, t2, 6f
    lw   t2, 4(sp)               # K_DET: the interrupted t2, never 0 once the step executed
    j    7f
6:  lw   t2, 4(s0)               # K_STDET: the stored word, 0 until the store executed
7:  bnez t2, 21f
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
23: srli t2, t0, 25              # landed before the step's instruction: not where MIE = 0
    andi t2, t2, 1
    bnez t2, 8f
    srli t0, t0, 16
    j    25f
24: srli t2, t0, 24              # landed after it: not where MIE = 0
    andi t2, t2, 1
    bnez t2, 8f
    srli t0, t0, 8
25: andi t0, t0, 0xff            # the coverage point, if any
    beqz t0, 26f
    addi t0, t0, -1
    li   t2, 1
    sll  t2, t2, t0
    ABS  t1, cov
    lw   t0, 0(t1)
    or   t0, t0, t2
    sw   t0, 0(t1)
26: addi a5, a5, 1               # the interrupt count (a5 belongs to the handler); the
    addi t0, a5, -STORM          # caller clears it before each call: more than STORM in one
    bgez t0, 27f                 # call is a livelock (an interrupt that re-arms itself)
    lw   t0, 0(sp)
    lw   t2, 4(sp)
    lw   a4, 8(sp)
    lw   gp, 12(sp)
    addi sp, sp, 16
    mret
27: FAIL 0x28                    # an interrupt storm
8:  FAIL 0x27                    # landed where MIE = 0 (inside a window, after CSRRCI, before
                                 # the CSRRSI or MRET that turns it on)
9:  FAIL 0x26                    # s1 is not a step marker

    .section .bss
    .align 2
buf:
    .space 16
off_tab:                         # = buf + OFF_TAB
    .space 4 * 128
ref:
    .space 4
calls:
    .space 4
voff:
    .space 4
swept:
    .space 4
lmax:
    .space 4
cal_left:
    .space 4
aimed:
    .space 4
cov:
    .space 4
qtab:
    .space 4 * (VDC_BITS + 1)
