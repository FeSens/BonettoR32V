"""Tests for the reference ISS (verif/cosim/iss3.h, iss_main; docs/contract.md sections 3-5 and 8):
CSRs, traps to mtvec, interrupt entry, the flash/PSRAM/IO map, the IO register model with volatile
reads, the self-loop end and the core region. Programs are linked at flash 0 and loaded from
sparse 16 MiB flash hex files (@addr records).

The contract v2 half (iss.h: the spike cross-check on the v2 programs, the v2 unit cases and the
v2 program coverage) was deleted with iss.h at the integration swap (plan Task 9, ruling R26).
The v3 ISS is cross-checked against spike by the arch gate (verif/arch-test/run_arch.py: every
test's signature, ISS against spike) and the v3 programs' coverage by
verif/programs/test_programs.py.

Run: pytest verif/cosim/test_iss.py -v
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build"
ISS = BUILD / "cosim" / "iss_main"
CC = "riscv32-unknown-elf-gcc"
PRELUDE = """
    .section .text.start, "ax"
    .global _start
_start:
"""


# ------------------------------------------------------------------------------------------------
# ISS v3 (docs/contract.md): `iss_main` on tiny programs linked at flash 0
OBJCOPY = "riscv32-unknown-elf-objcopy"
UNIT3 = BUILD / "cosim" / "iss3_unit"
LINK3 = """OUTPUT_ARCH(riscv)
ENTRY(_start)
SECTIONS
{
  . = 0;
  .text : { KEEP(*(.text.start)) *(.text .text.*) *(.rodata .rodata.*) *(.data .data.*) }
}
"""
REGNUM = {n: i for i, n in enumerate("zero ra sp gp tp t0 t1 t2 s0 s1 a0 a1 a2 a3 a4 a5".split())}
MRET = 0x30200073
IRQ_CAUSE = 0x8000000B


@pytest.fixture(scope="session")
def built3():
    p = subprocess.run(["bash", str(ROOT / "verif/cosim/build.sh")], capture_output=True,
                       text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert ISS.exists()
    return True


def asm3(name: str, body: str, extra=None):
    """Assemble a snippet linked at flash 0 into a sparse flash hex file: '@0' and one byte per
    line for the code, then each extra {address: bytes} blob as its own @ record with four bytes
    per line (both forms $readmemh accepts). Returns (hex path, code bytes)."""
    UNIT3.mkdir(parents=True, exist_ok=True)
    src, elf, binf, hexfile = (UNIT3 / f"{name}{s}" for s in (".S", ".elf", ".bin", ".hex"))
    ld = UNIT3 / "link3.ld"
    ld.write_text(LINK3)
    src.write_text(PRELUDE + body + "\n")
    subprocess.run([CC, "-march=rv32e_zicsr", "-mabi=ilp32e", "-mno-relax", "-nostdlib",
                    "-nostartfiles", "-static", "-T", str(ld), "-Wl,--no-warn-rwx-segments",
                    str(src), "-o", str(elf)], check=True)
    subprocess.run([OBJCOPY, "-O", "binary", str(elf), str(binf)], check=True)
    code = binf.read_bytes()
    lines = [f"// {name}", "@0"] + [f"{b:02x}" for b in code]
    for addr, blob in (extra or {}).items():
        lines.append(f"@{addr:x}")
        lines += [" ".join(f"{b:02x}" for b in blob[i:i + 4]) for i in range(0, len(blob), 4)]
    hexfile.write_text("\n".join(lines) + "\n")
    return hexfile, code


def run3(hexfile, *args):
    """Run iss_main --log --json. Returns (rc, retirements, events, summary): events are
    the log lines in order, interrupt entries ("irq_entry": 1) included; retirements leave them
    out. Capped at 20,000 retirements unless args give --max, so a broken ISS can't flood the
    log (these programs retire a few hundred at most)."""
    cap = [] if "--max" in args else ["--max", "20000"]
    p = subprocess.run([str(ISS), str(hexfile), "--log", "--json", *cap, *args],
                       capture_output=True, text=True, timeout=60)
    lines = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    assert lines and lines[-1].get("summary"), p.stdout + p.stderr
    events = lines[:-1]
    return p.returncode, [e for e in events if not e.get("irq_entry")], events, lines[-1]


def is_csr(r) -> bool:
    return (r["insn"] & 0x7F) == 0x73 and (r["insn"] >> 12) & 7 != 0


def csr_num(r) -> int:
    return r["insn"] >> 20


# contract v3 section 4: name -> (number, read/write bits, bits that read 1)
CSR3 = {
    "mstatus": (0x300, 0x00000088, 0x1800), "misa": (0x301, 0, 0), "mie": (0x304, 0x800, 0),
    "mtvec": (0x305, 0xFFFFFFFC, 0), "mscratch": (0x340, 0xFFFFFFFF, 0),
    "mepc": (0x341, 0xFFFFFFFC, 0), "mcause": (0x342, 0x8000000F, 0), "mtval": (0x343, 0, 0),
    "mip": (0x344, 0, 0),
}
CSR3_RO = {"mvendorid": 0xF11, "marchid": 0xF12, "mimpid": 0xF13, "mhartid": 0xF14}
CSR_SRC = {"zero": 0, "t0": 0xFFFFFFFF, "t1": 0x55555555, "t2": 0x80000883}
CSR_OPS = [  # (mnemonic, rd, rs1 register or zimm); the first op sets the arbitrary reset value
    ("csrrw", "zero", "zero"), ("csrrw", "a0", "t0"), ("csrrc", "a1", "t1"), ("csrrs", "a2", "t2"),
    ("csrrwi", "a3", 0x15), ("csrrsi", "a4", 0x0A), ("csrrci", "a5", 0x1F),
    ("csrrs", "s0", "zero"), ("csrrc", "s1", "zero"), ("csrrsi", "s0", 0), ("csrrci", "s1", 0),
    ("csrrwi", "a0", 0), ("csrrs", "a1", "zero"), ("csrrw", "a2", "t1"), ("csrrs", "a3", "zero"),
]
CSR_OPS_RO = [("csrrs", "a0", "zero"), ("csrrc", "a1", "zero"), ("csrrsi", "a2", 0),
              ("csrrci", "a3", 0)]


def test_csr_rw(built3):
    lines = ["li t0, -1", "li t1, 0x55555555", "li t2, 0x80000883"]
    expect = []  # (csr name, number, mnemonic, rd, source, old value)
    for name, (num, wmask, ones) in CSR3.items():
        stored = 0
        for mn, rd, src in CSR_OPS:
            lines.append(f"{mn} {rd}, {num:#x}, {src}")
            v = CSR_SRC[src] if isinstance(src, str) else src
            old = (stored & wmask) | ones
            kind = mn[:5]
            new = v if kind == "csrrw" else (stored | v) if kind == "csrrs" else (stored & ~v)
            stored = new & wmask
            expect.append((name, num, mn, rd, src, old))
    for name, num in CSR3_RO.items():
        for mn, rd, src in CSR_OPS_RO:
            lines.append(f"{mn} {rd}, {num:#x}, {src}")
            expect.append((name, num, mn, rd, src, 0))
    lines.append("j .")
    rc, log, _, s = run3(asm3("csr_rw", "\n    ".join([""] + lines))[0])
    assert rc == 0 and s["end"] == "selfloop", s
    csr = [r for r in log if is_csr(r)]
    assert len(csr) == len(expect)
    for r, (name, num, mn, rd, src, old) in zip(csr, expect):
        where = f"{mn} {rd}, {name}, {src} at pc {r['pc']:#x}"
        assert csr_num(r) == num, where
        assert r["rd"] == REGNUM[rd] and r["rdv"] == (old if rd != "zero" else 0), (where, r)
        if isinstance(src, str):  # register forms report rs1, immediate forms 0
            assert (r["rs1"], r["rs1v"]) == (REGNUM[src], CSR_SRC[src]), where
        else:
            assert (r["rs1"], r["rs1v"]) == (0, 0), where
        assert (r["rs2"], r["rmask"], r["wmask"], r["trap"], r["intr"]) == (0, 0, 0, 0, 0), where
        assert r["npc"] == r["pc"] + 4, where


def test_csr_mip_meip_is_volatile(built3):
    # mip.MEIP is the interrupt line: the DUT's value comes through volatile_read(VSLOT_MIP = 32)
    h, _ = asm3("csr_mip", "csrr a0, mip\n csrr a1, mip\n csrrs zero, mip, zero\n j .")
    rc, log, _, s = run3(h, "--vread", "32=0xffffffff", "--vread", "32=0xffffffff")
    assert rc == 0
    a0, a1, _ = [r for r in log if is_csr(r)]
    assert a0["rdv"] == 0x800 and a0["vslot"] == 32, "only MEIP is volatile; other bits read 0"
    assert a1["rdv"] == 0x800
    assert "vslot" not in log[2], "rd = x0: mip is not read"


TRAP3 = {  # name: (instructions, mcause); the last instruction traps
    "ecall": ("ecall", 11),
    "ebreak": ("ebreak", 3),
    "jal": (".word 0x006000ef", 0),                                  # jal ra, pc+6
    "jalr": ("auipc t0, 0\n    jalr ra, 11(t0)", 0),                 # (t0 + 11) & ~1 = t0 + 10
    "branch": ("li t1, 3\n    .word 0x00001363\n    .word 0x00630363", 0),  # bne (not taken), beq
    "zero": (".word 0x00000000", 2),
    "ones": (".word 0xffffffff", 2),
}
TRAP_PROG = """
    la    t0, handler
    csrw  mtvec, t0
    csrsi mstatus, 8          # MIE = 1 before the first trap
    {trap}
    csrr  s0, mstatus         # after MRET: MIE = MPIE = 1, MPIE = 1
    csrci mstatus, 8          # MIE = 0 before the second trap
    {trap}
    csrr  s1, mstatus         # after MRET: MIE = MPIE = 0, MPIE = 1
    j .
    .align 2
