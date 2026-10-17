#!/usr/bin/env python3
"""riscv-arch-test (RV32E) on the DUT. See docs/arch-test.md.

Two contracts until the integration swap (plan Task 9), like run_cosim.py: contract v2 (the
default, below) and contract v3 with --v3 (run_v3 and docs/arch-test.md "Contract v3").

Usage: run_arch.py [--src DIR] [--seeds 1,2,3] [--jobs N] [--only NAME ...]
       run_arch.py --v3 [--phase all|prep|dut] [--src DIR] [--seeds 1,2,3,4] [--jobs N]
                   [--only NAME ...]

Contract v2. For every RV32E test of third_party/riscv-arch-test (rv32e_m/E and
rv32e_m/privilege, minus the skips listed in SKIP and docs/arch-test.md):
  1. build one ELF (verif/arch-test/model_test.h, linked at 0x10000) and its 64 KB RAM image;
  2. reference signature from spike (cached in build/arch/ref/, keyed on the ELF and spike);
  3. the reference ISS must run the image to EBREAK without reading an unwritten register,
     hitting an unspecified instruction or touching the core region 0xFF80-0xFFFF (contract v2
     section 3), and leave the same signature as spike;
  4. the DUT runs it through run_cosim.run_one("pins", ..., dump_ram=...) (what
     `run_cosim.py --mode pins --dump-ram` does) for each seed: the cosim must pass (SPI writes,
     uo_out and the final RAM outside the region equal the ISS) and the signature in the dumped
     RAM must equal spike's. The signature must lie below the region.
Writes build/arch.json as {"pass": bool, "tests": {name: bool}} (removed first, so it is always
fresh; --only writes build/arch/only.json instead) and build/arch/report.json with the errors and
the skips. Exit 0 only if every test passes.

Contract v3 (--v3; docs/contract-v3.md). For every test of rv32e_m/E and rv32e_m/privilege minus
SKIP_V3 (each test built with the defines of its one RVTEST_CASE that applies to RV32E + Zicsr
with hardware misaligned data access, as riscof would select them):
  1. build one ELF: verif/programs/link.ld with .text moved to 0x1000, plus SIG_LD (the
     .signature section in PSRAM at 0x0101_0000) and model_test.h with -DBONETTO_V3. The DUT's
     16 MiB flash image is the ELF's load image plus `jal x0, 0x1000` at flash byte 0 (the DUT
     starts at pc 0, spike at its reset vector 0x1000);
  2. reference signature from spike (cached in build/arch/ref-v3/, keyed on the ELF and spike);
  3. the reference ISS (`iss_main --v3 --check-init --dump-psram`) must reach the program end (the
     self-loop, with GPIO_OUT = 0x5A) without reading an unwritten register or arbitrary-reset CSR,
     hitting an unspecified step or touching the core region (contract 3), and leave the same
     signature in PSRAM as spike;
  4. the DUT runs the image through run_cosim.run_one("pins", ..., v3=True, dump_ram=...) (the v3
     harness with --dump-psram) under each seed (default 1-4: every memory power-up mode): the
     cosim must pass (PSRAM writes, uo_out and the final PSRAM equal the ISS's) and the signature
     in the dumped PSRAM must equal spike's.
--phase splits the run for machines that have only part of the tools: `prep` does steps 1-3 (RISC-V
GCC, spike, a C++ compiler) and stages the images, the reference signatures and a manifest in
build/programs/arch-v3/ (where tools/hwe_run.sh mirrors them); `dut` does step 4 from that
manifest (Verilator; no GCC, spike or third_party/ needed); `all` (the default) does both.
build/arch.json is removed at the start of every phase and written only by `dut` and `all`.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "verif" / "arch-test"
SUITE = ROOT / "third_party" / "riscv-arch-test" / "riscv-test-suite"
ENV = SUITE / "env"
OUT = ROOT / "build" / "arch"
SUMMARY = ROOT / "build" / "arch.json"  # the gate's result (the v3 flow; v2 spells it out)
REF = OUT / "ref"
ISS = ROOT / "build" / "cosim" / "iss_main"
LINK = ROOT / "verif" / "programs" / "link.ld"
ELF2HEX = ROOT / "verif" / "programs" / "elf2hex.py"
sys.path.insert(0, str(ROOT / "verif" / "cosim"))
sys.path.insert(0, str(ELF2HEX.parent))
import elf2hex  # noqa: E402  (contract v3 flash images)
import run_cosim  # noqa: E402

BASE = 0x10000
REGION_LO = 0xFF80  # contract v2 section 3: RAM 0xFF80-0xFFFF is the core's; tests stay below
CROSS = os.environ.get("CROSS", "riscv32-unknown-elf-")
DIRS = ["rv32e_m/E/src", "rv32e_m/privilege/src"]
TRAP = "needs a trap handler (mtvec, mcause, mepc: CSRs), which the core doesn't have"
SKIP = {
    "ecall": "ECALL is a fatal trap here (contract section 4); the test " + TRAP,
    "ebreak": "EBREAK is the halt here (contract section 4); the test " + TRAP,
    **{f"misalign-{b}-01": f"a taken {b.upper()} to a target with bit 1 set is a fatal trap "
                           "here; the test needs C (case 0) or " + TRAP + " (case 1)"
       for b in ("beq", "bne", "blt", "bge", "bltu", "bgeu")},
    "misalign-jal-01": "a JAL to a target with bit 1 set is a fatal trap here; the test needs C "
                       "(case 0) or " + TRAP + " (case 1)",
    "misalign2-jalr-01": "a JALR to a target with bit 1 set is a fatal trap here; the test needs "
                         "C (case 0) or " + TRAP + " (case 1)",
}
CFLAGS = ["-march=rv32e", "-mabi=ilp32e", "-mno-relax", "-static", "-mcmodel=medany",
          "-nostdlib", "-nostartfiles", "-T", str(LINK), f"-Wl,--defsym=RAM_BASE={BASE:#x}",
          "-Wl,--section-start=.tramp=0x1000", "-Wl,-e,rvtest_entry_point",
          "-Wl,--no-warn-rwx-segments", f"-I{HERE}", f"-I{ENV}", "-DXLEN=32", "-DRVTEST_E=True",
          "-DTEST_CASE_1=True"]
SPIKE = ["spike", "--isa=rv32e", "--misaligned", "--disable-dtb", "-m0x1000:0x1f000",
         "--instructions=50000000"]

# ---- contract v3 (--v3) --------------------------------------------------------------------------
STAGE_V3 = ROOT / "build" / "programs" / "arch-v3"  # images, manifest (tools/hwe_run.sh mirrors it)
WORK_V3 = ROOT / "build" / "arch-v3"                # ELFs, per test; PSRAM dumps
REF_V3 = OUT / "ref-v3"
CODE_V3 = 0x1000            # code at flash 0x1000: spike's reset vector, above its debug module
STUB_V3 = 0x0000106F        # `jal x0, 0x1000`: flash byte 0 of the DUT image (the DUT starts at 0)
PSRAM_CPU_V3 = 0x01000000   # CPU address of PSRAM byte 0
SIG_V3 = 0x01010000         # .signature (plan Task 6)
REGION_CPU_V3 = 0x017FFF80  # the core region, CPU 0x017F_FF80-0x017F_FFFF (contract 3)
RESULT_PASS_V3 = 0x5A       # GPIO_OUT written by RVMODEL_HALT (model_test.h)
ISS_CAP_V3 = 20000000       # retirements: the largest test retires far fewer
# riscof's view of the model: the ISA string and the hardware-misaligned flag that the
# RVTEST_CASE conditions test (contract v3 section 4: RV32E + Zicsr, no C; section 3:
# misaligned loads and stores never trap).
ISA_V3 = "RV32EZicsr"
HW_MISALIGNED_V3 = True
# Every privilege test applies under contract v3: ECALL, EBREAK and misaligned jump targets trap to
# mtvec, the CSRs arch_test.h's handler uses (mtvec, mscratch, mepc, mcause, mtval, misa, mstatus)
# exist, and misaligned data accesses work in hardware.
SKIP_V3 = {}
SIG_LD = """\
/* run_arch.py --v3, given to the linker with -T before verif/programs/link.ld (ld inserts the
 * statements an INSERT script parsed before its INSERT): the signature in PSRAM at 0x0101_0000,
 * with its initial bytes in flash after the load image of .data (and .window). RVMODEL_BOOT
 * (model_test.h) copies them, like .data. */
