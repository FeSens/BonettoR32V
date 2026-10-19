#!/usr/bin/env python3
"""BonettoR32V evaluation: run every gate in order, stop at the first failure, write build/eval.json.

Gate order (docs/eval.md): cheapest first, so a bad candidate retires early.
  lint (~1 s), area (~10 s), cosim (minutes), arch (minutes), equiv, gl (minutes), and formal (the
  longest) only with --formal (contract section 7: formal runs at final signoff and on every
  tenth merged improvement). eval.json says "formal": "pass", "fail" or "skipped".
The score is coremark_ips = f_clk / CoreMark cycles per iteration (contract section 7): the
cosim gate reads the cycles from the CoreMark runs and f_clk from src/config.json CLOCK_PERIOD.
The area gate includes the `fits` pre-filter for the 1x2 tile: synth area above FIT_AREA_UM2
(23,100 um2) fails (a 1x2 harden confirms). 2026-10-18: hyp 112 a85f6cb, 23,073.4 um2 synth,
hardens clean in 1x2 at utilization 0.803 with the src/config.json of that date (sweep x6).
With --min-ips X, a candidate whose coremark_ips is below X (or null) stops right after the cosim
gate ("NO_GAIN", exit 3): it cannot win, so the rest of its gates are not worth running. A tie
continues through every gate: the contract's tiebreak (smaller synth area) may still make it win.

Each gate is one adapter function below (gate_<name>). An adapter runs the gate's runner and
judges the result from the runner's exit code AND its output json, which it deletes before the run
so a stale file can never pass. A gate whose runner is missing FAILS ("gate X: runner missing").
Changing a runner's interface means changing one adapter.

Usage:
  eval.py                 every gate but formal in order; writes build/eval.json; exit 0 iff all
                          pass
  eval.py --formal        the same, then formal last
  eval.py --gate NAME     one gate (lint cosim equiv formal formal-deep arch area gl);
                          writes build/gates/NAME.json; exit 0 iff it passes
  eval.py --min-ips X     stop after the cosim gate if coremark_ips < X (the champion's) or null
  eval.py -v              also stream runner output to the console (always on with --gate)

Runner output goes to build/logs/<gate>*.log. Formal runs under `lockf -k /tmp/bonetto-heavy.lock`.
The cosim and arch gates run their runner's parallel jobs: BONETTO_EVAL_JOBS (a positive integer),
else the machine's cores minus 2, at most 16. That changes scheduling, never a verdict.
"""
import argparse
import datetime
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import time
import re
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import src_check  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./+-]+$")
TOOLCHAIN_BIN = os.path.expanduser("~/bonetto/riscv-autoarch/.toolchain/oss-cad-suite/bin")
TOP = "tt_um_fesens_r32v"
ORDER = ["lint", "area", "cosim", "arch", "equiv", "gl"]
FORMAL_GATE = "formal"                  # appended to ORDER only with --formal
# The `fits` pre-filter for one 1x2 tile (contract section 7). The seed (LibreLane synth 20,442 um2)
# signs off in 1x2 at utilization 0.728 with src/config.json's routing settings (2026-10-18 sweep);
# nothing larger has been shown to route yet, so the pre-filter allows ~7.5% growth. The harden in a
# champion round has the last word; the lead raises this once a tile-fit search shows more room.
FIT_AREA_UM2 = 23100.0
FIT_TILES = "1x2"
COREMARK_PROG = "coremark"              # the program whose runs carry the score
# One heavy job at a time on the Mac. The path is overridable only so tests don't queue behind a
# real formal run; it changes scheduling, never a verdict.
HEAVY_LOCK = ["lockf", "-k", os.environ.get("BONETTO_HEAVY_LOCK", "/tmp/bonetto-heavy.lock")]
LATENCY_CAP = 2048                      # contract section 4
COSIM_MODES = ("rvfi", "pins")
COSIM_SEEDS = (1, 2, 3, 4)              # the four memory power-up modes (contract 2.4)
FUZZ_SEEDS = (1, 2, 3, 4, 5)            # random-image liveness fuzz (reserved encodings etc.)
FUZZ_MIN_RUNS = 2048 + 8                # run_cosim.py's default: 2,048 probe runs + 8 walks
ARCH_TESTS = 53                         # ruling R34: rv32e_m/E (37) + rv32e_m/privilege (16)


