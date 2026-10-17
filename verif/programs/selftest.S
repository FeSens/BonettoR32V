# BonettoR32V selftest (docs/contract.md sections 3 and 4).
#
# Executes every RV32E instruction (ECALL is in the trap_* programs, since it halts), every branch
# taken and not taken, x0 writes, JALR with bit 0 set, a not-taken branch to a misaligned target,
# misaligned LW/LH/LHU/SW/SH including the 64 KB wraps at 0xFFFD..0xFFFF, data and fetch aliasing,
# every shift amount, FENCE variants, and GPIO reads and writes on every byte lane.
#
# Results also go to a signature area in RAM, so the pins-mode cosim (final RAM image) sees every
# computed value. The verdict is written to GPIO: 0xA5 pass, 0xEE fail; then EBREAK.
#
# Registers: a5 (x15) test id, a4 (x14) expected value, a3 (x13) signature pointer,
# a2 (x12) GPIO base in the GPIO section. x1..x11 are scratch.
#
# Built with -DSPIKE_XCHECK for the spike cross-check of the ISS: that build skips the sections
# spike can't model (64 KB wrap and aliasing, GPIO).

    .macro TEST id
    li   a5, \id
    .endm

    # reg == value, else fail
    .macro EXPECT reg, val
    li   a4, \val
    beq  \reg, a4, .Lok\@
    j    fail
.Lok\@:
    .endm

    # ra == rb, else fail
    .macro EQ ra, rb
    beq  \ra, \rb, .Leq\@
    j    fail
.Leq\@:
    .endm

    .macro SIG reg
    sw   \reg, 0(a3)
    addi a3, a3, 4
    .endm

    .macro TAKEN br, ra, rb
    \br  \ra, \rb, .Lt\@
    j    fail
.Lt\@:
    .endm

    .macro NOTTAKEN br, ra, rb
    \br  \ra, \rb, .Lnt\@
    j    .Lntok\@
.Lnt\@:
    j    fail
.Lntok\@:
    .endm

    .macro ABS rd, sym
    lui  \rd, %hi(\sym)
    addi \rd, \rd, %lo(\sym)
    .endm

    .section .text.start, "ax"
    .global _start
_start:
    # The core's registers start unknown: write every one before any read.
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
    ABS  sp, _stack_top
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
# 5. Loads and stores, aligned and misaligned (inside one buffer, no 64 KB wrap)
    TEST 5
    ABS  x1, ldbuf
    lb   x2, 0(x1)
    EXPECT x2, 0xffffff80
    lbu  x2, 0(x1)
    EXPECT x2, 0x80
    lb   x2, 2(x1)
    EXPECT x2, 0x02
    lh   x2, 0(x1)
    EXPECT x2, 0xffff8180
    lhu  x2, 0(x1)
    EXPECT x2, 0x8180
    lh   x2, 1(x1)
    EXPECT x2, 0x0281
    lh   x2, 3(x1)
    EXPECT x2, 0xffff8483
    lhu  x2, 3(x1)
    EXPECT x2, 0x8483
    lh   x2, 5(x1)
    EXPECT x2, 0xffff8605
    lw   x2, 0(x1)
    EXPECT x2, 0x83028180
    lw   x2, 1(x1)
    EXPECT x2, 0x84830281
    lw   x2, 2(x1)
    EXPECT x2, 0x05848302
    lw   x2, 3(x1)
    EXPECT x2, 0x86058483
    SIG  x2
    addi x3, x1, 8
    lw   x2, -5(x3)            # negative offset, misaligned
    EXPECT x2, 0x86058483
    lw   x0, 1(x1)             # load into x0 is discarded
    add  x2, x0, x0
    EXPECT x2, 0

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
    # Loads of the instruction's own bytes: the load still needs its own SPI READ (contract
    # section 6, fetch and load completeness), it can't reuse the fetch.
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

