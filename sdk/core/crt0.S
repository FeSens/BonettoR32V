# BonettoR32V SDK startup (docs/contract.md sections 3-4). After reset the core runs from flash
# at pc = 0. Nothing here relies on power-up values (contract 1): every register is cleared,
# mtvec is set, .data is copied from its flash image and .bss is cleared (PSRAM starts arbitrary).
#   1. x1..x15 = 0; mtvec = r32v_trap (sdk/core/trap.S); sp = _stack_top (just below the
#      core region the CPU keeps in PSRAM, 0x017F_FF80).
#   2. .data from flash, .bss cleared, then C++ constructors (.init_array), if any.
#   3. r32v_init() (timebase, interrupts on), then main() (the SDK's weak main calls setup() once
#      and loop() forever). If main returns, the core parks in a self-loop.
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
    la   t0, r32v_trap
    csrw mtvec, t0
    la   sp, _stack_top
    la   t0, _data_lma
    la   t1, _data_start
    la   t2, _data_end
1:  bgeu t1, t2, 2f
    lw   a0, 0(t0)
    sw   a0, 0(t1)
    addi t0, t0, 4
    addi t1, t1, 4
    j    1b
2:  la   t1, _bss_start
    la   t2, _bss_end
3:  bgeu t1, t2, 4f
    sw   zero, 0(t1)
    addi t1, t1, 4
    j    3b
4:  la   s0, __init_array_start
    la   s1, __init_array_end
5:  bgeu s0, s1, 6f
    lw   t0, 0(s0)
    jalr t0
    addi s0, s0, 4
    j    5b
6:  call r32v_init
    call main
    .global _park
_park:
    j    _park