def eval_jobs():
    """Parallel jobs for the cosim and arch runners (scheduling only, never a verdict)."""
    v = os.environ.get("BONETTO_EVAL_JOBS", "")
    if v.isdigit() and int(v) > 0:
        return int(v)
    return max(1, min(16, (os.cpu_count() or 2) - 2))


class GateFail(Exception):
    """Raised by an adapter with a one-line reason."""


def tool_env():
    env = dict(os.environ)
    path = env.get("PATH", "")
    if os.path.isdir(TOOLCHAIN_BIN) and TOOLCHAIN_BIN not in path.split(os.pathsep):
        env["PATH"] = TOOLCHAIN_BIN + os.pathsep + path
    return env


class Ctx:
    def __init__(self, repo, verbose):
        self.repo = os.path.abspath(repo)
        self.build = os.path.join(self.repo, "build")
        self.logs = os.path.join(self.build, "logs")
        self.env = tool_env()
        self.verbose = verbose
        self.py = sys.executable
        self.notes = {}        # values gates hand to eval.json and to later gates
        os.makedirs(self.logs, exist_ok=True)

    def path(self, *parts):
        return os.path.join(self.repo, *parts)

    def need(self, gate, *relpaths):
        for rel in relpaths:
            if not os.path.exists(self.path(rel)):
                raise GateFail(f"gate {gate}: runner missing ({rel})")

    def need_tool(self, gate, tool):
        if shutil.which(tool, path=self.env["PATH"]) is None:
            raise GateFail(f"gate {gate}: tool missing ({tool} not on PATH)")

    def src_files(self, gate):
        srcs = sorted(glob.glob(self.path("src", "*.v")))
        if not srcs:
            raise GateFail(f"gate {gate}: no src/*.v files")
        return srcs

    def run(self, cmd, log_name, env=None):
        """Run cmd in the repo; tee output to build/logs/<log_name>.log. Return (rc, lines)."""
        log_path = os.path.join(self.logs, log_name + ".log")
        lines = []
        with open(log_path, "w") as log:
            log.write("$ " + " ".join(cmd) + "\n")
            log.flush()
            try:
                p = subprocess.Popen(cmd, cwd=self.repo, env=env or self.env, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     errors="replace")
            except OSError as e:
                log.write(f"cannot start: {e}\n")
                return 127, [f"cannot start {cmd[0]}: {e}"]
            for line in p.stdout:
                log.write(line)
                lines.append(line.rstrip("\n"))
                if self.verbose:
                    sys.stdout.write(line)
                    sys.stdout.flush()
            rc = p.wait()
            log.write(f"[exit {rc}]\n")
        return rc, lines

    def log_rel(self, log_name):
        return os.path.relpath(os.path.join(self.logs, log_name + ".log"), self.repo)

    def clear(self, rel):
        p = self.path(rel)
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.exists(p):
            os.remove(p)

    def fresh_json(self, gate, rel, t0):
        p = self.path(rel)
        if not os.path.isfile(p):
            raise GateFail(f"gate {gate}: runner wrote no {rel}")
        if os.path.getmtime(p) < t0 - 1:
            raise GateFail(f"gate {gate}: {rel} is stale (older than this run)")
        try:
            with open(p) as f:
                return json.load(f)
        except ValueError as e:
            raise GateFail(f"gate {gate}: {rel} is not valid json ({e})")


def first_problem(lines, prefixes=("%Error", "%Warning", "ERROR", "Error")):
    for line in lines:
        if line.lstrip().startswith(prefixes):
            return line.strip()[:240]
    return lines[-1].strip()[:240] if lines else "(no output)"


def last_json_line(lines):
    for line in reversed(lines):
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                return json.loads(s)
            except ValueError:
                continue
    return None


def positive_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def coremark_ips(cycles_per_iter, clock_period_ns):
    """CoreMark iterations per second: f_clk / cycles per iteration (contract section 7)."""
    return 1e9 / clock_period_ns / cycles_per_iter


def clock_period_ns(ctx, gate):
    """CLOCK_PERIOD (ns) from src/config.json, the signoff clock (contract section 7)."""
    rel = "src/config.json"
    try:
        with open(ctx.path(rel)) as f:
            period = json.load(f).get("CLOCK_PERIOD")
    except (OSError, ValueError, AttributeError) as e:
        raise GateFail(f"gate {gate}: cannot score CoreMark: {rel} unreadable ({e})")
    if not positive_number(period):
        raise GateFail(f"gate {gate}: cannot score CoreMark: bad CLOCK_PERIOD {period!r} in {rel}")
    return period


