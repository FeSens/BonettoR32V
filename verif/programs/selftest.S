# BonettoR32V selftest, contract v3 (docs/contract-v3.md sections 3-5).
#
# Executes every RV32E instruction (ECALL and EBREAK trap: see the trap_* programs), every branch
# taken and not taken, x0 writes, JALR with bit 0 set, JAL offsets with J-imm bits 16-20 (to
# .farcode at 0x000F_8000 and back), not-taken branches to targets with bit 1 set, loads of
# every width from flash and from PSRAM, misaligned LW/LH/LHU/SW/SH up to CPU 0x017F_FF7F (the
# last PSRAM byte below the core region), flash and PSRAM data aliasing (bits
# 30:25, and 23 for PSRAM), fetch aliasing in flash, every shift amount, FENCE variants, and IO:
# GPIO_OUT on every byte lane, slot decode by addr[6:2], and the read/write masks of the slots
# that have no side effects.
#
# Results also go to a signature area in PSRAM (.bss), so the pins-mode cosim (final PSRAM
# image) sees every computed value. Linked with crt0.S: main ends at _pass (GPIO_OUT = 0x5A) or,
# on a failed check, at _fail with the failing test id (a5).
#
# Registers: a5 (x15) test id, a4 (x14) expected value, a3 (x13) signature pointer,
# a2 (x12) IO base in the IO sections. x1..x11 are scratch; the stack is not used.
#include "bonetto.h"

    .macro TEST id
    li   a5, \id
    .endm

    # reg == value, else fail
    .macro EXPECT reg, val
    li   a4, \val
    beq  \reg, a4, .Lok\@
    j    st_fail
.Lok\@:
    .endm

    # ra == rb, else fail
    .macro EQ ra, rb
    beq  \ra, \rb, .Leq\@
    j    st_fail
.Leq\@:
    .endm

    .macro SIG reg
    sw   \reg, 0(a3)
    addi a3, a3, 4
    .endm

    .macro TAKEN br, ra, rb
    \br  \ra, \rb, .Lt\@
    j    st_fail
.Lt\@:
    .endm

    .macro NOTTAKEN br, ra, rb
    \br  \ra, \rb, .Lnt\@
    j    .Lntok\@
.Lnt\@:
    j    st_fail
.Lntok\@:
    .endm

    .macro ABS rd, sym
    lui  \rd, %hi(\sym)
    addi \rd, \rd, %lo(\sym)
    .endm

    .text
    .global main
main:
    ABS  a3, sig_area

# ---------------------------------------------------------------------------------------------
# 1. LUI, AUIPC
    TEST 1
    lui  x1, 0x12345
    EXPECT x1, 0x12345000
    SIG  x1
    lui  x1, 0xfffff
    EXPECT x1, 0xfffff000
    lui  x1, 0x80000
    EXPECT x1, 0x80000000
auipc0:
    auipc x2, 0
    ABS  x3, auipc0
    EQ   x2, x3
auipc1:
    auipc x2, 0x80001
    ABS  x3, auipc1
    li   x4, 0x80001000
    add  x3, x3, x4
    EQ   x2, x3
    SIG  x2

# ---------------------------------------------------------------------------------------------
# 2. OP-IMM: ADDI SLTI SLTIU XORI ORI ANDI
    TEST 2
    li   x1, 0x7fffffff
    addi x2, x1, 1
    EXPECT x2, 0x80000000
    addi x2, x1, -2048
    EXPECT x2, 0x7ffff7ff
    addi x2, x0, 2047
    EXPECT x2, 0x7ff
    addi x2, x0, -1
    EXPECT x2, 0xffffffff
    SIG  x2
    li   x1, 5
    slti x2, x1, 6
    EXPECT x2, 1
    slti x2, x1, 5
    EXPECT x2, 0
    slti x2, x1, -1
    EXPECT x2, 0
    li   x1, -5
    slti x2, x1, -4
    EXPECT x2, 1
    slti x2, x1, -2048
    EXPECT x2, 0
    sltiu x2, x1, -1
    EXPECT x2, 1
    sltiu x2, x1, 5
    EXPECT x2, 0
    sltiu x2, x0, 1
    EXPECT x2, 1
    li   x1, 0x80000000
    slti x2, x1, 0
    EXPECT x2, 1
    sltiu x2, x1, 0
    EXPECT x2, 0
    li   x1, 0x12345678
    xori x2, x1, -1
    EXPECT x2, 0xedcba987
    xori x2, x1, 0x7f0
    EXPECT x2, 0x12345188
    ori  x2, x1, -2048
    EXPECT x2, 0xfffffe78
    ori  x2, x1, 0x101
    EXPECT x2, 0x12345779
    andi x2, x1, -256
    EXPECT x2, 0x12345600
    andi x2, x1, 0x7f
    EXPECT x2, 0x78
    SIG  x2

