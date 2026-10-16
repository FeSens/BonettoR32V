# Fallback timing program for run_formal.py (used only when neither the cosim's
# --timing-json nor build/programs/selftest.hex is available). One or more of each
# instruction class, including the slow cases: 4-byte misaligned loads and stores
# that wrap at 64 KB, shifts by 31, GPIO accesses. Ends with EBREAK.
# riscv32-unknown-elf-gcc -march=rv32e -mabi=ilp32e -nostdlib -Ttext=0 -o p.elf timing_prog.S
	.text
	.globl _start
_start:
	addi  x1, x0, 0x7ff
	add   x2, x1, x1
	sub   x3, x2, x1
	xor   x4, x3, x2
	or    x5, x4, x1
	and   x6, x5, x2
	slt   x7, x6, x5
	sltu  x8, x6, x5
	slti  x9, x1, 3
	sltiu x10, x1, 3
	xori  x11, x1, -1
	ori   x12, x1, 0x55
	andi  x13, x1, 0x0f0
	addi  x14, x0, 31
	sll   x15, x1, x14
	srl   x15, x15, x14
	sra   x15, x11, x14
	slli  x15, x1, 31
	srli  x15, x15, 31
	srai  x15, x11, 31
	lui   x15, 0x12345
	auipc x15, 0x1
	fence
	# stores and loads in RAM, aligned
	addi  x2, x0, 0x400
	sw    x1, 0(x2)
	sh    x1, 4(x2)
	sb    x1, 6(x2)
	lw    x3, 0(x2)
	lh    x3, 4(x2)
	lhu   x3, 4(x2)
	lb    x3, 6(x2)
	lbu   x3, 6(x2)
	# misaligned, wrapping at 64 KB
	lui   x2, 0x10
	addi  x2, x2, -2
	sw    x1, 0(x2)
	lw    x3, 0(x2)
	sh    x1, 1(x2)
	lh    x3, 1(x2)
	# GPIO
	lui   x4, 0x80000
	sb    x1, 0(x4)
	sw    x1, 0(x4)
	lw    x5, 0(x4)
	lbu   x5, 0(x4)
	# branches taken and not taken, jumps
	beq   x0, x0, 1f
1:	bne   x0, x0, 2f
2:	blt   x0, x1, 3f
3:	bge   x1, x0, 4f
4:	bltu  x0, x1, 5f
5:	bgeu  x1, x0, 6f
6:	jal   x1, 7f
7:	auipc x2, 0
	jalr  x1, 12(x2)
	nop
	ebreak
