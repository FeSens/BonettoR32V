# Interrupt stress, contract v3 section 4 (Review Focus 2): an interrupt lands mid-instruction,
# right after an MRET that re-enables interrupts, after taken branches, loads, stores and CSR
# toggles; mepc must be the next instruction not executed.
#
# The harness toggles ui4 and ui5 together every IS_GAP_MIN..IS_GAP_MAX (37-91) cycles
# (expect.json gpio_script: densely at first, then in bursts); GPIO_EDGE flags ui4 rising and
# ui5 falling, so every toggle is one GPIO edge interrupt.
#
# The main loop is a sequence of steps. Each step is `auipc s1, 0` followed by one instruction,
# so s1 always holds the pc of the last step marker executed: each instruction of the loop
# records its pc before it runs. The handler checks that mepc is an instruction the loop has
# not executed yet: s1 + 4 (the step's instruction), s1 + 8 (the next step), or the step's
# control-flow target from the step_extra table. It also checks mcause and the source, clears
# the flags (W1C), counts, and records mepc in a ring. Every second entry returns with MIE = 0
# (it clears MPIE), so the loop always makes progress; the loop turns MIE back on in several
# steps (csrsi, csrrsi, and an MRET with MPIE = 1).
#
#   pass 1: no source enabled: the reference checksum (a0) and buffer contents;
#   pass 2: the GPIO edge interrupt enabled: the same loop must give the same checksum and
#           buffer, and at least one interrupt must have been taken;
#   then:   WFI with MIE = 0 wakes on a rising edge of ui6 (IS_PIN_WFI, pulsed every
#           IS_WFI_PERIOD) and nothing is taken.
#
# Registers: s1 progress marker, s0 data buffer, a0 checksum, a1 iterations, gp the loop's return
# address, tp IO base, a2, t0..t2 and ra scratch for the steps. The handler saves what it uses.
# Linked with crt0.S. Failure ids: 0x10-0x16 main, 0x20-0x23 handler.
# Its outcome depends on interrupt timing: expect.json is declared (ruling R13).
#include "bonetto.h"
#include "stim.h"

    .option norelax              # every step must stay exactly 8 bytes

    .set PINS, (1 << IS_PIN_RISE) | (1 << IS_PIN_FALL)
    .set POLL_MAX, 1 << 22

    .macro ABS rd, sym
    lui  \rd, %hi(\sym)
    addi \rd, \rd, %lo(\sym)
    .endm

    .macro FAIL id
    li   a0, \id
    j    _fail
    .endm

    # a step: the marker and one instruction (never a pseudo-instruction that expands)
    .macro STEP insn:vararg
    auipc s1, 0
    \insn
    .pushsection .rodata.steps, "a"
    .word 0
    .popsection
    .endm

    # a control-flow step: its target is also a valid mepc after it
    .macro STEPX target, insn:vararg
    auipc s1, 0
    \insn
    .pushsection .rodata.steps, "a"
    .word \target
    .popsection
    .endm

    .text
    .global main
main:
    addi sp, sp, -4
    sw   ra, 0(sp)
    lui  tp, 0x80000
    ABS  t0, irq_handler
    csrw mtvec, t0
    ABS  s0, buf

# pass 1: no interrupt source
    sw   zero, OFS_IRQ_EN(tp)
    call init_buf
    li   a0, 0x1234
    li   a1, IS_ITERS
    ABS  gp, 1f
    j    body
1:  ABS  t0, ref
    sw   a0, 0(t0)
    lw   t1, 0(s0)
    sw   t1, 4(t0)
    lw   t1, 4(s0)
    sw   t1, 8(t0)
    lw   t1, 8(s0)
    sw   t1, 12(t0)
    lw   t1, 12(s0)
    sw   t1, 16(t0)