# ---------------------------------------------------------------------------------------------
# 3. OP: ADD SUB SLT SLTU XOR OR AND
    TEST 3
    li   x1, 0x7fffffff
    li   x2, 1
    li   x3, 0x80000000
    li   x4, -1
    add  x5, x1, x2
    EXPECT x5, 0x80000000
    add  x5, x4, x2
    EXPECT x5, 0
    add  x5, x3, x3
    EXPECT x5, 0
    sub  x5, x3, x2
    EXPECT x5, 0x7fffffff
    sub  x5, x0, x2
    EXPECT x5, 0xffffffff
    sub  x5, x1, x4
    EXPECT x5, 0x80000000
    SIG  x5
    slt  x5, x3, x1
    EXPECT x5, 1
    slt  x5, x1, x3
    EXPECT x5, 0
    slt  x5, x4, x2
    EXPECT x5, 1
    slt  x5, x2, x2
    EXPECT x5, 0
    sltu x5, x1, x3
    EXPECT x5, 1
    sltu x5, x4, x2
    EXPECT x5, 0
    sltu x5, x0, x2
    EXPECT x5, 1
    sltu x5, x2, x2
    EXPECT x5, 0
    li   x6, 0xf0f0a5a5
    li   x7, 0x0ff05a5a
    xor  x5, x6, x7
    EXPECT x5, 0xff00ffff
    or   x5, x6, x7
    EXPECT x5, 0xfff0ffff
    and  x5, x6, x7
    EXPECT x5, 0x00f00000
    SIG  x5

# ---------------------------------------------------------------------------------------------
# 4. Shifts, every amount. SLL is checked against doubling, SRL/SRA against a chain of
#    one-bit shifts plus literals, and the immediate forms against the register forms.
    TEST 4
    li   x1, 0x9abcdef1        # negative
    li   x9, 0x5a3c0f69        # positive
    # SLL by k = 0..31 equals k doublings
    li   x4, 0
    mv   x5, x1
    li   x7, 32
4:  sll  x6, x1, x4
    EQ   x6, x5
    SIG  x6
    add  x5, x5, x5
    addi x4, x4, 1
    blt  x4, x7, 4b
    # SRL and SRA: shift(k) == shift1(shift(k-1)), for both a negative and a positive operand
    li   x8, 0
41: beqz x8, 42f
    mv   x1, x9
42: li   x4, 0
    mv   x5, x1                # expected SRL
    mv   x10, x1               # expected SRA
43: srl  x6, x1, x4
    EQ   x6, x5
    SIG  x6
    sra  x6, x1, x4
    EQ   x6, x10
    SIG  x6
    srli x5, x5, 1
    srai x10, x10, 1
    addi x4, x4, 1
    blt  x4, x7, 43b
    addi x8, x8, 1
    li   x6, 2
    blt  x8, x6, 41b
    li   x1, 0x9abcdef1
    # literals pin the one-bit shifts
    srli x6, x1, 1
    EXPECT x6, 0x4d5e6f78
    srai x6, x1, 1
    EXPECT x6, 0xcd5e6f78
    srli x6, x1, 31
    EXPECT x6, 1
    srai x6, x1, 31
    EXPECT x6, 0xffffffff
    slli x6, x1, 31
    EXPECT x6, 0x80000000
    srai x6, x9, 31
    EXPECT x6, 0
    srli x6, x1, 16
    EXPECT x6, 0x9abc
    srai x6, x1, 4
    EXPECT x6, 0xf9abcdef
    # only rs2[4:0] is the shift amount
    li   x4, 35
    sll  x6, x1, x4
    EXPECT x6, 0xd5e6f788
    li   x4, -1
    srl  x6, x1, x4
    EXPECT x6, 1
    li   x4, 0x40
    sra  x6, x1, x4
    EQ   x6, x1
    # immediate forms, every amount, against the register forms
    .irp k, 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31
    li   x4, \k
    slli x6, x1, \k
    sll  x8, x1, x4
    EQ   x6, x8
    srli x6, x1, \k
    srl  x8, x1, x4
    EQ   x6, x8
    srai x6, x1, \k
    sra  x8, x1, x4
    EQ   x6, x8
    SIG  x6
    srai x6, x9, \k
    sra  x8, x9, x4
    EQ   x6, x8
    .endr

