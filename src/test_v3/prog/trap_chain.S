# trap_chain: rvfi_intr on handler entry (contract 8: rvfi_intr = 1 on the first instruction of
# a trap handler, whether entered by a trap or an interrupt). Seven synchronous traps back to
# back (ecall, ebreak, 0x00000000, 0xFFFFFFFF, jal / jalr / taken branch to a misaligned
# target), then one timer interrupt. The handler records each cause and resumes after the
# trapping instruction (an interrupt resumes at mepc). Run with --expect-entries 8: exactly the
# 8 handler-entry retirements carry rvfi_intr = 1, and the ISS lockstep checks which ones.
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
    li    x8, 0                 # handler entries
    li    x9, 0                 # synchronous causes, a nibble each
    li    x10, 0
    li    x11, 0
    li    x12, 0
    li    x13, 0
    li    x14, 0
    li    x15, 0
    la    x2, handler
    csrw  mtvec, x2
    la    x5, target            # 4-byte aligned
    li    x6, 0x77
    li    x1, 0x55
    .align 2
    ecall
    ebreak
    .word 0x00000000
    .word 0xffffffff
    .word 0x006000ef            # jal x1, pc+6
    .word 0x003280e7            # jalr x1, 3(x5): target + 2
    .word 0x00630363            # beq x6, x6, pc+6 (taken)
    EXPECT(x8, 7)
    EXPECT(x9, 0x0B322000)      # causes 11, 3, 2, 2, 0, 0, 0
    EXPECT(x1, 0x55)            # the trapping JAL/JALR wrote nothing
    # one interrupt: timer compare pending at once (P = 0, CMP = 0), enabled by MIE
    lui   x14, 0x80000
    sw    x0, 16(x14)           # TIMER_CMP = 0
    sw    x0, 12(x14)           # TIMER_CNT = 0
    li    x3, 0x30              # enable, auto-reload, P = 0
    sw    x3, 24(x14)
    li    x3, 1
    sw    x3, 60(x14)           # IRQ_EN = CMP
    li    x3, 0x800
    csrs  mie, x3
    csrsi mstatus, 8            # taken at this boundary
    nop
    nop
    csrci mstatus, 8
    EXPECT(x8, 8)
    EXPECT(x9, 0x0B322000)      # the interrupt recorded no synchronous cause
    sw    x0, 24(x14)           # timer off
    li    x3, 1
    sw    x3, 64(x14)           # clear IRQ_PEND.CMP
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
target:
    j     fail
    nop
    j     fail

    .align 2
handler:
    addi  x8, x8, 1             # the first instruction: rvfi_intr = 1
    csrr  x2, mcause
    blt   x2, x0, h_irq
    slli  x9, x9, 4
    or    x9, x9, x2
    csrr  x2, mepc
    addi  x2, x2, 4
    csrw  mepc, x2
    mret
h_irq:
    sw    x0, 60(x14)           # IRQ_EN = 0: the line drops
    mret