# ---------------------------------------------------------------------------------------------
# Gate adapters. Each returns (detail, data) on PASS and raises GateFail on FAIL.
# ---------------------------------------------------------------------------------------------

def cell_models(gate):
    """The behavioral models of the whitelisted sky130 cells (verif/cells, contract section 1),
    read after src/*.v by every gate that compiles RTL. A missing model fails the gate."""
    models = src_check.cell_model_files()
    for p in models:
        if not os.path.isfile(p):
            raise GateFail(f"gate {gate}: cell model missing ({os.path.relpath(p, REPO)})")
    return models


def gate_lint(ctx):
    """tools/src_check.py first (static rules, then the verilator/yosys/iverilog
    differential preprocessor check), then verilator --lint-only -Wall over src/*.v and the cell
    models (top tt_um_fesens_r32v), plain and RISCV_FORMAL builds."""
    problems = src_check.check(ctx.path("src"))
    if not problems:
        problems = src_check.check_differential(ctx.path("src"), ctx.env)
    if problems:
        shown = "; ".join(problems[:3]) + (f" (+{len(problems) - 3} more)"
                                           if len(problems) > 3 else "")
        raise GateFail(f"src hygiene (tools/src_check.py): {shown}")
    ctx.need_tool("lint", "verilator")
    srcs = ctx.src_files("lint")
    models = cell_models("lint")
    # Paths go to Verilator as separate argv entries (no shell). They are absolute (so none can
    # read as an option) and must match SAFE_PATH, as a second line behind src_check.
    for p in srcs + models:
        if not SAFE_PATH.match(p):
            raise GateFail(f"gate lint: unsafe path {p!r}")
    base = ["verilator", "--lint-only", "-Wall", "--top-module", TOP]
    builds = (("plain", []), ("RISCV_FORMAL", ["-DRISCV_FORMAL"]))
    for label, extra in builds:
        log = "lint_" + label.lower()
        rc, lines = ctx.run(base + extra + srcs + models, log)
        if rc != 0:
            raise GateFail(f"verilator -Wall ({label} build) failed: {first_problem(lines)} "
                           f"(log {ctx.log_rel(log)})")
    return (f"verilator -Wall clean, plain and RISCV_FORMAL builds ({len(srcs)} files, "
            f"{len(models)} cell models)"), {}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def gate_cosim(ctx):
    """make -C verif/programs, then one `run_cosim.py --all --no-make --jobs N` (contract v3;
    writes build/cosim.json): every program `make -C verif/programs list` reports, modes rvfi and
    pins, seeds 1-4 (the four memory power-up modes), plus the rvfi-mode liveness fuzz under seeds
    1-5. The adapter re-checks the json itself: the program list, every (program, mode, seed) run
    exactly once and passing, every fuzz seed, the 2,048-cycle cap and the CoreMark fields."""
    ctx.need("cosim", "verif/cosim/run_cosim.py", "verif/programs/Makefile")
    rc, lines = ctx.run(["make", "-C", "verif/programs"], "cosim_programs")
    if rc != 0:
        raise GateFail(f"gate cosim: program build failed: {first_problem(lines)} "
                       f"(log {ctx.log_rel('cosim_programs')})")
    rc, lines = ctx.run(["make", "-s", "-C", "verif/programs", "list"], "cosim_list")
    progs = " ".join(ln for ln in lines if not ln.startswith(("$ ", "make"))).split()
    if rc != 0 or not progs:
        raise GateFail(f"gate cosim: `make -C verif/programs list` failed or listed nothing "
                       f"(log {ctx.log_rel('cosim_list')})")
    # The Pmod models check time-based limits (tCEM, SCK rate) at the signoff clock.
    clk = clock_period_ns(ctx, "cosim")
    out = "build/cosim.json"
    ctx.clear(out)
    t0 = time.time()
    rc, lines = ctx.run([ctx.py, "verif/cosim/run_cosim.py", "--all", "--no-make", "--jobs",
                         str(eval_jobs()), "--clk-ns", f"{clk:g}"], "cosim")
    log = ctx.log_rel("cosim")
    j = ctx.fresh_json("cosim", out, t0)
    if j.get("error"):
        raise GateFail(f"cosim FAIL: {str(j['error'])[:240]} (log {log})")
    if j.get("contract") != "v3":
        raise GateFail(f"cosim FAIL: {out} has contract {j.get('contract')!r}, not 'v3' (log {log})")
    if not _num(j.get("clk_ns")) or j["clk_ns"] != clk:
        raise GateFail(f"cosim FAIL: the runner used clk_ns {j.get('clk_ns')!r}, not the signoff "
                       f"CLOCK_PERIOD {clk:g} ns (log {log})")
    if j.get("programs") != progs:
        raise GateFail(f"cosim FAIL: the runner's programs {j.get('programs')!r} differ from "
                       f"`make -C verif/programs list` {progs!r} (log {log})")
    if j.get("seeds") != list(COSIM_SEEDS) or j.get("fuzz_seeds") != list(FUZZ_SEEDS):
        raise GateFail(f"cosim FAIL: seeds {j.get('seeds')!r} and fuzz seeds "
                       f"{j.get('fuzz_seeds')!r}, need {list(COSIM_SEEDS)} and {list(FUZZ_SEEDS)} "
                       f"(log {log})")
    runs = j.get("runs") if isinstance(j.get("runs"), list) else []
    bad = [r for r in runs if r.get("pass") is not True or r.get("error")]
    if bad:
        r = bad[0]
        raise GateFail(f"cosim FAIL on {r.get('prog')} --mode {r.get('mode')} --seed "
                       f"{r.get('seed')}: {r.get('error') or 'pass is not true'} "
                       f"({len(bad)} failing run(s); log {log})")
    seen = [(r.get("prog"), r.get("mode"), r.get("seed")) for r in runs]
    want = [(p, m, s) for p in progs for m in COSIM_MODES for s in COSIM_SEEDS]
    if sorted(seen, key=repr) != sorted(want, key=repr):
        lack = [k for k in want if k not in seen]
        extra = sorted({k for k in seen if k not in want or seen.count(k) > 1}, key=repr)
        raise GateFail(f"cosim FAIL: the runs are not every program x mode x seed exactly once "
                       f"(missing {lack[:3]}, extra or repeated {extra[:3]}; log {log})")
    for r in runs:
        where = f"{r['prog']} --mode {r['mode']} --seed {r['seed']}"
        gap = r.get("max_cycles_per_insn")
        if _num(gap) and gap > LATENCY_CAP:
            raise GateFail(f"cosim FAIL on {where}: max_cycles_per_insn {gap} exceeds the "
                           f"latency cap {LATENCY_CAP} (contract section 4)")
        cm = r.get("coremark_cycles_per_iter")
        if cm is not None and not positive_number(cm):
            raise GateFail(f"cosim FAIL on {where}: bad coremark_cycles_per_iter {cm!r} "
                           f"(log {log})")
    fuzz = j.get("fuzz") if isinstance(j.get("fuzz"), list) else []
    if sorted((f.get("seed") for f in fuzz), key=repr) != list(FUZZ_SEEDS):
        raise GateFail(f"cosim FAIL: liveness fuzz seeds "
                       f"{sorted((f.get('seed') for f in fuzz), key=repr)}, need "
                       f"{list(FUZZ_SEEDS)} once each (log {log})")
    for f in sorted(fuzz, key=lambda f: f["seed"]):
        runs_done, gap = f.get("runs"), f.get("max_cycles_per_insn")
        why = None
        if f.get("pass") is not True or f.get("error"):
            why = f.get("error") or "pass is not true"
        elif f.get("liveness_checked") is not True:
            why = "liveness was not checked"
        elif not (isinstance(runs_done, int) and runs_done >= FUZZ_MIN_RUNS):
            why = f"only {runs_done!r} fuzz runs (need >= {FUZZ_MIN_RUNS})"
        elif _num(gap) and gap > LATENCY_CAP:
            why = f"max_cycles_per_insn {gap} exceeds the latency cap {LATENCY_CAP}"
        if why:
            raise GateFail(f"cosim FAIL on liveness fuzz --seed {f['seed']}: {why} (log {log})")
    if rc != 0 or j.get("pass") is not True:
        raise GateFail(f"cosim FAIL: run_cosim.py --all exit {rc}, pass {j.get('pass')!r} "
                       f"(log {log})")
    runs = [{k: r.get(k) for k in ("prog", "mode", "seed", "retired", "cycles", "cpi",
                                   "max_cycles_per_insn", "coremark_cycles_per_iter")}
            for r in runs]
    # cpi: rvfi mode sees every retirement (pins mode can't), seed 1, every program.
    base = [r for r in runs if r["mode"] == "rvfi" and r["seed"] == COSIM_SEEDS[0]
            and isinstance(r["retired"], int) and isinstance(r["cycles"], int)]
    retired = sum(r["retired"] for r in base)
    cpi = round(sum(r["cycles"] for r in base) / retired, 3) if retired else None
    ctx.notes["cpi"] = cpi
    # The score: the slowest CoreMark run that reports cycles per iteration (every mode and
    # seed; they should agree). None when no run reports it (R17, R21); --min-ips then counts the
    # candidate as no gain.
    marks = [r["coremark_cycles_per_iter"] for r in runs
             if r["prog"] == COREMARK_PROG and r["coremark_cycles_per_iter"] is not None]
    cm_cycles = max(marks) if marks else None
    period = clock_period_ns(ctx, "cosim") if cm_cycles is not None else None
    ips = coremark_ips(cm_cycles, period) if cm_cycles is not None else None
    ctx.notes.update({"coremark_ips": ips, "coremark_cycles_per_iter": cm_cycles,
                      "clock_period_ns": period})
    worst = max(((r["max_cycles_per_insn"] or 0) for r in runs), default=0)
    fuzz_worst = max(((f.get("max_cycles_per_insn") or 0) for f in fuzz), default=0)
    return (f"{len(runs)} runs pass ({len(progs)} programs x {len(COSIM_MODES)} modes x "
            f"{len(COSIM_SEEDS)} seeds) + liveness fuzz x {len(FUZZ_SEEDS)}, cpi {cpi}, "
            f"worst retirement gap {worst} (fuzz {fuzz_worst}), coremark_ips {ips} "
            f"({cm_cycles} cycles/iteration at {period} ns)",
            {"runs": runs, "fuzz": [{k: f.get(k) for k in ("seed", "runs", "max_cycles_per_insn")}
                                    for f in sorted(fuzz, key=lambda f: f["seed"])],
             "cpi": cpi, "coremark_ips": ips, "coremark_cycles_per_iter": cm_cycles,
             "clock_period_ns": period})


