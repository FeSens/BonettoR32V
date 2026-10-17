#!/usr/bin/env python3
"""Generate a deterministic random RV32E instruction stream for the cosim (contract v3).

Usage: gen_random.py SEED N [-o OUT.S]

The program runs from flash (link.ld). Its prologue copies the data window's initial bytes from
flash to PSRAM (CPU 0x0100_8000-0x0100_83FF: PSRAM starts arbitrary, contract 3) and fills the 16
bytes below the core region (0x017F_FF70-0x017F_FF7F), then gives every register a value. Then
come N random instructions (valid RV32E only, never a reserved encoding, never a trap):
  - ALU ops: register, immediate and shift forms, LUI, AUIPC, edge-value constants, on all of
    x1..x15 (and x0 as a source or destination);
  - PSRAM loads and stores of every width, misaligned included, in the data window or the 16
    bytes below the core region (up to its last byte below the region), with random PSRAM alias
    bits (addr[30:25] and addr[23]). Nothing touches the core region;
  - flash loads (the .fwindow block of random bytes at 0x0001_0000, and the first 8 KiB of code)
    with random alias bits addr[30:25], and a few flash stores, which change nothing (contract 3);
  - IO loads and stores on slots without side effects, with random address bits addr[30:7]
    (the slot is addr[6:2]), naturally aligned on every byte lane: loads from GPIO_OUT, ALTFN,
    TIMER_CMP, TIMER_CTRL, PWM_CTRL, PWM_DUTY0-2, IRQ_EN, GPIO_EDGE and the unimplemented slots
    19-31; stores to GPIO_OUT, TIMER_CMP, PWM_DUTY0-2, IRQ_PEND and GPIO_EPEND (W1C, nothing
    pending) and slots 19-31. No volatile slot is read and nothing starts a counter or transfer;
  - the base register is either loaded just before the access (li) or a register whose value the
    generator still knows (it forgets everything at each branch target);
  - forward branches (every condition, taken and not taken), forward JAL and JALR (with bit 0 of
    the target set, negative offsets, rd == rs1), x0 as a destination, FENCE variants;
  - bounded loops: a backward branch closing 1-4 iterations of a short random body; the counter
    register is written only by the loop itself, and no branch from outside enters the body.
The epilogue stores x1..x15 to 0x0100_9000..0x0100_903B (x15 through mscratch), so the pins-mode
PSRAM comparison sees every register, then writes GPIO_OUT = 0x5A and self-loops (contract 4
program end). The `_psram_window` table names the window, the dump and the top 16 bytes.
GPIO_OUT takes random values on the way, so a random stream may show 0xA5, 0xC0 or 0xC1, but
never 0xEE (the fail mark): a store that reaches GPIO_OUT[7:0] takes its data from x0 or from a
register just loaded with a value whose low byte isn't 0xEE.
"""
import argparse
import random
import sys