handler:
    csrr  a0, mepc
    csrr  a1, mcause
    csrr  a2, mstatus
    addi  a3, a0, 4
    csrw  mepc, a3
    mret
"""


@pytest.mark.parametrize("kind", sorted(TRAP3))
def test_sync_traps(built3, kind):
    insns, cause = TRAP3[kind]
    rc, log, _, s = run3(asm3(f"trap3_{kind}", TRAP_PROG.format(trap=insns))[0])
    assert rc == 0 and s["end"] == "selfloop", s
    mtvec = next(r["rs1v"] for r in log if is_csr(r) and csr_num(r) == 0x305)
    traps = [i for i, r in enumerate(log) if r["trap"]]
    assert len(traps) == 2 and s["traps"] == 2
    assert sum(r["intr"] for r in log) == 2, "rvfi_intr only on each handler's first instruction"
    for k, old_mie in enumerate((1, 0)):
        i = traps[k]
        t = log[i]
        where = f"{kind} trap {k} at pc {t['pc']:#x}"
        assert (t["rd"], t["rdv"], t["rmask"], t["wmask"]) == (0, 0, 0, 0), where
        assert t["npc"] == mtvec and t["mcause"] == cause, where
        h = log[i + 1]
        assert h["intr"] == 1 and h["pc"] == mtvec, where
        mepc, mcause, mstatus = (log[i + n]["rdv"] for n in (1, 2, 3))
        assert mepc == t["pc"], where
        assert mcause == cause, where
        assert mstatus == 0x1800 | (old_mie << 7), f"{where}: MPIE = old MIE, MIE = 0, MPP = 3"
        mret = log[i + 6]
        assert mret["insn"] == MRET and mret["npc"] == t["pc"] + 4, where
        after = log[i + 7]
        assert after["pc"] == t["pc"] + 4 and after["intr"] == 0, where
        assert after["rdv"] == 0x1880 | (old_mie << 3), f"{where}: MIE = MPIE, MPIE = 1"
    if kind == "jalr":
        t = log[traps[0]]
        assert t["rs1"] == 5 and t["rs1v"] == log[traps[0] - 1]["rdv"], "truthful rs1"
    if kind == "branch":
        t = log[traps[0]]
        assert (t["rs1"], t["rs2"], t["rs1v"], t["rs2v"]) == (6, 6, 3, 3), "truthful rs1, rs2"
        nt = log[traps[0] - 1]
        assert nt["insn"] == 0x00001363 and not nt["trap"] and nt["npc"] == nt["pc"] + 4


IRQ_PROG = """
    la    t0, handler
    csrw  mtvec, t0
    li    t1, 0x800
    csrsi mstatus, 8          # MIE = 1 while MEIE = 0: still masked
    nop
    csrs  mie, t1             # MEIE = 1: an interrupt may be taken from here
    li    a0, 3