# ---------------------------------------------------------------------------------------------
# 5. Loads and stores, aligned and misaligned. Loads from flash (ldbuf, .rodata) and from PSRAM
#    (ldbuf_ram, .data: crt0 copied it from flash); stores to PSRAM (stbuf, .bss).
    .macro LOADCHECK base
    lb   x2, 0(\base)
    EXPECT x2, 0xffffff80
    lbu  x2, 0(\base)
    EXPECT x2, 0x80
    lb   x2, 2(\base)
    EXPECT x2, 0x02
    lh   x2, 0(\base)
    EXPECT x2, 0xffff8180
    lhu  x2, 0(\base)
    EXPECT x2, 0x8180
    lh   x2, 1(\base)
    EXPECT x2, 0x0281
    lh   x2, 3(\base)
    EXPECT x2, 0xffff8483
    lhu  x2, 3(\base)
    EXPECT x2, 0x8483
    lh   x2, 5(\base)
    EXPECT x2, 0xffff8605
    lw   x2, 0(\base)
    EXPECT x2, 0x83028180
    lw   x2, 1(\base)
    EXPECT x2, 0x84830281
    lw   x2, 2(\base)
    EXPECT x2, 0x05848302
    lw   x2, 3(\base)
    EXPECT x2, 0x86058483
    SIG  x2
    lw   x2, 9(\base)
    EXPECT x2, 0x0c8b7a09
    lh   x2, 13(\base)
    EXPECT x2, 0x0efd
    lhu  x2, 14(\base)
    EXPECT x2, 0x8f0e
    lb   x2, 15(\base)
    EXPECT x2, 0xffffff8f
    lw   x2, 12(\base)
    EXPECT x2, 0x8f0efd0c
    addi x3, \base, 8
    lw   x2, -5(x3)            # negative offset, misaligned
    EXPECT x2, 0x86058483
    lw   x0, 1(\base)          # load into x0 is discarded
    add  x2, x0, x0
    EXPECT x2, 0
    .endm

    TEST 5
    ABS  x1, ldbuf
    LOADCHECK x1
    TEST 0x15
    ABS  x1, ldbuf_ram
    LOADCHECK x1

    TEST 0x25
    ABS  x1, stbuf
    li   x2, 0xa1b2c3d4
    sw   x2, 0(x1)
    sw   x2, 4(x1)
    sw   x2, 8(x1)
    li   x2, 0x11223344
    sw   x2, 1(x1)             # bytes 1..4
    lw   x3, 0(x1)
    EXPECT x3, 0x223344d4
    lw   x3, 4(x1)
    EXPECT x3, 0xa1b2c311
    lbu  x3, 4(x1)
    EXPECT x3, 0x11
    li   x2, 0xbeef
    sh   x2, 3(x1)             # bytes 3..4
    lw   x3, 0(x1)
    EXPECT x3, 0xef3344d4
    lw   x3, 4(x1)
    EXPECT x3, 0xa1b2c3be
    lh   x3, 3(x1)
    EXPECT x3, 0xffffbeef
    lhu  x3, 3(x1)
    EXPECT x3, 0xbeef
    li   x2, 0xcafe1234
    sw   x2, 7(x1)             # bytes 7..10
    lw   x3, 4(x1)
    EXPECT x3, 0x34b2c3be
    lw   x3, 8(x1)
    EXPECT x3, 0xa1cafe12
    lw   x3, 7(x1)
    EXPECT x3, 0xcafe1234
    li   x2, 0x5566
    sh   x2, 1(x1)
    sh   x2, 11(x1)            # bytes 11..12
    lw   x3, 0(x1)
    EXPECT x3, 0xef5566d4
    lw   x3, 8(x1)
    EXPECT x3, 0x66cafe12
    lbu  x3, 12(x1)
    EXPECT x3, 0x55
    li   x2, 0x1ff
    sb   x2, 2(x1)             # SB stores only the low byte
    sb   x2, 13(x1)
    lw   x3, 0(x1)
    EXPECT x3, 0xefff66d4
    lb   x3, 13(x1)
    EXPECT x3, 0xffffffff
    sh   x0, 14(x1)
    lhu  x3, 14(x1)
    EXPECT x3, 0
    lw   x3, 12(x1)
    SIG  x3
    lw   x3, 0(x1)
    SIG  x3
    lw   x3, 4(x1)
    SIG  x3
    lw   x3, 8(x1)
    SIG  x3
    # Loads of the instruction's own bytes (flash): the load can't reuse a stale fetch.
    ABS  x6, .Lself1
