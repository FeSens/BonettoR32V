# CSR test, contract v3 section 4: every field of every listed CSR, every Zicsr instruction form
# (CSRRW/S/C, the immediate forms, rd = x0, rs1 = x0 / zimm = 0 not writing, rd = rs1), MRET
# without a trap (pc = mepc, MIE = MPIE, MPIE = 1, mepc bits 1:0), mip.MEIP following
# IRQ_PEND & IRQ_EN, and WFI falling through when mip.MEIP & mie.MEIE = 1 (with MIE = 0).
#
# No interrupt is ever taken: mstatus.MIE, mie.MEIE and mip.MEIP are never all 1 together. No trap
# either (the trap_* programs check trap entry). The CSRs 0xF11-0xF14 are only read (writing them
# is UNSPECIFIED, ruling R15). Linked with crt0.S, which wrote mtvec, mstatus and mie.
#
# a5 = test id (the failure id on GPIO before 0xEE), a4 = expected value, a2 = IO base.
#include "bonetto.h"

    .macro TEST id
    li   a5, \id
    .endm

    .macro EXPECT reg, val
    li   a4, \val
    beq  \reg, a4, .Lok\@
    j    csr_fail
.Lok\@:
    .endm

    # read csr into t1 and compare
    .macro READS csr, val
    csrr t1, \csr
    EXPECT t1, \val
    .endm

    .macro ABS rd, sym
    lui  \rd, %hi(\sym)
    addi \rd, \rd, %lo(\sym)
    .endm

    # reg == the address of sym
    .macro EXPECTSYM reg, sym
    ABS  a4, \sym
    beq  \reg, a4, .Lok\@
    j    csr_fail
.Lok\@:
    .endm

    .text
    .global main
main:
    lui  a2, 0x80000

# 1. mstatus: MIE (3) and MPIE (7) read/write, MPP (12:11) reads 3, other bits 0. mie.MEIE = 0
#    (crt0), so MIE = 1 is safe.
    TEST 1
    li   t0, -1
    csrw mstatus, t0
    READS mstatus, 0x1888
    csrw mstatus, zero
    READS mstatus, 0x1800
    csrrsi t1, mstatus, 8      # rd = the old value, then MIE = 1
    EXPECT t1, 0x1800
    READS mstatus, 0x1808
    li   t0, 0x80
    csrrs t1, mstatus, t0      # MPIE = 1
    EXPECT t1, 0x1808
    READS mstatus, 0x1888
    csrrci t1, mstatus, 8
    EXPECT t1, 0x1888
    READS mstatus, 0x1880
    csrrc t1, mstatus, t0
    EXPECT t1, 0x1880
    READS mstatus, 0x1800
    li   t0, 0xffffff77        # every bit but MIE and MPIE
    csrrw t1, mstatus, t0
    EXPECT t1, 0x1800
    READS mstatus, 0x1800
    li   t0, 0x88
    csrw mstatus, t0
    csrrs t1, mstatus, x0      # rs1 = x0: no write
    EXPECT t1, 0x1888
    csrrc t1, mstatus, x0
    EXPECT t1, 0x1888
    csrrsi t1, mstatus, 0      # zimm = 0: no write
    EXPECT t1, 0x1888
    csrrci t1, mstatus, 0
    EXPECT t1, 0x1888
    READS mstatus, 0x1888
    csrrw x0, mstatus, zero    # rd = x0
    READS mstatus, 0x1800
    csrrwi x0, mstatus, 8
    READS mstatus, 0x1808
    csrrwi t1, mstatus, 0
    EXPECT t1, 0x1808
    READS mstatus, 0x1800

# 2. misa: reads 0, writes ignored
    TEST 2
    READS misa, 0
    li   t0, -1
    csrw misa, t0
    READS misa, 0
    csrrs t1, misa, t0
    EXPECT t1, 0
    csrrsi t1, misa, 0x1f
    EXPECT t1, 0
    READS misa, 0