def gate_equiv(ctx):
    """tools/equiv.py: RVFI-stripped RISCV_FORMAL build == plain build (writes build/equiv.json)."""
    ctx.need("equiv", "tools/equiv.py")
    out = "build/equiv.json"
    ctx.clear(out)
    t0 = time.time()
    rc, lines = ctx.run([ctx.py, "tools/equiv.py", "--src", "src", "--top", TOP,
                         "--out", out, "--work", "build/equiv"], "equiv")
    j = ctx.fresh_json("equiv", out, t0)
    if rc != 0 or j.get("pass") is not True:
        raise GateFail(f"{j.get('detail', 'equiv failed')} (log {ctx.log_rel('equiv')})")
    return j.get("detail", ""), {k: j.get(k) for k in ("equiv_cells", "proved_by_simple",
                                                        "proved_by_induct", "rvfi_ports")}


def _formal(ctx, gate, deep):
    """verif/formal/run_formal.py [--deep] (writes build/formal.json). Local runs take the Mac
    heavy lock; with BONETTO_FORMAL_HOST set the solvers run on that host under its own lock, so
    the Mac lock is not taken (the runner only stages, times and rsyncs locally)."""
    ctx.need(gate, "verif/formal/run_formal.py")
    remote = bool(os.environ.get("BONETTO_FORMAL_HOST"))
    if not remote:
        ctx.need_tool(gate, "lockf")
    out = "build/formal.json"
    ctx.clear(out)
    t0 = time.time()
    cmd = ([] if remote else HEAVY_LOCK) + [ctx.py, "verif/formal/run_formal.py"] \
        + (["--deep"] if deep else [])
    rc, lines = ctx.run(cmd, gate)
    j = ctx.fresh_json(gate, out, t0)
    checks = j.get("checks") or {}
    bad = sorted(f"{k}={v}" for k, v in checks.items() if v != "PASS")
    if rc != 0 or j.get("pass") is not True or not checks or bad:
        why = ", ".join(bad[:10]) if bad else ("no checks reported" if not checks else f"exit {rc}")
        raise GateFail(f"formal FAIL: {why} (log {ctx.log_rel(gate)})")
    return (f"{len(checks)} checks PASS, depth {j.get('depth')}, {j.get('runtime_s')} s",
            {"depth": j.get("depth"), "checks": len(checks), "runtime_s": j.get("runtime_s")})


