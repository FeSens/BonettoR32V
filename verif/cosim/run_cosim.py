#!/usr/bin/env python3
"""Pin-level cosim driver. See docs/cosim.md.

One run:
  run_cosim.py --mode {rvfi,pins} --prog build/programs/<name>.hex [--seed S] [--max-cycles C]
               [--timing-json PATH] [--dump-ram PATH] [--src DIR] [--trace PATH]
prints one JSON line
  {"prog","mode","seed","pass","retired","cycles","cpi","max_cycles_per_insn","error"}
and exits 0 on a pass.

Everything (the `make cosim` gate):
  run_cosim.py --all [--src DIR] [--jobs N]
builds the programs, runs every program in both modes under seeds 1, 2 and 3, writes
build/cosim.json and exits 0 only if every run passes.

--max-cycles defaults to the latency cap's bound, 1,024 * (reference retirements + 16).
--timing-json (rvfi mode) writes {"first_retire_cycle", "max_cycles_per_insn", "per_class"}.
--dump-ram writes the final 64 KB RAM image (raw bytes) as the design left it.
Programs whose <name>.expect.json lists "log_contains" must leave those strings in the RAM log
ring at 0xF000-0xF7FF (NUL-terminated), e.g. coremark's "Correct operation validated".
"""
import argparse
import concurrent.futures
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
KEYS = ("prog", "mode", "seed", "pass", "retired", "cycles", "cpi", "max_cycles_per_insn", "error")

_build_lock = threading.Lock()


def src_files(src: Path):
    return sorted(p for p in list(src.glob("*.v")) + list(src.glob("*.sv"))
                  if not p.name.startswith("test_"))


def build_key(mode: str, src: Path, vflags: str, trace: bool) -> str:
    h = hashlib.sha256(f"{mode}|{vflags}|{trace}".encode())
    deps = [COSIM / n for n in ("harness.cpp", "spiram.h", "iss.h", "build.sh")]
    deps += [ROOT / "third_party/riscv-formal/checks/rvfi_macros.vh"]
    for p in src_files(src) + deps:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def build_dir_for(mode: str, src: Path, vflags: str, trace: bool) -> Path:
    name = mode
    if src.resolve() != SRC.resolve() or vflags:
        tag = hashlib.sha256(f"{src.resolve()}|{vflags}".encode()).hexdigest()[:12]
        name = f"{mode}-{tag}"
    if trace:
        name += "-trace"
    return ROOT / "build" / "cosim" / name


def ensure_built(mode: str, src: Path = SRC, vflags: str = "", trace: bool = False) -> Path:
    """Build the harness for (mode, src) unless an up-to-date build exists. Returns its path."""
    out = build_dir_for(mode, src, vflags, trace)
    exe, stamp = out / "harness", out / "stamp"
    key = build_key(mode, src, vflags, trace)
    with _build_lock:
        if exe.exists() and stamp.exists() and stamp.read_text() == key:
            return exe
        env = dict(os.environ, VFLAGS=vflags, TRACE="1" if trace else "0")
        p = subprocess.run(["bash", str(COSIM / "build.sh"), mode, str(src), str(out)],
                           env=env, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"cosim build failed ({mode}, {src}):\n{p.stderr[-3000:]}")
        stamp.write_text(key)
    return exe


def log_ring_text(ram: bytes) -> str:
    raw = ram[LOG_RING:LOG_RING_END]
    end = raw.find(b"\0")
    return raw[:end if end >= 0 else len(raw)].decode("latin-1")


def expect_for(hexfile: Path):
    exp = hexfile.with_name(hexfile.name[:-len(".hex")] + ".expect.json")
    return json.loads(exp.read_text()) if exp.exists() else {}


def run_one(mode: str, hexfile: Path, seed: int = 1, max_cycles: int = 0, timing_json=None,
            dump_ram=None, src: Path = SRC, vflags: str = "", trace=None):
    """Run one cosim. Returns (summary dict with KEYS, full harness result dict)."""
    hexfile = Path(hexfile)
    prog = hexfile.name[:-len(".hex")] if hexfile.name.endswith(".hex") else hexfile.stem
    summary = {"prog": prog, "mode": mode, "seed": seed, "pass": False, "retired": None,
               "cycles": None, "cpi": None, "max_cycles_per_insn": None, "error": None}
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
    if timing_json:
        timing = {"first_retire_cycle": res.get("first_retire_cycle"),
                  "max_cycles_per_insn": res.get("max_cycles_per_insn"),
                  "per_class": res.get("per_class")}
        Path(timing_json).write_text(json.dumps(timing, indent=1) + "\n")
    return summary, res


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


def main() -> int:
    ap = argparse.ArgumentParser(description="BonettoR32V pin-level cosim")
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--prog", type=Path)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-cycles", type=int, default=0)
    ap.add_argument("--timing-json", type=Path)
    ap.add_argument("--dump-ram", type=Path)
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--trace", type=Path, help="write an FST waveform (separate traced build)")
    ap.add_argument("--all", action="store_true", help="every program x mode x seed {1,2,3}")
    ap.add_argument("--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--no-make", action="store_true", help="--all: don't rebuild the programs")
    a = ap.parse_args()
    if a.all:
        return run_all(a.src, a.jobs, make=not a.no_make)
    if not a.mode or not a.prog:
        ap.error("--mode and --prog are required (or --all)")
    if a.seed <= 0:
        ap.error("--seed must be positive")
    if a.timing_json and a.mode != "rvfi":
        ap.error("--timing-json needs --mode rvfi (pins mode can't see retirements)")
    summary, _ = run_one(a.mode, a.prog, a.seed, a.max_cycles, a.timing_json, a.dump_ram, a.src,
                         trace=a.trace)
    print(json.dumps({k: summary[k] for k in KEYS}))
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
