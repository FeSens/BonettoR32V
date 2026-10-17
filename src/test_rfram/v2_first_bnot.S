# Directed test (hypothesis 005): the first instruction after reset is a branch not taken.
    .section .text.start, "ax"
    .global _start
_start:
    bne  x0, x0, 1f
    auipc a0, 0
    li   a1, 0x9000
    sw   a0, 0(a1)
1:  ebreak