def gate_formal(ctx):
    return _formal(ctx, "formal", deep=False)


def gate_formal_deep(ctx):
    return _formal(ctx, "formal-deep", deep=True)


def gate_arch(ctx):
    """verif/arch-test/run_arch.py --jobs N, the one-host flow (--phase all: GCC, spike and
    Verilator; writes build/arch.json): all 53 tests (ruling R34) under seeds 1-4."""
    ctx.need("arch", "verif/arch-test/run_arch.py")
    out = "build/arch.json"
    ctx.clear(out)
    t0 = time.time()
    rc, lines = ctx.run([ctx.py, "verif/arch-test/run_arch.py", "--jobs", str(eval_jobs())],
                        "arch")
    j = ctx.fresh_json("arch", out, t0)
    tests = j.get("tests") or {}
    failed = sorted(k for k, v in tests.items() if v is not True)
    if rc != 0 or j.get("pass") is not True or not tests or failed:
        why = ", ".join(failed[:10]) if failed else ("no tests reported" if not tests
                                                     else f"exit {rc}")
        raise GateFail(f"arch FAIL: {why} (log {ctx.log_rel('arch')})")
    if len(tests) != ARCH_TESTS:
        raise GateFail(f"arch FAIL: {len(tests)} tests ran, ruling R34 needs all {ARCH_TESTS} "
                       f"(log {ctx.log_rel('arch')})")
    return f"{len(tests)} riscv-arch-test tests pass", {"tests": len(tests)}


