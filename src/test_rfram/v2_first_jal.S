# Directed test (hypothesis 005): the first instruction after reset is JAL with rd != 0.
    .section .text.start, "ax"
    .global _start
_start:
    jal  ra, 1f
    ebreak                     # never runs
1:  li   a1, 0x9000
    sw   ra, 0(a1)
    ebreak