# pass 2: the GPIO edge interrupt
    li   t0, EDGE_EN(PINS) | EDGE_FALL(1 << IS_PIN_FALL)
    sw   t0, OFS_GPIO_EDGE(tp)
    li   t0, 0xff
    sw   t0, OFS_GPIO_EPEND(tp)
    li   t0, IRQ_GPIO
    sw   t0, OFS_IRQ_EN(tp)
    li   t0, MIE_MEIE
    csrs mie, t0
    call init_buf
    li   a0, 0x1234
    li   a1, IS_ITERS
    ABS  gp, 2f
    j    body
2:  ABS  t0, ref
    lw   t1, 0(t0)
    beq  a0, t1, 3f
    FAIL 0x10
3:  li   a2, 0
4:  add  t1, s0, a2
    lw   t1, 0(t1)
    add  t2, t0, a2
    lw   t2, 4(t2)
    beq  t1, t2, 5f
    FAIL 0x11
5:  addi a2, a2, 4
    li   t1, 16
    bltu a2, t1, 4b
    ABS  t0, irq_count
    lw   t0, 0(t0)
    bnez t0, 6f
    FAIL 0x12                    # the stimulus never interrupted pass 2

# WFI with MIE = 0: wakes on a rising edge of ui6, nothing is taken
6:  li   t0, EDGE_EN(1 << IS_PIN_WFI)
    sw   t0, OFS_GPIO_EDGE(tp)
    li   t0, 0xff
    sw   t0, OFS_GPIO_EPEND(tp)
    ABS  a2, irq_count
    lw   a3, 0(a2)
    wfi
    li   t1, POLL_MAX
7:  lw   t0, OFS_GPIO_EPEND(tp)
    andi t0, t0, 1 << IS_PIN_WFI
    bnez t0, 8f
    addi t1, t1, -1
    bnez t1, 7b
    FAIL 0x13
8:  lw   t0, 0(a2)
    beq  t0, a3, 9f
    FAIL 0x14                    # an interrupt with MIE = 0
9:  csrr t0, mip
    li   t1, MIP_MEIP
    beq  t0, t1, 10f
    FAIL 0x15
10: sw   zero, OFS_IRQ_EN(tp)
    sw   zero, OFS_GPIO_EDGE(tp)
    li   t0, 0xff
    sw   t0, OFS_GPIO_EPEND(tp)
    csrw mie, zero
    lw   ra, 0(sp)
    addi sp, sp, 4
    li   a0, 0
    ret

init_buf:
    li   t0, 0x0badf00d
    sw   t0, 0(s0)
    li   t0, 0x13579bdf
    sw   t0, 4(s0)
    li   t0, 0x2468ace0
    sw   t0, 8(s0)
    li   t0, 0x7f6e5d4c
    sw   t0, 12(s0)
    ret

# ---- the main loop: steps of 8 bytes; step_extra holds one word per step -------------------
    .pushsection .rodata.steps, "a"
    .align 2
step_extra:
    .popsection
    .align 2
body:
    STEP  csrsi mstatus, 8               # MIE = 1
    STEP  addi a0, a0, 0x123
    STEP  lw   t0, 0(s0)
    STEP  add  a0, a0, t0
    STEP  sw   a0, 4(s0)
    STEP  lw   t1, 4(s0)                 # the load right behind the store
    STEPX bad, bne t1, a0, bad           # not taken
    STEPX 1f, beq zero, zero, 1f         # taken over one step
    STEP  addi a0, a0, 0x7ff             # never runs
1:  STEP  xori a0, a0, 0x55
    STEP  csrci mstatus, 8               # MIE = 0
    STEP  slli t2, a0, 3
    STEP  csrsi mstatus, 8               # MIE = 1
    STEP  sub  a0, a0, t2
    STEP  sh   a0, 8(s0)
    STEP  lhu  t0, 8(s0)
    STEP  lb   t1, 9(s0)
    STEP  add  a0, a0, t0
    STEP  xor  a0, a0, t1
    STEP  csrci mstatus, 8               # an MRET to 2f, with MIE = 0 until then
    STEP  lui  t0, %hi(2f)
    STEP  addi t0, t0, %lo(2f)
    STEP  csrw mepc, t0
    STEP  addi t1, zero, MSTATUS_MPIE
    STEP  csrs mstatus, t1               # MPIE = 1
    STEPX 2f, mret                       # MIE = 1 at 2f
    STEP  addi a0, a0, 1                 # never runs
