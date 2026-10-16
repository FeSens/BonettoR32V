"""Tests for the reference ISS (verif/cosim/iss.h, iss_main).

1. Cross-check against spike on selftest, gpio_demo and coremark: every retirement's pc, insn,
   register write, store and load address must match spike's commit log. The programs are the
   spike builds from `make -C verif/programs spike` (linked at 0x10000, because spike's debug
   module sits at [0, 0x1000); selftest built with -DSPIKE_XCHECK skips its wrap, alias and GPIO
   sections). The ISS starts at pc 0x10000 (aliases RAM 0) with IO modeled as a RAM page, which is
   what spike does with -m0x80000000:0x1000.
2. Unit cases for what spike can't check: the fatal traps, the 64 KB wrap and aliasing, the GPIO
   byte lanes, reserved encodings, FENCE, register-field conventions and the ui_in schedules.

Run: pytest verif/cosim/test_iss.py -v
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build"
ISS = BUILD / "cosim" / "iss_main"
PROGS = BUILD / "programs"
SPIKE_PROGS = PROGS / "spike"
UNIT = BUILD / "cosim" / "iss_unit"
CC = "riscv32-unknown-elf-gcc"
M32 = 0xFFFFFFFF


def ui_value(seed: int, k: int) -> int:
    """Python twin of ui_value() in iss.h."""
    x = ((k & M32) * 0x9E3779B1 & M32) ^ (((seed + 0x7F4A7C15) & M32) * 0x85EBCA6B & M32)
    x ^= x >> 16
    x = x * 0x7FEB352D & M32
    x ^= x >> 15
    x = x * 0x846CA68B & M32
    x ^= x >> 16
    return x & 0xFF


@pytest.fixture(scope="session")
def built():
    subprocess.run(["make", "-C", str(ROOT / "verif/programs"), "-j8", "all", "spike"],
                   check=True, capture_output=True, text=True)
    assert ISS.exists()
    return True


def run_iss(hexfile, *args, max_n=None):
    cmd = [str(ISS), str(hexfile), "--log", *args]
    if max_n:
        cmd += ["--max", str(max_n)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    lines = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    return p.returncode, lines[:-1], lines[-1]


# ------------------------------------------------------------------------------------------------
# 1. spike cross-check
SPIKE_LINE = re.compile(r"^core\s+0: 3 0x([0-9a-f]+) \(0x([0-9a-f]+)\)(.*)$")
SPIKE_EXC = re.compile(r"^core\s+0: exception (\S+), epc 0x([0-9a-f]+)")


def spike_commits(elf: Path, limit: int, logpath: Path):
    """Yield ('commit', pc, insn, rd, rdv, load_addr, store) or ('exception', name, epc)."""
    subprocess.run(["spike", "-l", "--log-commits", f"--log={logpath}", "--isa=rv32e",
                    "--misaligned", "--disable-dtb", "-m0x1000:0x1f000,0x80000000:0x1000",
                    f"--instructions={limit}", str(elf)],
                   check=False, capture_output=True, timeout=600)
    with open(logpath) as f:
        for line in f:
            m = SPIKE_LINE.match(line)
            if m:
                pc, insn = int(m.group(1), 16), int(m.group(2), 16)
                toks = m.group(3).split()
                rd = rdv = load = store = None
                i = 0
                while i < len(toks):
                    t = toks[i]
                    if re.fullmatch(r"x\d+", t):
                        rd, rdv = int(t[1:]), int(toks[i + 1], 16)
                        i += 2
                    elif t == "mem":
                        addr = int(toks[i + 1], 16)
                        if i + 2 < len(toks) and toks[i + 2].startswith("0x"):
                            data = toks[i + 2]
                            store = (addr, int(data, 16), (len(data) - 2) // 2)
                            i += 3
                        else:
                            load = addr
                            i += 2
                    else:
                        i += 1
                yield ("commit", pc, insn, rd, rdv, load, store)
                continue
            m = SPIKE_EXC.match(line)
            if m:
                yield ("exception", m.group(1), int(m.group(2), 16))
                return


@pytest.mark.parametrize("prog", ["selftest", "gpio_demo", "coremark"])
def test_iss_matches_spike(built, prog, tmp_path):
    if not shutil.which("spike"):
        pytest.skip("spike not installed")
    hexfile = SPIKE_PROGS / f"{prog}.hex"
    rc, iss_log, summary = run_iss(hexfile, "--start-pc", "0x10000", "--io-ram", "--ui", "const:0")
    assert rc == 0, f"ISS did not halt on EBREAK: {summary}"
    assert iss_log[-1]["insn"] == 0x00100073 and iss_log[-1]["trap"] == 1
    events = spike_commits(SPIKE_PROGS / f"{prog}.elf", len(iss_log) + 16, tmp_path / "spike.log")
    first = next(events)
    assert first[0] == "commit" and first[1] == 0x1000, "expected the spike trampoline first"
    for i, r in enumerate(iss_log[:-1]):
        ev = next(events)
        where = f"{prog} retirement {i} pc {r['pc']:#x} insn {r['insn']:#010x}"
        assert ev[0] == "commit", f"{where}: spike raised {ev}"
        _, pc, insn, rd, rdv, load, store = ev
        assert (pc, insn) == (r["pc"], r["insn"]), f"{where}: spike at {pc:#x} {insn:#010x}"
        if r["rd"]:
            assert (rd, rdv) == (r["rd"], r["rdv"]), f"{where}: spike wrote x{rd}={rdv}"
        else:
            assert rd is None, f"{where}: spike wrote x{rd}"
        if r["wmask"]:
            nbytes = {1: 1, 3: 2, 15: 4}[r["wmask"]]
            assert store is not None, f"{where}: spike logged no store"
            assert store[0] == r["maddr"] and store[2] == nbytes, f"{where}: spike store {store}"
            assert store[1] == r["mwdata"], f"{where}: spike stored {store[1]:#x}"
        else:
            assert store is None, f"{where}: spike stored {store}"
        if r["rmask"]:
            assert load == r["maddr"], f"{where}: spike load address {load}"
    ev = next(events)
    assert ev[0] == "exception" and ev[1] == "trap_breakpoint" and ev[2] == iss_log[-1]["pc"], ev


# ------------------------------------------------------------------------------------------------
# 2. unit cases
PRELUDE = """
    .section .text.start, "ax"
    .global _start