def synth_area(ctx):
    """tools/area.py --top tt_um_fesens_r32v --src src/ (writes build/area.json)."""
    ctx.need("area", "tools/area.py")
    out = "build/area.json"
    ctx.clear(out)
    t0 = time.time()
    rc, lines = ctx.run([ctx.py, "tools/area.py", "--top", TOP, "--src", "src/"], "area")
    j = ctx.fresh_json("area", out, t0)
    if rc != 0:
        raise GateFail(f"area.py exited {rc}: {first_problem(lines)} (log {ctx.log_rel('area')})")
    area, cells = j.get("area_um2"), j.get("cells")
    if not isinstance(area, (int, float)) or area <= 0:
        raise GateFail(f"gate area: bad area_um2 {area!r} in {out}")
    if not isinstance(cells, int) or cells <= 0:
        raise GateFail(f"gate area: bad cells {cells!r} in {out}")
    if j.get("engine") != "librelane":
        raise GateFail(f"gate area: engine {j.get('engine')!r}, the score needs the librelane engine")
    latches = j.get("latches")
    if latches != 0:
        raise GateFail(f"gate area: {latches!r} latches (contract section 1: no latches)")
    netlist = j.get("netlist") or "build/synth/netlist.v"
    netlist = netlist if os.path.isabs(netlist) else ctx.path(netlist)
    if not os.path.isfile(netlist):
        raise GateFail(f"gate area: netlist {netlist} missing")
    ctx.notes.update({"area_um2": area, "cells": cells, "netlist": netlist, "area_t0": t0})
    return (f"{area} um2, {cells} cells, {j.get('flops')} flops, 0 latches",
            {"area_um2": area, "cells": cells, "flops": j.get("flops")})


def gate_area(ctx):
    """synth_area, then the `fits` pre-filter: synth area <= FIT_AREA_UM2 (one 1x2 tile,
    provisional; the harden in a champion round confirms)."""
    detail, data = synth_area(ctx)
    area = data["area_um2"]
    if area > FIT_AREA_UM2:
        raise GateFail(f"fits FAIL: synth area {area} um2 exceeds {FIT_AREA_UM2:,.0f} um2, the "
                       f"{FIT_TILES} pre-filter ({data['cells']} cells)")
    return f"{detail}, fits {FIT_TILES} (<= {FIT_AREA_UM2:,.0f} um2)", {**data, "fits": True}


def junit_counts(path):
    root = ET.parse(path).getroot()
    total = bad = skipped = 0
    for tc in root.iter("testcase"):
        total += 1
        if tc.find("failure") is not None or tc.find("error") is not None:
            bad += 1
        elif tc.find("skipped") is not None:
            skipped += 1
    return total, bad, skipped


