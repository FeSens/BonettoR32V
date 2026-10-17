#!/usr/bin/env python3
"""Generate a deterministic random RV32E instruction stream for the cosim.

Usage: gen_random.py SEED N [-o OUT.S]

The program has N random instructions (valid RV32E only, never a reserved encoding, never a trap)
between a prologue and an epilogue:
  - ALU ops: register, immediate and shift forms, LUI, AUIPC, edge-value constants, on all of
    x1..x15 (and x0 as a source or destination);
  - loads and stores of every width, misaligned included. Effective addresses carry random alias
    bits (addr[30:16]); RAM stores go to the data window 0x8000-0x8FFF (which starts with random
    bytes) or to 0xFF70-0xFF7F, up to the last byte below the core region; RAM loads may read
    anywhere in 0x0000-0xFF7F. No access touches the core region 0xFF80-0xFFFF (contract v2
    section 3: programs never do), so none wraps at 64 KB either;
  - GPIO loads and stores anywhere in the addr[31] = 1 space, on every byte lane (accesses wider
    than a byte only at addr[1:0] = 0: other IO accesses wider than a byte are unspecified);
  - the base register is either loaded just before the access (li) or a register whose value the
    generator still knows (it forgets everything at each branch target);
  - forward branches (every condition, taken and not taken), forward JAL and JALR (with bit 0 of
    the target set, negative offsets, rd == rs1), x0 as a destination, FENCE variants;
  - bounded loops: a backward branch closing 1-4 iterations of a short random body; the counter
    register is written only by the loop itself, and no branch from outside enters the body.
The epilogue stores x1..x15 to 0x0000..0x003B (over the prologue, so the pins-mode RAM comparison
sees every register), then EBREAK, then a guard that writes GPIO and RAM if a core ever runs past
the EBREAK.
"""
import argparse
import random
import sys

WIN_LO, WIN_HI = 0x8000, 0x8FFF
REGION_LO = 0xFF80  # contract v2 section 3: the core region 0xFF80-0xFFFF
TOP_LO = 0xFF70     # RAM stores here end at or below 0xFF7F
GPIO = 0x80000000
M32 = 0xFFFFFFFF

EDGE = [0, 1, 2, 0xFFFFFFFF, 0xFFFFFFFE, 0x7FFFFFFF, 0x80000000, 0x80000001, 0x55555555,
        0xAAAAAAAA, 0x0000FFFF, 0xFFFF0000, 0x00000800, 0xFFFFF800, 31, 32]
REGS = list(range(1, 16))
R_OPS = ["add", "sub", "slt", "sltu", "xor", "or", "and"]
SH_R = ["sll", "srl", "sra"]
I_OPS = ["addi", "slti", "sltiu", "xori", "ori", "andi"]
SH_I = ["slli", "srli", "srai"]
BR = ["beq", "bne", "blt", "bge", "bltu", "bgeu"]
LOADS = [("lb", 1), ("lbu", 1), ("lh", 2), ("lhu", 2), ("lw", 4)]
STORES = [("sb", 1), ("sh", 2), ("sw", 4)]
# (initial counter, step, branch) closing a loop of k iterations; "{c}" is the counter register
LOOP_FORMS = [
    (lambda k: k, -1, "bne {c}, x0, {L}"),
    (lambda k: k, -1, "blt x0, {c}, {L}"),
    (lambda k: k, -1, "bltu x0, {c}, {L}"),
    (lambda k: -k, 1, "blt {c}, x0, {L}"),
    (lambda k: -k, 1, "bne {c}, x0, {L}"),
]