_start:
"""


def asm(name: str, body: str) -> Path:
    """Assemble a snippet linked at 0 into a 64 KB image."""
    UNIT.mkdir(parents=True, exist_ok=True)
    src = UNIT / f"{name}.S"
    elf = UNIT / f"{name}.elf"
    hexfile = UNIT / f"{name}.hex"
    src.write_text(PRELUDE + body + "\n")
    subprocess.run([CC, "-march=rv32e", "-mabi=ilp32e", "-mno-relax", "-nostdlib", "-nostartfiles",
                    "-static",
                    "-T", str(ROOT / "verif/programs/link.ld"), "-Wl,--no-warn-rwx-segments",
                    str(src), "-o", str(elf)], check=True)
    subprocess.run(["python3", str(ROOT / "verif/programs/elf2hex.py"), str(elf), str(hexfile)],
                   check=True)
    return hexfile


def dump_ram(hexfile: Path, *args) -> bytes:
    out = hexfile.with_suffix(".ram")
    subprocess.run([str(ISS), str(hexfile), "--dump-ram", str(out), *args], capture_output=True)
    return out.read_bytes()


def by_insn(log, insn):
    return [r for r in log if r["insn"] == insn]


def test_ecall_traps_and_halts(built):
    rc, log, s = run_iss(asm("ecall", "li x1, 5\n ecall\n li x1, 9\n ebreak"))
    assert rc == 3 and s["halt"] == "ecall" and s["retired"] == 2
    t = log[-1]
    assert t["insn"] == 0x73 and t["trap"] == 1 and t["halt"] == 1
    assert t["rd"] == 0 and t["rdv"] == 0 and t["rs1"] == 0 and t["rs2"] == 0
    assert t["wmask"] == 0 and t["rmask"] == 0


def test_ebreak_traps_and_halts(built):
    rc, log, s = run_iss(asm("ebreak", "li x1, 5\n ebreak\n li x1, 9"))
    assert rc == 0 and s["halt"] == "ebreak" and s["retired"] == 2
    assert log[-1]["trap"] == 1 and log[-1]["halt"] == 1 and log[-1]["rd"] == 0


def test_jalr_target_bit1_traps(built):
    body = """
    lui x5, %hi(1f)
    addi x5, x5, %lo(1f)
    jalr x1, 3(x5)        # (1f + 3) & ~1 = 1f + 2: bit 1 set
    .align 2
