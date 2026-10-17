# boot_from_each_mode: after the reset sequence the first fetch comes from flash at 0; then a
# flash load, a PSRAM store and load-back (the PSRAM must have been reset), and the verdict.
# Run under all four Pmod power-up modes.
    .section .text.start, "ax"
    .global _start
_start:
    auipc x5, 0                 # x5 = 0: the first instruction runs at pc 0
    bnez  x5, fail
    lui   x6, %hi(word)
    lw    x7, %lo(word)(x6)     # flash load
    li    x8, 0x13579bdf
    bne   x7, x8, fail
    li    x9, 0x01000100        # PSRAM
    sw    x8, 0(x9)
    lw    x10, 0(x9)
    bne   x10, x8, fail
    sb    x8, 5(x9)
    lbu   x11, 5(x9)
    li    x12, 0xdf
    bne   x11, x12, fail
    lui   x13, 0x80000
    li    x14, 0x5a
    sw    x14, 0(x13)
1:  jal   x0, 1b
fail:
    lui   x13, 0x80000
    li    x14, 0xee
    sw    x14, 0(x13)
2:  jal   x0, 2b
    .align 2
word:
    .word 0x13579bdf
