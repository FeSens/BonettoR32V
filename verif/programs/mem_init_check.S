# PSRAM read-back test, contract v3 section 3. PSRAM starts arbitrary and the memories may start in
# any mode (2.4, Review Focus 1: the harness picks the power-up modes per seed), so everything
# here is written before it is read:
#   1. crt0 copied .data from flash and cleared .bss: .data equals its flash copy (.rodata) and
#      .bss reads 0.
#   2. 63 words spread over the PSRAM below the stack page (CPU 0x0101_0000 + k * 0x2_0200, at
#      varied low offsets: every address bit 2..22 changes), each an address-derived pattern; all
#      written, then all read.
#   3. A byte-written block across a 1 KiB page boundary (0x0110_03E0..0x0110_041F), read back
#      with LW, LH and LHU at every offset (misaligned included).
#   4. A halfword across a 64 KiB boundary (0x0101_FFFF..0x0102_0000), and the 16 bytes right
#      below the core region (0x017F_FF70..0x017F_FF7F, the stack top; main is a leaf).
# The `_psram_window` table lists every range written here for the pins-mode comparison.
# Linked with crt0.S. a5 = test id (the failure id), a4 = expected value.
#include "bonetto.h"

    .set STRIDE_BASE, 0x01010000
    .set STRIDE_STEP, 0x20200     # word k at STRIDE_BASE + k * STRIDE_STEP + (k * STRIDE_OFS & 0x1fc)
    .set STRIDE_N, 63             # k = 62 is CPU 0x017D_7CB8: below the stack page
    .set STRIDE_OFS, 0x24
    .set BLOCK, 0x011003e0
    .set BLOCK_N, 64
    .set HALF_AT, 0x0101ffff
    .set TOP, 0x017fff70

    .macro EXPECT reg, val
    li   a4, \val
    beq  \reg, a4, .Lok\@
    j    mic_fail
.Lok\@:
    .endm

    .macro EQ ra, rb
    beq  \ra, \rb, .Leq\@
    j    mic_fail
.Leq\@:
    .endm

    .macro ABS rd, sym
    lui  \rd, %hi(\sym)
    addi \rd, \rd, %lo(\sym)
    .endm

    .text
    .global main
main:
# 1. .data copied, .bss cleared
    li   a5, 1
    ABS  t0, table_ram
    ABS  t1, table_rom
    li   t2, 8
1:  lw   a0, 0(t0)
    lw   a1, 0(t1)
    EQ   a0, a1
    addi t0, t0, 4
    addi t1, t1, 4
    addi t2, t2, -1
    bnez t2, 1b
    li   a5, 2
    ABS  t0, zeros
    li   t2, 8
2:  lw   a0, 0(t0)
    EXPECT a0, 0
    addi t0, t0, 4
    addi t2, t2, -1
    bnez t2, 2b

# 2. pattern words spread over the PSRAM below the stack page: write all, then read all
    li   a5, 3
    li   s1, 0                 # 0: write pass, 1: read pass
3:  li   t0, STRIDE_BASE       # STRIDE_BASE + k * STRIDE_STEP
    li   t1, 0                 # k * STRIDE_OFS
    li   t2, STRIDE_N
4:  andi a0, t1, 0x1fc
    add  a0, a0, t0            # the address
    slli a1, a0, 13            # pattern(a) = a ^ (a << 13) ^ (a >> 7) ^ 0xa5c30f96
    xor  a1, a1, a0
    srli a2, a0, 7
    xor  a1, a1, a2
    li   a2, 0xa5c30f96
    xor  a1, a1, a2
    bnez s1, 5f
    sw   a1, 0(a0)
    j    6f
5:  lw   a3, 0(a0)
    EQ   a3, a1
6:  li   a2, STRIDE_STEP
    add  t0, t0, a2
    addi t1, t1, STRIDE_OFS
    addi t2, t2, -1
    bnez t2, 4b
    addi s1, s1, 1
    li   a2, 2
    bltu s1, a2, 3b

