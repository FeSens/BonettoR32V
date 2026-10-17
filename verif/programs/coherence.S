# Coherence test, contract v3 section 3 (Review Focus 3): "every load and fetch returns the value
# most recently stored to that address by this program, or the loaded image if nothing was
# stored". Write buffers, caches and prefetch must never return stale data.
#
#   1. Store, then at once load the same PSRAM address: every width (SB, SH, SW) at every offset
#      0..7 of an 8-aligned buffer, with the same-width loads (signed and unsigned), the last
#      byte, then the three containing words (expected values computed by the assembler).
#   2. Back-to-back stores to overlapping bytes, then a load; a store through PSRAM alias bits
#      (30:25, 23), then a load without them, and the other way round.
#   3. Stores to flash have no effect on memory, for loads and for fetch (contract 3).
#   4. Code in PSRAM: a function copied from flash runs; one instruction is overwritten (SW),
#      then part of one (SH), then one through an alias, and each time the new code runs at once.
#      The code runs through a fetch alias too. Self-modifying code stores over the very next
#      instruction and over the one after, then runs them. A loop in PSRAM loads and stores PSRAM
#      between its own fetches.
#
# Linked with crt0.S. a5 = test id (the failure id), a4 = expected value; gp, tp, s1, a0 hold the
# buffer's background words in section 1.
#include "bonetto.h"

    .set FN_WORDS, 3           # words of the code templates below
    .set SMC_WORDS, 6
    .set SUM_WORDS, 9

    .macro ABS rd, sym
    lui  \rd, %hi(\sym)
    addi \rd, \rd, %lo(\sym)
    .endm

    .macro EXPECT reg, val
    li   a4, \val
    beq  \reg, a4, .Lok\@
    j    coh_fail
.Lok\@:
    .endm

    .macro EQ ra, rb
    beq  \ra, \rb, .Leq\@
    j    coh_fail
.Leq\@:
    .endm

# ---- section 1 helpers: the buffer's bytes are 0xa0 + i before each case ---------------------
    # .set name = expected byte i after an n-byte store of v at offset o
    .macro SETBYTE name, i, o, n, v
    .set \name, ((((\i) >= (\o)) & ((\i) < ((\o) + (\n)))) & (((\v) >> ((((\i) - (\o)) & 3) * 8)) & 0xff)) | ((~(((\i) >= (\o)) & ((\i) < ((\o) + (\n))))) & (0xa0 + (\i)))
    .endm

    # .set expw = expected word j (bytes 4j..4j+3)
    .macro EXPWORD j, o, n, v
    SETBYTE eb0, (4 * \j), \o, \n, \v
    SETBYTE eb1, (4 * \j + 1), \o, \n, \v
    SETBYTE eb2, (4 * \j + 2), \o, \n, \v
    SETBYTE eb3, (4 * \j + 3), \o, \n, \v
    .set expw, eb0 | (eb1 << 8) | (eb2 << 16) | (eb3 << 24)
    .endm

    .macro CHECKWORD j, o, n, v
    EXPWORD \j, \o, \n, \v
    lw   t2, 4 * \j(s0)
    li   a4, expw
    beq  t2, a4, .Lw\@
    j    coh_fail
.Lw\@:
    .endm

    # one case: fill, store, then loads right behind it
    .macro SL o, n, st, ldu, lds, v, vs
    li   a5, ((\n) << 4) | (\o)
    sw   gp, 0(s0)
    sw   tp, 4(s0)
    sw   s1, 8(s0)
    sw   a0, 12(s0)
    li   t1, \v
    \st  t1, \o(s0)
    \ldu t2, \o(s0)            # the same address and width, right after the store
    EXPECT t2, \v
    .ifnb \lds
    \lds t2, \o(s0)
    EXPECT t2, \vs
    .endif
    lbu  t2, (\o + \n - 1)(s0) # its last byte
    EXPECT t2, (((\v) >> (8 * (\n - 1))) & 0xff)
    CHECKWORD 0, \o, \n, \v
    CHECKWORD 1, \o, \n, \v
    CHECKWORD 2, \o, \n, \v
    .endm

    .text
    .global main
main:
    addi sp, sp, -4
    sw   ra, 0(sp)