loop:
    addi  a0, a0, -1
    bnez  a0, loop
    li    a1, 7
    j .
    .align 2
handler:
    csrr  a2, mepc
    csrr  a3, mcause
    csrr  a4, mstatus
    mret
"""


def irq_entries(events):
    return [i for i, e in enumerate(events) if e.get("irq_entry")]


def check_irq_entry(events, k, resume_pc, mtvec):
    """events[k] is an interrupt entry before resume_pc; the handler runs and returns there."""
    e = events[k]
    assert e["intr"] == 1 and e["pc"] == resume_pc and e["npc"] == mtvec, e
    assert e["mcause"] == IRQ_CAUSE
    first = events[k + 1]
    assert first["intr"] == 1 and first["pc"] == mtvec and not first.get("irq_entry")
    mepc, mcause, mstatus = (events[k + n]["rdv"] for n in (1, 2, 3))
    assert (mepc, mcause, mstatus) == (resume_pc, IRQ_CAUSE, 0x1880)
    mret = events[k + 4]
    assert mret["insn"] == MRET and mret["npc"] == resume_pc


def test_interrupt_entry(built3):
    h, _ = asm3("irq_entry", IRQ_PROG)
    rc, log, events, s = run3(h)
    assert rc == 0 and s["end"] == "selfloop" and not irq_entries(events)
    assert not any(r["intr"] for r in log)
    mtvec = next(r["rs1v"] for r in log if is_csr(r) and csr_num(r) == 0x305)
    i_mie = next(i for i, r in enumerate(log) if is_csr(r) and csr_num(r) == 0x304)
    resume = log[i_mie]["npc"]

    # 1. requested from the start: taken at the first boundary with MIE = MEIE = 1
    rc, log1, ev1, s1 = run3(h, "--irq", "0")
    assert rc == 0 and s1["end"] == "selfloop" and s1["interrupts"] == 1
    assert irq_entries(ev1) == [i_mie + 1], "not before MEIE = 1"
    check_irq_entry(ev1, i_mie + 1, resume, mtvec)
    back = ev1[i_mie + 6]
    assert back["pc"] == resume and back["intr"] == 0, "MRET resumes the interrupted instruction"
    assert s1["retired"] == len(log1) == len(log) + 4, "the entry itself retires nothing"

    # 2. requested right after a taken branch: mepc = the branch target
    ib = next(i for i, r in enumerate(log)
              if (r["insn"] & 0x7F) == 0x63 and r["npc"] != r["pc"] + 4)
    rc, _, ev2, s2 = run3(h, "--irq", str(ib + 1))
    assert rc == 0 and s2["interrupts"] == 1
    (k,) = irq_entries(ev2)
    assert ev2[k - 1]["pc"] == log[ib]["pc"]
    check_irq_entry(ev2, k, log[ib]["npc"], mtvec)
    assert ev2[k + 5]["pc"] == log[ib]["npc"]

    # 3. requested inside the handler (MIE = 0): taken right after the MRET that sets MIE again
    rc, _, ev3, s3 = run3(h, "--irq", "0", "--irq", str(i_mie + 2))
    assert rc == 0 and s3["interrupts"] == 2
    k1, k2 = irq_entries(ev3)
    assert k2 == k1 + 5 and ev3[k2 - 1]["insn"] == MRET
    check_irq_entry(ev3, k2, resume, mtvec)


MEMMAP_PROG = """
    lui   a5, 0x80000             # IO base
    # flash at 0, aliased (addr[24] = 0; bits 30:25 ignored)
    lw    a0, 0(zero)
    li    t0, 0x02000000
    lw    a0, 0(t0)
    li    t0, 0x7e000000
    lw    a0, 0(t0)
    li    t0, 0x00abcde0          # bytes from an @ record
    lw    a1, 0(t0)
    lbu   a1, 5(t0)
    lh    a1, 3(t0)
    # a store to flash changes nothing
    li    t1, 0x12345678
    sw    t1, 0(zero)
    lw    a0, 0(zero)
    sb    t1, 1(t0)
    lw    a1, 0(t0)
    # PSRAM at 0x0100_0000, aliased every 8 MiB (bits 30:25 and 23 ignored)
    li    t0, 0x01000100
    li    t1, 0xcafef00d
    sw    t1, 0(t0)
    li    t0, 0x01800100
    lw    a2, 0(t0)
    li    t0, 0x7f000100
    lw    a2, 0(t0)
    li    t0, 0x7f800102
    lhu   a2, 0(t0)
    # misaligned PSRAM accesses are bytewise
    li    t0, 0x01000200
    sw    zero, 0(t0)
    sw    zero, 4(t0)
    li    t1, 0x44332211
    sw    t1, 1(t0)               # bytes 0x201..0x204
    lw    a3, 0(t0)
    lw    a3, 3(t0)
    lh    a3, 2(t0)
    sh    t1, 5(t0)               # bytes 0x205, 0x206
    lw    a3, 4(t0)
    li    t0, 0x01000ffe          # across a 4 KB boundary
    sw    t1, 0(t0)
    lw    a3, 0(t0)
    lbu   a3, 3(t0)
    # IO: slot addr[6:2], other bits ignored
    li    t1, 0x5a
    sw    t1, 0(a5)               # GPIO_OUT
    li    t0, 0xffffff80          # slot 0
    lw    a4, 0(t0)
    li    t0, 0x81234588          # slot 2: ALTFN
    li    t1, 0x0f
    sw    t1, 0(t0)
    lw    a4, 8(a5)
    li    t0, 0xc00000fc          # slot 31: not implemented
    sw    t1, 0(t0)
    lw    a4, 0(t0)
    # fetch from PSRAM, through an alias
    li    t0, 0x01000400
    li    t1, 0x02a00513          # addi a0, zero, 42
    sw    t1, 0(t0)
    li    t1, 0x00008067          # ret
    sw    t1, 4(t0)
    li    t0, 0x01800400
    jalr  ra, 0(t0)
    j .
