#!/usr/bin/env python3
"""Pin-level cosim driver. See docs/cosim.md.

Two contracts until the integration swap (plan Task 9): contract v2 (the default; harness_v2.cpp)
and contract v3 with --v3 (harness.cpp: the QSPI Pmod models, the v3 ISS in lockstep, the
peripheral pin models). --v3 becomes the default at the swap.

One run:
  run_cosim.py --mode {rvfi,pins} --prog build/programs/<name>.hex [--seed S] [--max-cycles C]
               [--timing-json PATH] [--dump-ram PATH] [--src DIR] [--vflags ARGS] [--trace PATH]
prints one JSON line
  {"prog","mode","seed","pass","retired","cycles","cpi","max_cycles_per_insn",
   "coremark_cycles_per_iter","error"}
and exits 0 on a pass. coremark_cycles_per_iter is null for every run without CoreMark timing
marks (always in v2): the cycles between uo_out changing to 0xC0 and to 0xC1, divided by
expect.json coremark_iters (contract v3; never 0).

Contract v3 (--v3): the same, plus --clk-ns (default 25, contract 7) and <name>.expect.json passed
to the harness as --expect (the peripheral stimulus and checks). The latency cap is 2,048 cycles.

Everything (the `make cosim` gate):
  run_cosim.py --all [--src DIR] [--jobs N]
builds the programs, runs every program in both modes under seeds 1, 2 and 3, writes
build/cosim.json and exits 0 only if every run passes.
  run_cosim.py --all --v3 [--src DIR] [--jobs N] [--no-make]
runs every program `make -C verif/programs list` reports (it must include the plan's 26,
V3_PROGRAMS) in both modes under seeds 1-4 (the four memory power-up modes, pmod_init_for_seed)
and the rvfi-mode liveness fuzz under seeds 1-5, and writes build/cosim.json.

The liveness fuzz (contract section 4 on reserved encodings; docs/cosim.md):
  run_cosim.py --fuzz-liveness --seed S [--mode rvfi|pins] [--fuzz-runs K] [--fuzz-cap N]
               [--src DIR]
runs K (default 2048) probe sub-runs and K/256 walk sub-runs from reset on random images, no
reference, checking liveness and the pin rules (rvfi mode, the default; pins mode can only check
the pin rules), and prints one JSON line {"prog": "fuzz_liveness", "mode", "seed", "pass",
"liveness_checked", "runs", "walks", "probes_retired", "halted", "capped", "retired", "cycles",
"max_cycles_per_insn", "error"}; exit 0 on a pass. With --v3 the images are random flash images and
the line has FUZZ_KEYS_V3 ("ended", "capped", "wfi_stalls" in place of "halted" and "capped":
nothing halts in v3).

--max-cycles defaults to the latency cap's bound, 1,024 * (reference retirements + 16).
--timing-json (rvfi mode) writes {"pass", "first_retire_cycle", "max_cycles_per_insn",
"per_class"} on every run; with "pass": false the numbers only cover the run up to the failure.
--dump-ram writes the final 64 KB RAM image (raw bytes) as the design left it.
Programs whose <name>.expect.json lists "log_contains" must leave those strings in the RAM log
ring at 0xF000-0xF7FF (NUL-terminated), e.g. coremark's "Correct operation validated"; in v3 the
log ring is PSRAM 0x7F_0000-0x7F_07FF (CPU 0x017F_0000), read from the harness's --dump-psram.
"""
import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COSIM = ROOT / "verif" / "cosim"
PROGS = ROOT / "build" / "programs"
SRC = ROOT / "src"
SEEDS = (1, 2, 3)
MODES = ("rvfi", "pins")
LOG_RING, LOG_RING_END = 0xF000, 0xF800
KEYS = ("prog", "mode", "seed", "pass", "retired", "cycles", "cpi", "max_cycles_per_insn",
        "coremark_cycles_per_iter", "error")

