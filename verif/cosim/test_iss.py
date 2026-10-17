"""Tests for the reference ISS (verif/cosim/iss.h, iss_main).

1. Cross-check against spike on selftest, gpio_demo and coremark: every retirement's pc, insn,
   register write, store and load address must match spike's commit log. The programs are the
   spike builds from `make -C verif/programs spike` (linked at 0x10000, because spike's debug
   module sits at [0, 0x1000); selftest built with -DSPIKE_XCHECK skips its top-of-RAM, alias and
   GPIO sections). The ISS starts at pc 0x10000 (aliases RAM 0) with IO modeled as a RAM page,
   which is what spike does with -m0x80000000:0x1000.
2. Unit cases for what spike can't check: the fatal traps, the core region of contract v2
   (accesses touching 0xFF80-0xFFFF are unspecified), aliasing, the GPIO byte lanes, reserved
   encodings, FENCE, register-field conventions and the ui_in schedules.
3. Program coverage (v2).
4. ISS v3 (verif/cosim/iss3.h, `iss_main --v3`, docs/contract-v3.md sections 3-5 and 8): CSRs,
   traps to mtvec, interrupt entry, the flash/PSRAM/IO map, the IO register model with volatile
   reads, the self-loop end and the core region. Programs are linked at flash 0 and loaded from
   sparse 16 MiB flash hex files (@addr records).

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


REGION_ACCESSES = {
    # contract v2 section 3: a fetch or RAM data access touching 0xFF80-0xFFFF is unspecified
    "sw_ff80": "li x1, 0xff80\n sw x0, 0(x1)",
    "lb_ffff": "li x1, 0xffff\n lb x2, 0(x1)",
    "lw_ff7d": "li x1, 0xff7d\n lw x2, 0(x1)",             # 0xff7d..0xff80: its last byte
    "sh_ff7f": "li x1, 0xff7f\n sh x0, 0(x1)",             # 0xff7f, 0xff80
    "sw_wrap": "li x1, 0xffff\n sw x0, 0(x1)",             # 0xffff, 0x0000.. (the 64 KB wrap)
    "lw_7ffffffe": "li x1, 0x7ffffffe\n lw x2, 0(x1)",     # RAM 0xfffe..0x0001 (bit 31 = 0)
    "lhu_alias": "li x1, 0x1234ffc0\n lhu x2, 0(x1)",      # alias bits: still the region
    "fetch_ff80": "li x1, 0xff80\n jalr x0, 0(x1)",        # the fetch at 0xff80
}


@pytest.mark.parametrize("name", sorted(REGION_ACCESSES))
def test_region_accesses_are_unspecified(built, name):
    rc, log, s = run_iss(asm(f"region_{name}", REGION_ACCESSES[name] + "\n ebreak"))
    assert rc == 2 and s["halt"] == "reserved" and s["region"] is True, s
    if name == "fetch_ff80":
        assert s["halt_pc"] == 0xFF80 and log[-1]["npc"] == 0xFF80


def test_accesses_up_to_ff7f_are_ram(built):
    # The last program byte is 0xFF7F: accesses ending there are plain RAM.
    body = """
    li x1, 0xff7c
    li x2, 0x44332211
    sw x2, 0(x1)
    lw x3, 0(x1)
    lh x4, 2(x1)          # 0xff7e, 0xff7f
    lbu x5, 3(x1)         # 0xff7f
    li x6, 0x7fffff7c     # bit 31 = 0, alias bits set
    lw x7, 0(x6)
    ebreak