"""
FAR_ADDR = 0x00ABCDE0
FAR = bytes(range(0x10, 0x20))


def test_memory_map(built3):
    h, code = asm3("memmap", MEMMAP_PROG, {FAR_ADDR: FAR})
    rc, log, _, s = run3(h)
    assert rc == 0 and s["end"] == "selfloop", s
    w0 = int.from_bytes(code[:4], "little")
    exp = [  # (address, mask, data, rd value)
        (0x0, 15, w0, w0), (0x02000000, 15, w0, w0), (0x7E000000, 15, w0, w0),
        (FAR_ADDR, 15, 0x13121110, 0x13121110), (FAR_ADDR + 5, 1, 0x15, 0x15),
        (FAR_ADDR + 3, 3, 0x1413, 0x1413),
        (0x0, 15, w0, w0), (FAR_ADDR, 15, 0x13121110, 0x13121110),
        (0x01800100, 15, 0xCAFEF00D, 0xCAFEF00D), (0x7F000100, 15, 0xCAFEF00D, 0xCAFEF00D),
        (0x7F800102, 3, 0xCAFE, 0xCAFE),
        (0x01000200, 15, 0x33221100, 0x33221100), (0x01000203, 15, 0x00004433, 0x00004433),
        (0x01000202, 3, 0x3322, 0x3322), (0x01000204, 15, 0x00221144, 0x00221144),
        (0x01000FFE, 15, 0x44332211, 0x44332211), (0x01001001, 1, 0x44, 0x44),
        (0xFFFFFF80, 15, 0x5A, 0x5A), (0x80000008, 15, 0x0F, 0x0F), (0xC00000FC, 15, 0, 0),
    ]
    got = [(r["maddr"], r["rmask"], r["mrdata"], r["rdv"]) for r in log if r["rmask"]]
    assert got == exp
    stores = [(r["maddr"], r["wmask"], r["mwdata"]) for r in log if r["wmask"]]
    assert stores[:2] == [(0x0, 15, 0x12345678), (FAR_ADDR + 1, 1, 0x78)], \
        "stores to flash retire normally (and change nothing)"
    fetched = [r for r in log if r["pc"] >> 24 == 0x01]
    assert [(r["pc"], r["insn"]) for r in fetched] == [(0x01800400, 0x02A00513),
                                                       (0x01800404, 0x00008067)]
    assert fetched[0]["rdv"] == 42 and fetched[1]["npc"] == fetched[1]["rs1v"]
    assert s["gpio_out"] == 0x5A and s["altfn"] == 0x0F
    assert s["uo_trace"] == [0x5A, 0x50], "GPIO-mode pins: GPIO_OUT & ~ALTFN"
    assert s["touched_region"] is False


BYTE_LANES_PROG = """
    lui   a5, 0x80000
    # TIMER_CTRL (slot 6, bits 9:0): a store writes only the bytes it covers
    li    t0, -1
    sw    t0, 0x18(a5)
    lw    a0, 0x18(a5)            # 0x3ff
    sb    zero, 0x19(a5)          # bits 15:8 only
    lw    a0, 0x18(a5)            # 0x0ff
    li    t1, 2
    sb    t1, 0x19(a5)
    lw    a0, 0x18(a5)            # 0x2ff
    sb    zero, 0x18(a5)          # bits 7:0 only
    lw    a0, 0x18(a5)            # 0x200
    sh    t0, 0x1a(a5)            # bytes 2-3: nothing implemented
    lw    a0, 0x18(a5)            # 0x200
    lbu   a1, 0x19(a5)            # 0x02
    lhu   a1, 0x18(a5)            # 0x200
    # IRQ_PEND (slot 16): CMP, CAP are W1C; RX, TXIDLE, GPIO read-only
    lw    a0, 0x40(a5)            # DUT (--vread 16=3): CMP = CAP = 1
    li    t1, 1
    sw    t1, 0x40(a5)            # clear CMP
    lw    a0, 0x40(a5)            # model: CAP | TXIDLE = 0x0a
    li    t1, 0x1c
    sw    t1, 0x40(a5)            # read-only bits: no effect
    lw    a0, 0x40(a5)            # 0x0a
    sb    zero, 0x40(a5)          # writing 0 clears nothing
    lw    a0, 0x40(a5)            # 0x0a
    li    t1, 2
    sb    t1, 0x40(a5)            # clear CAP with a byte store
    lw    a0, 0x40(a5)            # 0x08
    # UART: reading byte 0 of UART_DATA clears RX_FULL and RX_OVR
    lw    a0, 0x30(a5)            # DUT (--vread 12=0xc000): RX_OVR | RX_FULL
    lbu   a1, 0x2d(a5)            # UART_DATA byte 1: no side effect
    lw    a0, 0x30(a5)            # model 0xc000
    lbu   a1, 0x2c(a5)            # UART_DATA byte 0 (--vread 11=0x41)
    lw    a0, 0x30(a5)            # model 0x0000
    li    t1, 0x55
    sb    t1, 0x2d(a5)            # UART_DATA byte 1: not a send
    lw    a0, 0x30(a5)            # TX_BUSY = 0
    sb    t1, 0x2c(a5)            # byte 0: send, TX_BUSY = 1
    lw    a0, 0x30(a5)            # 0x2000
    lw    a0, 0x40(a5)            # TXIDLE = 0: 0x00
    j .
