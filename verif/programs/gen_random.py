#!/usr/bin/env python3
"""Generate a deterministic random RV32E instruction stream for the cosim.

Usage: gen_random.py SEED N [-o OUT.S]

The program has N random instructions (valid RV32E only, never a reserved encoding, never a trap)
between a prologue and an epilogue:
  - ALU ops: register, immediate and shift forms, LUI, AUIPC, edge-value constants;
  - loads and stores of every width into the data window 0x8000-0x8FFF, misaligned included,
    from varying base registers; the window starts with random bytes;
  - GPIO loads and stores on every byte lane (wider accesses only at addr[1:0] = 0: other
    IO accesses wider than a byte are unspecified);
  - forward branches (every condition, taken and not taken), forward JAL and JALR (with bit 0 of
    the target set, negative offsets, rd == rs1), x0 as a destination, FENCE variants.
The epilogue stores x1..x15 to 0x8FC0..0x8FFF (so the pins-mode RAM comparison sees them), then
EBREAK, then a guard that writes GPIO and RAM if a core ever runs past the EBREAK.

Register roles: x15 = 0x8800 (window base, never written), x14 = 0x80000000 (GPIO base, never
written), x1..x13 random.
"""
import argparse
import random
import sys

WIN_LO, WIN_HI = 0x8000, 0x8FFF
WIN_BASE = 0x8800
SIG_OFF = 0x7C0  # x15 + 0x7C0 = 0x8FC0

EDGE = [0, 1, 2, 0xFFFFFFFF, 0xFFFFFFFE, 0x7FFFFFFF, 0x80000000, 0x80000001, 0x55555555,
        0xAAAAAAAA, 0x0000FFFF, 0xFFFF0000, 0x00000800, 0xFFFFF800, 31, 32]
RANDREGS = list(range(1, 14))
R_OPS = ["add", "sub", "slt", "sltu", "xor", "or", "and"]
SH_R = ["sll", "srl", "sra"]
I_OPS = ["addi", "slti", "sltiu", "xori", "ori", "andi"]
SH_I = ["slli", "srli", "srai"]
BR = ["beq", "bne", "blt", "bge", "bltu", "bgeu"]
LOADS = [("lb", 1), ("lbu", 1), ("lh", 2), ("lhu", 2), ("lw", 4)]
STORES = [("sb", 1), ("sh", 2), ("sw", 4)]