def _tt_test(ctx, args, log, env, what):
    """One run of TT's cocotb test (test/): `make -C test` plus args; returns the pass count."""
    results = "test/results.xml"
    ctx.clear(results)
    t0 = time.time()
    rc, lines = ctx.run(["make", "-C", "test"] + args, log, env)
    rpath = ctx.path(results)
    if not os.path.isfile(rpath) or os.path.getmtime(rpath) < t0 - 1:
        raise GateFail(f"gl FAIL ({what}): no fresh {results} (exit {rc}; log {ctx.log_rel(log)})")
    total, bad, skipped = junit_counts(rpath)
    passed = total - bad - skipped
    if rc != 0 or bad or passed < 1:
        raise GateFail(f"gl FAIL ({what}): {bad} failed, {passed} passed, {skipped} skipped, "
                       f"exit {rc} (log {ctx.log_rel(log)})")
    return passed


def gate_gl(ctx):
    """TT's test (test/, contract v3: the QSPI Pmod model, gpio_demo in the four power-up modes,
    uart_hello) twice, as TT's CI runs it: on the RTL with Icarus (TT's test workflow; Icarus is
    stricter than Verilator, e.g. it refuses a net used before its declaration), then
    GATES=yes on the area netlist with the sky130 cell models."""
    ctx.need("gl", "test/Makefile")
    if "netlist" not in ctx.notes:          # standalone `make gl`: synthesize (no fits check)
        try:
            synth_area(ctx)
        except GateFail as e:
            raise GateFail(f"gate gl: needs the area netlist, and area failed: {e}")
    netlist = ctx.notes["netlist"]
    if os.path.getmtime(netlist) < ctx.notes["area_t0"] - 1:
        raise GateFail(f"gate gl: {os.path.relpath(netlist, ctx.repo)} is older than this run's "
                       f"area step (area.py did not rewrite it)")
    rtl_passed = _tt_test(ctx, [], "gl_rtl", dict(ctx.env), "RTL, Icarus")
    shutil.copyfile(netlist, ctx.path("test", "gate_level_netlist.v"))
    env = dict(ctx.env)
    if not env.get("PDK_ROOT") and os.path.isdir(os.path.expanduser("~/.ciel/sky130A")):
        env["PDK_ROOT"] = os.path.expanduser("~/.ciel")
    env.setdefault("PDK", "sky130A")
    passed = _tt_test(ctx, ["GATES=yes", f"GL_NETLIST={netlist}"], "gl", env, "gate level")
    return (f"TT test: {rtl_passed} cocotb tests pass on the RTL (Icarus), {passed} on "
            f"{os.path.relpath(netlist, ctx.repo)}"), {}


GATES = {
    "lint": gate_lint,
    "cosim": gate_cosim,
    "equiv": gate_equiv,
    "formal": gate_formal,
    "formal-deep": gate_formal_deep,
    "arch": gate_arch,
    "area": gate_area,
    "gl": gate_gl,
}


# ---------------------------------------------------------------------------------------------

def git(repo, *args):
    try:
        p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    except OSError:
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def tool_versions(env):
    out = {}
    for name, cmd in (("yosys", ["yosys", "-V"]), ("verilator", ["verilator", "--version"]),
                      ("iverilog", ["iverilog", "-V"])):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
            out[name] = (p.stdout or p.stderr).splitlines()[0].strip()
        except (OSError, IndexError, subprocess.TimeoutExpired):
            out[name] = None
    return out


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def run_gate(ctx, name):
    """Run one adapter. Returns {"pass", "detail", "runtime_s"} plus the adapter's data."""
    t = time.time()
    print(f"[eval] {name} ...", flush=True)
    data = {}
    try:
        detail, data = GATES[name](ctx)
        ok = True
    except GateFail as e:
        ok, detail = False, str(e)
    except KeyboardInterrupt:
        ok, detail = False, "interrupted"
    except Exception as e:  # an adapter bug must fail the gate, never pass it
        ok, detail = False, f"gate {name}: adapter error: {type(e).__name__}: {e}"
    res = {"pass": ok, "detail": detail, "runtime_s": round(time.time() - t, 1)}
    print(f"[eval] {name}: {'PASS' if ok else 'FAIL'} ({res['runtime_s']} s): {detail}",
          flush=True)
    return res, data