"""


def test_io_byte_lanes(built3):
    h, _ = asm3("io_lanes", BYTE_LANES_PROG)
    rc, log, _, s = run3(h, "--vread", "16=3", "--vread", "12=0xc000", "--vread", "11=0x41")
    assert rc == 0 and s["end"] == "selfloop", s
    loads = [(r["maddr"] & 0xFF, r["rdv"]) for r in log if r["rmask"]]
    assert loads == [
        (0x18, 0x3FF), (0x18, 0x0FF), (0x18, 0x2FF), (0x18, 0x200), (0x18, 0x200), (0x19, 0x02),
        (0x18, 0x200),
        (0x40, 0x03), (0x40, 0x0A), (0x40, 0x0A), (0x40, 0x0A), (0x40, 0x08),
        (0x30, 0xC000), (0x2D, 0x00), (0x30, 0xC000), (0x2C, 0x41), (0x30, 0x0000),
        (0x30, 0x0000), (0x30, 0x2000), (0x40, 0x00),
    ]
    # the ISS's own value for each volatile read (what volatile_read receives)
    vol = [(r["vslot"], r["viss"], r["vdut"]) for r in log if "vslot" in r]
    assert vol == [
        (16, 0x08, 0x03), (16, 0x0A, 0x0A), (16, 0x0A, 0x0A), (16, 0x0A, 0x0A), (16, 0x08, 0x08),
        (12, 0x0000, 0xC000), (12, 0xC000, 0xC000), (11, 0x00, 0x41), (12, 0x0000, 0x0000),
        (12, 0x0000, 0x0000), (12, 0x2000, 0x2000), (16, 0x00, 0x00),
    ]
    assert not any("vslot" in r for r in log if r["rmask"] and (r["maddr"] & 0xFF) in (0x18, 0x19))


# contract v3 section 5.1: reset values and the value read after writing all ones to each slot
# in order (writing UART_DATA and SPI_DATA starts a transfer: TX_BUSY and BUSY read 1 next)
IO_RESET = {14: 0x1, 16: 0x08}
IO_ONES = {0: 0xFF, 2: 0xFF, 3: 0xFFFF, 4: 0xFFFF, 6: 0x3FF, 7: 0x7FFF, 8: 0x3FF, 9: 0x3FF,
           10: 0x3FF, 12: 0x3FFF, 14: 0x1F, 15: 0x1F, 16: 0x00, 17: 0xFFFF}


def test_io_byte_lanes_reset_and_write_masks(built3):
    body = ["lui a5, 0x80000", "li t0, -1"]
    body += [f"lw a0, {4 * n}(a5)" for n in range(32)]
    for n in range(32):
        body += [f"sw t0, {4 * n}(a5)", f"lw a0, {4 * n}(a5)"]
    body.append("j .")
    rc, log, _, s = run3(asm3("io_masks", "\n    ".join([""] + body))[0])
    assert rc == 0, s
    loads = [r["rdv"] for r in log if r["rmask"]]
    assert loads[:32] == [IO_RESET.get(n, 0) for n in range(32)], "reset values"
    assert loads[32:] == [IO_ONES.get(n, 0) for n in range(32)], "write masks"


# The volatile bits of each slot for loads (the brief: GPIO_IN, TIMER_CNT, TIMER_CAP, UART_DATA
# read, UART_CTRL[15:13], SPI_DATA read, SPI_CTRL[4], IRQ_PEND, GPIO_EPEND), and mip.MEIP.
VOLATILE3 = {1: 0xFF, 3: 0xFFFF, 5: 0xFFFF, 11: 0xFF, 12: 0xE000, 13: 0xFF, 14: 0x10, 16: 0x1F,
             18: 0xFF}


def test_io_volatile_mask_table(built3):
    # The DUT answers all ones for every slot: a load takes exactly the volatile bits from it, and
    # the hook fires only where there are volatile bits.
    body = ["lui a5, 0x80000"] + [f"lw a0, {4 * n}(a5)" for n in range(32)]
    body += ["csrr a0, mip", "j ."]
    vreads = [a for n in range(33) for a in ("--vread", f"{n}=0xffffffff")]
    rc, log, _, s = run3(asm3("io_volatile", "\n    ".join([""] + body))[0], *vreads)
    assert rc == 0 and s["end"] == "selfloop", s
    loads = [r for r in log if r["rmask"]]
    assert len(loads) == 32
    for n, r in enumerate(loads):
        mask = VOLATILE3.get(n, 0)
        assert r["rdv"] == (IO_RESET.get(n, 0) & ~mask) | mask, f"slot {n}: {r['rdv']:#x}"
        assert ("vslot" in r) == bool(mask), f"slot {n}: the hook fires only for volatile slots"
        if mask:
            assert (r["vslot"], r["vdut"]) == (n, 0xFFFFFFFF), f"slot {n}"
    mip = next(r for r in log if is_csr(r))
    assert (mip["rdv"], mip["vslot"], mip["vdut"]) == (0x800, 32, 0xFFFFFFFF)


def test_jalr_odd_target_does_not_trap_v3(built3):
    # JALR clears bit 0 first; only target[1] = 1 traps (contract 4)
    body = """
    auipc t0, 0
    jalr  ra, 9(t0)           # (t0 + 9) & ~1 = t0 + 8: the next instruction
    auipc t0, 0
    jalr  ra, 13(t0)          # (t0 + 13) & ~1 = t0 + 12: over the ecall
    ecall
    li    a0, 1
    j .
