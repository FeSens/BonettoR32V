#!/usr/bin/env python3
"""riscv-arch-test (RV32E) on the DUT. See docs/arch-test.md.

Usage: run_arch.py [--src DIR] [--seeds 1,2,3] [--jobs N] [--only NAME ...]

For every RV32E test of third_party/riscv-arch-test (rv32e_m/E and rv32e_m/privilege, minus the
skips listed in SKIP and docs/arch-test.md):
  1. build one ELF (verif/arch-test/model_test.h, linked at 0x10000) and its 64 KB RAM image;
  2. reference signature from spike (cached in build/arch/ref/, keyed on the ELF and spike);
  3. the reference ISS must run the image to EBREAK without reading an unwritten register or
     hitting an unspecified instruction, and leave the same signature as spike;
  4. the DUT runs it through run_cosim.run_one("pins", ..., dump_ram=...) (what
     `run_cosim.py --mode pins --dump-ram` does) for each seed: the cosim must pass (SPI writes,
     uo_out and the final RAM equal the ISS) and the signature in the dumped RAM must equal
     spike's.
Writes build/arch.json as {"pass": bool, "tests": {name: bool}} (removed first, so it is always
fresh; --only writes build/arch/only.json instead) and build/arch/report.json with the errors and
the skips. Exit 0 only if every test passes.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "verif" / "arch-test"
SUITE = ROOT / "third_party" / "riscv-arch-test" / "riscv-test-suite"
ENV = SUITE / "env"
OUT = ROOT / "build" / "arch"
REF = OUT / "ref"
ISS = ROOT / "build" / "cosim" / "iss_main"
LINK = ROOT / "verif" / "programs" / "link.ld"
ELF2HEX = ROOT / "verif" / "programs" / "elf2hex.py"
sys.path.insert(0, str(ROOT / "verif" / "cosim"))
import run_cosim  # noqa: E402

BASE = 0x10000
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
    if not (BASE <= b < e <= BASE + 0x10000) or (e - b) % 4:
        return False, f"signature {b:#x}..{e:#x} is not inside the 64 KB RAM"
    try:
        ref = reference(name, elf)
    except (RuntimeError, subprocess.TimeoutExpired) as ex:
        return False, f"reference: {ex}"
    if len(ref) != (e - b) // 4:
        return False, f"spike's signature has {len(ref)} words, expected {(e - b) // 4}"
    # the reference ISS: EBREAK, no unwritten register read, no unspecified instruction
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


def main() -> int:
    ap = argparse.ArgumentParser(description="riscv-arch-test RV32E on the DUT")
    ap.add_argument("--src", type=Path, default=ROOT / "src")
    ap.add_argument("--seeds", default="1,2,3")
    ap.add_argument("--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--only", nargs="*",
                    help="run only these tests; results go to build/arch/only.json")
    a = ap.parse_args()
    seeds = [int(x) for x in a.seeds.split(",") if x]
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
