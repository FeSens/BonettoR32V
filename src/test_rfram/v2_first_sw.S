# Variant 2 directed test (hypothesis 002): the first instruction after reset is a RAM SW, the
# slowest class, so the boot slot plus that SW must fit the 1,024-cycle cap. Then misaligned
# SW/SH/LW whose second half crosses a carry (address + 2 in the BH2 slot), and every register as
# a store source. The harness compares every retirement and the final RAM with the ISS.
    .section .text.start, "ax"
    .global _start
_start:
    sw   x0, 0x400(x0)
    li   x1, 0
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
    li   a1, 0x11223344
    li   a2, 0xA5C3E10F
    sw   a1, 0x0FE(a0)         # bytes 0x90FE..0x9101: + 2 carries into bit 8
    sw   a2, 0x1FF(a0)         # 0x91FF..0x9202
    sw   a1, 0x3FD(a0)         # 0x93FD..0x9400
    li   a3, 0x9FFE
    sw   a2, 0(a3)             # 0x9FFE..0xA001: + 2 carries through bit 13
    li   a3, 0x7FFF
    sw   a1, 0(a3)             # 0x7FFF..0x8002: + 2 carries through bit 15
    sh   a2, 0x5FF(a0)         # one transaction across 0x95FF..0x9600
    sb   a2, 0x601(a0)
    lw   a4, 0x0FE(a0)
    lw   a5, 0x1FF(a0)
    lh   a4, 0x5FF(a0)
    lhu  a5, 0x5FF(a0)
    lw   a4, 0(a3)
    li   x1, 0x01010101
    li   x2, 0x02020202
    li   x3, 0x03030303
    li   x4, 0x04040404
    li   x5, 0x05050505
    li   x6, 0x06060606
    li   x7, 0x07070707
    li   x8, 0x08080808
    li   x9, 0x09090909
    li   x11, 0x0B0B0B0B
    li   x12, 0x0C0C0C0C
    li   x13, 0x0D0D0D0D
    li   x14, 0x0E0E0E0E
    li   x15, 0x0F0F0F0F
    li   x10, 0x9800
    sw   x0, 0(x10)
    sw   x1, 4(x10)
    sw   x2, 8(x10)
    sw   x3, 12(x10)
    sw   x4, 16(x10)
    sw   x5, 21(x10)
    sw   x6, 26(x10)
    sw   x7, 31(x10)
    sw   x8, 36(x10)
    sw   x9, 40(x10)
    sw   x10, 44(x10)
    sw   x11, 48(x10)
    sw   x12, 52(x10)
    sw   x13, 57(x10)
    sw   x14, 62(x10)
    sw   x15, 67(x10)
    ebreak