# 3. mie: MEIE (11) read/write, other bits 0. mstatus.MIE = 0 here.
    TEST 3
    li   t0, -1
    csrw mie, t0
    READS mie, 0x800
    csrw mie, zero
    READS mie, 0
    li   t0, 0x800
    csrrs t1, mie, t0
    EXPECT t1, 0
    READS mie, 0x800
    csrrc t1, mie, t0
    EXPECT t1, 0x800
    READS mie, 0
    li   t0, 0xfffff7ff
    csrw mie, t0
    READS mie, 0
    csrrsi t1, mie, 0x1f       # bits 4:0 don't exist
    EXPECT t1, 0
    READS mie, 0

# 4. mtvec: bits 31:2 read/write (direct mode), bits 1:0 read 0
    TEST 4
    csrr s0, mtvec             # crt0's _default_trap
    EXPECTSYM s0, _default_trap
    li   t0, -1
    csrw mtvec, t0
    READS mtvec, 0xfffffffc
    li   t0, 0x12345677
    csrrw t1, mtvec, t0
    EXPECT t1, 0xfffffffc
    READS mtvec, 0x12345674
    csrrci t1, mtvec, 0x14
    EXPECT t1, 0x12345674
    READS mtvec, 0x12345660
    csrrsi t1, mtvec, 0x1f
    EXPECT t1, 0x12345660
    READS mtvec, 0x1234567c
    csrw mtvec, zero
    READS mtvec, 0
    csrw mtvec, s0
    csrr t1, mtvec
    EXPECTSYM t1, _default_trap

# 5. mscratch: 32 bits read/write
    TEST 5
    li   t0, 0xdeadbeef
    csrw mscratch, t0
    READS mscratch, 0xdeadbeef
    li   t0, -1
    csrrw t1, mscratch, t0
    EXPECT t1, 0xdeadbeef
    READS mscratch, 0xffffffff
    csrrci t1, mscratch, 0x1f
    EXPECT t1, 0xffffffff
    READS mscratch, 0xffffffe0
    li   t0, 0x0f0f0f0f
    csrrc t1, mscratch, t0
    EXPECT t1, 0xffffffe0
    READS mscratch, 0xf0f0f0e0
    csrrsi t1, mscratch, 3
    READS mscratch, 0xf0f0f0e3
    li   t0, 0x80000001
    csrrs t1, mscratch, t0
    EXPECT t1, 0xf0f0f0e3
    READS mscratch, 0xf0f0f0e3
    csrw mscratch, zero
    READS mscratch, 0
    # rd = rs1: the old value goes to rd, the new value comes from the old rs1
    li   t0, 0x55
    csrw mscratch, t0
    li   t1, 0xaa
    csrrw t1, mscratch, t1
    EXPECT t1, 0x55
    READS mscratch, 0xaa
    li   t1, 0x0f
    csrrs t1, mscratch, t1
    EXPECT t1, 0xaa
    READS mscratch, 0xaf
    li   t1, 0xa0
    csrrc t1, mscratch, t1
    EXPECT t1, 0xaf
    READS mscratch, 0x0f

# 6. mepc: bits 31:2 read/write, bits 1:0 read 0
    TEST 6
    li   t0, -1
    csrw mepc, t0
    READS mepc, 0xfffffffc
    li   t0, 0x01000003
    csrrw t1, mepc, t0
    EXPECT t1, 0xfffffffc
    READS mepc, 0x01000000
    csrrsi t1, mepc, 0x1e
    EXPECT t1, 0x01000000
    READS mepc, 0x0100001c
    li   t0, 0x01000010
    csrrc t1, mepc, t0
    EXPECT t1, 0x0100001c
    READS mepc, 0x0000000c
    csrw mepc, zero
    READS mepc, 0

# 7. mcause: bit 31 and bits 3:0 read/write, other bits 0
    TEST 7
    li   t0, -1
    csrw mcause, t0
    READS mcause, 0x8000000f
    li   t0, 0x7ffffff0
    csrw mcause, t0
    READS mcause, 0
    li   t0, 0x8000000b
    csrw mcause, t0
    READS mcause, 0x8000000b
    csrrci t1, mcause, 0x1b
    EXPECT t1, 0x8000000b
    READS mcause, 0x80000000
    csrrsi t1, mcause, 0x15    # bit 4 doesn't exist
    EXPECT t1, 0x80000000
    READS mcause, 0x80000005
    li   t0, 0x80000000
    csrrc t1, mcause, t0
    READS mcause, 5
    csrw mcause, zero
    READS mcause, 0