.Lself1:
    lw   x5, 0(x6)             # 0x00032283, its own encoding
    EXPECT x5, 0x00032283
    SIG  x5
    ABS  x6, .Lself2
.Lself2:
    lw   x5, 2(x6)             # 0x00232283: its bytes 2..3, then bytes 0..1 of the next one
    addi x0, x0, 0x7ff         # 0x7ff00013
    EXPECT x5, 0x00130023
    SIG  x5

# ---------------------------------------------------------------------------------------------
# 6. Misaligned accesses that end at CPU 0x017F_FF7F, the last PSRAM byte below the core region
#    0x7F_FF80-0x7F_FFFF (contract 3), directly and through alias bits. 0x017F_FF78..7F is the
#    top of the stack, which selftest never uses (main is a leaf and doesn't touch sp).
    TEST 6
    li   x1, 0x017fff7c
    li   x2, 0x44332211
    sw   x2, 0(x1)             # 0x..7c..0x..7f = 11 22 33 44
    li   x2, 0x88776655
    sw   x2, -4(x1)            # 0x..78..0x..7b = 55 66 77 88
    lw   x3, -1(x1)            # 0x..7b
    EXPECT x3, 0x33221188
    lw   x3, -3(x1)            # 0x..79
    EXPECT x3, 0x11887766
    lh   x3, 1(x1)             # 0x..7d
    EXPECT x3, 0x3322
    lhu  x3, -1(x1)            # 0x..7b
    EXPECT x3, 0x1188
    lb   x3, 3(x1)             # 0x..7f
    EXPECT x3, 0x44
    li   x2, 0xddccbbaa
    sw   x2, -2(x1)            # 0x..7a..0x..7d = aa bb cc dd
    lw   x3, 0(x1)
    EXPECT x3, 0x4433ddcc
    lw   x3, -4(x1)
    EXPECT x3, 0xbbaa6655
    li   x2, 0xf234
    sh   x2, -1(x1)            # 0x..7b = 34, 0x..7c = f2
    lh   x3, -1(x1)
    EXPECT x3, 0xfffff234
    lhu  x3, 1(x1)             # 0x..7d..0x..7e = dd 33
    EXPECT x3, 0x33dd
    li   x2, 0x8001
    sh   x2, 2(x1)             # 0x..7e..0x..7f = 01 80
    lh   x3, 2(x1)
    EXPECT x3, 0xffff8001
    SIG  x3
    # the same bytes through PSRAM alias bits 30:25 and 23 (addr[31] = 0, addr[24] = 1)
    li   x4, 0x7fffff7c
    lw   x3, 0(x4)             # 0x..7c..0x..7f = f2 dd 01 80
    EXPECT x3, 0x8001ddf2
    li   x2, 0x13579bdf
    sw   x2, -3(x4)            # 0x..79..0x..7c = df 9b 57 13
    lw   x3, -4(x1)
    EXPECT x3, 0x579bdf55
    lw   x3, 0(x1)
    EXPECT x3, 0x8001dd13
    SIG  x3