"""
    h = asm("top", body)
    rc, log, s = run_iss(h)
    assert rc == 0 and s["region"] is False
    loads = [r for r in log if r["rmask"]]
    assert [r["rdv"] for r in loads] == [0x44332211, 0x4433, 0x44, 0x44332211]
    ram = dump_ram(h)
    assert ram[0xFF7C:0xFF80] == bytes([0x11, 0x22, 0x33, 0x44])


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
R_ALU = "add sub sll slt sltu xor srl sra or and".split()
I_ALU = "addi slti sltiu xori ori andi slli srli srai".split()


def test_selftest_covers_rv32e(built):
    rc, log, s = run_iss(PROGS / "selftest.hex", "--ui", "rvfi")
    assert rc == 0 and s["uo_final"] == 0xA5
    seen = {mnemonic(r["insn"]) for r in log}
    assert set(RV32E) <= seen, f"missing: {set(RV32E) - seen}"
    taken = {mnemonic(r["insn"]) for r in log if r["npc"] != r["pc"] + 4}
    not_taken = {mnemonic(r["insn"]) for r in log if r["npc"] == r["pc"] + 4}
    assert BRANCHES <= taken and BRANCHES <= not_taken
    # misaligned loads/stores, and accesses that end at 0xFF7F, the last byte below the core
    # region (contract v2 section 3; the 64 KB wrap touches the region, so it is untestable)
    mem = [r for r in log if (r["rmask"] or r["wmask"]) and not r["maddr"] >> 31]
    mis = {mnemonic(r["insn"]) for r in mem if r["maddr"] & 1}
    assert {"lw", "lh", "lhu", "sw", "sh"} <= mis
    top = {mnemonic(r["insn"]) for r in mem
           if (r["maddr"] & 0xFFFF) + bin(r["rmask"] | r["wmask"]).count("1") - 1 == 0xFF7F}
    assert {"lw", "lh", "lb", "sw", "sh"} <= top, top
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
    # x14 and x15 are ordinary ALU sources and destinations
    alu = set(R_ALU) | set(I_ALU)
    for x in (14, 15):
        assert any(r["rd"] == x and mnemonic(r["insn"]) in alu for r in log), f"x{x} as rd"
        assert any(x in (r["rs1"], r["rs2"]) and mnemonic(r["insn"]) in alu for r in log)
    # RAM addresses with alias bits, accesses up to 0xFF7F (loads and stores) and none into the
    # core region (contract v2 section 3), any IO address
    ram = [r for r in log if (r["rmask"] or r["wmask"]) and not r["maddr"] >> 31]
    io = [r for r in log if (r["rmask"] or r["wmask"]) and r["maddr"] >> 31]
    assert any(r["maddr"] >> 16 for r in ram if r["rmask"]) and any(r["maddr"] >> 16 for r in ram
                                                                     if r["wmask"])
    last = [((r["maddr"] & 0xFFFF) + bin(r["rmask"] | r["wmask"]).count("1") - 1, r) for r in ram]
    assert all(end < 0xFF80 for end, _ in last)
    top = [r for end, r in last if end == 0xFF7F]
    assert any(r["rmask"] for r in top) and any(r["wmask"] for r in top)
    assert any(r["maddr"] & 0x7FFFFFF0 for r in io if r["rmask"])
    assert any(r["maddr"] & 0x7FFFFFF0 for r in io if r["wmask"])
    for m in ("rmask", "wmask"):
        assert {r[m] for r in io if r[m]} >= {1, 15}, f"IO byte and word accesses ({m})"
    # bounded loops: taken backward branches
    back = [r for r in log if mnemonic(r["insn"]) in BRANCHES and r["npc"] < r["pc"]]
    assert len(back) >= 5, len(back)
    # deterministic per seed
    again = subprocess.run(["python3", str(ROOT / "verif/programs/gen_random.py"), str(seed),
                            "3000"], capture_output=True, text=True, check=True).stdout
    assert again == (PROGS / f"random_{seed}.S").read_text()


# ------------------------------------------------------------------------------------------------
# 4. ISS v3 (docs/contract-v3.md): `iss_main --v3` on tiny programs linked at flash 0
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
    """Run iss_main --v3 --log --json. Returns (rc, retirements, events, summary): events are
    the log lines in order, interrupt entries ("irq_entry": 1) included; retirements leave them
    out. Capped at 20,000 retirements unless args give --max, so a broken ISS can't flood the
    log (these programs retire a few hundred at most)."""
    cap = [] if "--max" in args else ["--max", "20000"]
    p = subprocess.run([str(ISS), "--v3", str(hexfile), "--log", "--json", *cap, *args],
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
    p = subprocess.run([str(ISS), "--v3", str(h), "--max", "20000"], capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0 and "selfloop" in p.stdout and "{" not in p.stdout, \
        "without --json the summary is plain text"
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
    # --dump-psram (run_arch.py --v3 reads signatures from it): the final 8 MiB PSRAM image, stored
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
