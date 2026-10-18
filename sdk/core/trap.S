# BonettoR32V SDK trap entry (mtvec = r32v_trap, set by crt0.S; contract section 4).
#
# An instruction takes about 110 cycles on this core, and more for loads and stores to PSRAM,
# so the C handler (r32v_trap_c in r32v_core.c, about 95 instructions with 12 registers saved
# on the stack) is too slow for two cases, which this file takes first:
#   1. UART receive. The UART holds one byte; at 115,200 baud the next one arrives 4,340 cycles
#      later, so the byte must be read within a few dozen instructions. This path reads it at its
#      sixth instruction and puts it in the Serial ring buffer (struct r32v_rx).
#   2. A tone on UO7 interrupts at every half period. While r32v_tf.on is set, this path does
#      the work when the period that just ended neither completes a 20 ms timebase unit nor is
#      the last one of a timed tone: acc += cur, the countdown, the output level, the flag
#      cleared (struct r32v_tf).
# Everything else jumps to r32v_trap_c, which saves what it needs and returns with mret itself.
#
# Registers: no stack. mscratch holds a5, and gp (x3) and tp (x4) are scratch: the SDK reserves
# them for this handler (GCC never allocates them, and the link defines no __global_pointer$, so
# the linker never makes gp-relative accesses; sdk/tests checks the images). Sketches must not
# use gp or tp in assembly.
#
# An exception (ECALL, EBREAK, illegal) while a byte waits or the tone path is on: this path
# handles the interrupt work and mret returns to mepc, the faulting instruction, which traps again
# and reaches r32v_trap_c. So the paths don't need to read mcause.
#
# IO: 0x8000_0000 + 4 * slot: UART_DATA slot 11 (44), UART_CTRL slot 12 (48, [14] RX_FULL,
# [15] RX_OVR), IRQ_PEND slot 16 (64, [0] the compare flag, W1C).
# struct r32v_rx (r32v_core.c): 0 head, 1 tail, 2 cbdue (bytes), 4 lost, 8 cb, 12 buf[64].
# struct r32v_tf (r32v_core.c): 0 on, 4 acc, 8 cur, 12 left, 16 hi, 20 unit.

    .section .text.r32v_trap, "ax"
    .global r32v_trap
    .type r32v_trap, @function
    .balign 4
r32v_trap:
    csrw mscratch, a5
    lui  a5, 0x80000
    lw   tp, 48(a5)             # UART_CTRL
    srli tp, tp, 14             # 1: a byte waits; 3: and the UART dropped one before it
    bnez tp, .Lrx
    lw   tp, 64(a5)             # IRQ_PEND
    andi tp, tp, 1
    beqz tp, .Lc                # not the compare flag
    lui  a5, %hi(r32v_tf)
    addi a5, a5, %lo(r32v_tf)
    lw   tp, 0(a5)              # on
    beqz tp, .Lc
    lw   tp, 4(a5)              # acc + cur
    lw   gp, 8(a5)
    add  tp, tp, gp
    lw   gp, 20(a5)
    bgeu tp, gp, .Lc            # a 20 ms unit ends: the C path adds it to the time
    lw   gp, 12(a5)             # left (0: an untimed tone)
    beqz gp, 1f
    addi gp, gp, -1
    beqz gp, .Lc                # the count is done: the C path stops the tone
    sw   gp, 12(a5)
1:  sw   tp, 4(a5)
    lw   tp, 16(a5)             # the tone output toggled
    xori tp, tp, 1
    sw   tp, 16(a5)
    lui  a5, 0x80000
    li   tp, 1
    sw   tp, 64(a5)             # IRQ_PEND = CMP: clear the flag
    csrr a5, mscratch
    mret
.Lc:
    csrr a5, mscratch
    j    r32v_trap_c

.Lrx:
    lbu  gp, 44(a5)             # the byte; reading it clears RX_FULL and RX_OVR
    lui  a5, %hi(r32v_rx)
    addi a5, a5, %lo(r32v_rx)
    addi tp, tp, -1
    beqz tp, 2f
    lw   tp, 4(a5)              # the UART overran: lost++
    addi tp, tp, 1
    sw   tp, 4(a5)
2:  lbu  tp, 0(a5)              # head: its slot is always free (the ring keeps one empty)
    add  tp, tp, a5
    sb   gp, 12(tp)             # buf[head] = byte
    sub  tp, tp, a5
    addi tp, tp, 1
    andi tp, tp, 63             # next head (64 slots)
    lbu  gp, 1(a5)              # tail
    beq  tp, gp, 3f             # full: the byte is dropped
    sb   tp, 0(a5)
    j    4f
3:  lw   tp, 4(a5)
    addi tp, tp, 1
    sw   tp, 4(a5)
4:  lw   tp, 8(a5)              # an INT_SERIAL callback: r32v_trap_c calls it
    beqz tp, 5f
    li   tp, 1
    sb   tp, 2(a5)
    csrr a5, mscratch
    j    r32v_trap_c
5:  csrr a5, mscratch
    mret
    .size r32v_trap, . - r32v_trap