def main(argv=None):
    ap = argparse.ArgumentParser(description="BonettoR32V evaluation (see docs/eval.md)")
    ap.add_argument("--gate", choices=sorted(GATES), help="run one gate only")
    ap.add_argument("--out", help="eval json path (default build/eval.json)")
    ap.add_argument("--repo", default=REPO, help=argparse.SUPPRESS)
    ap.add_argument("--formal", action="store_true",
                    help="also run the formal gate, last (contract section 7 cadence)")
    ap.add_argument("--min-ips", type=float, metavar="IPS",
                    help="retire the candidate after the cosim gate if coremark_ips < IPS "
                         "(a tie continues: the area tiebreak may win)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    ctx = Ctx(args.repo, verbose=args.verbose or bool(args.gate))
    sha = git(ctx.repo, "rev-parse", "HEAD")
    # Untracked scratch files inside a submodule don't change what is evaluated; a moved
    # submodule commit or an edited tracked file does.
    status = git(ctx.repo, "status", "--porcelain", "--ignore-submodules=untracked")
    dirty = None if status is None else bool(status)
    if sha is None:
        # A tools/hwe_run.sh mirror has no .git: that script passes the checkout's own sha and
        # dirty state, computed where the checkout lives.
        sha = os.environ.get("BONETTO_GIT_SHA") or "unknown"
        env_dirty = os.environ.get("BONETTO_GIT_DIRTY")
        dirty = None if env_dirty not in ("0", "1") else env_dirty == "1"
    t0 = time.time()

    if args.gate:
        res, data = run_gate(ctx, args.gate)
        doc = {"gate": args.gate, "sha": sha, "dirty": dirty, **res, "data": data,
               "timestamp": now_iso()}
        write_json(os.path.join(ctx.build, "gates", args.gate + ".json"), doc)
        return 0 if res["pass"] else 1

    out = os.path.abspath(args.out) if args.out else os.path.join(ctx.build, "eval.json")
    if os.path.exists(out):
        os.remove(out)          # a crashed run must not leave a stale passing file behind
    order = ORDER + ([FORMAL_GATE] if args.formal else [])
    doc = {"sha": sha, "dirty": dirty, "pass": False, "gates": {}, "area_um2": None,
           "cells": None, "coremark_ips": None, "cpi": None, "formal": "skipped",
           "coremark_cycles_per_iter": None, "clock_period_ns": None, "timestamp": now_iso(),
           "order": order, "stopped_at": None, "min_ips": args.min_ips, "no_gain": False,
           "tools": tool_versions(ctx.env)}
    interrupted = False
    skip_why = None
    for name in order:
        res, _ = run_gate(ctx, name)
        doc["gates"][name] = res
        if not res["pass"]:
            doc["stopped_at"] = name
            interrupted = res["detail"] == "interrupted"
            skip_why = f"not run (stopped after {name} failed)"
            break
        if name == "cosim" and args.min_ips is not None:
            ips = ctx.notes.get("coremark_ips")
            if ips is None or ips < args.min_ips:
                doc["stopped_at"], doc["no_gain"] = name, True
                skip_why = f"not run (coremark_ips {ips} is below --min-ips {args.min_ips})"
                break
    for name in order:
        doc["gates"].setdefault(name, {"pass": None, "detail": skip_why})
    doc["pass"] = all(doc["gates"][n]["pass"] is True for n in order)
    if args.formal and doc["gates"][FORMAL_GATE]["pass"] is not None:
        doc["formal"] = "pass" if doc["gates"][FORMAL_GATE]["pass"] else "fail"
    for k in ("area_um2", "cells", "coremark_ips", "cpi", "coremark_cycles_per_iter",
              "clock_period_ns"):
        doc[k] = ctx.notes.get(k)
    doc["runtime_s"] = round(time.time() - t0, 1)
    write_json(out, doc)
    verdict = ("PASS" if doc["pass"] else "NO_GAIN (slower than --min-ips)"
               if doc["no_gain"] else f"FAIL at {doc['stopped_at']}")
    print(f"[eval] {verdict}: sha {sha[:12]}{' (dirty)' if dirty else ''}, "
          f"coremark_ips {doc['coremark_ips']}, area {doc['area_um2']} um2, "
          f"cells {doc['cells']}, cpi {doc['cpi']}, formal {doc['formal']} -> "
          f"{os.path.relpath(out, ctx.repo)}")
    if interrupted:
        return 130
    if doc["no_gain"]:
        return 3
    return 0 if doc["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