# ---------------------------------------------------------------------------------------------
# 7. Aliasing (contract 3): flash ignores address bits 30:25 and PSRAM bits 30:25 and 23, for
#    data and for fetch
    TEST 7
    ABS  x1, stbuf
    li   x2, 0x02000000
    add  x3, x1, x2            # stbuf + 32 MiB (bit 25)
    li   x4, 0x600dcafe
    sw   x4, 16(x3)
    lw   x5, 16(x1)
    EQ   x5, x4
    li   x2, 0x7e800000        # bits 30:25 and 23
    add  x3, x1, x2
    lw   x5, 16(x3)
    EQ   x5, x4
    li   x4, 0x1234
    sh   x4, 18(x3)
    lw   x5, 16(x1)
    EXPECT x5, 0x1234cafe
    SIG  x5
    li   x2, 0x00800000        # bit 23 alone
    add  x3, x1, x2
    li   x4, 0x77
    sb   x4, 17(x3)
    lw   x5, 16(x1)
    EXPECT x5, 0x123477fe
    SIG  x5
    # flash data through bits 30:25
    ABS  x1, ldbuf
    li   x2, 0x7e000000
    add  x3, x1, x2
    lw   x5, 9(x3)
    EXPECT x5, 0x0c8b7a09
    li   x2, 0x04000000
    add  x3, x1, x2
    lh   x5, 13(x3)
    EXPECT x5, 0x0efd
    # fetch alias: run alias_code at +32 MiB (bit 25) and at +0x7e000000 (bits 30:25)
    ABS  x1, alias_code
    li   x2, 0x02000000
    add  x1, x1, x2
    li   x7, 0
    jalr ra, 0(x1)
    ABS  x6, alias_code
    li   x2, 0x02000000
    add  x6, x6, x2
    EQ   x3, x6                # auipc saw the aliased pc
    addi x6, x6, 8
    EQ   x4, x6                # jal linked the aliased pc + 4
    EXPECT x7, 0x2468ace0      # a load relative to the aliased pc
    SIG  x4
    ABS  x1, alias_code
    li   x2, 0x7e000000
    add  x1, x1, x2
    li   x7, 0
    jalr ra, 0(x1)
    ABS  x6, alias_code
    li   x2, 0x7e000000
    add  x6, x6, x2
    EQ   x3, x6
    EXPECT x7, 0x2468ace0
    SIG  x3
    j    alias_done
alias_code:
    auipc x3, 0
    jal  x4, 1f
1:  beq  x0, x0, 2f
    j    st_fail
2:  lw   x7, 24(x3)            # alias_word (alias_code + 24), through the aliased pc
    jalr x0, 0(ra)
    .align 2
alias_word:
    .word 0x2468ace0
alias_done:

# ---------------------------------------------------------------------------------------------
# 8. Branches: each one taken and not taken, signed and unsigned edges
    TEST 8
    li   x1, -1
    li   x2, 1
    li   x3, 0x80000000
    li   x4, 0x7fffffff
    TAKEN    beq, x2, x2
    NOTTAKEN beq, x1, x2
    TAKEN    bne, x1, x2
    NOTTAKEN bne, x3, x3
    TAKEN    blt, x1, x2
    NOTTAKEN blt, x2, x1
    TAKEN    blt, x3, x4
    NOTTAKEN blt, x4, x3
    NOTTAKEN blt, x2, x2
    TAKEN    bge, x2, x1
    TAKEN    bge, x2, x2
    NOTTAKEN bge, x1, x2
    NOTTAKEN bge, x3, x4
    TAKEN    bge, x4, x3
    TAKEN    bltu, x2, x1
    NOTTAKEN bltu, x1, x2
    TAKEN    bltu, x4, x3
    NOTTAKEN bltu, x2, x2
    TAKEN    bgeu, x1, x2
    NOTTAKEN bgeu, x2, x1
    TAKEN    bgeu, x2, x2
    TAKEN    bgeu, x3, x4
    NOTTAKEN bgeu, x0, x2
    TAKEN    beq, x0, x0
    # a backward taken branch
    li   x5, 3
81: addi x5, x5, -1
    bnez x5, 81b
    EXPECT x5, 0
    # not-taken branches to targets with bit 1 set must not trap
    .word 0x00001363           # bne x0, x0, pc+6
    .word 0x00110363           # beq x2, x1, pc+6 (1 != -1)
    .word 0xfe20dde3           # bge x1, x2, pc-6 (-1 >= 1 is false)

# ---------------------------------------------------------------------------------------------
# 9. JAL, JALR
    TEST 9
    jal  x1, 91f
jal_link:
    j    st_fail
91: ABS  x2, jal_link
    EQ   x1, x2
    SIG  x1
    jal  x0, 92f
    j    st_fail
92: ABS  x5, 93f
    addi x5, x5, 1             # bit 0 set: JALR clears it
    jalr x6, 0(x5)
jalr_link:
    j    st_fail
93: ABS  x2, jalr_link
    EQ   x6, x2
    ABS  x7, 94f
    addi x7, x7, -8
    jalr x7, 8(x7)             # rd == rs1: the target uses the old value
jalr_link2:
    j    st_fail
94: ABS  x2, jalr_link2
    EQ   x7, x2
    ABS  x8, 95f
    addi x8, x8, 2047
    jalr x9, -2047(x8)         # large negative offset
    j    st_fail