# contract v3
SEEDS_V3 = (1, 2, 3, 4)            # pmod_init_for_seed: every flash x PSRAM power-up mode
FUZZ_SEEDS_V3 = (1, 2, 3, 4, 5)
LAT_CAP_V3 = 2048                   # contract 4
CLK_NS = 25.0                       # contract 7: 40 MHz signoff
LOG_RING_V3, LOG_RING_V3_END = 0x7F0000, 0x7F0800  # PSRAM bytes (CPU 0x017F_0000)
V3_PROGRAMS = (                     # plan Task 4 "Produces": a subset of `make list`
    "selftest", "csr", "trap_ecall", "trap_ebreak", "trap_jal", "trap_jalr", "trap_branch",
    "trap_zero", "trap_ones", "irq_timer", "irq_stress", "wfi", "coherence", "uart_echo",
    "spi_loop", "pwm", "ir_loop", "gpio_edge", "mem_init_check", "random_1", "random_2",
    "random_3", "random_4", "random_5", "random_6", "coremark")

_build_lock = threading.Lock()


def src_files(src: Path):
    """The design: every src/*.v file, in sorted (byte) order (contract section 1)."""
    return sorted((p for p in src.glob("*.v") if p.is_file()), key=lambda p: p.name.encode())


def key_files(src: Path):
    """What the build stamp hashes: every file under src/ except __pycache__ and the unit tests'
    non-Verilog test_* files (a test_*.v file is part of the design). Over-inclusive on purpose:
    a spurious rebuild is cheap, a stale model is not."""
    return sorted(p for p in src.rglob("*")
                  if p.is_file() and (p.suffix == ".v" or not p.name.startswith("test_"))
                  and "__pycache__" not in p.relative_to(src).parts)


_verilator_version = None


def verilator_version() -> str:
    global _verilator_version
    if _verilator_version is None:
        try:
            p = subprocess.run(["verilator", "--version"], capture_output=True, text=True)
            _verilator_version = p.stdout.strip() or p.stderr.strip()
        except OSError as e:
            _verilator_version = f"verilator unavailable: {e}"
    return _verilator_version


HARNESS_DEPS = {
    "v2": ("harness_v2.cpp", "spiram.h", "iss.h", "build.sh"),
    "v3": ("harness.cpp", "pmod.h", "iss3.h", "periph_models.h", "build.sh"),
}


def build_key(mode: str, src: Path, vflags: str, trace: bool, v3: bool = False) -> str:
    hv = "v3" if v3 else "v2"
    h = hashlib.sha256(f"{mode}|{vflags}|{trace}|{hv}|{verilator_version()}".encode())
    deps = [COSIM / n for n in HARNESS_DEPS[hv]]
    for p in key_files(src):
        h.update(str(p.relative_to(src)).encode() + b"\0")
        h.update(p.read_bytes())
    for p in deps:
        h.update(p.name.encode() + b"\0")
        h.update(p.read_bytes())
    return h.hexdigest()


def build_dir_for(mode: str, src: Path, vflags: str, trace: bool, v3: bool = False) -> Path:
    name = mode + ("-v3" if v3 else "")
    if src.resolve() != SRC.resolve() or vflags:
        tag = hashlib.sha256(f"{src.resolve()}|{vflags}".encode()).hexdigest()[:12]
        name = f"{name}-{tag}"
    if trace:
        name += "-trace"
    return ROOT / "build" / "cosim" / name


def ensure_built(mode: str, src: Path = SRC, vflags: str = "", trace: bool = False,
                 v3: bool = False) -> Path:
    """Build the harness (v2, or the contract v3 one with v3=True) for (mode, src) unless an
    up-to-date build exists. Returns its path."""
    out = build_dir_for(mode, src, vflags, trace, v3)
    exe, stamp = out / "harness", out / "stamp"
    key = build_key(mode, src, vflags, trace, v3)
    out.parent.mkdir(parents=True, exist_ok=True)
    # _build_lock serializes threads of this process; the lockf lock on build/cosim/<dir>.lock
    # serializes processes (two pytest or eval runs building the same model).
    with _build_lock, open(out.parent / f"{out.name}.lock", "a") as lk:
        fcntl.lockf(lk, fcntl.LOCK_EX)
        try:
            if exe.exists() and stamp.exists() and stamp.read_text() == key:
                return exe
            if stamp.exists():
                stamp.unlink()
            env = dict(os.environ, VFLAGS=vflags, TRACE="1" if trace else "0",
                       HARNESS="v3" if v3 else "v2")
            p = subprocess.run(["bash", str(COSIM / "build.sh"), mode, str(src), str(out)],
                               env=env, capture_output=True, text=True)
            if p.returncode != 0:
                raise RuntimeError(f"cosim build failed ({mode}, {src}):\n{p.stderr[-3000:]}")
            stamp.write_text(key)
        finally:
            fcntl.lockf(lk, fcntl.LOCK_UN)
    return exe