1:  ebreak
"""
    rc, log, s = run_iss(asm("jalr_trap", body))
    assert rc == 3 and s["halt"] == "trap"
    t = log[-1]
    assert t["trap"] == 1 and t["halt"] == 1
    assert t["rd"] == 0 and t["rdv"] == 0, "a trapping JALR must not write rd"
    assert t["rs1"] == 5 and t["rs1v"] == log[1]["rdv"], "JALR read rs1 to compute the target"


def test_jalr_clears_bit0(built):
    body = """
    lui x5, %hi(1f)
    addi x5, x5, %lo(1f)
    addi x5, x5, 1
    jalr x1, 0(x5)
    ecall
    .align 2
1:  ebreak
"""
    rc, log, s = run_iss(asm("jalr_bit0", body))
    assert rc == 0
    j = log[3]
    assert j["trap"] == 0 and j["rd"] == 1 and j["rdv"] == j["pc"] + 4
    assert j["npc"] == log[2]["rdv"] - 1


def test_jal_target_bit1_traps(built):
    rc, log, s = run_iss(asm("jal_trap", "li x1, 7\n .word 0x006000ef\n ebreak"))
    assert rc == 3
    t = log[-1]
    assert t["insn"] == 0x006000EF and t["trap"] == 1 and t["rd"] == 0
    assert t["rs1"] == 0 and t["rs2"] == 0


def test_branch_target_bit1_traps_only_when_taken(built):
    # bne x0, x0, pc+6 (never taken), then beq x6, x6, pc+6 (taken)
    rc, log, s = run_iss(asm("br_trap", "li x6, 3\n .word 0x00001363\n .word 0x00630363\n ebreak"))
    assert rc == 3
    assert log[1]["trap"] == 0 and log[1]["npc"] == log[1]["pc"] + 4
    t = log[2]
    assert t["trap"] == 1 and t["halt"] == 1 and t["rs1"] == 6 and t["rs2"] == 6
    assert t["rs1v"] == 3 and t["rs2v"] == 3 and t["rd"] == 0


@pytest.mark.parametrize("word", [0x00000000, 0xFFFFFFFF])
def test_illegal_words_trap(built, word):
    rc, log, s = run_iss(asm(f"ill_{word:x}", f"li x1, 1\n .word {word:#x}\n ebreak"))
    assert rc == 3
    t = log[-1]
    assert t["insn"] == word and t["trap"] == 1 and t["halt"] == 1
    assert t["rs1"] == 0 and t["rs2"] == 0 and t["rd"] == 0


def test_wrap_at_64k(built):
    body = """
    li x1, 0xffff
    li x2, 0x44332211
    sw x2, 0(x1)          # 0xffff, 0x0000, 0x0001, 0x0002
    lw x3, 0(x1)
    lw x4, -1(x1)         # 0xfffe (image byte 0), 0xffff, 0x0000, 0x0001
    lh x5, 0(x1)
    li x2, 0xbeef
    sh x2, 0(x1)          # 0xffff, 0x0000
    lhu x6, 0(x1)
    ebreak
"""
    h = asm("wrap", body)
    rc, log, s = run_iss(h)
    assert rc == 0
    sw, sh = [r for r in log if r["wmask"]]
    lw3, lw4, lh5, lhu6 = [r for r in log if r["rmask"]]
    assert (sw["maddr"], sw["wmask"], sw["mwdata"]) == (0xFFFF, 0xF, 0x44332211)
    assert (lw3["maddr"], lw3["mrdata"], lw3["rdv"]) == (0xFFFF, 0x44332211, 0x44332211)
    assert (lw4["maddr"], lw4["rdv"]) == (0xFFFE, 0x33221100)
    assert (lh5["rmask"], lh5["rdv"]) == (3, 0x2211)
    assert (sh["maddr"], sh["wmask"], sh["mwdata"]) == (0xFFFF, 3, 0xBEEF)
    assert lhu6["rdv"] == 0xBEEF
    ram = dump_ram(h)
    assert (ram[0xFFFF], ram[0x0000], ram[0x0001], ram[0x0002]) == (0xEF, 0xBE, 0x33, 0x44)


def test_wrap_lw_fffe_reads_reset_code_bytes(built):
    # LW at 0xfffe returns 0xfffe, 0xffff, then RAM 0x0000, 0x0001 (the first instruction).
    h = asm("wrap2", "li x1, 0xfffe\n lw x2, 0(x1)\n ebreak")
    rc, log, s = run_iss(h)
    first_insn = log[0]["insn"]
    assert log[-2]["rdv"] == (first_insn & 0xFFFF) << 16


def test_access_class_follows_first_byte(built):
    # addr[31] of the effective address picks RAM for the whole access, even when addr + k has
    # bit 31 set: LW at 0x7ffffffe reads RAM 0xfffe, 0xffff, 0x0000, 0x0001 (contract section 3).
    body = """
    li x1, 0xfffe
    li x2, 0x11223344
    sw x2, 0(x1)
    li x3, 0x7ffffffe
    lw x4, 0(x3)
    li x5, 0x55667788
    sw x5, 0(x3)
    ebreak