# 1. store -> load, every width and offset
    ABS  s0, buf
    li   gp, 0xa3a2a1a0
    li   tp, 0xa7a6a5a4
    li   s1, 0xabaaa9a8
    li   a0, 0xafaeadac
    .irp o, 0, 1, 2, 3, 4, 5, 6, 7
    SL   \o, 1, sb, lbu, lb, 0xc5, 0xffffffc5
    .endr
    .irp o, 0, 1, 2, 3, 4, 5, 6, 7
    SL   \o, 2, sh, lhu, lh, 0x9d3e, 0xffff9d3e
    .endr
    .irp o, 0, 1, 2, 3, 4, 5, 6, 7
    SL   \o, 4, sw, lw, , 0x8b7c6d5e,
    .endr

# 2. overlapping stores, then loads; stores and loads through PSRAM alias bits
    li   a5, 0x50
    sw   gp, 0(s0)
    sw   tp, 4(s0)
    sw   s1, 8(s0)
    sw   a0, 12(s0)
    li   t1, 0x11223344
    sw   t1, 4(s0)
    li   t1, 0x55
    sb   t1, 5(s0)
    li   t1, 0x6677
    sh   t1, 6(s0)
    lw   t2, 4(s0)             # 77 66 55 44
    EXPECT t2, 0x66775544
    li   t1, 0x8899aabb
    sw   t1, 5(s0)             # bytes 5..8
    lhu  t2, 4(s0)
    EXPECT t2, 0xbb44
    lw   t2, 6(s0)
    EXPECT t2, 0xa98899aa      # bytes 6..9: aa 99 88 a9
    li   a5, 0x51
    li   t0, 0x7e800000        # bits 30:25 and 23: the same PSRAM bytes
    add  t0, t0, s0
    li   t1, 0xcafef00d
    sw   t1, 9(t0)             # through the alias, misaligned
    lw   t2, 9(s0)             # without it, right after
    EXPECT t2, 0xcafef00d
    li   t1, 0x1234
    sh   t1, 10(s0)
    lw   t2, 9(t0)
    EXPECT t2, 0xca12340d
    li   t0, 0x00800000        # bit 23 alone
    add  t0, t0, s0
    li   t1, 0x99
    sb   t1, 12(t0)
    lbu  t2, 12(s0)
    EXPECT t2, 0x99
    lw   t2, 9(s0)
    EXPECT t2, 0x9912340d

# 3. stores to flash change nothing (contract 3), for loads and for fetch
    li   a5, 0x52
    ABS  t0, flash_data
    li   t1, 0x01020304
    sw   t1, 0(t0)
    lw   t2, 0(t0)
    EXPECT t2, 0x600df1a5
    sh   t1, 5(t0)
    sb   t1, 7(t0)
    lw   t2, 4(t0)
    EXPECT t2, 0x7e57ab1e
    lw   t2, 3(t0)
    EXPECT t2, 0x57ab1e60
    li   t2, 0x7e000000        # through flash alias bits 30:25
    add  t2, t2, t0
    sw   t1, 0(t2)
    lw   t2, 0(t0)
    EXPECT t2, 0x600df1a5
    li   a5, 0x53
    ABS  t0, fn_template       # flash code: overwrite its first instruction, then run it
    li   t1, 0x22200513        # li a0, 0x222
    sw   t1, 0(t0)
    jalr ra, 0(t0)
    EXPECT a0, 0x133           # the flash code is unchanged
    ABS  t0, fn_template
    lw   t2, 0(t0)
    EXPECT t2, 0x11100513