class Gen:
    def __init__(self, seed: int, n: int):
        self.r = random.Random(seed)
        self.n = n
        self.out: list[str] = []
        self.count = 0  # random-body instructions emitted
        self.pending: list[list] = []  # [label, instructions_left]
        self.nlabel = 0

    # -- helpers ----------------------------------------------------------------------------
    def x(self, i: int) -> str:
        return f"x{i}"

    def src(self) -> str:
        return "x0" if self.r.random() < 0.06 else self.x(self.r.choice(RANDREGS))

    def dst(self) -> str:
        return "x0" if self.r.random() < 0.05 else self.x(self.r.choice(RANDREGS))

    def label(self) -> str:
        self.nlabel += 1
        return f".L{self.nlabel}"

    def emit(self, *lines: str, n: int = 0) -> None:
        """Emit lines as one unit (no branch target lands inside it); n instructions (default:
        one per line). Then place every forward label that has become due."""
        for text in lines:
            self.out.append("    " + text)
        n = n or len(lines)
        self.count += n
        for p in self.pending:
            p[1] -= n
        due = [p for p in self.pending if p[1] <= 0]
        for p in due:
            self.out.append(f"{p[0]}:")
            self.pending.remove(p)

    def forward(self, skip: int) -> str:
        lab = self.label()
        self.pending.append([lab, skip])
        return lab

    def flush_labels(self) -> None:
        for p in self.pending:
            self.out.append(f"{p[0]}:")
        self.pending = []

    def li(self, rd: str, v: int) -> None:
        self.emit(f"li {rd}, {v & 0xFFFFFFFF:#x}", n=2)

    # -- instruction kinds ------------------------------------------------------------------
    def alu_r(self) -> None:
        self.emit(f"{self.r.choice(R_OPS)} {self.dst()}, {self.src()}, {self.src()}")

    def shift_r(self) -> None:
        self.emit(f"{self.r.choice(SH_R)} {self.dst()}, {self.src()}, {self.src()}")

    def alu_i(self) -> None:
        r = self.r
        imm = r.choice([r.randint(-2048, 2047), r.choice([-2048, -1, 0, 1, 2047])])
        self.emit(f"{r.choice(I_OPS)} {self.dst()}, {self.src()}, {imm}")

    def shift_i(self) -> None:
        r = self.r
        self.emit(f"{r.choice(SH_I)} {self.dst()}, {self.src()}, {r.randint(0, 31)}")

    def upper(self) -> None:
        r = self.r
        op = r.choice(["lui", "auipc"])
        self.emit(f"{op} {self.dst()}, {r.randint(0, 0xFFFFF):#x}")

    def const(self) -> None:
        r = self.r
        v = r.choice(EDGE) if r.random() < 0.5 else r.getrandbits(32)
        self.li(self.x(r.choice(RANDREGS)), v)

    def mem_base(self, n: int) -> tuple[list[str], str, int]:
        """Pick a base register and offset whose effective address lies in the window. Returns
        the setup instructions (emitted in the same unit as the access), base and offset."""
        r = self.r
        ea = r.randint(WIN_LO, WIN_HI - (n - 1))
        if r.random() < 0.7:
            return [], "x15", ea - WIN_BASE
        # derive another base register from x15
        rb = self.x(r.choice(RANDREGS))
        lo = max(ea - 2047, -2048 + WIN_BASE)
        hi = min(ea + 2048, 2047 + WIN_BASE)
        base = r.randint(lo, hi)
        return [f"addi {rb}, x15, {base - WIN_BASE}"], rb, ea - base

    def load(self) -> None:
        op, n = self.r.choice(LOADS)
        setup, base, off = self.mem_base(n)
        self.emit(*setup, f"{op} {self.dst()}, {off}({base})")

    def store(self) -> None:
        op, n = self.r.choice(STORES)
        setup, base, off = self.mem_base(n)
        self.emit(*setup, f"{op} {self.src()}, {off}({base})")

    def io_load(self) -> None:
        r = self.r
        op, n = r.choice(LOADS)
        # IO accesses wider than a byte need addr[1:0] = 0 (otherwise unspecified)
        off = r.randrange(0, 16, 1 if n == 1 else 4)
        self.emit(f"{op} {self.dst()}, {off}(x14)")

    def io_store(self) -> None:
        r = self.r
        op, n = r.choice(STORES)
        off = r.randrange(0, 16, 1 if n == 1 else 4)
        self.emit(f"{op} {self.src()}, {off}(x14)")

    def branch(self) -> None:
        r = self.r
        a = self.src()
        b = a if r.random() < 0.25 else self.src()
        lab = self.forward(r.randint(1, 6))
        self.emit(f"{r.choice(BR)} {a}, {b}, {lab}")

    def jal(self) -> None:
        lab = self.forward(self.r.randint(1, 5))
        self.emit(f"jal {self.dst()}, {lab}")

    def jalr(self) -> None:
        r = self.r
        skip = r.randint(1, 5)
        adj = r.choice([0, 0, 8, -12, 2047, -2046])
        off = -adj + (1 if r.random() < 0.5 else 0)  # bit 0 of the sum is cleared by JALR
        rb = self.x(r.choice(RANDREGS))
        rd = rb if r.random() < 0.2 else self.dst()
        # la (2 instructions) and jalr form one unit; the target is `skip` instructions later
        lab = self.forward(skip + 3)
        la = f"la {rb}, {lab}{adj:+d}" if adj else f"la {rb}, {lab}"
        self.emit(la, f"jalr {rd}, {off}({rb})", n=3)

    def fence(self) -> None:
        self.emit(self.r.choice(["fence", "fence rw, rw", "fence.tso", "fence i, o",
                                 ".word 0x0100000f"]))

    # -- program ----------------------------------------------------------------------------
    def body(self) -> None:
        kinds = [(self.alu_r, 16), (self.shift_r, 6), (self.alu_i, 15), (self.shift_i, 6),
                 (self.upper, 3), (self.const, 4), (self.load, 13), (self.store, 11),
                 (self.io_load, 3), (self.io_store, 3), (self.branch, 12), (self.jal, 2),
                 (self.jalr, 2), (self.fence, 1)]
        funcs = [k for k, _ in kinds]
        weights = [w for _, w in kinds]
        while self.count < self.n:
            self.r.choices(funcs, weights)[0]()

    def program(self, seed: int) -> str:
        r = self.r
        head = [
            f"# Random RV32E stream: seed {seed}, {self.n} instructions. Generated by gen_random.py.",
            "    .section .text.start, \"ax\"",
            "    .global _start",
            "_start:",
        ]
        self.out = head
        # every register gets a value before any read (the core's registers start unknown)
        for i in RANDREGS:
            v = r.choice(EDGE) if r.random() < 0.3 else r.getrandbits(32)
            self.out.append(f"    li x{i}, {v:#x}")
        self.out.append(f"    li x15, {WIN_BASE:#x}")
        self.out.append("    lui x14, 0x80000")
        self.body()
        self.flush_labels()
        for i in range(1, 16):
            self.out.append(f"    sw x{i}, {SIG_OFF + 4 * (i - 1)}(x15)")
        self.out += [
            "    ebreak",
            "    # guard: never executes",
            "    li x1, 0x5e",
            "    sb x1, 0(x14)",
            "    li x1, 0xe5",
            "    sb x1, 0(x14)",
            "    sw x1, 0(x15)",
            "1:  j 1b",
            "",
            "    .section .window, \"aw\"",
        ]
        data = [r.getrandbits(8) for _ in range(WIN_HI - WIN_LO + 1)]
        for i in range(0, len(data), 16):
            self.out.append("    .byte " + ", ".join(f"{b:#04x}" for b in data[i:i + 16]))
        return "\n".join(self.out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("seed", type=int)
    ap.add_argument("n", type=int)
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    text = Gen(a.seed, a.n).program(a.seed)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