SECTIONS
{
  .signature 0x01010000 : AT(ALIGN(LOADADDR(.window) + SIZEOF(.window), 16)) {
    _sig_start = .;
    KEEP(*(.signature))
    . = ALIGN(4);
    _sig_end = .;
  }
  _sig_lma = LOADADDR(.signature);
}
INSERT AFTER .window;
ASSERT(_bss_end <= 0x01010000, "arch v3: .data/.bss reach the signature at 0x01010000")
ASSERT(_sig_end <= 0x017F0000, "arch v3: the signature reaches the log ring, stack and core region")
ASSERT(_sig_lma + SIZEOF(.signature) <= 0x01000000, "arch v3: flash image larger than 16 MiB")
"""
CFLAGS_V3 = ["-march=rv32e_zicsr", "-mabi=ilp32e", "-mno-relax", "-static", "-mcmodel=medany",
             "-nostdlib", "-nostartfiles", f"-Wl,--section-start=.text={CODE_V3:#x}",
             "-Wl,-e,rvtest_entry_point", "-Wl,--no-warn-rwx-segments", f"-I{HERE}", f"-I{ENV}",
             "-DXLEN=32", "-DBONETTO_V3"]
# Memory: flash 0x1000-0xFFFFFF (its first 4 KiB is spike's debug module), PSRAM, and a scratch
# page at 0x8000_0000 for RVMODEL_HALT's GPIO_OUT store. --priv=m: machine mode only (mstatus.MPP
# reads 3, contract 4).
SPIKE_V3 = ["spike", "--priv=m", "--disable-dtb",
            "-m0x1000:0xfff000,0x1000000:0x800000,0x80000000:0x1000", "--instructions=50000000"]
# The ISA, with hardware misaligned loads and stores (contract 3): the Zicclsm extension, which is
# spike's only way since riscv-isa-sim d6a5e90c (Dec 2025) removed --misaligned; spike builds that
# don't know Zicclsm (e.g. Homebrew's 1.1.1-dev) take --misaligned instead (spike_isa_v3 picks).
SPIKE_ISA_V3 = (["--isa=rv32e_zicsr_zicclsm"], ["--isa=rv32e_zicsr", "--misaligned"])
RVTEST_CASE_RE = re.compile(r'^\s*RVTEST_CASE\(\s*(\d+)\s*,\s*"([^"]*)"', re.M)


def discover():
    """{name: source} for every test in DIRS, and the skips that matched."""
    tests, skipped = {}, {}
    for d in DIRS:
        for src in sorted((SUITE / d).glob("*.S")):
            name = src.stem
            if name in SKIP:
                skipped[name] = SKIP[name]
            else:
                tests[name] = src
    missing = sorted(set(SKIP) - set(skipped))
    if missing:
        raise SystemExit(f"run_arch.py: SKIP lists tests the suite doesn't have: {missing}")
    missing = sorted(set(EXCLUDED) - set(tests))
    if missing:
        raise SystemExit(f"run_arch.py: EXCLUDED lists tests the suite doesn't run: {missing}")
    return tests, skipped


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def symbols(elf: Path) -> dict:
    p = run([CROSS + "nm", elf], check=True)
    syms = {}
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            syms[parts[2]] = int(parts[0], 16)
    return syms


def sig_words(ram: bytes, begin: int, end: int) -> list:
    lo, hi = begin - BASE, end - BASE
    return [f"{w:08x}" for w in struct.unpack_from(f"<{(hi - lo) // 4}I", ram, lo)]


def spike_key(elf: Path) -> str:
    h = hashlib.sha256(elf.read_bytes())
    spike = shutil.which("spike") or "spike"
    st = os.stat(spike)
    h.update(f"{' '.join(SPIKE)}|{spike}|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()


def reference(name: str, elf: Path) -> list:
    """spike's signature words, cached in build/arch/ref/<name>.sig (+ .key)."""
    sig, keyf = REF / f"{name}.sig", REF / f"{name}.key"
    key = spike_key(elf)
    if not (sig.exists() and keyf.exists() and keyf.read_text() == key):
        tmp = REF / f"{name}.sig.tmp"
        tmp.unlink(missing_ok=True)
        p = run(SPIKE + [f"+signature={tmp}", "+signature-granularity=4", elf], timeout=600)
        if p.returncode != 0 or not tmp.exists():
            raise RuntimeError(f"spike failed (exit {p.returncode}): {p.stderr[-500:]}")
        tmp.replace(sig)
        keyf.write_text(key)
    return sig.read_text().split()