"""
    h = asm("class", body)
    rc, log, s = run_iss(h, "--ui", "const:0xab")
    assert rc == 0 and s["uo_trace"] == []
    lw = [r for r in log if r["rmask"]][0]
    assert (lw["maddr"], lw["rdv"]) == (0x7FFFFFFE, 0x11223344)
    ram = dump_ram(h)
    assert (ram[0xFFFE], ram[0xFFFF], ram[0], ram[1]) == (0x88, 0x77, 0x66, 0x55)


def test_data_alias(built):
    body = """
    li x1, 0x12340
    li x2, 0xcafef00d
    sw x2, 0(x1)
    li x3, 0x2340
    lw x4, 0(x3)
    li x5, 0x7fff2342
    lhu x6, 0(x5)
    ebreak
"""
    rc, log, s = run_iss(asm("alias", body))
    assert rc == 0
    sw = [r for r in log if r["wmask"]][0]
    lw, lhu = [r for r in log if r["rmask"]]
    assert sw["maddr"] == 0x12340
    assert (lw["maddr"], lw["rdv"]) == (0x2340, 0xCAFEF00D)
    assert (lhu["maddr"], lhu["rdv"]) == (0x7FFF2342, 0xCAFE)


def test_fetch_alias_keeps_full_pc(built):
    body = """
    lui x1, %hi(1f)
    addi x1, x1, %lo(1f)
    li x2, 0x10000
    add x1, x1, x2
    jalr x0, 0(x1)
    ecall
1:  auipc x3, 0
    jal x4, 2f
2:  ebreak
"""
    rc, log, s = run_iss(asm("falias", body))
    assert rc == 0
    au = [r for r in log if (r["insn"] & 0x7F) == 0x17][0]
    assert au["pc"] >= 0x10000 and au["rdv"] == au["pc"]
    jal = [r for r in log if (r["insn"] & 0x7F) == 0x6F][0]
    assert jal["rdv"] == jal["pc"] + 4 and jal["rdv"] > 0x10000


def test_io_read_lanes(built):
    body = """
    lui x1, 0x80000
    lw x2, 0(x1)
    lbu x3, 1(x1)
    lbu x4, 2(x1)
    lbu x5, 3(x1)
    lh x6, 4(x1)
    lhu x7, 0(x1)
    lb x8, 0(x1)
    lbu x9, 4(x1)
    li x10, 0xfffffffc
    lbu x11, 0(x10)
    li x10, 0x80001235
    lbu x12, 0(x10)
    ebreak
"""
    rc, log, s = run_iss(asm("io_read", body), "--ui", "const:0xab")
    assert rc == 0
    loads = [r for r in log if r["rmask"]]
    exp = [(0x80000000, 0xF, 0xAB, 0xAB), (0x80000001, 1, 0, 0), (0x80000002, 1, 0, 0),
           (0x80000003, 1, 0, 0), (0x80000004, 3, 0xAB, 0xAB), (0x80000000, 3, 0xAB, 0xAB),
           (0x80000000, 1, 0xAB, 0xFFFFFFAB), (0x80000004, 1, 0xAB, 0xAB),
           (0xFFFFFFFC, 1, 0xAB, 0xAB), (0x80001235, 1, 0, 0)]
    got = [(r["maddr"], r["rmask"], r["mrdata"], r["rdv"]) for r in loads]
    assert got == exp


def test_io_write_lanes(built):
    body = """
    lui x1, 0x80000
    li x2, 0x12345678
    sb x2, 1(x1)          # ignored
    sb x2, 3(x1)          # ignored
    sw x2, 0(x1)          # uo = 0x78
    li x3, 0xabcd
    sb x3, 2(x1)          # ignored
    sh x3, 0(x1)          # uo = 0xcd
    li x4, 0x5a
    sb x4, 4(x1)          # uo = 0x5a
    li x5, 0xc0000001
    sb x2, 0(x5)          # ignored
    ebreak