2:  STEP  addi a0, a0, 3
    STEPX 3f, jal ra, 3f
    STEP  addi a0, a0, 5                 # never runs
3:  STEP  add  a0, a0, ra                # ra = the skipped step's marker
    STEP  addi a2, zero, 3               # an inner loop of 3
4:  STEP  addi a0, a0, 7
    STEP  addi a2, a2, -1
    STEPX 4b, bne a2, zero, 4b           # taken backward twice
    STEP  sw   a0, 12(s0)
    STEP  lbu  t0, 13(s0)
    STEP  csrrsi zero, mstatus, 8        # MIE = 1
    STEP  add  a0, a0, t0
    STEP  csrrw zero, mscratch, a0       # unrelated CSR traffic
    STEP  csrr t1, mscratch
    STEPX bad, bne t1, a0, bad           # not taken
    STEP  addi a1, a1, -1
    STEPX body, bne a1, zero, body       # the next iteration
body_end:
    csrci mstatus, 8
    jalr x0, 0(gp)

bad:
    FAIL 0x16

# ---- the handler -------------------------------------------------------------------------------
    .align 2
irq_handler:
    addi sp, sp, -20
    sw   t0, 0(sp)
    sw   t1, 4(sp)
    sw   t2, 8(sp)
    sw   a4, 12(sp)
    sw   a5, 16(sp)
    csrr t0, mcause
    li   t1, MCAUSE_EXT_IRQ
    beq  t0, t1, 1f
    FAIL 0x20
1:  lw   t0, OFS_IRQ_PEND(tp)
    andi t0, t0, IRQ_GPIO
    bnez t0, 2f
    FAIL 0x21
2:  lw   t0, OFS_GPIO_EPEND(tp)
    sw   t0, OFS_GPIO_EPEND(tp)      # W1C the flags seen
    csrr t0, mepc
    ABS  t1, body                    # s1 must be a step marker of the loop
    bltu s1, t1, 4f
    ABS  t2, body_end
    bgeu s1, t2, 4f
    sub  t2, s1, t1
    andi a4, t2, 7
    bnez a4, 4f
    addi a4, s1, 4                   # mepc: the step's instruction ...
    beq  t0, a4, 5f
    addi a4, s1, 8                   # ... the next step ...
    beq  t0, a4, 5f
    srli t2, t2, 1                   # ... or the step's control-flow target
    ABS  a4, step_extra
    add  a4, a4, t2
    lw   a4, 0(a4)
    beq  t0, a4, 5f
    FAIL 0x22
4:  FAIL 0x23
5:  ABS  t1, irq_count
    lw   t2, 0(t1)
    addi t2, t2, 1
    sw   t2, 0(t1)
    andi a5, t2, 63                  # the mepc ring (64 entries)
    slli a5, a5, 2
    ABS  a4, mepc_log
    add  a4, a4, a5
    sw   t0, 0(a4)
    andi t2, t2, 1                   # every second entry returns with MIE = 0
    beqz t2, 6f
    li   t1, MSTATUS_MPIE
    csrc mstatus, t1
6:  lw   t0, 0(sp)
    lw   t1, 4(sp)
    lw   t2, 8(sp)
    lw   a4, 12(sp)
    lw   a5, 16(sp)
    addi sp, sp, 20
    mret

    .section .bss
    .align 2
buf:
    .space 16
ref:
    .space 20
irq_count:
    .space 4
mepc_log:
    .space 4 * 64