#ifndef SPIKE_XCHECK
# ---------------------------------------------------------------------------------------------
# 6. Misaligned accesses that wrap at 64 KB. They overwrite RAM 0x0000..0x0003, the reset
#    code, which never runs again.
    TEST 6
    li   x1, 0xfffc
    li   x2, 0x44332211
    sw   x2, 0(x1)             # 0xfffc..0xffff = 11 22 33 44
    li   x2, 0x88776655
    sw   x2, 0(x0)             # 0x0000..0x0003 = 55 66 77 88
    lw   x3, 2(x1)             # 0xfffe
    EXPECT x3, 0x66554433
    lw   x3, 3(x1)             # 0xffff
    EXPECT x3, 0x77665544
    lw   x3, 1(x1)             # 0xfffd
    EXPECT x3, 0x55443322
    lh   x3, 3(x1)             # 0xffff
    EXPECT x3, 0x5544
    lhu  x3, 3(x1)
    EXPECT x3, 0x5544
    li   x2, 0xddccbbaa
    sw   x2, 2(x1)             # 0xfffe: aa bb | cc dd at 0x0000
    lbu  x3, 2(x1)
    EXPECT x3, 0xaa
    lbu  x3, 3(x1)
    EXPECT x3, 0xbb
    lbu  x3, 0(x0)
    EXPECT x3, 0xcc
    lbu  x3, 1(x0)
    EXPECT x3, 0xdd
    lbu  x3, 2(x0)
    EXPECT x3, 0x77
    li   x2, 0xf234
    sh   x2, 3(x1)             # 0xffff = 34, 0x0000 = f2
    lh   x3, 3(x1)
    EXPECT x3, 0xfffff234
    lhu  x3, 3(x1)
    EXPECT x3, 0xf234
    li   x2, 0x0badf00d
    sw   x2, 3(x1)             # 0xffff, 0x0000, 0x0001, 0x0002
    lw   x3, 3(x1)
    EXPECT x3, 0x0badf00d
    lw   x3, 0(x0)
    EXPECT x3, 0x880badf0
    SIG  x3
    # addr[31] = 0 selects RAM for the whole access, even when addr + k reaches bit 31
    li   x4, 0x7ffffffe
    lw   x3, 0(x4)             # RAM 0xfffe, 0xffff, 0x0000, 0x0001
    EXPECT x3, 0xadf00daa
    li   x2, 0x13579bdf
    sw   x2, 0(x4)
    lw   x3, 2(x1)
    EXPECT x3, 0x13579bdf
    lhu  x3, 0(x0)
    EXPECT x3, 0x1357
    SIG  x3

# ---------------------------------------------------------------------------------------------
# 7. Aliasing: data and fetch use only address bits [15:0] (and addr[31] = 0 for data)
    TEST 7
    ABS  x1, stbuf
    li   x2, 0x10000
    add  x3, x1, x2            # stbuf + 64 KB
    li   x4, 0x600dcafe
    sw   x4, 16(x3)
    lw   x5, 16(x1)
    EQ   x5, x4
    li   x2, 0x7fff0000
    add  x3, x1, x2
    lw   x5, 16(x3)
    EQ   x5, x4
    li   x4, 0x1234
    sh   x4, 18(x3)
    lw   x5, 16(x1)
    EXPECT x5, 0x1234cafe
    SIG  x5
    # fetch alias: run alias_code at +64 KB and at 0xabcd0000 + offset
    ABS  x1, alias_code
    li   x2, 0x10000
    add  x1, x1, x2
    jalr ra, 0(x1)
    ABS  x6, alias_code
    li   x2, 0x10000
    add  x6, x6, x2
    EQ   x3, x6                # auipc saw the aliased pc
    addi x6, x6, 8
    EQ   x4, x6                # jal linked the aliased pc + 4
    SIG  x4
    ABS  x1, alias_code
    li   x2, 0xabcd0000
    add  x1, x1, x2
    jalr ra, 0(x1)
    ABS  x6, alias_code
    li   x2, 0xabcd0000
    add  x6, x6, x2
    EQ   x3, x6
    SIG  x3
    j    alias_done
alias_code:
    auipc x3, 0
    jal  x4, 1f
1:  beq  x0, x0, 2f
    j    fail
2:  jalr x0, 0(ra)
alias_done:
    # JAL and branches with offsets whose low 16 bits are small: the target aliases to code a few
    # bytes ahead, while the 32-bit pc moves by the full offset (J immediate bits 16..20, the
    # sign included; riscv-arch-test's jal-01 cases of this kind don't fit a 64 KB RAM)
    auipc x6, 0                # P
    jal  x5, 3f + 0xf0000      # P + 4 -> P + 0xf0010 (runs 3f at pc 3f + 0xf0000)
    j    fail
    j    fail
3:  auipc x7, 0                # P + 0x10 + 0xf0000
    sub  x7, x7, x6
    EXPECT x7, 0x000f0010
    sub  x5, x5, x6
    EXPECT x5, 8
    SIG  x7
    auipc x6, 0                # Q (= its link-time address + 0xf0000)
    jal  x5, 4f - 0x100000     # offset 0x10 - 0x100000: to Q + 0x14 - 0x100000
    j    fail
    j    fail
    j    fail
4:  auipc x7, 0
    sub  x7, x7, x6
    EXPECT x7, 0xfff00014
    SIG  x7
    jal  x0, alias_back + 0x10000  # pc is link-time - 0x10000 here: back to link-time
    j    fail
alias_back:
    auipc x7, 0
    ABS  x6, alias_back
    EQ   x7, x6
#endif

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
    j    fail
91: ABS  x2, jal_link
    EQ   x1, x2
    SIG  x1
    jal  x0, 92f
    j    fail
