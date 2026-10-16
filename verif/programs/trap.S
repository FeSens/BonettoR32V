# Fatal-trap programs (docs/contract.md section 4). Built six times, once per TRAP_KIND:
#   1 ecall   2 jal to pc+6   3 jalr with target[1] = 1 (after bit 0 is cleared)
#   4 taken branch to pc+6    5 the all-zeros word    6 the all-ones word
# Each sets up visible state (GPIO and RAM), then executes the trapping instruction. The trap must
# retire with trap = halt = 1 and no register or memory write, and the core must never retire or
# write again. The code after it (the guard) must never run: it changes GPIO and RAM.
#
# The trapping instructions are hand-encoded words so the assembler can't reject or relax them.

    .section .text.start, "ax"
    .global _start
_start:
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
    lui  a2, 0x80000
    li   x1, 0x3c
    sb   x1, 0(a2)             # uo_out = 0x3c
    lui  x3, %hi(buf)
    addi x3, x3, %lo(buf)
    li   x4, 0x11223344
    sw   x4, 0(x3)
    li   x1, 0x55              # a JAL/JALR trap must not overwrite x1
    li   x6, 0x77
    lui  x5, %hi(target)
    addi x5, x5, %lo(target)   # 4-byte aligned

#if TRAP_KIND == 1
    ecall
#elif TRAP_KIND == 2
    .word 0x006000ef           # jal x1, pc+6
#elif TRAP_KIND == 3
    .word 0x003280e7           # jalr x1, 3(x5): (target + 3) & ~1 = target + 2
#elif TRAP_KIND == 4
    .word 0x00630363           # beq x6, x6, pc+6 (taken)
#elif TRAP_KIND == 5
    .word 0x00000000
#elif TRAP_KIND == 6
    .word 0xffffffff
#else
#error "TRAP_KIND must be 1..6"
#endif

    # Guard: never executes.
    .align 2
target:
    li   x7, 0xee
    sb   x7, 0(a2)
    sw   x7, 0(x3)
    sw   x1, 4(x3)
    ebreak
1:  j    1b

    .section .bss
    .align 2
buf:
    .space 16