"""
    rc, log, s = run_iss(asm("io_write", body))
    assert rc == 0 and s["uo_trace"] == [0x78, 0xCD, 0x5A] and s["uo_final"] == 0x5A
    stores = [r for r in log if r["wmask"]]
    assert [(r["maddr"], r["wmask"], r["mwdata"]) for r in stores] == [
        (0x80000001, 1, 0x78), (0x80000003, 1, 0x78), (0x80000000, 0xF, 0x12345678),
        (0x80000002, 1, 0xCD), (0x80000000, 3, 0xABCD), (0x80000004, 1, 0x5A),
        (0xC0000001, 1, 0x78)]


@pytest.mark.parametrize("body", ["lw x2, 1(x1)", "lh x2, 3(x1)", "lhu x2, 2(x1)", "sw x0, 2(x1)",
                                  "sh x0, 1(x1)", "sh x0, 2(x1)"])
def test_misaligned_io_is_unspecified(built, body):
    name = "io_mis_" + re.sub(r"\W", "_", body)
    rc, log, s = run_iss(asm(name, f"lui x1, 0x80000\n {body}\n ebreak"))
    assert rc == 2 and s["halt"] == "reserved"


RESERVED = {
    "add_rd16": 0x00000833, "add_rs1_16": 0x000800B3, "add_rs2_17": 0x011000B3,
    "addi_rd16": 0x00000813, "lui_rd16": 0x00000837, "lw_rs1_16": 0x00082083,
    "sw_rs2_16": 0x0100A023, "beq_rs1_31": 0x000F8063, "jal_rd16": 0x0000086F,
    "mul": 0x021080B3, "slli_shamt5": 0x02009093, "srai_badf7": 0x6000D093,
    "sll_f7_20": 0x401090B3, "jalr_f3": 0x000010E7, "ld": 0x0000B083, "lwu": 0x0000E083,
    "sd": 0x0010B023, "branch_f3_2": 0x00002063, "fence_i": 0x0000100F, "misc_f3_2": 0x0000200F,
    "csrrw": 0x300090F3, "wfi": 0x10500073, "mret": 0x30200073, "ecall_rd": 0x000000F3,
    "ebreak_rs1": 0x00108073, "compressed": 0x00000001, "c_li": 0x00004501, "c_q2": 0x0000808A,
    "custom0": 0x0000000B, "op_7f": 0x0000007F, "amo": 0x0000202F,
}


@pytest.mark.parametrize("name", sorted(RESERVED))
def test_reserved_encodings(built, name):
    word = RESERVED[name]
    rc, log, s = run_iss(asm(f"res_{name}", f"li x1, 4\n .word {word:#x}\n ebreak"))
    assert rc == 2 and s["halt"] == "reserved" and s["halt_insn"] == word, (name, s)
    assert s["retired"] == 1


def test_fence_variants_are_noops(built):
    body = """
    li x5, 0x1234
    li x6, 0x5678
    fence
    fence.tso
    .word 0x0100000f
    .word 0x0ff2828f
    .word 0x0003030f
    .word 0xfff0000f
    .word 0x0ff8000f      # rs1 field = x16: still a FENCE (fields are ignored)
    add x7, x5, x6
    ebreak
"""
    rc, log, s = run_iss(asm("fence", body))
    assert rc == 0
    fences = [r for r in log if (r["insn"] & 0x7F) == 0x0F]
    assert len(fences) == 7
    for r in fences:
        assert (r["rd"], r["rs1"], r["rs2"], r["rmask"], r["wmask"]) == (0, 0, 0, 0, 0)
    assert log[-2]["rdv"] == 0x1234 + 0x5678


def test_x0_writes_report_zero(built):
    body = """
    li x1, 9
    addi x0, x1, 5
    lui x0, 1
    add x0, x1, x1
    lw x0, 0(x0)
    jal x0, 1f