92: ABS  x5, 93f
    addi x5, x5, 1             # bit 0 set: JALR clears it
    jalr x6, 0(x5)
jalr_link:
    j    fail
93: ABS  x2, jalr_link
    EQ   x6, x2
    ABS  x7, 94f
    addi x7, x7, -8
    jalr x7, 8(x7)             # rd == rs1: the target uses the old value
jalr_link2:
    j    fail
94: ABS  x2, jalr_link2
    EQ   x7, x2
    ABS  x8, 95f
    addi x8, x8, 2047
    jalr x9, -2047(x8)         # large negative offset
    j    fail
95: ABS  x8, 96f
    addi x8, x8, 3
    jalr x0, -3(x8)            # negative offset
    j    fail
96: SIG  x9

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

#ifndef SPIKE_XCHECK
# ---------------------------------------------------------------------------------------------
# 12. GPIO: writes and reads on every byte lane
    TEST 12
    lui  a2, 0x80000
    li   x1, 0x3c
    sb   x1, 0(a2)             # uo_out = 0x3c
    li   x1, 0xff
    sb   x1, 1(a2)             # ignored
    sb   x1, 2(a2)             # ignored
    sb   x1, 3(a2)             # ignored
    li   x1, 0x12345678
    sw   x1, 0(a2)             # uo_out = 0x78
    li   x1, 0xabcd
    sh   x1, 4(a2)             # uo_out = 0xcd
    sh   x1, 0(a2)             # same value: no change
    li   x1, 0x5a
    sb   x1, 4(a2)             # 0x80000004: uo_out = 0x5a
    li   x2, 0x80001230
    li   x1, 0x69
    sb   x1, 0(x2)             # uo_out = 0x69
    li   x2, 0xfffffffc
    li   x1, 0x96
    sb   x1, 0(x2)             # uo_out = 0x96
    li   x2, 0xc0000001
    li   x1, 0x11
    sb   x1, 0(x2)             # ignored
    sb   x1, 1(x2)             # 0xc0000002: ignored
    # reads: byte 0 of each word is ui_in, every other byte is 0. IO accesses wider than a byte
    # are only specified at addr[1:0] = 0.
    lw   x3, 0(a2)
    srli x4, x3, 8
    EXPECT x4, 0
    SIG  x3
    lbu  x3, 1(a2)
    EXPECT x3, 0
    lbu  x3, 2(a2)
    EXPECT x3, 0
    lb   x3, 3(a2)
    EXPECT x3, 0
    lhu  x3, 4(a2)
    srli x4, x3, 8
    EXPECT x4, 0
    lh   x3, 0(a2)
    srli x4, x3, 8
    EXPECT x4, 0
    SIG  x3
    lb   x3, 0(a2)             # sign-extended ui_in
    SIG  x3
    lbu  x3, 4(a2)
    SIG  x3
    li   x2, 0xfffffffc
    lbu  x3, 0(x2)
    SIG  x3
    li   x2, 0x80000005
    lbu  x3, 0(x2)
    EXPECT x3, 0
    lw   x3, 4(a2)
    srli x4, x3, 8
    EXPECT x4, 0
    # echo ui_in to uo_out, then a word store whose upper bytes must not matter
    lbu  x3, 0(a2)
    sb   x3, 0(a2)
    lw   x3, 0(a2)
    li   x4, 0xffffff00
    or   x3, x3, x4
    sw   x3, 0(a2)
#endif

# ---------------------------------------------------------------------------------------------
pass:
    ABS  x1, verdict
    li   x2, 0xa5
    sw   x2, 0(x1)
    lui  a2, 0x80000
    sb   x2, 0(a2)
    ebreak
    # Guard: a core that doesn't halt at EBREAK makes visible changes.
    li   x2, 0x5e
    sb   x2, 0(a2)
    sw   x2, 0(x1)
    li   x2, 0xe5
    sb   x2, 0(a2)
1:  j    1b

fail:
    ABS  x1, verdict
    sw   a5, 4(x1)             # failing test id
    li   x2, 0xee
    sw   x2, 0(x1)
    lui  a2, 0x80000
    sb   x2, 0(a2)
    ebreak
    li   x2, 0x5e
    sb   x2, 0(a2)
    li   x2, 0xe5
    sb   x2, 0(a2)
1:  j    1b

    .section .data
    .align 2
ldbuf:
    .byte 0x80, 0x81, 0x02, 0x83, 0x84, 0x05, 0x86, 0x07
    .byte 0xf8, 0x09, 0x7a, 0x8b, 0x0c, 0xfd, 0x0e, 0x8f

    .section .bss
    .align 2
verdict:
    .space 8
stbuf:
    .space 32
sig_area:
    .space 4096
