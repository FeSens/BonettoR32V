# Directed test (hypothesis 005): the first instruction after reset is AUIPC (pc = 0 in the
# buffer), then a JAL with rd != 0.
    .section .text.start, "ax"
    .global _start
_start:
    auipc a0, 0x12345
    li   a1, 0x9000
    sw   a0, 0(a1)
    jal  a2, 1f
    ebreak                     # never runs
1:  sw   a2, 4(a1)
    ebreak