# 8. mtval: reads 0, writes ignored
    TEST 8
    READS mtval, 0
    li   t0, -1
    csrw mtval, t0
    READS mtval, 0
    csrrs t1, mtval, t0
    EXPECT t1, 0
    csrrwi t1, mtval, 7
    EXPECT t1, 0
    READS mtval, 0

# 9. mip: MEIP (11) is the interrupt line OR(IRQ_PEND & IRQ_EN), read-only; other bits 0.
#    mstatus.MIE = 0 throughout, so a pending line is never taken.
    TEST 9
    csrw mstatus, zero
    csrw mie, zero
    READS mip, 0               # IRQ_EN = 0: no source enabled
    li   t0, -1
    csrw mip, t0               # writes ignored
    READS mip, 0
    csrrs t1, mip, t0
    EXPECT t1, 0
    csrrsi t1, mip, 0x1f
    EXPECT t1, 0
    READS mip, 0
    # IRQ_PEND.TXIDLE = !TX_BUSY = 1 while the UART is idle
    li   t0, IRQ_TXIDLE
    sw   t0, OFS_IRQ_EN(a2)
    nop                        # the line is sampled at most 3 cycles late
    nop
    nop
    nop
    READS mip, 0x800
    li   t0, 0x800
    csrw mip, t0               # still read-only
    READS mip, 0x800
    # WFI with mip.MEIP & mie.MEIE = 1 doesn't wait (MIE = 0: nothing is taken)
    csrs mie, t0
    wfi
    READS mip, 0x800
    READS mie, 0x800
    READS mstatus, 0x1800
    csrw mie, zero
    sw   zero, OFS_IRQ_EN(a2)
    nop
    nop
    nop
    nop
    READS mip, 0
    # sources with nothing pending: CMP (timer off), CAP (capture off), GPIO (no edge enabled)
    li   t0, IRQ_CMP | IRQ_CAP | IRQ_GPIO
    sw   t0, OFS_IRQ_EN(a2)
    nop
    nop
    nop
    nop
    READS mip, 0
    sw   zero, OFS_IRQ_EN(a2)

# 10. mvendorid, marchid, mimpid, mhartid read 0 (no writes: UNSPECIFIED)
    TEST 10
    READS mvendorid, 0
    READS marchid, 0
    READS mimpid, 0
    READS mhartid, 0
    li   t1, 0x77
    csrrs t1, mhartid, x0      # rs1 = x0: a read
    EXPECT t1, 0
    li   t1, 0x77
    csrrci t1, mvendorid, 0    # zimm = 0: a read
    EXPECT t1, 0

# 11. MRET without a trap: pc = mepc, MIE = MPIE, MPIE = 1. mie.MEIE = 0, so MIE = 1 is safe.
    TEST 11
    ABS  t0, 1f
    csrw mepc, t0
    li   t0, 0x80              # MPIE = 1, MIE = 0
    csrw mstatus, t0
    mret
    j    csr_fail
1:  READS mstatus, 0x1888      # MIE = 1 (from MPIE), MPIE = 1
    ABS  t0, 2f
    csrw mepc, t0
    li   t0, 0x08              # MPIE = 0, MIE = 1
    csrw mstatus, t0
    mret
    j    csr_fail
2:  READS mstatus, 0x1880      # MIE = 0, MPIE = 1
    ABS  t0, 3f
    addi t0, t0, 3             # mepc bits 1:0 read 0: MRET goes to 3f
    csrw mepc, t0
    csrr t1, mepc
    EXPECTSYM t1, 3f
    mret
    j    csr_fail
3:  READS mstatus, 0x1888
    csrw mstatus, zero
    READS mstatus, 0x1800

    j    _pass

csr_fail:
    mv   a0, a5
    j    _fail