def log_ring_text(ram: bytes, lo: int = LOG_RING, hi: int = LOG_RING_END) -> str:
    raw = ram[lo:hi]
    end = raw.find(b"\0")
    return raw[:end if end >= 0 else len(raw)].decode("latin-1")


def expect_for(hexfile: Path):
    exp = hexfile.with_name(hexfile.name[:-len(".hex")] + ".expect.json")
    return json.loads(exp.read_text()) if exp.exists() else {}


def run_one(mode: str, hexfile: Path, seed: int = 1, max_cycles: int = 0, timing_json=None,
            dump_ram=None, src: Path = SRC, vflags: str = "", trace=None, v3: bool = False,
            clk_ns: float = CLK_NS):
    """Run one cosim. Returns (summary dict with KEYS, full harness result dict). With v3=True
    the contract v3 harness runs it, and dump_ram receives the 8 MiB PSRAM image."""
    if v3:
        summary, res = _run3(mode, hexfile, seed, max_cycles, dump_ram, src, vflags, trace,
                             clk_ns)
    else:
        summary, res = _run(mode, hexfile, seed, max_cycles, dump_ram, src, vflags, trace)
    if timing_json:
        # Written on every run, so a stale file can't survive. "pass": false means the numbers
        # stop at the failure (partial) or are null.
        timing = {"pass": bool(summary["pass"]),
                  "first_retire_cycle": res.get("first_retire_cycle"),
                  "max_cycles_per_insn": res.get("max_cycles_per_insn"),
                  "per_class": res.get("per_class")}
        Path(timing_json).write_text(json.dumps(timing, indent=1) + "\n")
    return summary, res