95: ABS  x8, 96f
    addi x8, x8, 3
    jalr x0, -3(x8)            # negative offset
    j    st_fail
96: SIG  x9
    # JAL offsets with J-imm bits 16-20: to far_code (CPU 0x000F_8000, about +0xF7C00: bits 16-19
    # = 1, bit 20 = 0) and back (about -0xF7C00: bits 16-19 = 0, bit 20 = 1)
    jal  x1, far_code
far_link:
    j    st_fail
far_back:
    ABS  x2, far_link
    EQ   x1, x2
    ABS  x2, far_code
    EQ   x10, x2               # far_code's AUIPC ran at its own address
    SIG  x10

    .pushsection .farcode, "ax"
far_code:
    auipc x10, 0
    jal  x0, far_back
    .popsection

# ---------------------------------------------------------------------------------------------
# 10. Writes to x0 are discarded; x0 reads as 0
    TEST 10
    li   x1, 0x55
    addi x0, x1, 5
    lui  x0, 0x12345
    auipc x0, 0x1
    add  x0, x1, x1
    slli x0, x1, 3
    sltiu x0, x0, 1
    xor  x0, x1, x0
    jal  x0, 101f
101:
    add  x2, x0, x0
    EXPECT x2, 0
    add  x3, x0, x1
    EXPECT x3, 0x55
    sub  x3, x0, x1
    EXPECT x3, 0xffffffab
    ABS  x4, stbuf
    sw   x0, 0(x4)
    lw   x5, 0(x4)
    EXPECT x5, 0
    SIG  x3

# ---------------------------------------------------------------------------------------------
# 11. FENCE: every encoding is a no-op
    TEST 11
    li   x5, 0x1234
    li   x6, 0x5678
    fence
    fence rw, rw
    fence i, o
    fence.tso
    .word 0x0100000f           # pause
    .word 0x0ff2828f           # fence iorw, iorw with rd = x5, rs1 = x5
    .word 0x0003030f           # fence with all-zero pred/succ, rd = x6, rs1 = x6
    .word 0xfff0000f           # fm = 1111 (reserved fm: a normal fence)
    EXPECT x5, 0x1234
    EXPECT x6, 0x5678


# ---------------------------------------------------------------------------------------------
# 12. GPIO_OUT and GPIO_IN (contract 5.1): every byte lane, the slot decode by addr[6:2]. Slot
#     bits that aren't implemented ignore writes and read 0; a halfword at slot byte 2 is legal.
    TEST 12
    lui  a2, 0x80000
    li   x1, 0x3c
    sb   x1, OFS_GPIO_OUT(a2)  # uo = 0x3c
    lw   x3, OFS_GPIO_OUT(a2)
    EXPECT x3, 0x3c
    li   x1, 0xff
    sb   x1, 1(a2)             # bytes 1..3: not implemented
    sb   x1, 2(a2)
    sb   x1, 3(a2)
    lw   x3, 0(a2)
    EXPECT x3, 0x3c
    li   x1, 0x12345678
    sw   x1, 0(a2)             # uo = 0x78
    lw   x3, 0(a2)
    EXPECT x3, 0x78
    li   x1, 0xabcd
    sh   x1, 0(a2)             # uo = 0xcd
    sh   x1, 2(a2)             # bytes 2..3: ignored
    lhu  x3, 2(a2)
    EXPECT x3, 0
    lhu  x3, 0(a2)
    EXPECT x3, 0xcd
    lbu  x3, 1(a2)
    EXPECT x3, 0
    lb   x3, 0(a2)
    EXPECT x3, 0xffffffcd      # sign-extended byte
    SIG  x3
    li   x2, 0x80000080        # addr[6:2] = 0: GPIO_OUT (bit 7 and above ignored)
    li   x1, 0x69
    sb   x1, 0(x2)             # uo = 0x69
    lw   x3, 0(a2)
    EXPECT x3, 0x69
    li   x2, 0xffffff80        # slot 0 again
    li   x1, 0x96
    sb   x1, 0(x2)             # uo = 0x96
    lw   x3, 0(a2)
    EXPECT x3, 0x96
    li   x2, 0xc0000001        # slot 0, byte 1: ignored
    li   x1, 0x11
    sb   x1, 0(x2)
    lw   x3, -1(x2)            # 0xc0000000: slot 0
    EXPECT x3, 0x96
    li   x2, 0x8000ff84        # addr[6:2] = 1: GPIO_IN; bits 31:8 read 0
    lw   x3, 0(x2)
    srli x4, x3, 8
    EXPECT x4, 0
    SIG  x4
    lbu  x3, OFS_GPIO_IN+1(a2)
    EXPECT x3, 0
    lhu  x3, OFS_GPIO_IN+2(a2)
    EXPECT x3, 0
    li   x1, -1
    sw   x1, OFS_GPIO_IN(a2)   # read-only: ignored
    lhu  x3, OFS_GPIO_IN+2(a2)
    EXPECT x3, 0