# 3. bytes across a 1 KiB page boundary, read back at every offset and width
    li   a5, 4
    li   t0, BLOCK
    li   t1, 0
7:  slli a0, t1, 3             # byte(i) = (7i + 3) & 0xff
    sub  a0, a0, t1
    addi a0, a0, 3
    add  a1, t0, t1
    sb   a0, 0(a1)
    addi t1, t1, 1
    li   a2, BLOCK_N
    bltu t1, a2, 7b
    li   a5, 5
    li   a1, 0x18110a03        # bytes 0..3: 03 0a 11 18
    li   t1, 0
8:  add  a2, t0, t1            # LW, LHU, LH at offset i (a1 = bytes i..i+3)
    lw   a0, 0(a2)
    EQ   a0, a1
    lhu  a0, 0(a2)
    slli a3, a1, 16
    srli a3, a3, 16
    EQ   a0, a3
    lh   a0, 2(a2)
    srai a3, a1, 16
    EQ   a0, a3
    addi a2, t1, 4             # slide: a1 = (a1 >> 8) | byte(i + 4) << 24
    slli a0, a2, 3
    sub  a0, a0, a2
    addi a0, a0, 3
    slli a0, a0, 24
    srli a1, a1, 8
    or   a1, a1, a0
    addi t1, t1, 1
    li   a2, BLOCK_N - 3
    bltu t1, a2, 8b

# 4. a halfword across a 64 KiB boundary; the bytes right below the core region
    li   a5, 6
    li   t0, HALF_AT
    li   t1, 0xbeef
    sh   t1, 0(t0)
    lbu  a0, 0(t0)
    EXPECT a0, 0xef
    lbu  a0, 1(t0)
    EXPECT a0, 0xbe
    lhu  a0, 0(t0)
    EXPECT a0, 0xbeef
    li   a5, 7
    li   t0, TOP
    li   t1, 0x0f1e2d3c
    sw   t1, 0(t0)
    li   t1, 0x4b5a6978
    sw   t1, 4(t0)
    li   t1, 0x8796a5b4
    sw   t1, 8(t0)
    li   t1, 0xc3d2e1f0
    sw   t1, 12(t0)
    lw   a0, 12(t0)
    EXPECT a0, 0xc3d2e1f0
    lw   a0, 0(t0)
    EXPECT a0, 0x0f1e2d3c
    lw   a0, 9(t0)             # bytes 0x..79..0x..7c
    EXPECT a0, 0xf08796a5
    lh   a0, 14(t0)            # bytes 0x..7e..0x..7f, the last byte below the region
    EXPECT a0, 0xffffc3d2
    lbu  a0, 15(t0)
    EXPECT a0, 0xc3
    li   a0, 0
    ret

mic_fail:
    mv   a0, a5
    j    _fail

    .section .rodata
    .align 2
table_rom:
    .word 0x01234567, 0x89abcdef, 0xfedcba98, 0x76543210
    .word 0x00000000, 0xffffffff, 0x5a5a5a5a, 0xa5a5a5a5
    # every PSRAM range main writes (mkexpect.py adds .data and .bss)
    .global _psram_window
_psram_window:
    .set k, 0
    .rept STRIDE_N
    .word STRIDE_BASE + k * STRIDE_STEP + ((k * STRIDE_OFS) & 0x1fc)
    .word STRIDE_BASE + k * STRIDE_STEP + ((k * STRIDE_OFS) & 0x1fc) + 4
    .set k, k + 1
    .endr
    .word BLOCK, BLOCK + BLOCK_N
    .word HALF_AT, HALF_AT + 2
    .word TOP, TOP + 16
    .word 0, 0

    .section .data
    .align 2
table_ram:
    .word 0x01234567, 0x89abcdef, 0xfedcba98, 0x76543210
    .word 0x00000000, 0xffffffff, 0x5a5a5a5a, 0xa5a5a5a5

    .section .bss
    .align 2
zeros:
    .space 32