def _run(mode, hexfile, seed, max_cycles, dump_ram, src, vflags, trace):
    hexfile = Path(hexfile)
    prog = hexfile.name[:-len(".hex")] if hexfile.name.endswith(".hex") else hexfile.stem
    summary = {k: None for k in KEYS}
    summary.update(prog=prog, mode=mode, seed=seed, **{"pass": False})
    try:
        exe = ensure_built(mode, src, vflags, trace=bool(trace))
    except RuntimeError as e:
        summary["error"] = str(e)
        return summary, {}
    with tempfile.TemporaryDirectory() as td:
        ram_path = Path(dump_ram) if dump_ram else Path(td) / "ram.bin"
        cmd = [str(exe), str(hexfile), "--seed", str(seed), "--dump-ram", str(ram_path)]
        if max_cycles:
            cmd += ["--max-cycles", str(max_cycles)]
        if trace:
            cmd += ["--trace", str(trace)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        except subprocess.TimeoutExpired:
            summary["error"] = "harness wall-clock timeout (2 h)"
            return summary, {}
        res = {}
        lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
        if lines:
            try:
                res = json.loads(lines[-1])
            except json.JSONDecodeError:
                res = {}
        if not res or p.returncode not in (0, 1):
            summary["error"] = (f"harness crashed (exit {p.returncode}): "
                                f"{(p.stderr or p.stdout)[-1500:]}")
            return summary, res
        for k in ("pass", "retired", "cycles", "cpi", "max_cycles_per_insn", "error"):
            summary[k] = res.get(k)
        if summary["pass"] and p.returncode != 0:
            summary["pass"] = False
            summary["error"] = f"harness exit {p.returncode} with pass=true"
        exp = expect_for(hexfile)
        if summary["pass"] and exp.get("log_contains"):
            text = log_ring_text(ram_path.read_bytes())
            for want in exp["log_contains"]:
                if want not in text:
                    summary["pass"] = False
                    summary["error"] = f"RAM log ring lacks {want!r}; log: {text[-400:]!r}"
                    break
    return summary, res


def _harness_json(p):
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    try:
        return json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        return {}


def _run3(mode, hexfile, seed, max_cycles, dump_psram, src, vflags, trace, clk_ns):
    hexfile = Path(hexfile)
    prog = hexfile.name[:-len(".hex")] if hexfile.name.endswith(".hex") else hexfile.stem
    summary = {k: None for k in KEYS}
    summary.update(prog=prog, mode=mode, seed=seed, **{"pass": False})
    try:
        exe = ensure_built(mode, src, vflags, trace=bool(trace), v3=True)
    except RuntimeError as e:
        summary["error"] = str(e)
        return summary, {}
    exp_path = hexfile.with_name(prog + ".expect.json")
    exp = json.loads(exp_path.read_text()) if exp_path.exists() else {}
    with tempfile.TemporaryDirectory() as td:
        cmd = [str(exe), str(hexfile), "--seed", str(seed), "--clk-ns", str(clk_ns)]
        if exp_path.exists():
            cmd += ["--expect", str(exp_path)]
        psram = Path(dump_psram) if dump_psram else (
            Path(td) / "psram.bin" if exp.get("log_contains") else None)
        if psram:
            cmd += ["--dump-psram", str(psram)]
        if max_cycles:
            cmd += ["--max-cycles", str(max_cycles)]
        if trace:
            cmd += ["--trace", str(trace)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        except subprocess.TimeoutExpired:
            summary["error"] = "harness wall-clock timeout (2 h)"
            return summary, {}
        res = _harness_json(p)
        if not res or p.returncode not in (0, 1):
            summary["error"] = (f"harness exit {p.returncode}: "
                                f"{(p.stderr or p.stdout)[-1500:]}")
            return summary, res
        for k in KEYS[3:]:
            summary[k] = res.get(k)
        if not summary["coremark_cycles_per_iter"]:
            summary["coremark_cycles_per_iter"] = None  # null, never 0
        if summary["pass"] and p.returncode != 0:
            summary["pass"] = False
            summary["error"] = f"harness exit {p.returncode} with pass=true"
        if summary["pass"] and exp.get("log_contains"):
            text = log_ring_text(psram.read_bytes(), LOG_RING_V3, LOG_RING_V3_END)
            for want in exp["log_contains"]:
                if want not in text:
                    summary["pass"] = False
                    summary["error"] = f"PSRAM log ring lacks {want!r}; log: {text[-400:]!r}"
                    break
    return summary, res


FUZZ_RUNS = 2048  # probe sub-runs per seed: every (bits [6:0], funct3) pair twice
FUZZ_CAP = 2000   # retirements per sub-run
FUZZ_KEYS = ["prog", "mode", "seed", "pass", "liveness_checked", "runs", "walks",
             "probes_retired", "halted", "capped", "retired", "cycles", "max_cycles_per_insn",
             "error"]
FUZZ_KEYS_V3 = ["prog", "mode", "seed", "pass", "liveness_checked", "runs", "walks",
                "probes_retired", "ended", "capped", "wfi_stalls", "retired", "cycles",
                "max_cycles_per_insn", "error"]


def run_fuzz(mode: str, seed: int, runs: int = FUZZ_RUNS, cap: int = FUZZ_CAP, src: Path = SRC,
             vflags: str = "", v3: bool = False) -> dict:
    """The liveness fuzz (harness --fuzz-liveness): `runs` probe sub-runs plus runs/256 walk
    sub-runs from reset on random images (reserved encodings included), checking only liveness
    (rvfi mode) and the pin rules. Returns the summary dict with FUZZ_KEYS ("runs" counts both
    kinds)."""
    keys = FUZZ_KEYS_V3 if v3 else FUZZ_KEYS
    s = {k: None for k in keys}
    s.update(prog="fuzz_liveness", mode=mode, seed=seed, **{"pass": False})
    try:
        exe = ensure_built(mode, src, vflags, v3=v3)
    except RuntimeError as e:
        s["error"] = str(e)
        return s
    try:
        p = subprocess.run([str(exe), "--fuzz-liveness", str(runs), "--fuzz-cap", str(cap),
                            "--seed", str(seed)], capture_output=True, text=True, timeout=7200)
    except subprocess.TimeoutExpired:
        s["error"] = "harness wall-clock timeout (2 h)"
        return s
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    try:
        res = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        res = {}
    if not res or p.returncode not in (0, 1):
        s["error"] = f"harness crashed (exit {p.returncode}): {(p.stderr or p.stdout)[-1500:]}"
        return s
    for k in keys[4:]:
        s[k] = res.get(k)
    s["pass"] = bool(res.get("pass")) and p.returncode == 0 and not res.get("error")
    if res.get("pass") and p.returncode != 0:
        s["error"] = f"harness exit {p.returncode} with pass=true"
    return s


def program_list():
    p = subprocess.run(["make", "-s", "-C", str(ROOT / "verif/programs"), "list"],
                       capture_output=True, text=True, check=True)
    return p.stdout.split()


def run_all(src: Path, jobs: int, make: bool = True) -> int:
    t0 = time.time()
    if make:
        p = subprocess.run(["make", "-C", str(ROOT / "verif/programs"), f"-j{jobs}", "all"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            print(json.dumps({"pass": False, "error": "program build failed: " + p.stderr[-2000:]}))
            return 1
    progs = program_list()
    for mode in MODES:
        try:
            ensure_built(mode, src)
        except RuntimeError as e:
            print(json.dumps({"pass": False, "error": str(e)}))
            Path(ROOT / "build" / "cosim.json").write_text(
                json.dumps({"pass": False, "error": str(e)}, indent=1) + "\n")
            return 1
    runs = [(m, PROGS / f"{p}.hex", s) for p in progs for m in MODES for s in SEEDS]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(run_one, m, h, s, src=src): (m, h, s) for m, h, s in runs}
        for f in concurrent.futures.as_completed(futs):
            summary, _ = f.result()
            results.append(summary)
            print(json.dumps(summary), flush=True)
    order = {p: i for i, p in enumerate(progs)}
    results.sort(key=lambda r: (order.get(r["prog"], 99), r["mode"], r["seed"]))
    ok = all(r["pass"] for r in results) and len(results) == len(runs)
    out = {"pass": ok, "runs": results, "programs": progs, "seeds": list(SEEDS),
           "runtime_s": round(time.time() - t0, 1)}
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "cosim.json").write_text(json.dumps(out, indent=1) + "\n")
    failed = [r for r in results if not r["pass"]]
    print(json.dumps({"pass": ok, "runs": len(results), "failed": len(failed),
                      "runtime_s": out["runtime_s"]}))
    for r in failed[:10]:
        print(f"FAIL {r['prog']} {r['mode']} seed {r['seed']}: {r['error']}", file=sys.stderr)
    return 0 if ok else 1


def missing_v3_programs(progs) -> list:
    """The plan's programs (V3_PROGRAMS) that a program list lacks."""
    return [p for p in V3_PROGRAMS if p not in set(progs)]


def run_all_v3(src: Path, jobs: int, make: bool = True) -> int:
    """The contract v3 gate: every program `make list` reports (a superset of V3_PROGRAMS) x both
    modes x seeds 1-4, plus the rvfi-mode liveness fuzz under seeds 1-5. Writes build/cosim.json."""
    t0 = time.time()
    out_path = ROOT / "build" / "cosim.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def setup_fail(err):
        print(json.dumps({"pass": False, "error": err}))
        out_path.write_text(json.dumps({"pass": False, "contract": "v3", "error": err},
                                       indent=1) + "\n")
        return 1

    if make:
        p = subprocess.run(["make", "-C", str(ROOT / "verif/programs"), f"-j{jobs}", "all"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return setup_fail("program build failed: " + p.stderr[-2000:])
    for mode in MODES:
        try:
            ensure_built(mode, src, v3=True)
        except RuntimeError as e:
            return setup_fail(str(e))
    try:
        progs = program_list()
    except subprocess.CalledProcessError as e:
        return setup_fail(f"make -C verif/programs list failed: {e.stderr[-2000:]}")
    lacking = missing_v3_programs(progs)
    if lacking:
        return setup_fail("make -C verif/programs list lacks the plan's programs: " +
                          ", ".join(lacking))
    missing = [p for p in progs if not (PROGS / f"{p}.hex").exists()]
    runs = [(m, PROGS / f"{p}.hex", s) for p in progs if p not in missing
            for m in MODES for s in SEEDS_V3]
    results = []
    for p in missing:
        r = {k: None for k in KEYS}
        r.update(prog=p, error=f"missing build/programs/{p}.hex", **{"pass": False})
        results.append(r)
    fuzz = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(run_one, m, h, s, src=src, v3=True): "run" for m, h, s in runs}
        futs.update({ex.submit(run_fuzz, "rvfi", s, src=src, v3=True): "fuzz"
                     for s in FUZZ_SEEDS_V3})
        for f in concurrent.futures.as_completed(futs):
            if futs[f] == "fuzz":
                s = f.result()
                fuzz.append(s)
                print(json.dumps({k: s[k] for k in FUZZ_KEYS_V3}), flush=True)
            else:
                summary, _ = f.result()
                results.append(summary)
                print(json.dumps({k: summary[k] for k in KEYS}), flush=True)
    order = {p: i for i, p in enumerate(progs)}
    results.sort(key=lambda r: (order.get(r["prog"], 99), r["mode"] or "", r["seed"] or 0))
    fuzz.sort(key=lambda r: r["seed"])
    ok = (not missing and len(results) == len(runs) and all(r["pass"] for r in results)
          and len(fuzz) == len(FUZZ_SEEDS_V3)
          and all(f["pass"] and f["liveness_checked"] for f in fuzz))
    out = {"pass": ok, "contract": "v3", "runs": results, "fuzz": fuzz, "programs": progs,
           "seeds": list(SEEDS_V3), "fuzz_seeds": list(FUZZ_SEEDS_V3),
           "runtime_s": round(time.time() - t0, 1)}
    out_path.write_text(json.dumps(out, indent=1) + "\n")
    failed = [r for r in results if not r["pass"]] + [f for f in fuzz if not f["pass"]]
    print(json.dumps({"pass": ok, "runs": len(results), "fuzz": len(fuzz), "failed": len(failed),
                      "runtime_s": out["runtime_s"]}))
    for r in failed[:10]:
        print(f"FAIL {r['prog']} {r['mode']} seed {r['seed']}: {r['error']}", file=sys.stderr)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="BonettoR32V pin-level cosim")
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--prog", type=Path)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-cycles", type=int, default=0)
    ap.add_argument("--timing-json", type=Path)
    ap.add_argument("--dump-ram", type=Path)
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--vflags", default="",
                    help="extra Verilator arguments (harness self-tests on mutant copies)")
    ap.add_argument("--trace", type=Path, help="write an FST waveform (separate traced build)")
    ap.add_argument("--all", action="store_true", help="every program x mode x seed {1,2,3}")
    ap.add_argument("--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--no-make", action="store_true", help="--all: don't rebuild the programs")
    ap.add_argument("--fuzz-liveness", action="store_true",
                    help="the liveness fuzz on random images (rvfi mode unless --mode pins)")
    ap.add_argument("--fuzz-runs", type=int, default=FUZZ_RUNS,
                    help=f"--fuzz-liveness: probe sub-runs (default {FUZZ_RUNS}; walks: 1/256)")
    ap.add_argument("--fuzz-cap", type=int, default=FUZZ_CAP,
                    help=f"--fuzz-liveness: retirements per sub-run (default {FUZZ_CAP})")
    ap.add_argument("--v3", action="store_true",
                    help="contract v3 (harness.cpp; becomes the default at the integration swap)")
    ap.add_argument("--clk-ns", type=float, default=CLK_NS,
                    help="--v3: the clock period for the memory timing rules (default 25)")
    a = ap.parse_args()
    if a.fuzz_liveness:
        if a.all or a.prog:
            ap.error("--fuzz-liveness takes no --prog or --all (it makes its own images)")
        if a.seed <= 0 or a.fuzz_runs <= 0 or a.fuzz_cap <= 0:
            ap.error("--seed, --fuzz-runs and --fuzz-cap must be positive")
        summary = run_fuzz(a.mode or "rvfi", a.seed, a.fuzz_runs, a.fuzz_cap, a.src, a.vflags,
                           v3=a.v3)
        print(json.dumps({k: summary[k] for k in (FUZZ_KEYS_V3 if a.v3 else FUZZ_KEYS)}))
        return 0 if summary["pass"] else 1
    if a.all:
        if a.v3:
            return run_all_v3(a.src, a.jobs, make=not a.no_make)
        return run_all(a.src, a.jobs, make=not a.no_make)
    if not a.mode or not a.prog:
        ap.error("--mode and --prog are required (or --all)")
    if a.seed <= 0:
        ap.error("--seed must be positive")
    if a.timing_json and a.mode != "rvfi":
        ap.error("--timing-json needs --mode rvfi (pins mode can't see retirements)")
    summary, _ = run_one(a.mode, a.prog, a.seed, a.max_cycles, a.timing_json, a.dump_ram, a.src,
                         vflags=a.vflags, trace=a.trace, v3=a.v3, clk_ns=a.clk_ns)
    print(json.dumps({k: summary[k] for k in KEYS}))
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