class Gen:
    def __init__(self, seed: int, n: int):
        self.r = random.Random(seed)
        self.n = n
        self.out: list[str] = []
        self.count = 0  # random-body instructions emitted
        self.pending: list[list] = []  # [label, instructions_left]
        self.nlabel = 0
        self.known: dict[int, int] = {}  # register -> value, valid at the current point
        self.locked: int | None = None  # loop counter: nothing else writes it
        self.in_loop = False

    # -- helpers ----------------------------------------------------------------------------
    def free(self) -> list[int]:
        return [i for i in REGS if i != self.locked]

    def src(self) -> str:
        return "x0" if self.r.random() < 0.06 else f"x{self.r.choice(REGS)}"

    def wreg(self) -> int:
        """A register about to be written (never the loop counter); its value becomes unknown."""
        i = self.r.choice(self.free())
        self.known.pop(i, None)
        return i

    def dst(self) -> str:
        return "x0" if self.r.random() < 0.05 else f"x{self.wreg()}"

    def label(self) -> str:
        self.nlabel += 1
        return f".L{self.nlabel}"

    def place(self, lab: str) -> None:
        self.out.append(f"{lab}:")
        self.known.clear()  # paths merge here

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
            self.place(p[0])
            self.pending.remove(p)

    def forward(self, skip: int) -> str:
        lab = self.label()
        self.pending.append([lab, skip])
        return lab

    def flush_labels(self) -> None:
        for p in self.pending:
            self.place(p[0])
        self.pending = []

    def li(self, rd: int, v: int) -> str:
        """li text for rd = v; records the value."""
        self.known[rd] = v & M32
        return f"li x{rd}, {v & M32:#x}"

    # -- instruction kinds ------------------------------------------------------------------
    def alu_r(self) -> None:
        a, b = self.src(), self.src()
        self.emit(f"{self.r.choice(R_OPS)} {self.dst()}, {a}, {b}")

    def shift_r(self) -> None:
        a, b = self.src(), self.src()
        self.emit(f"{self.r.choice(SH_R)} {self.dst()}, {a}, {b}")

    def alu_i(self) -> None:
        r = self.r
        imm = r.choice([r.randint(-2048, 2047), r.choice([-2048, -1, 0, 1, 2047])])
        a = self.src()
        self.emit(f"{r.choice(I_OPS)} {self.dst()}, {a}, {imm}")

    def shift_i(self) -> None:
        r = self.r
        a = self.src()
        self.emit(f"{r.choice(SH_I)} {self.dst()}, {a}, {r.randint(0, 31)}")

    def upper(self) -> None:
        r = self.r
        op = r.choice(["lui", "auipc"])
        self.emit(f"{op} {self.dst()}, {r.randint(0, 0xFFFFF):#x}")

    def const(self) -> None:
        r = self.r
        v = r.choice(EDGE) if r.random() < 0.5 else r.getrandbits(32)
        self.emit(self.li(self.wreg(), v), n=2)

    # -- memory -----------------------------------------------------------------------------
    @staticmethod
    def valid(ea: int, n: int, io: bool, store: bool) -> bool:
        if io:
            return ea >> 31 == 1 and (n == 1 or ea & 3 == 0)
        if ea >> 31:
            return False
        lo = ea & 0xFFFF
        if lo + n > REGION_LO:  # touches the region (or wraps at 64 KB through it)
            return False
        return not store or WIN_LO <= lo <= WIN_HI - (n - 1) or lo >= TOP_LO

    def pick_ea(self, n: int, io: bool, store: bool) -> int:
        r = self.r
        if io:
            if r.random() < 0.5:
                ea = GPIO | r.randrange(0, 16)  # the canonical lanes, so uo_out changes often
            else:
                ea = GPIO | r.getrandbits(31)
            return ea & ~3 if n > 1 else ea
        alias = 0 if r.random() < 0.4 else r.getrandbits(15)
        if r.random() < 0.85:
            lo = r.randint(WIN_LO, WIN_HI - (n - 1))
        elif r.random() < 0.5:
            lo = REGION_LO - n  # ends at 0xFF7F, the last byte below the core region
        else:
            lo = r.randint(TOP_LO, REGION_LO - n)
        return alias << 16 | lo

    def mem_base(self, n: int, io: bool, store: bool) -> tuple[list[str], str, int]:
        """Base register and offset for one access, plus setup instructions (emitted in the same
        unit as the access)."""
        r = self.r
        if self.known and r.random() < 0.4:
            regs = list(self.known.items())
            r.shuffle(regs)
            for reg, v in regs:
                for _ in range(8):
                    off = r.randint(-2048, 2047)
                    if self.valid((v + off) & M32, n, io, store):
                        return [], f"x{reg}", off
        ea = self.pick_ea(n, io, store)
        off = r.randint(-2048, 2047)
        rb = self.wreg()
        return [self.li(rb, ea - off)], f"x{rb}", off

    def access(self, io: bool, store: bool) -> None:
        op, n = self.r.choice(STORES if store else LOADS)
        setup, base, off = self.mem_base(n, io, store)
        if store:
            self.emit(*setup, f"{op} {self.src()}, {off}({base})", n=2 * len(setup) + 1)
        else:
            self.emit(*setup, f"{op} {self.dst()}, {off}({base})", n=2 * len(setup) + 1)

    def load(self) -> None:
        self.access(io=False, store=False)

    def store(self) -> None:
        self.access(io=False, store=True)

    def io_load(self) -> None:
        self.access(io=True, store=False)

    def io_store(self) -> None:
        self.access(io=True, store=True)

    # -- control flow -----------------------------------------------------------------------
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
        rb = self.wreg()
        rd = f"x{rb}" if r.random() < 0.2 else self.dst()
        # la (2 instructions) and jalr form one unit; the target is `skip` instructions later
        lab = self.forward(skip + 3)
        la = f"la x{rb}, {lab}{adj:+d}" if adj else f"la x{rb}, {lab}"
        self.emit(la, f"jalr {rd}, {off}(x{rb})", n=3)

    def loop(self) -> None:
        if self.in_loop:  # no nesting
            self.alu_r()
            return
        r = self.r
        self.flush_labels()  # no branch from before the loop lands inside it
        c = self.wreg()
        k = r.randint(1, 4)
        init, step, br = r.choice(LOOP_FORMS)
        self.emit(f"li x{c}, {init(k)}")
        self.known.pop(c, None)
        top = self.label()
        self.place(top)
        self.locked, self.in_loop = c, True
        end = self.count + r.randint(2, 8)
        funcs, weights = self.kinds()
        while self.count < end:
            r.choices(funcs, weights)[0]()
        # one unit: no branch target between the counter update and the backward branch
        self.emit(f"addi x{c}, x{c}, {step}", br.format(c=f"x{c}", L=top))
        self.locked, self.in_loop = None, False
        self.known.pop(c, None)

    def fence(self) -> None:
        self.emit(self.r.choice(["fence", "fence rw, rw", "fence.tso", "fence i, o",
                                 ".word 0x0100000f"]))

    # -- program ----------------------------------------------------------------------------
    def kinds(self):
        kinds = [(self.alu_r, 15), (self.shift_r, 6), (self.alu_i, 14), (self.shift_i, 6),
                 (self.upper, 3), (self.const, 4), (self.load, 12), (self.store, 10),
                 (self.io_load, 4), (self.io_store, 4), (self.branch, 11), (self.jal, 2),
                 (self.jalr, 2), (self.fence, 1), (self.loop, 2)]
        return [k for k, _ in kinds], [w for _, w in kinds]

    def body(self) -> None:
        funcs, weights = self.kinds()
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
        for i in REGS:
            v = r.choice(EDGE) if r.random() < 0.3 else r.getrandbits(32)
            self.out.append("    " + self.li(i, v))
        self.body()
        self.flush_labels()
        # register dump over the prologue (0x0000..0x003B), which never runs again
        for i in range(1, 16):
            self.out.append(f"    sw x{i}, {4 * (i - 1)}(x0)")
        self.out += [
            "    ebreak",
            "    # guard: never executes",
            "    lui x1, 0x80000",
            "    li x2, 0x5e",
            "    sb x2, 0(x1)",
            "    li x2, 0xe5",
            "    sb x2, 0(x1)",
            "    sw x2, 0x40(x0)",
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