1:  add x2, x0, x1
    ebreak
"""
    rc, log, s = run_iss(asm("x0", body))
    assert rc == 0
    for r in log[1:6]:
        assert r["rd"] == 0 and r["rdv"] == 0
    assert log[6]["rs1"] == 0 and log[6]["rs1v"] == 0 and log[6]["rdv"] == 9


def test_register_field_conventions(built):
    body = """
    li x1, 3
    li x2, 4
    addi x3, x1, 1
    lui x4, 1
    sw x2, 0x100(x0)
    beq x1, x2, 1f
1:  slli x5, x1, 2
    ebreak
"""
    rc, log, s = run_iss(asm("fields", body))
    addi, lui, sw, beq, slli = log[2:7]
    assert (addi["rs1"], addi["rs2"], addi["rd"]) == (1, 0, 3)
    assert (lui["rs1"], lui["rs2"], lui["rd"]) == (0, 0, 4)
    assert (sw["rs1"], sw["rs2"], sw["rd"], sw["rs1v"], sw["rs2v"]) == (0, 2, 0, 0, 4)
    assert (beq["rs1"], beq["rs2"], beq["rd"]) == (1, 2, 0)
    assert (slli["rs1"], slli["rs2"], slli["rd"], slli["rdv"]) == (1, 0, 5, 12)


def test_store_data_lanes(built):
    body = """
    li x1, 0x400
    li x2, 0x89abcdef
    sb x2, 1(x1)
    sh x2, 3(x1)
    lh x3, 3(x1)
    ebreak
"""
    rc, log, s = run_iss(asm("lanes", body))
    sb, sh, lh = [r for r in log if r["wmask"] or r["rmask"]]
    assert (sb["maddr"], sb["wmask"], sb["mwdata"]) == (0x401, 1, 0xEF)
    assert (sh["maddr"], sh["wmask"], sh["mwdata"]) == (0x403, 3, 0xCDEF)
    assert (lh["maddr"], lh["rmask"], lh["mrdata"], lh["rdv"]) == (0x403, 3, 0xCDEF, 0xFFFFCDEF)


def test_ui_schedules(built):
    body = """
    lui x1, 0x80000
    lbu x2, 0(x1)
    lbu x3, 0(x1)
    sb x3, 0(x1)
    lbu x4, 0(x1)
    sb x0, 0(x1)
    lbu x5, 0(x1)
    ebreak