"""
    rc, log, _, s = run3(asm3("jalr_odd3", body)[0])
    assert rc == 0 and s["end"] == "selfloop" and s["traps"] == 0, s
    jalrs = [i for i, r in enumerate(log) if (r["insn"] & 0x7F) == 0x67]
    assert len(jalrs) == 2
    for i, off in zip(jalrs, (9, 13)):
        j = log[i]
        assert j["trap"] == 0 and j["rs1"] == 5 and j["rs1v"] == log[i - 1]["rdv"]
        assert j["npc"] == j["rs1v"] + off - 1 and log[i + 1]["pc"] == j["npc"]
        assert (j["rd"], j["rdv"]) == (1, j["pc"] + 4)


def test_selfloop_end(built3):
    h, _ = asm3("selfloop3", "li a0, 1\n j .")
    rc, log, _, s = run3(h)
    assert rc == 0 and s["end"] == "selfloop" and s["retired"] == 3
    assert [r["insn"] for r in log[1:]] == [0x6F, 0x6F] and log[1]["pc"] == log[2]["pc"]
    p = subprocess.run([str(ISS), str(h), "--max", "20000"], capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0 and "selfloop" in p.stdout and "{" not in p.stdout, \
        "without --json the summary is plain text"
    # --v3 (the mode flag before the swap) is still accepted and changes nothing
    p3 = subprocess.run([str(ISS), "--v3", str(h), "--max", "20000"], capture_output=True,
                        text=True, timeout=60)
    assert (p3.returncode, p3.stdout) == (p.returncode, p.stdout)
    h, _ = asm3("noloop3", "1: addi a0, a0, 1\n j 1b")
    rc, log, _, s = run3(h, "--max", "100")
    assert rc == 4 and s["end"] == "max" and s["retired"] == 100
    # An interrupt entry retires nothing: jal x0, 0, entry, jal x0, 0 (the handler is the loop
    # itself) are still two retirements in a row at the same pc.
    h, _ = asm3("selfloop3_irq", "la t0, 1f\n csrw mtvec, t0\n li t1, 0x800\n csrs mie, t1\n"
                "csrsi mstatus, 8\n1: j 1b")
    rc, log, _, s = run3(h)
    first_j = len(log) - 2
    rc, log, events, s = run3(h, "--irq", str(first_j + 1))
    assert rc == 0 and s["end"] == "selfloop" and s["interrupts"] == 1, s
    assert s["retired"] == first_j + 2 and log[-1]["intr"] == 1 and events[-2].get("irq_entry")


def psram_seed_byte(a: int) -> int:
    """iss3.h psram_seed_byte: the model's initial PSRAM content."""
    m = 0xFFFFFFFF
    x = ((a & 0x7FFFFF) * 0x9E3779B1 + 0x7F4A7C15) & m
    x ^= x >> 16
    x = (x * 0x7FEB352D) & m
    x ^= x >> 15
    x = (x * 0x846CA68B) & m
    x ^= x >> 16
    return x & 0xFF


