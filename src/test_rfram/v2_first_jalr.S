# Variant 2 directed test (hypothesis 002): the first instruction after reset is a JALR with
# rd != 0 (seven slots: pc + 4 through the scratch word), so the boot slot plus that JALR must fit
# the 1,024-cycle cap. Then JALR with rd == rs1, JAL and branches back to back.
    .section .text.start, "ax"
    .global _start
_start:
    jalr ra, 8(x0)             # to 8, ra = 4
    ebreak                     # never runs
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
    li   a0, 0x9000
    sw   ra, 0(a0)
    la   x5, 1f
    jalr x5, 0(x5)             # rd == rs1
1:  sw   x5, 4(a0)
    la   x6, 2f
    jalr x0, 0(x6)
    ebreak
2:  jal  x7, 3f
    ebreak
3:  sw   x7, 8(a0)
    li   x8, 5
4:  addi x8, x8, -1
    bnez x8, 4b
    beq  x8, x0, 5f
    ebreak
5:  blt  x8, x7, 6f
    ebreak
6:  sw   x8, 12(a0)
    ebreak