# ---------------------------------------------------------------------------------------------
# 13. The read/write masks of the slots without side effects (contract 5.1), reset values,
#     read-only bits, unimplemented slots. Every register goes back to its reset value.
#     No counter, transfer or interrupt starts: TIMER_CTRL, PWM_CTRL keep enable = 0, the capture
#     edge stays off, and mstatus.MIE = mie.MEIE = 0 while IRQ_EN is set.
    TEST 13
    lw   x3, OFS_SPI_CTRL(a2)  # reset: CS_n = 1, D = 0, BUSY = 0
    EXPECT x3, 1
    lw   x3, OFS_TIMER_CAP(a2) # nothing captured since reset
    EXPECT x3, 0
    li   x1, -1
    sw   x1, OFS_ALTFN(a2)     # every uo pin carries its (idle) peripheral function
    lw   x3, OFS_ALTFN(a2)
    EXPECT x3, 0xff
    li   x2, 0x80000088        # addr[6:2] = 2: ALTFN
    li   x1, 0x0f
    sw   x1, 0(x2)
    lw   x3, OFS_ALTFN(a2)
    EXPECT x3, 0x0f
    sw   zero, OFS_ALTFN(a2)
    lw   x3, OFS_ALTFN(a2)
    EXPECT x3, 0
    li   x1, -1
    sw   x1, OFS_TIMER_CMP(a2)
    lw   x3, OFS_TIMER_CMP(a2)
    EXPECT x3, 0xffff
    li   x1, 0x1234
    sh   x1, OFS_TIMER_CMP+2(a2)   # bytes 2..3: not implemented
    lw   x3, OFS_TIMER_CMP(a2)
    EXPECT x3, 0xffff
    sb   x1, OFS_TIMER_CMP+1(a2)   # byte 1 only
    lw   x3, OFS_TIMER_CMP(a2)
    EXPECT x3, 0x34ff
    lhu  x3, OFS_TIMER_CMP(a2)
    EXPECT x3, 0x34ff
    sw   zero, OFS_TIMER_CMP(a2)
    li   x1, 0xffff1234
    sw   x1, OFS_TIMER_CNT(a2)     # the timer is disabled: CNT holds the value
    lw   x3, OFS_TIMER_CNT(a2)
    EXPECT x3, 0x1234
    sw   zero, OFS_TIMER_CNT(a2)
    li   x1, -1
    sw   x1, OFS_TIMER_CAP(a2)     # read-only
    lw   x3, OFS_TIMER_CAP(a2)
    EXPECT x3, 0
    li   x1, 0x6f                  # P = 15, auto-reload, toggle mode; enable 0, capture off
    sw   x1, OFS_TIMER_CTRL(a2)
    lw   x3, OFS_TIMER_CTRL(a2)
    EXPECT x3, 0x6f
    li   x1, 0xffff00a3            # P = 3, auto-reload, PWM mode; bits above 9 not implemented
    sw   x1, OFS_TIMER_CTRL(a2)
    lw   x3, OFS_TIMER_CTRL(a2)
    EXPECT x3, 0xa3
    sw   zero, OFS_TIMER_CTRL(a2)
    li   x1, 0xffff5fff            # PER 0x3ff, Q 7, IR gate; enable 0; bits above 14 unused
    sw   x1, OFS_PWM_CTRL(a2)
    lw   x3, OFS_PWM_CTRL(a2)
    EXPECT x3, 0x5fff
    sw   zero, OFS_PWM_CTRL(a2)
    li   x1, -1
    sw   x1, OFS_PWM_DUTY0(a2)
    li   x1, 0x12345
    sw   x1, OFS_PWM_DUTY1(a2)
    li   x1, 0x2aa
    sh   x1, OFS_PWM_DUTY2(a2)
    lw   x3, OFS_PWM_DUTY0(a2)
    EXPECT x3, 0x3ff
    lw   x3, OFS_PWM_DUTY1(a2)
    EXPECT x3, 0x345
    lw   x3, OFS_PWM_DUTY2(a2)
    EXPECT x3, 0x2aa
    sw   zero, OFS_PWM_DUTY0(a2)
    sw   zero, OFS_PWM_DUTY1(a2)
    sw   zero, OFS_PWM_DUTY2(a2)
    li   x1, -1
    sw   x1, OFS_UART_CTRL(a2)     # DIV = 0x1fff; TX_BUSY, RX_FULL, RX_OVR are read-only
    lw   x3, OFS_UART_CTRL(a2)
    li   x4, 0x1fff
    and  x3, x3, x4
    EXPECT x3, 0x1fff
    lw   x3, OFS_UART_CTRL(a2)
    srli x3, x3, 13
    andi x3, x3, 1
    EXPECT x3, 0                   # TX_BUSY: nothing sent
    sw   zero, OFS_UART_CTRL(a2)
    li   x1, -1
    sw   x1, OFS_SPI_CTRL(a2)      # CS_n = 1, D = 7; BUSY is read-only
    lw   x3, OFS_SPI_CTRL(a2)
    EXPECT x3, 0x0f
    li   x1, 1
    sw   x1, OFS_SPI_CTRL(a2)
    lw   x3, OFS_SPI_CTRL(a2)
    EXPECT x3, 1
    li   x1, -1
    sw   x1, OFS_IRQ_EN(a2)        # mstatus.MIE = mie.MEIE = 0: no interrupt
    lw   x3, OFS_IRQ_EN(a2)
    EXPECT x3, 0x1f
    sw   zero, OFS_IRQ_EN(a2)
    lw   x3, OFS_IRQ_EN(a2)
    EXPECT x3, 0
    li   x1, -1
    sw   x1, OFS_IRQ_PEND(a2)      # W1C on CMP and CAP (both 0), the rest read-only
    lw   x3, OFS_IRQ_PEND(a2)
    andi x3, x3, IRQ_CMP | IRQ_CAP | IRQ_TXIDLE
    EXPECT x3, IRQ_TXIDLE          # no match, no capture, TX idle
    li   x1, 0xffff
    sw   x1, OFS_GPIO_EDGE(a2)
    lw   x3, OFS_GPIO_EDGE(a2)
    EXPECT x3, 0xffff
    li   x1, 0x00a5c3              # bits above 15 not implemented
    sw   x1, OFS_GPIO_EDGE(a2)
    lw   x3, OFS_GPIO_EDGE(a2)
    EXPECT x3, 0xa5c3
    sw   zero, OFS_GPIO_EDGE(a2)
    li   x1, -1
    sw   x1, OFS_GPIO_EPEND(a2)    # W1C: clears any flag the edges above may have set
    sw   x1, 0x4c(a2)              # slots 19, 25 and 31: not implemented
    sw   x1, 0x64(a2)
    sw   x1, 0x7c(a2)
    lw   x3, 0x4c(a2)
    EXPECT x3, 0
    lw   x3, 0x64(a2)
    EXPECT x3, 0
    lw   x3, 0x7c(a2)
    EXPECT x3, 0
    lhu  x3, 0x7e(a2)
    EXPECT x3, 0
    SIG  x3

# ---------------------------------------------------------------------------------------------
    ABS  x1, sig_end
    bltu x1, a3, st_fail       # the signature area overflowed
    j    _pass

st_fail:
    mv   a0, a5                # the failing test id
    j    _fail

    .section .rodata
    .align 2
ldbuf:
    .byte 0x80, 0x81, 0x02, 0x83, 0x84, 0x05, 0x86, 0x07
    .byte 0xf8, 0x09, 0x7a, 0x8b, 0x0c, 0xfd, 0x0e, 0x8f
    .align 2
    .global _psram_window
_psram_window:                 # results outside .data/.bss (mkexpect.py): section 6's bytes
    .word 0x017fff78, 0x017fff80
    .word 0, 0

    .section .data
    .align 2
ldbuf_ram:
    .byte 0x80, 0x81, 0x02, 0x83, 0x84, 0x05, 0x86, 0x07
    .byte 0xf8, 0x09, 0x7a, 0x8b, 0x0c, 0xfd, 0x0e, 0x8f

    .section .bss
    .align 2
stbuf:
    .space 32
sig_area:
    .space 1280
sig_end:
