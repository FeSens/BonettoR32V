"""Smoke test for the BonettoR32V seed core: cocotb 2.1 with Icarus, at the pins.

It assembles RV32E programs with riscv32-unknown-elf-gcc and runs them from a Python
23LC512 model. The model checks the SPI waveform rules of docs/contract.md section 2 on
every cycle and drives noise on MISO whenever the contract says MISO is undefined.

What it checks:
- Main program: ALU ops, shifts, LUI/AUIPC, branches, JAL/JALR, FENCE, loads and stores of
  every width (aligned, misaligned, and wrapping at 0xFFFF), GPIO in and out, then EBREAK.
  Results land in RAM; the whole 64 KB image and the uo_out trace are compared with
  expected values written down here (no ISS).
- Trap programs: every fatal-trap class and a set of reserved encodings (strict decoding).
  Each must halt with no register, RAM or GPIO effect and no SPI traffic afterwards.
- Reset values, uio_oe, unused outputs, the latency cap, and random power-up state.
- In the RISCV_FORMAL build: RVFI framing against the observed SPI transactions (fetch and
  data bytes, transaction order, IO accesses off the bus), PC chaining, register read
  consistency, GPIO timing, halt behavior, and per-class cycle counts.

Run:  .venv/bin/python -m pytest -v src/test_seed_smoke.py
  or: .venv/bin/python src/test_seed_smoke.py
Build outputs go to build/seed_smoke/.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import zlib
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
BUILD = ROOT / "build" / "seed_smoke"
CROSS = "riscv32-unknown-elf-"
TOP = "tt_um_fesens_r32v"

LATENCY_CAP = 1024      # contract section 4
IDLE_HALT = 1500        # CS_n high this long after traffic means the core halted
RESET_CYCLES = 6
RESULTS = 0x2800        # main program result words
EBREAK = 0x00100073


def u32(x: int) -> int:
    return x & 0xFFFFFFFF


def s32(x: int) -> int:
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x & 0x80000000 else x


def sext(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return u32(v - (1 << bits)) if v >> (bits - 1) else v


# --------------------------------------------------------------------------------------------
# Program building
# --------------------------------------------------------------------------------------------

def assemble(name: str, asm: str) -> tuple[bytearray, dict[str, int]]:
    """Assemble and link at address 0; return the 64 KB image and the symbol table."""
    d = BUILD / "progs"
    d.mkdir(parents=True, exist_ok=True)
    src, elf, binf = d / f"{name}.S", d / f"{name}.elf", d / f"{name}.bin"
    src.write_text(asm)
    subprocess.run(
        [CROSS + "gcc", "-march=rv32e", "-mabi=ilp32e", "-nostdlib", "-nostartfiles",
         "-static", "-Wl,-Ttext=0", "-Wl,--no-relax", "-Wl,-e,_start", "-Wl,--build-id=none",
         "-o", str(elf), str(src)],
        check=True)
    subprocess.run([CROSS + "objcopy", "-O", "binary", "-j", ".text", str(elf), str(binf)],
                   check=True)
    data = binf.read_bytes()
    assert len(data) <= 65536, f"{name}: image is {len(data)} bytes"
    img = bytearray(65536)
    img[:len(data)] = data
    syms: dict[str, int] = {}
    out = subprocess.run([CROSS + "nm", str(elf)], check=True, capture_output=True, text=True)
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            syms[parts[2]] = int(parts[0], 16)
    return img, syms


ASM_HEADER = """
    .option norvc
    .macro RES reg
    sw \\reg, 0(s1)
    addi s1, s1, 4
    .endm
    .section .text
    .globl _start
"""

# Load data at 0x3000, a 0xEE-filled store area at 0x3100, wrap bytes at 0xFFFC.
LD_DATA = [0x80, 0x01, 0xFE, 0x7F, 0x11, 0x22, 0x33, 0x44,
           0x55, 0x66, 0x77, 0x88, 0x99, 0xAA, 0xBB, 0xCC]
WRAP_DATA = [0xC1, 0xC2, 0xC3, 0xC4]
UI_START = 0xA5
A_, B_, N_ = 0x12345678, 0x0F0F0F0F, 0x80000001


class MainProgram:
    """The main program, its expected result words, RAM image changes and uo_out trace."""

    def __init__(self) -> None:
        self.cases: list[tuple[str, str, list]] = []
        self.store_area = bytearray([0xEE] * 32)        # mirror of 0x3100..0x311F
        self.wrap: dict[int, int] = {}                  # mirror of 0xFFFC..0x0003, set later
        self.uo_trace = [0x3C, 0xC3, 0xEF, 0x99, 0x42, 0x11, 0x23, 0x24, 0xD0]
        self._build_cases()
        body = []
        for name, asm, exps in self.cases:
            body.append(f"# ---- {name}")
            body.append(asm)
        self.asm = ASM_HEADER + f"""
_start:
    j start
    .org 0x100