def test_dump_psram_v3(built3, tmp_path):
    # --dump-psram (run_arch.py reads signatures from it): the final 8 MiB PSRAM image, stored
    # bytes at their PSRAM offsets (through any alias), the seed pattern elsewhere, whatever the
    # end reason
    h, _ = asm3("dump_psram3", "li t0, 0x01000100\n li t1, 0x11223344\n sw t1, 0(t0)\n"
                "li t0, 0x037FFF7E\n li t1, 0xA5\n sb t1, 0(t0)\n j .")
    out = tmp_path / "psram.bin"
    rc, _, _, s = run3(h, "--dump-psram", str(out))
    m = out.read_bytes()
    assert rc == 0 and s["end"] == "selfloop" and len(m) == 1 << 23
    assert m[0x100:0x104] == bytes([0x44, 0x33, 0x22, 0x11]) and m[0x7FFF7E] == 0xA5
    assert all(m[a] == psram_seed_byte(a) for a in (0, 0xFF, 0x104, 0x7FFF7D, 0x7FFF7F, 0x7FFFFF))
    rc, _, _, s = run3(h, "--dump-psram", str(out), "--max", "5")  # 2 li (2 each), the sw
    m = out.read_bytes()
    assert rc == 4 and s["end"] == "max" and m[0x100:0x104] == bytes([0x44, 0x33, 0x22, 0x11])
    assert m[0x7FFF7E] == psram_seed_byte(0x7FFF7E), "the sb hadn't run yet"


