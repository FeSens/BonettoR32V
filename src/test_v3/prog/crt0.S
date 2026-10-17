# Seed unit-test startup (contract v3): clear x1..x15, mtvec = _default_trap, copy .data from
# flash to PSRAM, clear .bss, call main. main's result: 0 -> uo_out = 0x5A, else 0xEE (with
# ALTFN = 0), then the end self-loop (jal x0, 0).
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
    la   t0, _default_trap
    csrw mtvec, t0
    la   sp, _stack_top
    la   t0, _data_load
    la   t1, _data_start
    la   t2, _data_end
1:  bgeu t1, t2, 2f
    lw   a0, 0(t0)
    sw   a0, 0(t1)
    addi t0, t0, 4
    addi t1, t1, 4
    j    1b
2:  la   t0, _bss_start
    la   t1, _bss_end
3:  bgeu t0, t1, 4f
    sw   zero, 0(t0)
    addi t0, t0, 4
    j    3b
4:  call main
    li   t0, 0x5a
    beqz a0, 5f
    .global _fail
_fail:
    li   t0, 0xee
5:  lui  t1, 0x80000
    sw   zero, 8(t1)          # ALTFN = 0
    sw   t0, 0(t1)            # GPIO_OUT = verdict
    .global _end_loop
_end_loop:
    jal  x0, _end_loop
    .align 2
    .global _default_trap
_default_trap:
    li   t0, 0xee
    j    5b