# ---- splitting tests that don't fit the 64 KB RAM ------------------------------------------------
# The branch tests carry hundreds of KB of NOP padding. Their cases (inst_N: blocks) are
# independent: each loads its own operands and stores at an explicit offset below 2048 from the
# current signature base, which only RVTEST_SIGBASE lines set. A piece keeps the whole test except
# the cases of other pieces (every RVTEST_SIGBASE line stays, in place), so each case runs in
# exactly one piece, on spike and on the DUT alike. A single case larger than CODE_BUDGET can't run
# in 64 KB at all (jal-01's 512 KB and 1 MB jumps alias onto the jump itself): it is excluded and
# listed in build/arch/report.json and docs/arch-test.md.
CODE_BUDGET = 0xB000  # case bytes per piece; leaves room for boot, epilog, data, 4 KB alignment
# The lead's ruling R10: exactly these cases are excluded, nothing else. Any other exclusion (a
# suite update, a planning bug) fails the test instead of silently shrinking the suite.
EXCLUDED = {"jal-01": set(range(1, 16))}
CASE_RE = re.compile(r"^inst_(\d+):")
SIGBASE_RE = re.compile(r"^\s*RVTEST_SIGBASE\(")


def parse_cases(text: str):
    """(head lines, items, tail lines); items are [kind, lines, case number] with kind 'case',
    'base' (an RVTEST_SIGBASE line) or 'other'."""
    lines = text.splitlines(keepends=True)
    i0 = next(i for i, ln in enumerate(lines) if ln.strip() == "#ifdef TEST_CASE_1")
    i1 = next(i for i in range(i0 + 1, len(lines)) if lines[i].lstrip().startswith("#"))
    if not lines[i1].strip().startswith("#endif"):
        raise RuntimeError(f"unexpected preprocessor line in the cases: {lines[i1].strip()}")
    items, cur = [], None
    for ln in lines[i0 + 1:i1]:
        m = CASE_RE.match(ln)
        if m:
            cur = ["case", [ln], int(m.group(1))]
            items.append(cur)
        elif SIGBASE_RE.match(ln):
            cur = None
            items.append(["base", [ln], None])
        elif cur is None:
            items.append(["other", [ln], None])
        else:
            cur[1].append(ln)
    return lines[:i0 + 1], items, lines[i1:]


def plan(name: str, src: Path, d: Path):
    """Pieces (lists of case numbers, None = the unmodified test) and the excluded cases."""
    obj = d / f"{name}.o"
    p = run([CROSS + "gcc", "-c"] + [f for f in CFLAGS if not f.startswith(("-T", "-Wl"))] +
            [src, "-o", obj])
    if p.returncode != 0:
        raise RuntimeError("assembly failed: " + p.stderr[-800:])
    syms = symbols(obj)
    obj.unlink()
    cases = sorted((addr, int(s[5:])) for s, addr in syms.items() if re.fullmatch(r"inst_\d+", s))
    end = syms["rvtest_code_end"]
    sizes = {n: (cases[i + 1][0] if i + 1 < len(cases) else end) - a
             for i, (a, n) in enumerate(cases)}
    if sum(sizes.values()) <= CODE_BUDGET:
        return [None], {}
    pieces, excluded, cur, used = [], {}, [], 0
    for _, n in cases:
        if sizes[n] > CODE_BUDGET:
            excluded[n] = sizes[n]
            continue
        if used + sizes[n] > CODE_BUDGET:
            pieces.append(cur)
            cur, used = [], 0
        cur.append(n)
        used += sizes[n]
    if cur:
        pieces.append(cur)
    return pieces, excluded


def piece_source(src: Path, keep, out: Path) -> Path:
    head, items, tail = parse_cases(src.read_text())
    numbers = [it[2] for it in items if it[0] == "case"]
    if len(numbers) != len(set(numbers)):
        raise RuntimeError("duplicate inst_N labels")
    keep = set(keep)
    body = [ln for it in items if it[0] != "case" or it[2] in keep for ln in it[1]]
    out.write_text("".join(head + body + tail))
    return out


