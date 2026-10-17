# csr: every contract-4 CSR field, all six CSR instructions, rd = x0, rs1 = x0 / zimm = 0,
# rd == rs1, read-only and hard-wired bits. Verdict on uo_out.
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
    lui   x14, 0x80000          # IO base
    la    x1, trap_fail
    csrw  mtvec, x1
    # mstatus: MIE resets to 0, MPP reads 3, MPIE resets arbitrary
    csrr  x2, mstatus
    li    x3, 0xFFFFFF7F
    and   x2, x2, x3
    EXPECT(x2, 0x1800)
    li    x3, -1
    csrw  mstatus, x3           # MIE = MPIE = 1 (mie.MEIE = 0: no interrupt)
    csrr  x2, mstatus
    EXPECT(x2, 0x1888)
    csrw  mstatus, x0
    csrr  x2, mstatus
    EXPECT(x2, 0x1800)
    li    x3, 8
    csrrs x2, mstatus, x3       # old value, then MIE set
    EXPECT(x2, 0x1800)
    csrrc x2, mstatus, x3
    EXPECT(x2, 0x1808)
    csrrsi x2, mstatus, 8
    EXPECT(x2, 0x1800)
    csrrci x2, mstatus, 8
    EXPECT(x2, 0x1808)
    csrrwi x2, mstatus, 0
    EXPECT(x2, 0x1800)
    li    x3, 0x80
    csrrw x2, mstatus, x3       # MPIE only
    csrr  x2, mstatus
    EXPECT(x2, 0x1880)
    csrw  mstatus, x0
    # misa reads 0 and ignores writes
    csrr  x2, misa
    EXPECT(x2, 0)
    li    x3, -1
    csrw  misa, x3
    csrr  x2, misa
    EXPECT(x2, 0)
    # mie: MEIE only
    csrr  x2, mie
    EXPECT(x2, 0)
    csrw  mie, x3
    csrr  x2, mie
    EXPECT(x2, 0x800)
    csrrc x2, mie, x3
    EXPECT(x2, 0x800)
    csrr  x2, mie
    EXPECT(x2, 0)
    # mtvec: bits 1:0 read 0
    csrrw x2, mtvec, x3
    la    x4, trap_fail
    bne   x2, x4, fail
    csrr  x2, mtvec
    EXPECT(x2, 0xFFFFFFFC)
    csrw  mtvec, x4
    # mscratch: 32 bits; rd == rs1 swaps
    li    x5, 0xA5A55A5A
    csrw  mscratch, x5
    csrr  x2, mscratch
    bne   x2, x5, fail
    li    x6, 0x12345678
    csrrw x6, mscratch, x6
    bne   x6, x5, fail
    csrr  x2, mscratch
    EXPECT(x2, 0x12345678)
    csrrs x6, mscratch, x6      # rd == rs1: old 0x12345678, new | old
    EXPECT(x6, 0x12345678)
    csrr  x2, mscratch
    li    x15, 0x12345678 | 0xA5A55A5A
    bne   x2, x15, fail
    li    x7, 0x0F0F0F0F
    csrrc x7, mscratch, x7      # rd == rs1, clear
    li    x15, 0x12345678 | 0xA5A55A5A
    bne   x7, x15, fail
    csrr  x2, mscratch
    li    x15, (0x12345678 | 0xA5A55A5A) & ~0x0F0F0F0F
    bne   x2, x15, fail
    csrrsi x2, mscratch, 0      # zimm = 0: no write
    csrrs  x8, mscratch, x0     # rs1 = x0: no write
    bne   x2, x8, fail
    csrrwi x2, mscratch, 21
    csrr  x2, mscratch
    EXPECT(x2, 21)
    csrrsi x2, mscratch, 10
    EXPECT(x2, 21)
    csrr  x2, mscratch
    EXPECT(x2, 31)
    csrrci x2, mscratch, 17
    csrr  x2, mscratch
    EXPECT(x2, 14)
    csrrw x0, mscratch, x5      # rd = x0
    csrr  x2, mscratch
    bne   x2, x5, fail
    # mepc: bits 1:0 read 0
    csrw  mepc, x3
    csrr  x2, mepc
    EXPECT(x2, 0xFFFFFFFC)
    li    x9, 0x01234567
    csrw  mepc, x9
    csrr  x2, mepc
    EXPECT(x2, 0x01234564)
    # mcause: bit 31 and bits 3:0
    csrw  mcause, x3
    csrr  x2, mcause
    EXPECT(x2, 0x8000000F)
    li    x9, 0x7FFFFFF5
    csrw  mcause, x9
    csrr  x2, mcause
    EXPECT(x2, 5)
    # mtval reads 0, ignores writes
    csrw  mtval, x3
    csrr  x2, mtval
    EXPECT(x2, 0)
    # mip: MEIP follows the interrupt line, read-only
    sw    x0, 60(x14)           # IRQ_EN = 0
    csrr  x2, mip
    EXPECT(x2, 0)
    li    x10, 8
    sw    x10, 60(x14)          # IRQ_EN = TXIDLE (the UART transmitter is idle)
    csrr  x2, mip
    EXPECT(x2, 0x800)
    csrw  mip, x0
    csrr  x2, mip
    EXPECT(x2, 0x800)
    sw    x0, 60(x14)
    csrr  x2, mip
    EXPECT(x2, 0)
    # the id CSRs read 0
    csrr  x2, mvendorid
    bnez  x2, fail
    csrr  x2, marchid
    bnez  x2, fail
    csrr  x2, mimpid
    bnez  x2, fail
    csrr  x2, mhartid
    bnez  x2, fail
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
trap_fail:
    j     fail