REGION3 = {
    "lw_017fff80": "li t0, 0x017fff80\n lw a0, 0(t0)",
    "sb_017fffff": "li t0, 0x017fffff\n sb zero, 0(t0)",
    "lw_017fff7d": "li t0, 0x017fff7d\n lw a0, 0(t0)",        # its last byte is 0x7fff80
    "sh_017fff7f": "li t0, 0x017fff7f\n sh zero, 0(t0)",
    "alias_bit23": "li t0, 0x01ffff80\n lw a0, 0(t0)",
    "alias_bits30_25": "li t0, 0x7fffffc0\n lhu a0, 0(t0)",
    "fetch": "li t0, 0x017fff80\n jalr zero, 0(t0)",
}


@pytest.mark.parametrize("name", sorted(REGION3))
def test_region_touch(built3, name):
    rc, log, _, s = run3(asm3(f"region3_{name}", REGION3[name] + "\n j .")[0])
    assert rc == 3 and s["end"] == "region" and s["touched_region"] is True, s
    assert s["retired"] == len(log) and not any(r["rmask"] or r["wmask"] for r in log)
    if name == "fetch":
        assert s["end_pc"] == 0x017FFF80


def test_region_neighbours_are_not_region(built3):
    body = """
    li    t0, 0x017fff7c
    sw    zero, 0(t0)             # 0x7fff7c..0x7fff7f: the last bytes below the region
    lw    a0, 0(t0)
    li    t0, 0x007fff80          # addr[24] = 0: flash
    lw    a0, 0(t0)
    li    t0, 0x017fff80
    j .
"""
    rc, log, _, s = run3(asm3("region3_near", body)[0])
    assert rc == 0 and s["end"] == "selfloop" and s["touched_region"] is False, s


UNSPEC3 = {
    "fetch_io": "li t0, 0x80000000\n jalr zero, 0(t0)",
    "io_misaligned_lw": "lui a5, 0x80000\n lw a0, 2(a5)",
    "io_misaligned_sh": "lui a5, 0x80000\n sh zero, 1(a5)",
    "span_flash_psram": "li t0, 0x00fffffe\n lw a0, 0(t0)",
    "span_alias_flash_psram": "li t0, 0x7efffffd\n lw a0, 0(t0)",
    "csr_unknown": "csrr a0, 0x7c0",
    "csr_write_mhartid": "csrw mhartid, zero",
    "mul": ".word 0x021080b3",
    "add_rd16": ".word 0x00000833",
    "csr_rs1_16": ".word 0x34081073",  # csrrw zero, mscratch, x16
    "sret": ".word 0x10200073",
    "fence_i": ".word 0x0000100f",
}


@pytest.mark.parametrize("name", sorted(UNSPEC3))
def test_unspecified_ends_run_v3(built3, name):
    rc, log, _, s = run3(asm3(f"unspec3_{name}", UNSPEC3[name] + "\n j .")[0])
    assert rc == 2 and s["end"] == "unspecified" and s["touched_region"] is False, s


@pytest.mark.parametrize("body", ["csrr a0, mepc", "csrr a0, mscratch", "csrr a0, mcause",
                                  "csrr a0, mtvec", "ecall", "mret"])
def test_uninit_csr_read_is_flagged_v3(built3, body):
    # mtvec, mepc, mscratch and mcause reset to arbitrary values (contract 4)
    h, _ = asm3("uninit3_" + re.sub(r"\W", "_", body), f"{body}\n j .")
    rc, log, _, s = run3(h, "--check-init", "--max", "50")
    assert rc == 5 and s["uninit_read"] is True, s
    h, _ = asm3("init3_ok", "la t0, 1f\n csrw mtvec, t0\n csrw mepc, t0\n csrrw a0, mepc, t0\n"
                "csrci mstatus, 8\n ecall\n .align 2\n1: j .")
    rc, log, _, s = run3(h, "--check-init")
    assert rc == 0 and s["uninit_read"] is False, s


def test_reset_interrupt_state_is_known_v3(built3):
    # contract 4 (R23): mstatus.MIE, mstatus.MPIE and mie.MEIE reset to 0, so reading them first
    # is not a read before write; MRET with MPIE never written gives MIE = 0.
    h, _ = asm3("reset3_known", "csrr a0, mstatus\n csrr a1, mie\n la t0, 1f\n csrw mepc, t0\n"
                " mret\n .align 2\n1: csrr a2, mstatus\n j .")
    rc, log, _, s = run3(h, "--check-init")
    assert rc == 0 and s["uninit_read"] is False and s["end"] == "selfloop", s
    rd = {r["rd"]: r["rdv"] for r in log if is_csr(r)}
    a0, a1, a2 = REGNUM["a0"], REGNUM["a1"], REGNUM["a2"]
    assert rd[a0] == 0x1800, "mstatus at reset: MIE = MPIE = 0, MPP = 3"
    assert rd[a1] == 0, "mie at reset: MEIE = 0"
    assert rd[a2] == 0x1880, "after MRET: MIE = old MPIE = 0, MPIE = 1"
