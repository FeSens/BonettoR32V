# Directed test (hypothesis 005): the first instruction after reset is a taken branch, so its pc
# read (the PX slot) must see pc = 0 although nothing has written the pc slot yet.
    .section .text.start, "ax"
    .global _start
_start:
    beq  x0, x0, 1f
    ebreak                     # never runs
1:  auipc a0, 0
    li   a1, 0x9000
    sw   a0, 0(a1)
    beq  a0, a0, 2f            # a later taken branch reads the real pc slot
    ebreak
2:  auipc a2, 0
    sw   a2, 4(a1)
    ebreak