start:
    li s1, {RESULTS:#x}
{chr(10).join(body)}
    li s0, 0x80000000
    li a0, 0xd0
    sb a0, 0(s0)
end_ebreak:
    ebreak

fn_ra:
    mv a2, ra
    ret
fn_t0:
    mv a2, t0
    jr t0
fn_66:
    li a2, 0x66
    ret
fn_t1:
    mv a2, t1
    jr t1
code_end:

    .org 0x3000
    .byte {", ".join(f"{b:#x}" for b in LD_DATA)}
    .org 0x3100
    .fill 32, 1, 0xee

    .org 0x7400
back_tgt:
    li a2, 0x55
    ret
    .org 0x8000
far_fn:
    beq zero, zero, back_tgt

    .org 0xfffc
    .byte {", ".join(f"{b:#x}" for b in WRAP_DATA)}
"""

    def case(self, name: str, asm: str, *exps) -> None:
        """asm leaves its result in a2 (stored by RES), or calls RES itself (len(exps) > 1)."""
        if len(exps) == 1:
            asm = asm + "\nRES a2"
        self.cases.append((name, asm, list(exps)))

    def setup(self, name: str, asm: str) -> None:
        self.cases.append((name, asm, []))

    def expected_words(self, syms: dict[str, int], img0: bytes) -> list[tuple[str, int]]:
        out = []
        for name, _, exps in self.cases:
            for i, e in enumerate(exps):
                v = e(syms, img0) if callable(e) else e
                out.append((f"{name}[{i}]" if len(exps) > 1 else name, u32(v)))
        return out

    def _build_cases(self) -> None:
        c, st = self.case, self.setup
        A, B, N = A_, B_, N_

        # ---------------- ALU, register-register
        st("alu_setup", f"li a0, {A:#x}\nli a1, {B:#x}\nli a3, {N:#x}")
        c("add", "add a2, a0, a1", u32(A + B))
        c("sub", "sub a2, a0, a1", u32(A - B))
        c("sub_neg", "sub a2, a1, a0", u32(B - A))
        c("add_wrap", "li a4, -1\nli a5, 2\nadd a2, a4, a5", 1)
        c("xor", "xor a2, a0, a1", A ^ B)
        c("or", "or a2, a0, a1", A | B)
        c("and", "and a2, a0, a1", A & B)
        for amt_reg in (0x23, 31, 0x20, 0x41, 16):
            amt = amt_reg & 31
            c(f"sll_{amt_reg:#x}", f"li a4, {amt_reg}\nsll a2, a3, a4", u32(N << amt))
            c(f"srl_{amt_reg:#x}", f"li a4, {amt_reg}\nsrl a2, a3, a4", N >> amt)
            c(f"sra_{amt_reg:#x}", f"li a4, {amt_reg}\nsra a2, a3, a4", u32(s32(N) >> amt))
        c("sra_pos", "li a4, 4\nsra a2, a0, a4", A >> 4)
        c("slt_neg_pos", "slt a2, a3, a0", 1)
        c("slt_pos_neg", "slt a2, a0, a3", 0)
        c("slt_eq", "slt a2, a0, a0", 0)
        c("sltu_big_small", "sltu a2, a3, a0", 0)
        c("sltu_small_big", "sltu a2, a0, a3", 1)
        c("sltu_x0", "sltu a2, zero, a0", 1)
        c("add_x0_src", "add a2, zero, a0", A)
        c("x0_write", "add zero, a0, a1\naddi zero, zero, 5\nlui zero, 0x12345\nmv a2, zero", 0)

        # ---------------- ALU, immediates, LUI, AUIPC
        c("addi_min", "addi a2, a0, -2048", u32(A - 2048))
        c("addi_max", "addi a2, a0, 2047", u32(A + 2047))
        c("slti_1", "slti a2, a3, -1", 1)
        c("slti_0", "slti a2, a0, 2047", 0)
        c("slti_negneg", "li a4, -5\nslti a2, a4, -4", 1)
        c("sltiu_m1", "sltiu a2, a0, -1", 1)
        c("sltiu_big", "sltiu a2, a3, 5", 0)
        c("seqz", "li a4, 0\nsltiu a2, a4, 1", 1)
        c("xori_not", "xori a2, a0, -1", u32(~A))
        c("xori", "xori a2, a0, 0x555", A ^ 0x555)
        c("ori", "ori a2, a0, 0x700", A | 0x700)
        c("ori_neg", "ori a2, a0, -256", A | 0xFFFFFF00)
        c("andi", "andi a2, a0, 0x0f0", A & 0xF0)
        c("andi_neg", "andi a2, a0, -16", A & 0xFFFFFFF0)
        for amt in (0, 1, 7, 31):
            c(f"slli_{amt}", f"slli a2, a3, {amt}", u32(N << amt))
            c(f"srli_{amt}", f"srli a2, a3, {amt}", N >> amt)
            c(f"srai_{amt}", f"srai a2, a3, {amt}", u32(s32(N) >> amt))
        c("lui_top", "lui a2, 0xfffff", 0xFFFFF000)
        c("lui_one", "lui a2, 1", 0x1000)
        c("auipc", "auipc_l1: auipc a2, 0x12345", lambda sy, im: sy["auipc_l1"] + 0x12345000)
        c("auipc_neg", "auipc_l2: auipc a2, 0xfffff", lambda sy, im: sy["auipc_l2"] - 0x1000)
        c("auipc_zero", "auipc_l3: auipc a2, 0", lambda sy, im: sy["auipc_l3"])
        c("chain", "li a2, 1\nadd a2, a2, a2\nadd a2, a2, a2\nslli a2, a2, 3\naddi a2, a2, -1", 31)

        # ---------------- register file: every register holds its own value
        regs = [r for r in range(1, 16) if r != 9]          # x9 = s1 is the result pointer
        vals = {r: (r * 0x01010101) ^ 0x80000000 ^ (r << 4) for r in regs}
        asm = "\n".join(f"li x{r}, {vals[r]:#x}" for r in regs)
        asm += "\n" + "\n".join(f"RES x{r}" for r in regs)
        c("regfile", asm, *[vals[r] for r in regs])

        # ---------------- FENCE variants are no-ops (rd/rs1 fields ignored, even x16+)
        c("fence", "li a2, 0x1234\nli t0, 0x77\nfence\nfence rw, rw\nfence.tso\n"
          ".word 0x0100000f\n.word 0x0ff3028f\n.word 0x0ff00a0f\nadd a2, a2, t0",
          0x1234 + 0x77)

        # ---------------- branches: result 1 if taken
        st("br_setup", "li t0, -1\nli t1, 1\nli t2, 1\nli a4, 0x80000000\nli a5, 0x7fffffff")
        for op, r1, r2, taken in [
            ("beq", "t1", "t2", 1), ("beq", "t0", "t1", 0),
            ("bne", "t0", "t1", 1), ("bne", "t1", "t2", 0),
            ("blt", "t0", "t1", 1), ("blt", "t1", "t0", 0), ("blt", "t1", "t2", 0),
            ("blt", "a4", "a5", 1),
            ("bge", "t1", "t0", 1), ("bge", "t0", "t1", 0), ("bge", "t1", "t2", 1),
            ("bge", "a5", "a4", 1), ("bge", "a4", "a5", 0),
            ("bltu", "t1", "t0", 1), ("bltu", "t0", "t1", 0), ("bltu", "t1", "t2", 0),
            ("bltu", "a5", "a4", 1),
            ("bgeu", "t0", "t1", 1), ("bgeu", "t1", "t0", 0), ("bgeu", "t1", "t2", 1),
            ("bgeu", "a4", "a5", 1), ("bgeu", "a5", "a4", 0),
            ("beq", "zero", "zero", 1), ("bne", "zero", "zero", 0),
        ]:
            c(f"{op}_{r1}_{r2}", f"li a2, 1\n{op} {r1}, {r2}, 1f\nli a2, 0\n1:", taken)
        c("loop_bne", "li a2, 0\nli a3, 10\n1: addi a2, a2, 3\naddi a3, a3, -1\nbnez a3, 1b", 30)
        c("loop_blt", "li a2, 0\nli a3, -5\n1: addi a2, a2, 1\naddi a3, a3, 1\nblt a3, zero, 1b", 5)
        c("loop_bltu", "li a2, 0\nli a3, 0\nli a4, 4\n1: addi a2, a2, 2\naddi a3, a3, 1\n"
          "bltu a3, a4, 1b", 8)
        # 600 zero words would trap if the branch fell through
        c("branch_far_fwd", "li a2, 7\nbeq zero, zero, 2f\n.fill 600, 4, 0\n2: addi a2, a2, 1", 8)
        c("call_far", "li a2, 0\njal ra, far_fn", 0x55)

        # ---------------- JAL / JALR
        c("jal_link", "jal ra, fn_ra\njal_ret1:", lambda sy, im: sy["jal_ret1"])
        c("jal_rd_t0", "jal t0, fn_t0\njal_ret2:", lambda sy, im: sy["jal_ret2"])
        c("jalr_odd", "li a2, 0\nla t1, fn_66\naddi t1, t1, 1\njalr ra, 0(t1)", 0x66)
        c("jalr_link", "la t1, fn_ra\njalr ra, 0(t1)\njalr_ret3:", lambda sy, im: sy["jalr_ret3"])
        c("jalr_negoff", "li a2, 0\nla t1, fn_66 + 16\njalr ra, -16(t1)", 0x66)
        c("jalr_posoff_odd", "li a2, 0\nla t1, fn_66 - 7\njalr ra, 8(t1)", 0x66)
        c("jalr_rd_eq_rs1", "la t1, fn_t1\njalr t1, 0(t1)\njalr_ret4:",
          lambda sy, im: sy["jalr_ret4"])
        c("jalr_rd_x0", "li a2, 0\nla t1, 3f\njalr zero, 0(t1)\nli a2, 0xbad\n3: addi a2, a2, 0x77",
          0x77)

        # ---------------- loads from 0x3000
        st("ld_setup", "li s0, 0x3000")
        D = LD_DATA

        def le(b):
            return sum(x << (8 * i) for i, x in enumerate(b))
        for off in range(4):
            c(f"lb_{off}", f"lb a2, {off}(s0)", sext(D[off], 8))
            c(f"lbu_{off}", f"lbu a2, {off}(s0)", D[off])
        for off in range(4):
            c(f"lh_{off}", f"lh a2, {off}(s0)", sext(le(D[off:off + 2]), 16))
            c(f"lhu_{off}", f"lhu a2, {off}(s0)", le(D[off:off + 2]))
        for off in range(4):
            c(f"lw_{off}", f"lw a2, {off}(s0)", le(D[off:off + 4]))
        c("lw_negoff", "li s0, 0x3008\nlw a2, -5(s0)", le(D[3:7]))
        c("lh_negoff", "li s0, 0x3008\nlh a2, -1(s0)", sext(le(D[7:9]), 16))
        c("lb_alias", "li s0, 0x12343000\nlb a2, 2(s0)", sext(D[2], 8))
        c("lw_to_x0", "li s0, 0x3000\nlw zero, 0(s0)\nmv a2, zero", 0)

        # ---------------- loads wrapping at 0xFFFF (0x0000..0x0003 hold the first instruction)
        W = WRAP_DATA

        def wrap_le(addrs):
            return lambda sy, im: sum(
                (W[a - 0xFFFC] if a >= 0xFFFC else im[a]) << (8 * i) for i, a in enumerate(addrs))
        st("wrap_setup", "li s0, 0xffff")
        c("wrap_lbu", "lbu a2, 0(s0)", W[3])
        c("wrap_lb", "lb a2, 0(s0)", sext(W[3], 8))
        c("wrap_lhu", "lhu a2, 0(s0)", wrap_le([0xFFFF, 0x0000]))
        c("wrap_lh", "lh a2, 0(s0)",
          lambda sy, im: sext(wrap_le([0xFFFF, 0x0000])(sy, im), 16))
        c("wrap_lw_ffff", "lw a2, 0(s0)", wrap_le([0xFFFF, 0, 1, 2]))
        c("wrap_lw_fffe", "lw a2, -1(s0)", wrap_le([0xFFFE, 0xFFFF, 0, 1]))
        c("wrap_lw_fffd", "lw a2, -2(s0)", wrap_le([0xFFFD, 0xFFFE, 0xFFFF, 0]))
        c("wrap_lw_fffc", "lw a2, -3(s0)", wrap_le([0xFFFC, 0xFFFD, 0xFFFE, 0xFFFF]))
        c("wrap_lw_7fffffff", "li s0, 0x7fffffff\nlw a2, 0(s0)", wrap_le([0xFFFF, 0, 1, 2]))
        c("wrap_lhu_alias", "li s0, 0x0001fffe\nlhu a2, 0(s0)", wrap_le([0xFFFE, 0xFFFF]))

        # ---------------- stores into the 0xEE area at 0x3100 (mirrored in self.store_area)
        sa = self.store_area

        def pst(off, n, val):
            for k in range(n):
                sa[off + k] = (val >> (8 * k)) & 0xFF
        V1, V2, V3 = 0xA1B2C3D4, 0x55667788, 0x0BADF00D
        asm = (f"li s0, 0x3100\nli a0, {V1:#x}\n"
               "sb a0, 1(s0)\nsh a0, 3(s0)\nsw a0, 9(s0)\nsw a0, 16(s0)\nsh a0, 22(s0)\n"
               f"sb a0, 31(s0)\nli a0, {V2:#x}\nsh a0, 29(s0)\nsw a0, 24(s0)\n"
               f"li s0, 0x12343100\nli a0, {V3:#x}\nsw a0, 0x13(s0)\nli s0, 0x3100\nli a2, 0")
        for off, n, v in [(1, 1, V1), (3, 2, V1), (9, 4, V1), (16, 4, V1), (22, 2, V1),
                          (31, 1, V1), (29, 2, V2), (24, 4, V2), (0x13, 4, V3)]:
            pst(off, n, v)
        c("stores", asm, 0)
        snap = bytes(sa)

        def rd(off, n):
            return sum(snap[off + k] << (8 * k) for k in range(n))
        for off in (0, 1, 4, 8, 9, 12, 16, 20, 24, 28):
            c(f"st_lw_{off}", f"lw a2, {off}(s0)", rd(off, 4))
        c("st_lhu_3", "lhu a2, 3(s0)", rd(3, 2))
        c("st_lh_29", "lh a2, 29(s0)", sext(rd(29, 2), 16))
        c("st_lbu_31", "lbu a2, 31(s0)", rd(31, 1))

        # ---------------- stores wrapping at 0xFFFF (mirrored by self.wrap_ops)
        self.wrap_ops = [(0xFFFF, 2, 0x5AA5), (0xFFFE, 4, 0x01234567), (0xFFFF, 4, 0x89ABCDEF),
                         (0xFFFF, 1, 0x3C)]

        def after(nops, addr, n):
            """Bytes at addr..addr+n-1 (mod 64 KB) after the first nops wrap stores."""
            def f(sy, im):
                mem = bytearray(im)
                mem[0xFFFC:0x10000] = bytes(WRAP_DATA)
                for a, k, v in self.wrap_ops[:nops]:
                    for i in range(k):
                        mem[(a + i) & 0xFFFF] = (v >> (8 * i)) & 0xFF
                return sum(mem[(addr + i) & 0xFFFF] << (8 * i) for i in range(n))
            return f
        c("wrap_sh", "li s0, 0xffff\nli a0, 0x5aa5\nsh a0, 0(s0)\nlhu a2, 0(s0)",
          after(1, 0xFFFF, 2))
        c("wrap_sw_fffe", "li a0, 0x01234567\nsw a0, -1(s0)\nlw a2, -1(s0)",
          after(2, 0xFFFE, 4))
        c("wrap_lbu_10000", "lbu a2, 1(s0)", after(2, 0x0000, 1))
        c("wrap_sw_ffff", "li a0, 0x89abcdef\nsw a0, 0(s0)\nlw a2, -3(s0)",
          after(3, 0xFFFC, 4))
        c("wrap_sb_alias", "li s0, 0x1ffff\nli a0, 0x3c\nsb a0, 0(s0)\nlhu a2, 0(s0)",
          after(4, 0xFFFF, 2))
        c("wrap_lw_0", "li s0, 0x10000\nlw a2, 0(s0)", after(4, 0x0000, 4))

        # ---------------- GPIO input (ui_in = 0xA5), IO never touches the RAM at 0x5000
        st("io_setup", "li s0, 0x80000000")
        c("io_lbu", "lbu a2, 0(s0)", UI_START)
        c("io_lb", "lb a2, 0(s0)", sext(UI_START, 8))
        c("io_lbu_1", "lbu a2, 1(s0)", 0)
        c("io_lbu_2", "lbu a2, 2(s0)", 0)
        c("io_lbu_3", "lbu a2, 3(s0)", 0)
        c("io_lh", "lh a2, 0(s0)", UI_START)
        c("io_lhu_2", "lhu a2, 2(s0)", 0)
        c("io_lw", "lw a2, 0(s0)", UI_START)
        c("io_lw_4", "lw a2, 4(s0)", UI_START)
        c("io_top", "li a4, 0xfffffffc\nlbu a2, 0(a4)", UI_START)
        c("io_mid", "li a4, 0x80005000\nlw a2, 0(a4)", UI_START)

        # ---------------- GPIO output: only stores that write byte X[1:0] = 0 change uo_out
        c("io_store",
          "li s0, 0x80005000\nli a0, 0x3c\nsb a0, 0(s0)\nli a0, 0x77\nsb a0, 1(s0)\n"
          "sb a0, 2(s0)\nsb a0, 3(s0)\nsh a0, 2(s0)\nli a0, 0x123456c3\nsw a0, 0(s0)\n"
          "li a0, 0xbeef\nsh a0, 4(s0)\nli a0, 0x99\nsb a0, 8(s0)\nli a4, 0xfffffffd\n"
          "sb a0, 0(a4)\nli s0, 0x80000000\nli a0, 0x42\nsb a0, 0(s0)\nli a2, 1", 1)

        # ---------------- GPIO handshake with the testbench
        c("io_handshake",
          "li s0, 0x80000000\nli a0, 0x11\nsb a0, 0(s0)\nli a3, 0x22\n"
          "1: lbu a2, 0(s0)\nbne a2, a3, 1b\naddi a0, a2, 1\nsb a0, 0(s0)\nRES a2\n"
          "li a3, 0x5a\nli a0, 0x24\nsb a0, 0(s0)\n"
          "2: lw a2, 0(s0)\nbne a2, a3, 2b\nRES a2",
          0x22, 0x5A)
        # (uo_out waits: 0x11 -> drive 0x22, 0x24 -> drive 0x5A; see UI_SCRIPT)

    def expected_image(self, img0: bytes, syms: dict[str, int]) -> bytearray:
        exp = bytearray(img0)
        for i, (_, v) in enumerate(self.expected_words(syms, img0)):
            exp[RESULTS + 4 * i: RESULTS + 4 * i + 4] = v.to_bytes(4, "little")
        exp[0x3100:0x3120] = self.store_area
        for a, n, v in self.wrap_ops:
            for k in range(n):
                exp[(a + k) & 0xFFFF] = (v >> (8 * k)) & 0xFF
        return exp


UI_SCRIPT = [(0x11, 800, 0x22), (0x24, 300, 0x5A)]   # (wait for uo_out, delay, new ui_in)


# ---------------- trap programs: (name, body, uo marker); body labels the trapping word
TRAP_TEMPLATE = ASM_HEADER + """
_start:
    li s0, 0x80000000
    li a0, 0x42
    sb a0, 0(s0)
    li s1, 0x2000
    li a1, 0x11111111
    sw a1, 0(s1)
    li ra, 0x5555
    li t0, 0x6666
{body}
    li a0, 0xee
    sb a0, 0(s0)
    sw a0, 4(s1)
    ebreak
    .balign 16
trap_tgt:
    li a0, 0xef
    sb a0, 0(s0)
    sw a0, 8(s1)
    ebreak
"""

TRAP_CASES = [
    ("ebreak", "trap_insn: ebreak", 0x42),
    ("ecall", "trap_insn: ecall", 0x42),
    ("all_zeros", "trap_insn: .word 0x00000000", 0x42),
    ("all_ones", "trap_insn: .word 0xffffffff", 0x42),
    ("jal_misaligned", "trap_insn: .word 0x006000ef", 0x42),             # jal ra, .+6
    ("jalr_misaligned", "la t1, trap_tgt\naddi t1, t1, 2\ntrap_insn: jalr ra, 0(t1)", 0x42),
    ("jalr_misaligned_odd", "la t1, trap_tgt\naddi t1, t1, 3\ntrap_insn: jalr ra, 0(t1)", 0x42),
    # bne x0,x0,.+6 is not taken (no trap); beq x0,x0,.+6 is taken (trap)
    ("branch_misaligned", ".word 0x00001363\nli a0, 0x43\nsb a0, 0(s0)\n"
     "trap_insn: .word 0x00000363", 0x43),
    ("rd_x16", "trap_insn: .word 0x00100813", 0x42),                     # addi x16, x0, 1
    ("rs2_x17", "trap_insn: .word 0x011100b3", 0x42),                    # add x1, x2, x17
    ("rs1_x16_load", "trap_insn: .word 0x00082503", 0x42),               # lw a0, 0(x16)
    ("mul", "trap_insn: .word 0x023100b3", 0x42),                        # mul x1, x2, x3
    ("store_f3_011", "trap_insn: .word 0x00b4b023", 0x42),               # sd a1, 0(s1)
    ("load_f3_011", "trap_insn: .word 0x0004b503", 0x42),                # ld a0, 0(s1)
    ("fence_i", "trap_insn: .word 0x0000100f", 0x42),
    ("csrrw", "trap_insn: .word 0x34001073", 0x42),                      # csrrw x0, mscratch
    ("slli_shamt5", "trap_insn: .word 0x02009093", 0x42),                # slli x1, x1, 32
    ("srai_bad_funct7", "trap_insn: .word 0x4220d093", 0x42),
    ("branch_f3_010", "trap_insn: .word 0x00002063", 0x42),
    ("jalr_f3_001", "la t1, trap_tgt\ntrap_insn: .word 0x000310e7", 0x42),
    ("custom0", "trap_insn: .word 0x0000000b", 0x42),
    ("compressed", "trap_insn: .word 0x00000001", 0x42),
]


# --------------------------------------------------------------------------------------------
# The bench: SPI RAM model, pin checks, GPIO driver and RVFI monitor, all in one per-cycle hook
# --------------------------------------------------------------------------------------------

class Txn:
    __slots__ = ("start", "end", "nbits", "cmd", "addr", "data", "wbyte", "drive")

    def __init__(self, start: int) -> None:
        self.start, self.end, self.nbits = start, None, 0
        self.cmd = self.addr = self.wbyte = 0
        self.data = bytearray()
        self.drive = None

    def __repr__(self) -> str:
        kind = {3: "READ", 2: "WRITE"}.get(self.cmd, f"cmd{self.cmd:#x}")
        return f"{kind}@{self.addr:#06x}[{self.data.hex()}] c{self.start}-{self.end}"


RVFI_FIELDS = ["order", "insn", "trap", "halt", "intr", "mode", "ixl", "rs1_addr", "rs2_addr",
               "rs1_rdata", "rs2_rdata", "rd_addr", "rd_wdata", "pc_rdata", "pc_wdata",
               "mem_addr", "mem_rmask", "mem_wmask", "mem_rdata", "mem_wdata"]

STATE_REGS = ["state", "cs_n", "sck", "sr", "bitcnt", "ir", "pc", "gpio_out", "order_q"]


class Bench:
    def __init__(self, dut, image: bytes, ui: int = 0, seed: int = 1,
                 ui_script: list | None = None) -> None:
        self.dut = dut
        self.log = dut._log
        self.mem = bytearray(image)
        self.rng = random.Random(seed)
        self.has_rvfi = hasattr(dut, "rvfi_valid")
        self.errors: list[str] = []
        self.cycle = 0
        self.prev = None
        self.cur: Txn | None = None
        self.txns: list[Txn] = []
        self.ui = ui
        self.ui_script = list(ui_script or [])
        self.ui_wait = None
        self.ui_window = {ui}
        self.uo_prev = None
        self.uo_trace: list[int] = []
        self.release = None
        self.last_cs_low = 0
        # RVFI state
        self.retires: list[dict] = []
        self.last_retire = None
        self.halted_at = None
        self.next_txn = 0
        self.shadow: dict[int, int] = {}
        self.uo_expect = None
        self.gaps: dict[str, list[int]] = {}

    def err(self, msg: str) -> None:
        if len(self.errors) < 40:
            self.errors.append(f"cycle {self.cycle}: {msg}")

    def randomize_state(self) -> None:
        """Deposit random values into internal flops (those that exist) before reset."""
        done = []
        for name in STATE_REGS:
            h = getattr(self.dut, name, None)
            if h is not None:
                h.value = self.rng.getrandbits(len(h))
                done.append(f"{name}[{len(h)}]")
        regs = getattr(self.dut, "regs", None)
        if regs is not None:
            for i in range(1, 16):
                regs[i].value = self.rng.getrandbits(32)
            done.append("regs[1:15]")
        self.log.info("random power-up values in: " + " ".join(done))

    async def start(self, randomize: bool) -> None:
        d = self.dut
        d.ena.value = 1
        d.rst_n.value = 0
        d.ui_in.value = self.ui
        d.uio_in.value = self.rng.getrandbits(8)
        if randomize:
            self.randomize_state()
        Clock(d.clk, 50, unit="ns").start(start_high=False)
        cocotb.start_soon(self._loop())

    async def _loop(self) -> None:
        await RisingEdge(self.dut.clk)      # clk X -> 0 at time 0 is a falling edge; skip it
        while True:
            await FallingEdge(self.dut.clk)
            self.cycle += 1
            self.sample()

    # ---- per-cycle work, at the falling clk edge: outputs hold X(t) of rising edge t
    def sample(self) -> None:
        d, t = self.dut, self.cycle
        rst_n = int(d.rst_n.value)
        out = str(d.uio_out.value)                  # MSB first: out[7 - i] is bit i
        oe = d.uio_oe.value
        uo_v = d.uo_out.value
        if not oe.is_resolvable or oe.to_unsigned() != 0x0B:
            self.err(f"uio_oe = {oe}, expected 00001011")
        if out[0:4] != "0000" or out[5] != "0":
            self.err(f"uio_out[7:4] or uio_out[2] not 0: {out}")
        c = int(out[7]) if out[7] in "01" else None
        m = int(out[6]) if out[6] in "01" else None
        s = int(out[4]) if out[4] in "01" else None
        if c is None or s is None:
            self.err(f"CS_n or SCK unresolved: {out}")
            c = 1 if c is None else c
            s = 0 if s is None else s
        uo = uo_v.to_unsigned() if uo_v.is_resolvable else None
        if uo is None:
            self.err(f"uo_out unresolved: {uo_v}")

        if rst_n == 0:
            if c != 1 or s != 0 or uo != 0:
                self.err(f"in reset: CS_n={c} SCK={s} uo_out={uo}, expected 1, 0, 0")
        elif self.release is None:
            self.release = t                         # first rising edge with rst_n high

        # ---- SPI waveform rules and the 23LC512 model
        prev_c = 1
        if self.prev is not None:
            pc_, ps, pm = self.prev
            prev_c = pc_
            if c != pc_ and not (ps == 0 and s == 0):
                self.err("rule 1: CS_n changed while SCK was high")
            if pc_ == 1 and c == 0:
                if self.halted_at is not None:
                    self.err("SPI transaction started after the halt")
                self.cur = Txn(t)
            if c == 0 and pc_ == 0 and ps == 0 and s == 1:
                if m is None or pm is None or m != pm:
                    self.err(f"rule 2: MOSI changed (or unresolved) on a rising SCK edge "
                             f"({pm} -> {m})")
                if self.cur is None:
                    self.err("rising SCK edge with CS_n low but no transaction framed")
                else:
                    self._spi_bit(m or 0)
            if s == 0 and self.cur is not None:
                self.cur.drive = None
            if pc_ == 0 and c == 1 and self.cur is not None:
                self._spi_end(t)
        if c == 0:
            self.last_cs_low = t
        self.prev = (c, s, m)

        # ---- RVFI retirement (before the uo_out check: a store's change may land this cycle)
        if self.has_rvfi and rst_n and int(d.rvfi_valid.value):
            self._retire(t, c, prev_c)

        # ---- GPIO out
        if uo is not None:
            if self.uo_prev is not None and uo != self.uo_prev:
                self.uo_trace.append(uo)
                if self.has_rvfi:
                    if self.uo_expect is None or self.uo_expect[1] != uo:
                        self.err(f"uo_out changed to {uo:#x} with no matching IO store "
                                 f"(expected {self.uo_expect})")
                    self.uo_expect = None
            if self.uo_expect is not None and t >= self.uo_expect[0]:
                if uo != self.uo_expect[1]:
                    self.err(f"uo_out = {uo:#x}, IO store wanted {self.uo_expect[1]:#x} "
                             "one cycle after retirement")
                self.uo_expect = None
            self.uo_prev = uo

        # ---- drive inputs for the next rising edge
        if rst_n == 0 and t >= RESET_CYCLES:
            d.rst_n.value = 1
        if self.ui_script and uo is not None:
            want, delay, new = self.ui_script[0]
            if self.ui_wait is None and uo == want:
                self.ui_wait = t + delay
            if self.ui_wait is not None and t >= self.ui_wait:
                self.ui = new
                d.ui_in.value = new
                self.ui_window.add(new)
                self.ui_script.pop(0)
                self.ui_wait = None
        drive = None
        if c == 0 and s == 1 and self.cur is not None:
            drive = self.cur.drive
        miso = drive if drive is not None else self.rng.getrandbits(1)
        d.uio_in.value = (self.rng.getrandbits(8) & 0xFB) | (miso << 2)

    def _spi_bit(self, m: int) -> None:
        tx = self.cur
        tx.nbits += 1
        n = tx.nbits
        tx.drive = None
        if n <= 8:
            tx.cmd = (tx.cmd << 1) | m
            if n == 8 and tx.cmd not in (2, 3):
                self.err(f"SPI command {tx.cmd:#x} is neither READ nor WRITE")
        elif n <= 24:
            tx.addr = (tx.addr << 1) | m
        else:
            j = n - 25
            a = (tx.addr + j // 8) & 0xFFFF
            if tx.cmd == 3:
                tx.drive = (self.mem[a] >> (7 - j % 8)) & 1
                if j % 8 == 7:
                    tx.data.append(self.mem[a])
            elif tx.cmd == 2:
                tx.wbyte = ((tx.wbyte << 1) | m) & 0xFF
                if j % 8 == 7:
                    self.mem[a] = tx.wbyte
                    tx.data.append(tx.wbyte)

    def _spi_end(self, t: int) -> None:
        tx = self.cur
        tx.end = t
        self.cur = None
        if tx.nbits < 8:
            self.err(f"transaction ended inside the command: {tx}")
        if tx.cmd == 2 and (tx.nbits < 32 or (tx.nbits - 24) % 8):
            self.err(f"rule 5: WRITE ended off a byte boundary ({tx.nbits} bits): {tx}")
        # seed-level expectation: every transaction moves exactly 1, 2 or 4 whole bytes
        if tx.nbits not in (32, 40, 56):
            self.err(f"transaction of {tx.nbits} bits (want 24 + 8n, n in 1, 2, 4): {tx}")
        self.txns.append(tx)

    # ---- RVFI checks for one retirement
    def _retire(self, t: int, c: int, prev_c: int) -> None:
        d = self.dut
        r = {}
        for f in RVFI_FIELDS:
            v = getattr(d, "rvfi_" + f).value
            if not v.is_resolvable:
                self.err(f"rvfi_{f} unresolved at retirement: {v}")
                return
            r[f] = int(v)
        r["cycle"] = t
        if c != 1 or prev_c != 1 or self.cur is not None:
            self.err("rvfi_valid while SPI traffic was in flight (CS_n must be high this cycle "
                     "and the one before)")
        if self.halted_at is not None:
            self.err(f"retirement after the halt: {r}")
        if r["order"] != len(self.retires):
            self.err(f"rvfi_order {r['order']} != {len(self.retires)}")
        if self.last_retire is None:
            if r["pc_rdata"] != 0:
                self.err(f"first pc_rdata {r['pc_rdata']:#x} != 0")
            gap = t - self.release
        else:
            if r["pc_rdata"] != self.last_retire["pc_wdata"]:
                self.err(f"pc_rdata {r['pc_rdata']:#x} != previous pc_wdata "
                         f"{self.last_retire['pc_wdata']:#x}")
            gap = t - self.last_retire["cycle"]
        if gap > LATENCY_CAP:
            self.err(f"retirement gap {gap} > {LATENCY_CAP}")
        if (r["intr"], r["mode"], r["ixl"]) != (0, 3, 1):
            self.err(f"intr/mode/ixl = {r['intr']}/{r['mode']}/{r['ixl']}")
        if r["trap"] != r["halt"]:
            self.err("rvfi_trap != rvfi_halt")
        for k in ("rs1", "rs2"):
            a, v = r[k + "_addr"], r[k + "_rdata"]
            if a >= 16:
                self.err(f"{k}_addr {a} names a register outside RV32E")
            if a == 0 and v != 0:
                self.err(f"{k}_addr 0 with rdata {v:#x}")
            if a in self.shadow and self.shadow[a] != v:
                self.err(f"{k} x{a} read {v:#x}, last written {self.shadow[a]:#x}")
        if r["rd_addr"] >= 16 or (r["rd_addr"] == 0 and r["rd_wdata"] != 0):
            self.err(f"bad rd fields: x{r['rd_addr']} = {r['rd_wdata']:#x}")
        if r["rd_addr"]:
            self.shadow[r["rd_addr"]] = r["rd_wdata"]

        # transactions since the last retirement belong to this instruction
        txs = self.txns[self.next_txn:]
        self.next_txn = len(self.txns)
        if not txs:
            self.err("retirement without a fetch transaction")
            return
        f = txs[0]
        if (f.cmd != 3 or f.addr != (r["pc_rdata"] & 0xFFFF) or len(f.data) != 4
                or int.from_bytes(f.data, "little") != r["insn"]):
            self.err(f"fetch {f} does not match pc {r['pc_rdata']:#x} insn {r['insn']:#010x}")
        rest = txs[1:]
        rmask, wmask = r["mem_rmask"], r["mem_wmask"]
        if r["trap"]:
            if r["rd_addr"] or wmask:
                self.err(f"trap with side effects: rd x{r['rd_addr']}, wmask {wmask:#x}")
            if rest:
                self.err(f"trapping instruction moved data: {rest}")
            self.halted_at = t
        elif rmask or wmask:
            if rmask and wmask:
                self.err("both rmask and wmask set")
            mask = rmask | wmask
            n = {1: 1, 3: 2, 15: 4}.get(mask)
            if n is None:
                self.err(f"mask {mask:#x} is not LSB-aligned 1/2/4 bytes")
                n = 4
            addr = r["mem_addr"]
            data = r["mem_rdata"] if rmask else r["mem_wdata"]
            lanes = [(data >> (8 * k)) & 0xFF for k in range(n)]
            if addr >> 31:
                if rest:
                    self.err(f"IO access at {addr:#x} appeared on SPI: {rest}")
                for k in range(n):
                    hit = ((addr + k) & 3) == 0
                    if rmask:
                        ok = lanes[k] in self.ui_window if hit else lanes[k] == 0
                        if not ok:
                            self.err(f"IO load byte {k} at {addr:#x} = {lanes[k]:#x}")
                    elif hit:
                        self.uo_expect = (t + 1, lanes[k])
            else:
                want = (3 if rmask else 2, addr & 0xFFFF, bytes(lanes))
                if len(rest) != 1 or (rest[0].cmd, rest[0].addr, bytes(rest[0].data)) != want:
                    self.err(f"data transactions {rest} do not match RVFI "
                             f"{'load' if rmask else 'store'} {want}")
        elif rest:
            self.err(f"non-memory instruction {r['insn']:#010x} moved data: {rest}")

        cls = classify(r)
        self.gaps.setdefault(cls, []).append(gap)
        self.ui_window = {self.ui}
        self.retires.append(r)
        self.last_retire = r

    async def run_until_halt(self, max_cycles: int) -> None:
        while True:
            await ClockCycles(self.dut.clk, 32)
            if self.txns and self.cycle - self.last_cs_low > IDLE_HALT:
                break
            if self.cycle > max_cycles:
                self.err(f"no halt within {max_cycles} cycles")
                return
        # stay halted: no SPI activity (and, in the RVFI build, no retirement) for a while longer
        ntx, low = len(self.txns), self.last_cs_low
        await ClockCycles(self.dut.clk, 3 * LATENCY_CAP)
        if len(self.txns) != ntx or self.last_cs_low != low:
            self.err("SPI activity after the core looked halted")

    def max_txn_gap(self) -> int:
        """Largest gap between transaction starts (a hang detector for the plain build; the
        exact retirement latency is checked from RVFI in the RISCV_FORMAL build)."""
        starts = [self.release] + [tx.start for tx in self.txns]
        worst = max((b - a for a, b in zip(starts, starts[1:])), default=0)
        if worst > LATENCY_CAP:
            self.err(f"gap between transaction starts {worst} > {LATENCY_CAP}")
        return worst


def classify(r: dict) -> str:
    if r["trap"]:
        return "trap"
    op, f3 = r["insn"] & 0x7F, (r["insn"] >> 12) & 7
    if op in (0x13, 0x33, 0x37, 0x17):
        return "alu"
    if op == 0x63:
        return "branch_taken" if r["pc_wdata"] != u32(r["pc_rdata"] + 4) else "branch_not_taken"
    if op == 0x6F:
        return "jal"
    if op == 0x67:
        return "jalr"
    if op == 0x0F:
        return "fence"
    n = {0: 1, 1: 2, 2: 4}[f3 & 3]
    io = r["mem_addr"] >> 31
    kind = "load" if op == 0x03 else "store"
    return f"{kind}_io" if io else f"{kind}_ram_{n}B"


def report(b: Bench, what: str) -> None:
    if b.errors:
        raise AssertionError(f"{what}: {len(b.errors)} error(s):\n  " + "\n  ".join(b.errors))


# --------------------------------------------------------------------------------------------
# cocotb tests
# --------------------------------------------------------------------------------------------

MAIN = MainProgram()


async def run_main(dut, seed: int | None) -> Bench:
    img, syms = assemble("main", MAIN.asm)
    assert syms["code_end"] < RESULTS, f"code ends at {syms['code_end']:#x}"
    img0 = bytes(img)
    b = Bench(dut, img, ui=UI_START, seed=seed or 7, ui_script=UI_SCRIPT)
    await b.start(randomize=seed is not None)
    await b.run_until_halt(max_cycles=400_000)
    report(b, "main program run")

    # the halt is the EBREAK at end_ebreak
    last = b.txns[-1]
    if (last.cmd, last.addr, int.from_bytes(last.data, "little")) != (3, syms["end_ebreak"], EBREAK):
        b.err(f"last transaction {last} is not the fetch of the final EBREAK")
    first = b.txns[0]
    if (first.cmd, first.addr, len(first.data)) != (3, 0, 4):
        b.err(f"first transaction {first} is not a fetch at 0")

    exp_words = MAIN.expected_words(syms, img0)
    for i, (name, v) in enumerate(exp_words):
        got = int.from_bytes(b.mem[RESULTS + 4 * i: RESULTS + 4 * i + 4], "little")
        if got != v:
            b.err(f"result {name}: got {got:#010x}, expected {v:#010x}")
    exp_img = MAIN.expected_image(img0, syms)
    diffs = [a for a in range(65536) if b.mem[a] != exp_img[a]]
    if diffs:
        b.err(f"{len(diffs)} RAM byte(s) differ from the expected image, first at "
              + ", ".join(f"{a:#06x}: {b.mem[a]:#04x} (want {exp_img[a]:#04x})"
                          for a in diffs[:8]))
    if b.uo_trace != MAIN.uo_trace:
        b.err(f"uo_out trace {[hex(x) for x in b.uo_trace]} != "
              f"{[hex(x) for x in MAIN.uo_trace]}")
    worst = b.max_txn_gap()
    report(b, "main program results")
    dut._log.info(f"main program: {len(b.txns)} SPI transactions, {len(exp_words)} result "
                  f"words, halted {b.last_cs_low - b.release} cycles after reset release, "
                  f"largest gap between transaction starts {worst}")
    if b.has_rvfi:
        if not b.retires or b.retires[-1]["insn"] != EBREAK or b.halted_at is None:
            b.err("the last retirement is not a halting EBREAK")
        table = {k: {"count": len(v), "min": min(v), "max": max(v)}
                 for k, v in sorted(b.gaps.items())}
        BUILD.mkdir(parents=True, exist_ok=True)
        (BUILD / f"cycles_seed{seed}.json").write_text(json.dumps(table, indent=1) + "\n")
        for k, v in table.items():
            dut._log.info(f"  cycles {k:18s} n={v['count']:4d} min={v['min']:4d} max={v['max']:4d}")
        report(b, "main program RVFI")
    return b


@cocotb.test()
async def main_program_xinit(dut):
    """Main program with every flop starting as X (Icarus default)."""
    await run_main(dut, seed=None)


@cocotb.test()
@cocotb.parametrize(seed=[1, 2])
async def main_program_random_init(dut, seed):
    """Main program with random power-up values in every flop and the register file."""
    await run_main(dut, seed=seed)


@cocotb.test()
@cocotb.parametrize(case=[cocotb.Param(value=c[0], name=c[0]) for c in TRAP_CASES])
async def trap_halts(dut, case):
    """A fatal trap retires with no effect, then the core never moves again."""
    _, body, marker = next(c for c in TRAP_CASES if c[0] == case)
    img, syms = assemble(f"trap_{case}", TRAP_TEMPLATE.format(body=body))
    img0 = bytes(img)
    b = Bench(dut, img, ui=0x3C, seed=zlib.crc32(case.encode()))
    await b.start(randomize=True)
    await b.run_until_halt(max_cycles=60_000)
    report(b, f"trap {case} run")
    addr = syms["trap_insn"]
    word = int.from_bytes(img0[addr:addr + 4], "little")
    last = b.txns[-1]
    if (last.cmd, last.addr, int.from_bytes(last.data, "little")) != (3, addr, word):
        b.err(f"last transaction {last} is not the fetch of the trapping word "
              f"{word:#010x} at {addr:#x}")
    exp = bytearray(img0)
    exp[0x2000:0x2004] = (0x11111111).to_bytes(4, "little")
    diffs = [a for a in range(65536) if b.mem[a] != exp[a]]
    if diffs:
        b.err(f"RAM changed at {[hex(a) for a in diffs[:8]]}")
    if b.uo_trace != [0x42] + ([0x43] if marker == 0x43 else []):
        b.err(f"uo_out trace {[hex(x) for x in b.uo_trace]}")
    if b.has_rvfi:
        r = b.retires[-1] if b.retires else None
        if r is None or not (r["trap"] and r["halt"]) or r["insn"] != word:
            b.err(f"last retirement {r} is not a halting trap of {word:#010x}")
    report(b, f"trap {case}")


# --------------------------------------------------------------------------------------------
# pytest / command-line runner
# --------------------------------------------------------------------------------------------

def _run(variant: str, defines: dict) -> None:
    """SEED_SMOKE_SRC (os.pathsep-separated .v files) overrides src/*.v, e.g. for mutants;
    SEED_SMOKE_TAG keeps such builds apart."""
    from cocotb_tools.runner import get_runner
    bdir = BUILD / (variant + os.environ.get("SEED_SMOKE_TAG", ""))
    srcs = os.environ.get("SEED_SMOKE_SRC")
    sources = srcs.split(os.pathsep) if srcs else sorted(str(p) for p in SRC.glob("*.v"))
    runner = get_runner("icarus")
    runner.build(sources=sources, hdl_toplevel=TOP,
                 build_dir=bdir, defines=defines, build_args=["-g2005", "-Wall"],
                 timescale=("1ns", "1ps"), always=True)
    xml = runner.test(hdl_toplevel=TOP, test_module="test_seed_smoke", build_dir=bdir,
                      test_dir=bdir, results_xml=str(bdir / "results.xml"))
    from cocotb_tools.check_results import get_results
    ntests, nfail = get_results(xml)
    assert nfail == 0, f"{variant}: {nfail} of {ntests} cocotb tests failed"


def test_seed_smoke_plain():
    _run("plain", {})


def test_seed_smoke_rvfi():
    _run("rvfi", {"RISCV_FORMAL": 1})


if __name__ == "__main__":
    sys.path.insert(0, str(SRC))
    os.chdir(ROOT)
    test_seed_smoke_plain()
    test_seed_smoke_rvfi()
    print("seed smoke: PASS")
