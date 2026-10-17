# Directed test (hypothesis 005): the first instruction after reset is a GPIO store, then GPIO
# loads of every width and sign.
    .section .text.start, "ax"
    .global _start
_start:
    sb   x0, -2048(x0)         # 0xFFFFF800: GPIO (address bit 31), byte 0
    li   a1, 0x80000000
    li   a2, 0xA5
    sb   a2, 0(a1)
    lbu  a3, 0(a1)
    lb   a4, 0(a1)
    lhu  a5, 0(a1)
    lw   a0, 0(a1)
    sw   a3, 0(a1)
    li   a1, 0x9000
    sw   a4, 0(a1)
    sw   a5, 4(a1)
    sw   a0, 8(a1)
    ebreak