PSRAM = 0x01000000        # CPU address of PSRAM byte 0
WIN_LO, WIN_HI = 0x8000, 0x8400   # PSRAM byte ranges, half-open: the data window
REGION_LO = 0x7FFF80      # contract 3: the core region 0x7F_FF80-0x7F_FFFF
TOP_LO = 0x7FFF70         # the 16 bytes below it
DUMP = 0x01009000         # register dump (CPU address)
FW_LO, FW_HI = 0x10000, 0x11000   # flash .fwindow
CODE_HI = 0x2000          # flash loads may also read the first 8 KiB of code
IO = 0x80000000
IO_LOAD_SLOTS = [0, 2, 4, 6, 7, 8, 9, 10, 15, 17] + list(range(19, 32))
IO_STORE_SLOTS = [0, 4, 8, 9, 10, 16, 18] + list(range(19, 32))
M32 = 0xFFFFFFFF
FAIL_MARK = 0xEE          # GPIO_OUT value of a failed self-check: never written here

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
    # kind: "psram", "flash" or "io"
    @staticmethod
    def valid(ea: int, n: int, kind: str, store: bool) -> bool:
        if kind == "io":
            slots = IO_STORE_SLOTS if store else IO_LOAD_SLOTS
            return ea >> 31 == 1 and ea % n == 0 and (ea >> 2) & 31 in slots
        if ea >> 31:
            return False
        if kind == "flash":
            lo = ea & 0xFFFFFF
            if (ea >> 24) & 1 or lo + n > 1 << 24:
                return False
            return FW_LO <= lo and lo + n <= FW_HI or (not store and lo + n <= CODE_HI)
        if not (ea >> 24) & 1:
            return False
        lo = ea & 0x7FFFFF
        return WIN_LO <= lo and lo + n <= WIN_HI or TOP_LO <= lo and lo + n <= REGION_LO

    def pick_ea(self, n: int, kind: str, store: bool) -> int:
        r = self.r
        if kind == "io":
            slots = IO_STORE_SLOTS if store else IO_LOAD_SLOTS
            slot = 0 if r.random() < 0.4 else r.choice(slots)  # GPIO_OUT often: uo_out changes
            lane = r.randrange(0, 4) & ~(n - 1)
            high = 0 if r.random() < 0.5 else r.getrandbits(24) << 7
            return IO | high | slot << 2 | lane
        if kind == "flash":
            alias = 0 if r.random() < 0.5 else r.getrandbits(6) << 25
            if store or r.random() < 0.8:
                lo = r.randint(FW_LO, FW_HI - n)
            else:
                lo = r.randint(0, CODE_HI - n)
            return alias | lo
        alias = 0 if r.random() < 0.4 else (r.getrandbits(6) << 25) | (r.getrandbits(1) << 23)
        if r.random() < 0.85:
            lo = r.randint(WIN_LO, WIN_HI - n)
        elif r.random() < 0.5:
            lo = REGION_LO - n  # ends at 0x7F_FF7F, the last byte below the core region
        else:
            lo = r.randint(TOP_LO, REGION_LO - n)
        return PSRAM | alias | lo

    def mem_base(self, n: int, kind: str, store: bool) -> tuple[list[str], int, int, int]:
        """Base register, offset and effective address for one access, plus setup instructions
        (emitted in the same unit as the access)."""
        r = self.r
        if self.known and r.random() < 0.4:
            regs = list(self.known.items())
            r.shuffle(regs)
            for reg, v in regs:
                for _ in range(8):
                    off = r.randint(-2048, 2047)
                    if self.valid((v + off) & M32, n, kind, store):
                        return [], reg, off, (v + off) & M32
        ea = self.pick_ea(n, kind, store)
        assert self.valid(ea, n, kind, store), (hex(ea), n, kind, store)
        off = r.randint(-2048, 2047)
        rb = self.wreg()
        return [self.li(rb, ea - off)], rb, off, ea

    def access(self, kind: str, store: bool) -> None:
        op, n = self.r.choice(STORES if store else LOADS)
        setup, rb, off, ea = self.mem_base(n, kind, store)
        base = f"x{rb}"
        if store:
            src = self.src()
            if kind == "io" and (ea >> 2) & 31 == 0 and ea & 3 == 0 and src != "x0":
                # the store reaches GPIO_OUT[7:0]: data with a low byte other than the fail mark
                rd = self.r.choice([i for i in self.free() if i != rb])
                v = self.r.getrandbits(32)
                if v & 0xFF == FAIL_MARK:
                    v ^= 0x01
                setup = setup + [self.li(rd, v)]
                src = f"x{rd}"
            self.emit(*setup, f"{op} {src}, {off}({base})", n=2 * len(setup) + 1)
        else:
            self.emit(*setup, f"{op} {self.dst()}, {off}({base})", n=2 * len(setup) + 1)

    def load(self) -> None:
        self.access("psram", store=False)

    def store(self) -> None:
        self.access("psram", store=True)

    def flash_load(self) -> None:
        self.access("flash", store=False)

    def flash_store(self) -> None:
        self.access("flash", store=True)

    def io_load(self) -> None:
        self.access("io", store=False)

    def io_store(self) -> None:
        self.access("io", store=True)

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
                 (self.upper, 3), (self.const, 4), (self.load, 9), (self.store, 10),
                 (self.flash_load, 4), (self.flash_store, 1), (self.io_load, 3),
                 (self.io_store, 4), (self.branch, 11), (self.jal, 2), (self.jalr, 2),
                 (self.fence, 1), (self.loop, 2)]
        return [k for k, _ in kinds], [w for _, w in kinds]

    def body(self) -> None:
        funcs, weights = self.kinds()
        while self.count < self.n:
            self.r.choices(funcs, weights)[0]()

    def program(self, seed: int) -> str:
        r = self.r
        top = [r.getrandbits(32) for _ in range(4)]
        self.out = [
            f"# Random RV32E stream: seed {seed}, {self.n} instructions. Generated by gen_random.py.",
            "    .section .text.start, \"ax\"",
            "    .global _start",
            "_start:",
            "    # the data window's initial bytes: flash -> PSRAM (PSRAM starts arbitrary)",
            "    lui x1, %hi(_window_lma)",
            "    addi x1, x1, %lo(_window_lma)",
            "    lui x2, %hi(_window_start)",
            "    addi x2, x2, %lo(_window_start)",
            "    lui x3, %hi(_window_end)",
            "    addi x3, x3, %lo(_window_end)",
            "1:  lw x4, 0(x1)",
            "    sw x4, 0(x2)",
            "    addi x1, x1, 4",
            "    addi x2, x2, 4",
            "    bltu x2, x3, 1b",
            "    # the 16 bytes below the core region",
            f"    li x1, {PSRAM + TOP_LO:#x}",
        ]
        for k, v in enumerate(top):
            self.out += [f"    li x2, {v:#x}", f"    sw x2, {4 * k}(x1)"]
        # every register gets a value before any read (the core's registers start unknown)
        for i in REGS:
            v = r.choice(EDGE) if r.random() < 0.3 else r.getrandbits(32)
            self.out.append("    " + self.li(i, v))
        self.body()
        self.flush_labels()
        # register dump: x15 goes through mscratch, then x15 is the base
        self.out += ["    csrw mscratch, x15", f"    lui x15, {DUMP >> 12:#x}"]
        for i in range(1, 15):
            self.out.append(f"    sw x{i}, {4 * (i - 1)}(x15)")
        self.out += [
            "    csrr x14, mscratch",
            "    sw x14, 56(x15)",
            "    lui x1, 0x80000",
            "    li x2, 0x5a",
            "    sw x2, 0(x1)          # GPIO_OUT = 0x5A: the end (ALTFN is still 0)",
            "2:  j 2b",
            "",
            "    .section .rodata",
            "    .align 2",
            "    .global _psram_window",
            "_psram_window:",
            f"    .word {PSRAM + WIN_LO:#x}, {PSRAM + WIN_HI:#x}",
            f"    .word {DUMP:#x}, {DUMP + 60:#x}",
            f"    .word {PSRAM + TOP_LO:#x}, {PSRAM + REGION_LO:#x}",
            "    .word 0, 0",
            "",
            "    .section .window, \"aw\"",
        ]
        data = [r.getrandbits(8) for _ in range(WIN_HI - WIN_LO)]
        for i in range(0, len(data), 16):
            self.out.append("    .byte " + ", ".join(f"{b:#04x}" for b in data[i:i + 16]))
        self.out += ["", "    .section .fwindow, \"a\""]
        data = [r.getrandbits(8) for _ in range(FW_HI - FW_LO)]
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