# 4. code in PSRAM
    li   a5, 0x54
    ABS  a0, code_buf          # copy fn_template (FN_WORDS words) from flash
    ABS  a1, fn_template
    li   a2, FN_WORDS
    call copy_words
    ABS  t0, code_buf
    jalr ra, 0(t0)             # li a0, 0x111; addi a0, a0, 0x22; ret
    EXPECT a0, 0x133
    li   a5, 0x55
    ABS  t0, code_buf
    li   t1, 0x22200513        # li a0, 0x222
    sw   t1, 0(t0)
    jalr ra, 0(t0)             # right after the store
    EXPECT a0, 0x244
    li   a5, 0x56
    ABS  t0, code_buf
    li   t1, 0x3330            # the upper half of `li a0, 0x333`
    sh   t1, 2(t0)
    jalr ra, 0(t0)
    EXPECT a0, 0x355
    lw   t2, 0(t0)             # a load of the patched word
    EXPECT t2, 0x33300513
    li   a5, 0x57
    ABS  t0, code_buf
    li   t1, 0x7e800000        # patch through PSRAM alias bits
    add  t1, t1, t0
    li   t2, 0x04450513        # addi a0, a0, 0x44
    sw   t2, 4(t1)
    jalr ra, 0(t0)
    EXPECT a0, 0x377
    li   a5, 0x58
    ABS  t0, code_buf          # run it through a fetch alias (bits 30:25 and 23)
    li   t1, 0x7e800000
    add  t0, t0, t1
    jalr ra, 0(t0)
    EXPECT a0, 0x377
    li   a5, 0x59
    ABS  t0, code_buf          # and through bit 25 only, after patching it there
    li   t1, 0x02000000
    add  t0, t0, t1
    li   t2, 0x05550513        # addi a0, a0, 0x55
    sw   t2, 4(t0)
    jalr ra, 0(t0)
    EXPECT a0, 0x388

    # self-modifying code: SW over the very next instruction, then over the one after
    li   a5, 0x5b
    ABS  a0, code_buf2
    ABS  a1, smc_template
    li   a2, SMC_WORDS
    call copy_words
    ABS  t0, code_buf2
    li   a1, 0x00200513        # li a0, 2: replaces `li a0, 1` (the next instruction)
    li   a2, 0x00750513        # addi a0, a0, 7: replaces `addi a0, a0, 1` (two ahead)
    jalr ra, 0(t0)
    EXPECT a0, 9
    li   a5, 0x5c
    ABS  t0, code_buf2         # again: the stores now write what is already there
    jalr ra, 0(t0)
    EXPECT a0, 9
    li   a5, 0x5d
    ABS  t0, code_buf2
    li   a1, 0x00300513        # li a0, 3
    li   a2, 0x00150513        # addi a0, a0, 1
    jalr ra, 0(t0)
    EXPECT a0, 4

    # a loop in PSRAM: prefix sums of 1..16 in place (PSRAM loads and stores between fetches)
    li   a5, 0x5e
    ABS  a0, code_buf3
    ABS  a1, sum_template
    li   a2, SUM_WORDS
    call copy_words
    ABS  t0, array
    li   t1, 1
1:  sw   t1, 0(t0)
    addi t0, t0, 4
    addi t1, t1, 1
    li   t2, 17
    bltu t1, t2, 1b
    ABS  a0, array
    li   a1, 16
    ABS  t0, code_buf3
    jalr ra, 0(t0)
    EXPECT a0, 136
    ABS  t0, array
    lw   t2, 0(t0)
    EXPECT t2, 1
    lw   t2, 28(t0)
    EXPECT t2, 36
    lw   t2, 60(t0)
    EXPECT t2, 136

    lw   ra, 0(sp)
    addi sp, sp, 4
    li   a0, 0
    ret

coh_fail:
    mv   a0, a5
    j    _fail

# copy_words(a0 = dst, a1 = src, a2 = words)
copy_words:
1:  lw   t1, 0(a1)
    sw   t1, 0(a0)
    addi a0, a0, 4
    addi a1, a1, 4
    addi a2, a2, -1
    bnez a2, 1b
    ret

# ---- code templates (run from flash or copied to PSRAM) --------------------------------------
    .align 2
fn_template:
    li   a0, 0x111             # 0x11100513
    addi a0, a0, 0x22          # 0x02250513
    ret

    .align 2
smc_template:                  # a1 = the new +8 word, a2 = the new +16 word
    auipc t0, 0
    sw   a1, 8(t0)             # over the next instruction
    li   a0, 1                 # +8
    sw   a2, 16(t0)            # over the next one but one
    addi a0, a0, 1             # +16
    ret

    .align 2
sum_template:                  # a0 = int array, a1 = n: prefix sums in place, returns the total
    li   a2, 0
1:  lw   a3, 0(a0)
    add  a2, a2, a3
    sw   a2, 0(a0)
    addi a0, a0, 4
    addi a1, a1, -1
    bnez a1, 1b
    mv   a0, a2
    ret

    .section .rodata
    .align 2
flash_data:
    .word 0x600df1a5, 0x7e57ab1e

    .section .bss
    .align 3
buf:
    .space 16
code_buf:
    .space 4 * 3
    .align 2
code_buf2:
    .space 4 * 6
code_buf3:
    .space 4 * 9
array:
    .space 64
