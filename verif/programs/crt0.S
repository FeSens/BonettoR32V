# Startup for C test programs. Execution starts at pc = 0 after reset.
# Registers start unknown on the core (contract section 1), so clear them all first:
# otherwise gcc's prologues would store unknown callee-saved values into RAM.
    .section .text.start, "ax"
    .global _start
_start:
    li   x1, 0
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
    la   sp, _stack_top
    # .bss is already zero in the RAM image; clear it anyway so programs never depend on that.
    la   t0, _bss_start
    la   t1, _bss_end
1:  bgeu t0, t1, 2f
    sw   zero, 0(t0)
    addi t0, t0, 4
    j    1b
2:  call main
    .global _halt
_halt:
    ebreak
    # Guard: a core that doesn't halt at EBREAK runs into visible side effects.
    lui  t0, 0x80000
    li   t1, 0x5e
    sb   t1, 0(t0)
    li   t1, 0xe5
    sb   t1, 0(t0)
    sw   t1, -4(sp)
3:  j    3b