def one(name: str, src: Path, design: Path, seeds) -> tuple:
    """Returns (passed, error or None, info)."""
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    try:
        pieces, excluded = plan(name, src, d)
    except (RuntimeError, KeyError, StopIteration) as ex:
        return False, f"planning failed: {ex}", {}
    info = {"pieces": len(pieces), "excluded_cases": excluded}
    if set(excluded) != EXCLUDED.get(name, set()):
        return False, (f"excluded cases {sorted(excluded)} differ from the ruled set "
                       f"{sorted(EXCLUDED.get(name, set()))} (ruling R10)"), info
    if not pieces or any(keep is not None and not keep for keep in pieces):
        return False, f"no runnable piece (pieces {pieces})", info
    for k, keep in enumerate(pieces):
        tag = name if keep is None else f"{name}.p{k}"
        psrc = src if keep is None else piece_source(src, keep, d / f"{tag}.S")
        ok, err = one_piece(tag, psrc, d, design, seeds)
        if not ok:
            return False, (err if keep is None else f"piece {k} (cases {keep[0]}..{keep[-1]}): "
                           f"{err}"), info
    return True, None, info


def one_piece(name: str, src: Path, d: Path, design: Path, seeds) -> tuple:
    """Returns (passed, error or None)."""
    elf, hexf = d / f"{name}.elf", d / f"{name}.hex"
    p = run([CROSS + "gcc"] + CFLAGS + [src, "-o", elf])
    if p.returncode != 0:
        return False, "build failed: " + p.stderr[-800:]
    p = run([sys.executable, ELF2HEX, elf, hexf, "--base", hex(BASE)])
    if p.returncode != 0:
        return False, "elf2hex failed: " + (p.stderr or p.stdout)[-400:]
    syms = symbols(elf)
    if "begin_signature" not in syms or "end_signature" not in syms:
        return False, "no begin_signature/end_signature symbols"
    b, e = syms["begin_signature"], syms["end_signature"]
    if not (BASE <= b < e <= BASE + REGION_LO) or (e - b) % 4:
        return False, (f"signature {b:#x}..{e:#x} is not inside program RAM {BASE:#x}.."
                       f"{BASE + REGION_LO - 1:#x} (below the core region, contract section 3)")
    try:
        ref = reference(name, elf)
    except (RuntimeError, subprocess.TimeoutExpired) as ex:
        return False, f"reference: {ex}"
    if len(ref) != (e - b) // 4:
        return False, f"spike's signature has {len(ref)} words, expected {(e - b) // 4}"
    # the reference ISS: EBREAK, no unwritten register read, no unspecified instruction, no access
    # to the core region (iss_main exits 2 with "region": true)
    iss_ram = d / f"{name}.iss.bin"
    iss_ram.unlink(missing_ok=True)
    p = run([ISS, hexf, "--check-init", "--ui", "pins", "--seed", "1", "--dump-ram", iss_ram])
    if p.returncode != 0:
        tail = p.stdout.strip().splitlines()[-1:] or [p.stderr[-300:]]
        return False, f"reference ISS run failed (exit {p.returncode}): {tail[0][:400]}"
    iss_sig = sig_words(iss_ram.read_bytes(), b, e)
    if iss_sig != ref:
        k = next(i for i in range(len(ref)) if iss_sig[i] != ref[i])
        return False, (f"the reference ISS disagrees with spike at signature word {k} "
                       f"(0x{b + 4 * k:x}): {iss_sig[k]} vs {ref[k]}")
    for seed in seeds:
        ram = d / f"{name}.dut-{seed}.bin"
        ram.unlink(missing_ok=True)
        s, _ = run_cosim.run_one("pins", hexf, seed, dump_ram=ram, src=design)
        if not s["pass"]:
            return False, f"cosim (pins, seed {seed}): {s['error']}"
        got = sig_words(ram.read_bytes(), b, e)
        if got != ref:
            k = next(i for i in range(len(ref)) if got[i] != ref[i])
            return False, (f"seed {seed}: signature word {k} (0x{b + 4 * k:x}) = {got[k]}, "
                           f"spike {ref[k]}")
    return True, None


# ---- contract v3 (--v3) --------------------------------------------------------------------------
# No splitting and no excluded cases: every test fits the 16 MiB flash whole (jal-01, the largest,
# is 14 MB of code), and nothing aliases inside it.
def discover_v3():
    """{name: source} for every test in DIRS, and the SKIP_V3 entries that matched."""
    tests, skipped = {}, {}
    for d in DIRS:
        for src in sorted((SUITE / d).glob("*.S")):
            if src.stem in SKIP_V3:
                skipped[src.stem] = SKIP_V3[src.stem]
            else:
                tests[src.stem] = src
    if not tests:
        raise SystemExit(f"run_arch.py --v3: no tests under {SUITE} (third_party/riscv-arch-test "
                         "not checked out?)")
    missing = sorted(set(SKIP_V3) - set(skipped))
    if missing:
        raise SystemExit(f"run_arch.py --v3: SKIP_V3 lists tests the suite doesn't have: {missing}")
    return tests, skipped


def case_defines(text: str) -> list:
    """The -D flags of the one RVTEST_CASE whose conditions hold for the v3 model (ISA_V3,
    HW_MISALIGNED_V3), as riscof selects them: e.g. ["-DRVTEST_E=True", "-DTEST_CASE_1=True"]."""
    chosen = []
    for num, body in RVTEST_CASE_RE.findall(text):
        ok, defs = True, []
        for item in (s.strip() for s in body.lstrip("/").split(";")):
            if not item:
                continue
            m = re.fullmatch(r"check\s+ISA\s*:=\s*regex\((.*)\)", item)
            if m:
                ok = ok and re.match(m.group(1), ISA_V3) is not None
                continue
            m = re.fullmatch(r"check\s+hw_data_misaligned_support\s*:=\s*(True|False)", item)
            if m:
                ok = ok and (m.group(1) == "True") == HW_MISALIGNED_V3
                continue
            m = re.fullmatch(r"def\s+(\w+)\s*=\s*(\S+)", item)
            if m:
                defs.append(f"-D{m.group(1)}={m.group(2)}")
                continue
            raise RuntimeError(f"RVTEST_CASE({num}): unknown condition {item!r}")
        if ok:
            chosen.append((num, defs))
    if len(chosen) != 1:
        raise RuntimeError(f"{len(chosen)} RVTEST_CASEs apply to {ISA_V3} with hardware "
                           f"misaligned access {HW_MISALIGNED_V3} (cases "
                           f"{[c[0] for c in chosen]}); expected exactly one")
    return chosen[0][1]