"""
    h = asm("ui", body)
    rc, log, s = run_iss(h, "--ui", "rvfi", "--seed", "7")
    loads = [(i, r) for i, r in enumerate(log) if r["rmask"]]
    for i, r in loads:
        assert r["mrdata"] == ui_value(7, i), "rvfi schedule: instruction i sees ui_value(seed, i)"
    rc, log, s = run_iss(h, "--ui", "pins", "--seed", "7")
    vals = [r["mrdata"] for r in log if r["rmask"]]
    v0 = ui_value(7, 0)
    # uo_out changes at the first sb (to v0, unless v0 == 0) and again at sb x0
    c1 = 1 if v0 != 0 else 0
    c2 = c1 + (1 if v0 != 0 else 0)
    assert vals == [v0, v0, ui_value(7, c1), ui_value(7, c2)]


def test_uninitialized_register_read_is_flagged(built):
    rc, log, s = run_iss(asm("uninit", "add x1, x2, x3\n ebreak"), "--check-init")
    assert rc == 5 and s["uninit_read"] is True


# ------------------------------------------------------------------------------------------------
# 3. program coverage (Task 2's requirements, checked on the ISS trace)
def mnemonic(insn: int) -> str:
    op, f3, f7 = insn & 0x7F, (insn >> 12) & 7, insn >> 25
    if insn == 0x00100073:
        return "ebreak"
    if insn == 0x00000073:
        return "ecall"
    table = {
        0x37: lambda: "lui", 0x17: lambda: "auipc", 0x6F: lambda: "jal", 0x67: lambda: "jalr",
        0x63: lambda: ["beq", "bne", "?", "?", "blt", "bge", "bltu", "bgeu"][f3],
        0x03: lambda: ["lb", "lh", "lw", "?", "lbu", "lhu", "?", "?"][f3],
        0x23: lambda: ["sb", "sh", "sw", "?", "?", "?", "?", "?"][f3],
        0x13: lambda: ["addi", "slli", "slti", "sltiu", "xori", "srai" if f7 else "srli", "ori",
                       "andi"][f3],
        0x33: lambda: ["sub" if f7 else "add", "sll", "slt", "sltu", "xor",
                       "sra" if f7 else "srl", "or", "and"][f3],
        0x0F: lambda: "fence",
    }
    return table.get(op, lambda: "?")()


RV32E = ("lui auipc jal jalr beq bne blt bge bltu bgeu lb lh lw lbu lhu sb sh sw addi slti sltiu "
         "xori ori andi slli srli srai add sub sll slt sltu xor srl sra or and fence ebreak").split()
BRANCHES = {"beq", "bne", "blt", "bge", "bltu", "bgeu"}


def test_selftest_covers_rv32e(built):
    rc, log, s = run_iss(PROGS / "selftest.hex", "--ui", "rvfi")
    assert rc == 0 and s["uo_final"] == 0xA5
    seen = {mnemonic(r["insn"]) for r in log}
    assert set(RV32E) <= seen, f"missing: {set(RV32E) - seen}"
    taken = {mnemonic(r["insn"]) for r in log if r["npc"] != r["pc"] + 4}
    not_taken = {mnemonic(r["insn"]) for r in log if r["npc"] == r["pc"] + 4}
    assert BRANCHES <= taken and BRANCHES <= not_taken
    # misaligned loads/stores and the 64 KB wrap
    mem = [r for r in log if (r["rmask"] or r["wmask"]) and not r["maddr"] >> 31]
    mis = {mnemonic(r["insn"]) for r in mem if r["maddr"] & 1}
    assert {"lw", "lh", "lhu", "sw", "sh"} <= mis
    wraps = {mnemonic(r["insn"]) for r in mem
             if (r["maddr"] & 0xFFFF) + bin(r["rmask"] | r["wmask"]).count("1") > 0x10000}
    assert {"lw", "lh", "sw", "sh"} <= wraps
    # x0 writes, JALR with bit 0 set
    assert any((r["insn"] >> 7) & 31 == 0 and mnemonic(r["insn"]) == "addi" and r["insn"] != 0x13
               for r in log)
    assert any(mnemonic(r["insn"]) == "jalr" and (r["rs1v"] + (r["insn"] >> 20)) & 1 for r in log)
    # GPIO reads on every byte lane, writes to lanes 0 (applied) and others (ignored)
    io = [r for r in log if (r["rmask"] or r["wmask"]) and r["maddr"] >> 31]
    assert {r["maddr"] & 3 for r in io if r["rmask"]} == {0, 1, 2, 3}
    assert {r["maddr"] & 3 for r in io if r["wmask"]} >= {0, 1, 2, 3}
    # every shift amount, register and immediate forms
    for m in ("sll", "srl", "sra"):
        assert {r["rs2v"] & 31 for r in log if mnemonic(r["insn"]) == m} == set(range(32))
    for m in ("slli", "srli", "srai"):
        assert {(r["insn"] >> 20) & 31 for r in log if mnemonic(r["insn"]) == m} == set(range(32))


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_random_programs_are_valid_and_varied(built, seed):
    rc, log, s = run_iss(PROGS / f"random_{seed}.hex", "--check-init", "--ui", "rvfi")
    assert rc == 0, s
    seen = {mnemonic(r["insn"]) for r in log}
    assert set(RV32E) - {"ebreak"} <= seen, set(RV32E) - seen
    taken = {mnemonic(r["insn"]) for r in log if r["npc"] != r["pc"] + 4}
    not_taken = {mnemonic(r["insn"]) for r in log if r["npc"] == r["pc"] + 4}
    assert BRANCHES <= taken and BRANCHES <= not_taken
    assert any(r["maddr"] >> 31 for r in log if r["rmask"])
    assert any(r["maddr"] >> 31 for r in log if r["wmask"])
    assert any(r["maddr"] & 3 for r in log if (r["rmask"] or r["wmask"]) and not r["maddr"] >> 31)
    # deterministic per seed
    again = subprocess.run(["python3", str(ROOT / "verif/programs/gen_random.py"), str(seed),
                            "3000"], capture_output=True, text=True, check=True).stdout
    assert again == (PROGS / f"random_{seed}.S").read_text()
