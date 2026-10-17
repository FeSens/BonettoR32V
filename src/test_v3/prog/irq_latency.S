# irq_latency: the worst retirement gaps. A timer compare interrupt is pending all the time
# (P = 0, CMP = 0, auto-reload), so the core enters the handler at nearly every boundary where
# MIE = 1 (MRET's retirement included). The handler's first instruction is a CSR instruction
# (csrrw sp, mscratch, sp: the longest sequence), so the gap from the interrupted retirement
# covers interrupt entry plus the longest instruction. The main loop runs shifts by 31 (the
# longest pre-roll), SW and LW. (A JALR trap as a handler's first instruction would re-enter
# itself forever; its sequence is one slot shorter than the CSR one.)
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
    li    x2, 0x017FFF00          # sp
    li    x3, 0x017FF000          # the handler's stack, swapped in through mscratch
    csrw  mscratch, x3
    la    x4, handler1
    csrw  mtvec, x4
    lui   x14, 0x80000            # IO
    sw    x0, 16(x14)             # TIMER_CMP = 0
    sw    x0, 12(x14)             # TIMER_CNT = 0
    li    x5, 0x30                # enable, auto-reload, P = 0
    sw    x5, 24(x14)
    li    x5, 1
    sw    x5, 60(x14)             # IRQ_EN = CMP
    li    x5, 0x800
    csrs  mie, x5
    li    x8, 0                   # handler1 count
    li    x10, 0x12345678
    li    x11, 0x01000040
    csrsi mstatus, 8
loop1:
    slli  x12, x10, 31
    srai  x12, x10, 31
    sw    x10, 0(x11)
    lw    x13, 0(x11)
    bne   x13, x10, fail
    li    x5, 6
    blt   x8, x5, loop1
    csrci mstatus, 8
    sw    x0, 24(x14)             # timer off
    sw    x0, 60(x14)
    li    x5, 1
    sw    x5, 64(x14)             # clear IRQ_PEND.CMP
    li    x13, 0x5a
    j     done
fail:
    li    x13, 0xee
done:
    lui   x14, 0x80000
    sw    x0, 8(x14)
    sw    x13, 0(x14)
1:  jal   x0, 1b

    .align 2
handler1:
    csrrw x2, mscratch, x2        # first instruction: the longest sequence
    addi  x8, x8, 1
    sw    x7, 0(x2)
    li    x1, 6
    blt   x8, x1, 2f
    sw    x0, 60(x14)             # IRQ_EN = 0 after 6 entries
2:  csrrw x2, mscratch, x2
    mret