def flow_key_v3() -> str:
    """Everything a staged image depends on besides the suite; --phase dut refuses a manifest
    made under another key."""
    h = hashlib.sha256()
    for p in (HERE / "model_test.h", LINK, ELF2HEX):
        h.update(p.name.encode() + b"\0" + p.read_bytes())
    settings = json.dumps([SIG_LD, CFLAGS_V3, SPIKE_V3, SPIKE_ISA_V3, ISA_V3, HW_MISALIGNED_V3,
                           CODE_V3, STUB_V3, SIG_V3, RESULT_PASS_V3])
    h.update(settings.replace(str(ROOT), "<root>").encode())  # the same key in any checkout
    return h.hexdigest()


def suite_key_v3() -> str:
    """A hash of the suite's content (the env headers and the test sources), so a suite bump is
    caught on any checkout, including tools/hwe_run.sh mirrors, which carry no .git."""
    h = hashlib.sha256()
    for p in sorted(ENV.glob("*")) + [s for d in DIRS for s in sorted((SUITE / d).glob("*.S"))]:
        if p.is_file():
            h.update(str(p.relative_to(SUITE)).encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


_spike_isa = None
_spike_lock = threading.Lock()


def spike_isa_v3(elf: Path) -> list:
    """SPIKE_ISA_V3's Zicclsm form, unless this spike rejects the extension (probed once, on a
    test's ELF): then its --misaligned form, if this spike has that option."""
    global _spike_isa
    with _spike_lock:
        if _spike_isa is None:
            p = run(["spike"] + SPIKE_ISA_V3[0] + SPIKE_V3[1:-1] + ["--instructions=1", elf],
                    timeout=60)
            if "bad --isa" not in p.stdout + p.stderr:
                _spike_isa = SPIKE_ISA_V3[0]
            else:
                h = run(["spike", "--help"], timeout=60)
                if "--misaligned" not in h.stdout + h.stderr:
                    raise RuntimeError("this spike takes neither Zicclsm nor --misaligned: "
                                       + (p.stdout + p.stderr).strip()[-300:])
                _spike_isa = SPIKE_ISA_V3[1]
        return _spike_isa


def suite_commit_v3():
    """The suite submodule's HEAD (for the record), or None where the checkout has no git data
    (a tools/hwe_run.sh mirror): suite_key_v3 is what --phase dut checks."""
    p = run(["git", "-C", SUITE.parent, "rev-parse", "--show-toplevel", "HEAD"])
    lines = p.stdout.split()
    if p.returncode != 0 or len(lines) != 2 or Path(lines[0]).resolve() != SUITE.parent.resolve():
        return None
    return lines[1]


def tool_version(cmd: list) -> str:
    try:
        p = run(cmd + ["--version"], timeout=60)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return f"unavailable: {ex}"
    return ((p.stdout or p.stderr).strip().splitlines() or ["?"])[0]


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def flash_image_v3(elf: Path, hexf: Path) -> int:
    """Writes the DUT's flash image: the ELF's load image plus STUB_V3 at flash byte 0. Returns the
    image's end (bytes)."""
    segs = list(elf2hex.load_segments(str(elf)))
    low = sorted(a for a, _ in segs if a < CODE_V3)
    if low:
        raise RuntimeError(f"a load segment at {low[0]:#x}, below the code at {CODE_V3:#x}")
    segs.append((0, struct.pack("<I", STUB_V3)))
    try:
        hexf.write_text(elf2hex.to_hex(segs))
    except SystemExit as ex:  # elf2hex reports a segment outside flash or an overlap this way
        raise RuntimeError(f"flash image: {ex}")
    return max(a + len(s) for a, s in segs)


def sig_words_v3(psram: bytes, begin: int, end: int) -> list:
    lo, hi = begin - PSRAM_CPU_V3, end - PSRAM_CPU_V3
    return [f"{w:08x}" for w in struct.unpack_from(f"<{(hi - lo) // 4}I", psram, lo)]


def first_diff(got: list, ref: list) -> int:
    return next(i for i in range(len(ref)) if got[i] != ref[i])


def reference_v3(name: str, elf: Path) -> list:
    """spike's signature words, cached in build/arch/ref-v3/<name>.sig (+ .key)."""
    sig, keyf = REF_V3 / f"{name}.sig", REF_V3 / f"{name}.key"
    args = SPIKE_V3 + spike_isa_v3(elf)
    h = hashlib.sha256(elf.read_bytes())
    spike = shutil.which("spike") or "spike"
    st = os.stat(spike)
    h.update(f"{' '.join(args)}|{spike}|{st.st_size}|{st.st_mtime_ns}".encode())
    key = h.hexdigest()
    if not (sig.exists() and keyf.exists() and keyf.read_text() == key):
        tmp = REF_V3 / f"{name}.sig.tmp"
        tmp.unlink(missing_ok=True)
        p = run(args + [f"+signature={tmp}", "+signature-granularity=4", elf], timeout=600)
        if p.returncode != 0 or not tmp.exists():
            raise RuntimeError(f"spike failed (exit {p.returncode}; 1 = an unexpected trap reached "
                               f"rvmodel_trap): {(p.stderr or p.stdout)[-500:]}")
        tmp.replace(sig)
        keyf.write_text(key)
    return sig.read_text().split()


def prep_one_v3(name: str, src: Path, sig_ld: Path) -> dict:
    """Steps 1-3 for one test. Returns its manifest entry, or {"error": ...}."""
    d = WORK_V3 / name
    d.mkdir(parents=True, exist_ok=True)
    try:
        defs = case_defines(src.read_text())
    except RuntimeError as ex:
        return {"error": f"RVTEST_CASE selection: {ex}"}
    elf, hexf = d / f"{name}.elf", STAGE_V3 / f"{name}.hex"
    # the INSERT script goes first: ld inserts the statements parsed before its INSERT
    p = run([CROSS + "gcc"] + CFLAGS_V3 + ["-T", sig_ld, "-T", LINK] + defs + [src, "-o", elf])
    if p.returncode != 0:
        return {"error": "build failed: " + p.stderr[-800:]}
    syms = symbols(elf)
    for s in ("rvtest_entry_point", "begin_signature", "end_signature"):
        if s not in syms:
            return {"error": f"no {s} symbol"}
    if syms["rvtest_entry_point"] != CODE_V3:
        return {"error": f"rvtest_entry_point is at {syms['rvtest_entry_point']:#x}, not at the "
                         f"code start {CODE_V3:#x} (spike's reset vector, the stub's target)"}
    b, e = syms["begin_signature"], syms["end_signature"]
    if not (SIG_V3 <= b < e <= REGION_CPU_V3) or (e - b) % 4:
        return {"error": f"signature {b:#x}..{e:#x} is not in PSRAM between {SIG_V3:#x} and the "
                         f"core region at {REGION_CPU_V3:#x} (contract v3 section 3)"}
    try:
        flash_end = flash_image_v3(elf, hexf)
    except RuntimeError as ex:
        return {"error": str(ex)}
    try:
        ref = reference_v3(name, elf)
    except (RuntimeError, subprocess.TimeoutExpired) as ex:
        return {"error": f"reference: {ex}"}
    if len(ref) != (e - b) // 4:
        return {"error": f"spike's signature has {len(ref)} words, expected {(e - b) // 4}"}
    # the reference ISS: to the self-loop after RVMODEL_HALT, no unwritten register or
    # arbitrary-reset CSR read, no unspecified step, no core-region access
    dump = d / f"{name}.iss-psram.bin"
    dump.unlink(missing_ok=True)
    try:
        p = run([ISS, "--v3", hexf, "--json", "--check-init", "--max", ISS_CAP_V3,
                 "--dump-psram", dump], timeout=600)
    except subprocess.TimeoutExpired:
        return {"error": "reference ISS run timed out (600 s)"}
    summ = {}
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    if lines:
        try:
            summ = json.loads(lines[-1])
        except json.JSONDecodeError:
            summ = {}
    why = None
    if p.returncode != 0 or summ.get("end") != "selfloop":
        why = (f"exit {p.returncode}, end {summ.get('end')!r} at pc {summ.get('end_pc', 0):#x}: "
               f"{p.stderr.strip()[-300:]}")
    elif summ.get("touched_region") or summ.get("uninit_read"):
        why = f"core region touched or uninitialized read: {lines[-1][:300]}"
    elif summ.get("gpio_out") != RESULT_PASS_V3:
        why = (f"it ended with GPIO_OUT = {summ.get('gpio_out', 0):#04x}, not RVMODEL_HALT's "
               f"{RESULT_PASS_V3:#04x} (0xee: an unexpected trap reached rvmodel_trap)")
    if why:
        dump.unlink(missing_ok=True)
        return {"error": f"reference ISS run failed ({why})"}
    iss_sig = sig_words_v3(dump.read_bytes(), b, e)
    dump.unlink()
    if iss_sig != ref:
        k = first_diff(iss_sig, ref)
        return {"error": f"the reference ISS disagrees with spike at signature word {k} "
                         f"(0x{b + 4 * k:x}): {iss_sig[k]} vs {ref[k]}"}
    return {"hex": hexf.name, "sha256": sha256_file(hexf), "sig": [b, e], "ref": ref,
            "defines": defs, "iss_retired": summ.get("retired"), "iss_traps": summ.get("traps"),
            "flash_bytes": flash_end}


def prep_v3(only, jobs: int) -> bool:
    """Steps 1-3 for every test; stages the images and STAGE_V3/manifest.json."""
    p = run(["make", "-s", "-C", ROOT / "verif" / "programs", ISS])
    if p.returncode != 0:
        print(json.dumps({"pass": False, "error": "ISS build failed: " + p.stderr[-800:]}))
        return False
    tests, skipped = discover_v3()
    if only:
        unknown = sorted(set(only) - set(tests))
        if unknown:
            raise SystemExit(f"run_arch.py --v3: unknown or skipped tests: {unknown}")
        tests = {n: tests[n] for n in only}
    if STAGE_V3.exists():
        shutil.rmtree(STAGE_V3)
    for d in (STAGE_V3, WORK_V3, REF_V3):
        d.mkdir(parents=True, exist_ok=True)
    sig_ld = WORK_V3 / "signature.ld"
    sig_ld.write_text(SIG_LD)
    entries = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(prep_one_v3, n, s, sig_ld): n for n, s in tests.items()}
        for f in concurrent.futures.as_completed(futs):
            n = futs[f]
            try:
                ent = f.result()
            except Exception as e:  # a bug here must fail the test, not pass silently
                ent = {"error": f"{type(e).__name__}: {e}"}
            entries[n] = ent
            err = ent.get("error")
            print(f"{'PREP' if not err else 'FAIL'} {n}" + (f": {err}" if err else
                  f" ({len(ent['ref'])} signature words, ISS {ent['iss_retired']} retired, "
                  f"{ent['iss_traps']} traps)"), flush=True)
    manifest = {"contract": "v3", "flow_key": flow_key_v3(), "suite_key": suite_key_v3(),
                "suite_commit": suite_commit_v3(), "isa": ISA_V3,
                "spike_isa": _spike_isa, "gcc": tool_version([CROSS + "gcc"]),
                "only": only or None, "skipped": skipped,
                "tests": {n: entries[n] for n in sorted(entries)}}
    (STAGE_V3 / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    bad = sorted(n for n, e in entries.items() if "error" in e)
    print(json.dumps({"phase": "prep", "pass": not bad, "tests": len(entries), "failed": len(bad),
                      "skipped": len(skipped), "spike_isa": _spike_isa,
                      "manifest": str(STAGE_V3 / "manifest.json")}))
    return not bad


def dut_one_v3(name: str, ent: dict, design: Path, seeds) -> tuple:
    """Step 4 for one test. Returns (passed, error or None, info)."""
    if "error" in ent:
        return False, f"prep: {ent['error']}", {}
    hexf = STAGE_V3 / ent["hex"]
    if not hexf.exists() or sha256_file(hexf) != ent["sha256"]:
        return False, f"{hexf.name} is missing or differs from the manifest: rerun --phase prep", {}
    b, e = ent["sig"]
    ref = ent["ref"]
    cycles = {}
    for seed in seeds:
        dump = WORK_V3 / "dut" / f"{name}.seed{seed}.psram.bin"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.unlink(missing_ok=True)
        s, _ = run_cosim.run_one("pins", hexf, seed, dump_ram=dump, src=design, v3=True)
        if not s["pass"]:
            dump.unlink(missing_ok=True)  # 8 MiB; the harness error says what went wrong
            return False, f"cosim (pins, seed {seed}): {s['error']}", {"cycles": cycles}
        got = sig_words_v3(dump.read_bytes(), b, e)
        if got != ref:  # the dump stays for debugging
            k = first_diff(got, ref)
            return False, (f"seed {seed}: signature word {k} (0x{b + 4 * k:x}) = {got[k]}, "
                           f"spike {ref[k]}"), {"cycles": cycles}
        dump.unlink()  # 8 MiB each
        cycles[seed] = s["cycles"]
    return True, None, {"cycles": cycles}


def manifest_problem_v3(man: dict, only):
    """Why the staged manifest can't serve this --phase dut run, or None. Each image's sha256 is
    checked later, per test (dut_one_v3)."""
    if man.get("flow_key") != flow_key_v3():
        return ("the staged images were made by another flow (model_test.h, link.ld, elf2hex.py "
                "or run_arch.py's v3 settings differ)")
    tests = man.get("tests", {})
    if only:
        unknown = sorted(set(only) - set(tests))
        if unknown:
            return f"tests not in the manifest: {unknown}"
    elif man.get("only"):
        return (f"the manifest covers only {man['only']} (prep ran with --only), so it can't serve "
                "a full run")
    if SUITE.exists():  # where the suite is checked out, the manifest must come from it
        if man.get("suite_key") != suite_key_v3():
            return (f"the suite changed since prep (manifest suite commit "
                    f"{man.get('suite_commit')}, now {suite_commit_v3()})")
        names, _ = discover_v3()
        if not only and set(names) != set(tests):
            return (f"the manifest's tests differ from the suite's: missing "
                    f"{sorted(set(names) - set(tests))}, extra {sorted(set(tests) - set(names))}")
    return None


def dut_v3(only, design: Path, seeds, jobs: int) -> int:
    """Step 4 for every test of the manifest; writes build/arch.json and build/arch/report.json.
    Refuses (exit 1, before any cosim) a manifest that doesn't match this checkout: see
    manifest_problem_v3."""
    out = OUT / "only.json" if only else SUMMARY
    report = OUT / "report.json"
    OUT.mkdir(parents=True, exist_ok=True)

    def setup_fail(err):
        print(json.dumps({"pass": False, "error": err}))
        out.write_text(json.dumps({"pass": False, "tests": {}}, indent=1) + "\n")
        report.write_text(json.dumps({"pass": False, "contract": "v3", "error": err},
                                     indent=1) + "\n")
        return 1

    mpath = STAGE_V3 / "manifest.json"
    if not mpath.exists():
        return setup_fail(f"no {mpath}: run --phase prep first")
    man = json.loads(mpath.read_text())
    err = manifest_problem_v3(man, only)
    if err:
        return setup_fail(err + ": rerun --phase prep")
    tests = {n: man["tests"][n] for n in only} if only else man["tests"]
    try:
        run_cosim.ensure_built("pins", design, v3=True)
    except RuntimeError as ex:
        return setup_fail(str(ex))
    results, errors, cycles = {}, {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(dut_one_v3, n, ent, design, seeds): n for n, ent in tests.items()}
        for f in concurrent.futures.as_completed(futs):
            n = futs[f]
            try:
                ok, err, info = f.result()
            except Exception as e:  # a bug here must fail the test, not pass silently
                ok, err, info = False, f"{type(e).__name__}: {e}", {}
            results[n] = ok
            if err:
                errors[n] = err
            cycles[n] = info.get("cycles", {})
            note = ("cycles " + ", ".join(f"s{s} {c}" for s, c in sorted(cycles[n].items()))
                    if ok else err)
            print(f"{'PASS' if ok else 'FAIL'} {n}: {note}", flush=True)
    ordered = {n: results[n] for n in sorted(results)}
    ok = bool(ordered) and all(ordered.values()) and len(ordered) == len(tests)
    out.write_text(json.dumps({"pass": ok, "tests": ordered}, indent=1) + "\n")
    report.write_text(json.dumps(
        {"pass": ok, "contract": "v3", "seeds": seeds, "src": str(design), "only": only,
         "errors": errors, "skipped": man.get("skipped", {}), "isa": man.get("isa"),
         "suite_commit": man.get("suite_commit"), "spike_isa": man.get("spike_isa"),
         "gcc": man.get("gcc"), "cycles": {n: cycles[n] for n in sorted(cycles)}},
        indent=1) + "\n")
    print(json.dumps({"pass": ok, "contract": "v3", "tests": len(ordered),
                      "failed": sum(not v for v in ordered.values()),
                      "skipped": len(man.get("skipped", {})), "seeds": seeds}))
    return 0 if ok else 1


def run_v3(a) -> int:
    seeds = [int(x) for x in a.seeds.split(",") if x] if a.seeds else list(run_cosim.SEEDS_V3)
    if not seeds or min(seeds) <= 0:
        raise SystemExit("run_arch.py --v3: --seeds must list positive seeds")
    SUMMARY.unlink(missing_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.phase in ("all", "prep"):
        ok = prep_v3(a.only, a.jobs)
        if a.phase == "prep":
            return 0 if ok else 1
    return dut_v3(a.only, a.src, seeds, a.jobs)


def main() -> int:
    ap = argparse.ArgumentParser(description="riscv-arch-test RV32E on the DUT")
    ap.add_argument("--src", type=Path, default=ROOT / "src")
    ap.add_argument("--seeds", default=None,
                    help="comma-separated cosim seeds (default 1,2,3; with --v3 1,2,3,4: every "
                         "memory power-up mode)")
    ap.add_argument("--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--only", nargs="*",
                    help="run only these tests; results go to build/arch/only.json")
    ap.add_argument("--v3", action="store_true",
                    help="contract v3 (flash/PSRAM; becomes the default at the integration swap)")
    ap.add_argument("--phase", choices=("all", "prep", "dut"), default="all",
                    help="--v3: prep = build, spike and ISS, staged in build/programs/arch-v3/; "
                         "dut = the cosim from the staged images; all = both (default)")
    a = ap.parse_args()
    if a.v3:
        return run_v3(a)
    if a.phase != "all":
        ap.error("--phase needs --v3")
    seeds = [int(x) for x in (a.seeds or "1,2,3").split(",") if x]
    summary = ROOT / "build" / "arch.json"
    summary.unlink(missing_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    REF.mkdir(parents=True, exist_ok=True)
    p = run(["make", "-s", "-C", ROOT / "verif" / "programs", ISS])
    if p.returncode != 0:
        print(json.dumps({"pass": False, "error": "ISS build failed: " + p.stderr[-800:]}))
        return 1
    tests, skipped = discover()
    if a.only:
        unknown = sorted(set(a.only) - set(tests))
        if unknown:
            ap.error(f"unknown or skipped tests: {unknown}")
        tests = {n: tests[n] for n in a.only}
    try:
        run_cosim.ensure_built("pins", a.src)
    except RuntimeError as ex:
        print(json.dumps({"pass": False, "error": str(ex)}))
        summary.write_text(json.dumps({"pass": False, "tests": {}}, indent=1) + "\n")
        return 1
    results, errors, split, excluded = {}, {}, {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = {ex.submit(one, n, s, a.src, seeds): n for n, s in tests.items()}
        for f in concurrent.futures.as_completed(futs):
            n = futs[f]
            try:
                ok, err, info = f.result()
            except Exception as e:  # a bug here must fail the test, not pass silently
                ok, err, info = False, f"{type(e).__name__}: {e}", {}
            results[n] = ok
            if err:
                errors[n] = err
            if info.get("pieces", 1) > 1:
                split[n] = info["pieces"]
            if info.get("excluded_cases"):
                excluded[n] = {f"inst_{c}": f"{sz} bytes of code: does not fit the 64 KB RAM"
                               for c, sz in sorted(info["excluded_cases"].items())}
            note = f" ({info['pieces']} pieces)" if info.get("pieces", 1) > 1 else ""
            print(f"{'PASS' if ok else 'FAIL'} {n}{note}" + (f": {err}" if err else ""),
                  flush=True)
    ordered = {n: results[n] for n in sorted(results)}
    ok = bool(ordered) and all(ordered.values()) and len(ordered) == len(tests)
    # ruling R10 for the whole run: the excluded cases are exactly EXCLUDED (for the tests run)
    ruled = {n: {f"inst_{c}" for c in cs} for n, cs in EXCLUDED.items() if n in tests}
    got = {n: set(cs) for n, cs in excluded.items()}
    if got != ruled:
        ok = False

        def show(d):
            return sorted((n, sorted(c, key=lambda s: int(s.split("_")[1]))) for n, c in d.items())

        errors["(excluded cases)"] = f"excluded {show(got)}, ruled {show(ruled)}"
        print(f"FAIL excluded cases differ from ruling R10: {errors['(excluded cases)']}",
              flush=True)
    # a partial run (--only) never writes build/arch.json, so it can't pass for the full suite
    out = OUT / "only.json" if a.only else summary
    out.write_text(json.dumps({"pass": ok, "tests": ordered}, indent=1) + "\n")
    (OUT / "report.json").write_text(json.dumps(
        {"pass": ok, "seeds": seeds, "src": str(a.src), "only": a.only, "errors": errors,
         "skipped": skipped, "split": split, "excluded_cases": excluded}, indent=1) + "\n")
    print(json.dumps({"pass": ok, "tests": len(ordered),
                      "failed": sum(not v for v in ordered.values()),
                      "skipped": len(skipped), "split": split,
                      "excluded_cases": {n: len(c) for n, c in excluded.items()}}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
