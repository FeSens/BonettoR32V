#!/usr/bin/env python3
"""BonettoR32V evaluation: run every gate in order, stop at the first failure, write build/eval.json.

Gate order (docs/eval.md): cheapest first, so a bad candidate retires early.
  lint (~1 s), area (~10 s), cosim (minutes), arch (minutes), equiv, gl (minutes), formal (longest).
With --max-area X, a candidate whose area_um2 is not below X stops right after the area gate
("NO_GAIN", exit 3): it cannot win, so its correctness is not worth the formal run.

Each gate is one adapter function below (gate_<name>). An adapter runs the gate's runner and
judges the result from the runner's exit code AND its output json, which it deletes before the run
so a stale file can never pass. A gate whose runner is missing FAILS ("gate X: runner missing").
Changing a runner's interface means changing one adapter.

Usage:
  eval.py                 every gate in order; writes build/eval.json; exit 0 iff all pass
  eval.py --gate NAME     one gate (lint cosim equiv formal formal-deep arch area gl);
                          writes build/gates/NAME.json; exit 0 iff it passes
  eval.py --max-area X    stop after the area gate unless area_um2 < X (the champion's area)
  eval.py -v              also stream runner output to the console (always on with --gate)

Runner output goes to build/logs/<gate>*.log. Formal runs under `lockf -k /tmp/bonetto-heavy.lock`.
"""
import argparse
import datetime
import glob
import json
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
ORDER = ["lint", "area", "cosim", "arch", "equiv", "gl", "formal"]
# One heavy job at a time on the Mac. The path is overridable only so tests don't queue behind a
# real formal run; it changes scheduling, never a verdict.
HEAVY_LOCK = ["lockf", "-k", os.environ.get("BONETTO_HEAVY_LOCK", "/tmp/bonetto-heavy.lock")]
LATENCY_CAP = 1024                       # contract section 4
COSIM_MODES = ("rvfi", "pins")
COSIM_SEEDS = (1, 2, 3)
EXPECT_RETIRED_KEYS = ("retired", "retired_count", "instret", "insns", "instructions")


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


# ---------------------------------------------------------------------------------------------
# Gate adapters. Each returns (detail, data) on PASS and raises GateFail on FAIL.
# ---------------------------------------------------------------------------------------------

def gate_lint(ctx):
    """tools/src_check.py first (static rules, then the verilator/yosys/iverilog
    differential preprocessor check), then verilator --lint-only -Wall over src/*.v (top tt_um_fesens_r32v),
    plain and RISCV_FORMAL builds."""
    problems = src_check.check(ctx.path("src"))
    if not problems:
        problems = src_check.check_differential(ctx.path("src"), ctx.env)
    if problems:
        shown = "; ".join(problems[:3]) + (f" (+{len(problems) - 3} more)"
                                           if len(problems) > 3 else "")
        raise GateFail(f"src hygiene (tools/src_check.py): {shown}")
    ctx.need_tool("lint", "verilator")
    srcs = ctx.src_files("lint")
    # Paths go to Verilator as separate argv entries (no shell). They are absolute (so none can
    # read as an option) and must match SAFE_PATH, as a second line behind src_check.
    for p in srcs:
        if not SAFE_PATH.match(p):
            raise GateFail(f"gate lint: unsafe path {p!r}")
    base = ["verilator", "--lint-only", "-Wall", "--top-module", TOP]
    builds = (("plain", []), ("RISCV_FORMAL", ["-DRISCV_FORMAL"]))
    for label, extra in builds:
        log = "lint_" + label.lower()
        rc, lines = ctx.run(base + extra + srcs, log)
        if rc != 0:
            raise GateFail(f"verilator -Wall ({label} build) failed: {first_problem(lines)} "
                           f"(log {ctx.log_rel(log)})")
    return f"verilator -Wall clean, plain and RISCV_FORMAL builds ({len(srcs)} files)", {}


def expect_retired(path):
    with open(path) as f:
        exp = json.load(f)
    for k in EXPECT_RETIRED_KEYS:
        if isinstance(exp.get(k), int):
            return exp[k]
    raise GateFail(f"gate cosim: no retired-instruction count in {os.path.basename(path)} "
                   f"(looked for {', '.join(EXPECT_RETIRED_KEYS)}; keys: {', '.join(exp)})")


def gate_cosim(ctx):
    """Every program in build/programs, modes rvfi and pins, seeds 1-3 (plan Task 3)."""
    ctx.need("cosim", "verif/cosim/run_cosim.py", "verif/programs/Makefile")
    rc, lines = ctx.run(["make", "-C", "verif/programs"], "cosim_programs")
    if rc != 0:
        raise GateFail(f"gate cosim: program build failed: {first_problem(lines)} "
                       f"(log {ctx.log_rel('cosim_programs')})")
    hexes = sorted(glob.glob(ctx.path("build", "programs", "*.hex")))
    first = {"selftest": 0, "gpio_demo": 1}
    hexes.sort(key=lambda h: (first.get(os.path.basename(h)[:-4], 2), h))
    if not hexes:
        raise GateFail("gate cosim: no build/programs/*.hex after make -C verif/programs")
    runs = []
    for hexpath in hexes:
        name = os.path.basename(hexpath)[:-4]
        expect = hexpath[:-4] + ".expect.json"
        if not os.path.isfile(expect):
            raise GateFail(f"gate cosim: missing {os.path.relpath(expect, ctx.repo)}")
        max_cycles = LATENCY_CAP * (expect_retired(expect) + 1)
        for mode in COSIM_MODES:
            for seed in COSIM_SEEDS:
                log = f"cosim_{name}_{mode}_{seed}"
                cmd = [ctx.py, "verif/cosim/run_cosim.py", "--mode", mode,
                       "--prog", os.path.relpath(hexpath, ctx.repo), "--seed", str(seed),
                       "--max-cycles", str(max_cycles)]
                rc, lines = ctx.run(cmd, log)
                s = last_json_line(lines) or {}
                where = f"{name} --mode {mode} --seed {seed}"
                if rc != 0 or s.get("pass") is not True or s.get("error"):
                    why = s.get("error") or (f"exit {rc}" if s else "no JSON summary line")
                    raise GateFail(f"cosim FAIL on {where}: {why} (log {ctx.log_rel(log)})")
                gap = s.get("max_cycles_per_insn")
                if isinstance(gap, (int, float)) and gap > LATENCY_CAP:
                    raise GateFail(f"cosim FAIL on {where}: max_cycles_per_insn {gap} exceeds the "
                                   f"latency cap {LATENCY_CAP} (contract section 4)")
                runs.append({"prog": name, "mode": mode, "seed": seed,
                             "retired": s.get("retired"), "cycles": s.get("cycles"),
                             "cpi": s.get("cpi"), "max_cycles_per_insn": gap})
    base = [r for r in runs if r["mode"] == "pins" and r["seed"] == COSIM_SEEDS[0]
            and isinstance(r["retired"], int) and isinstance(r["cycles"], int)]
    retired = sum(r["retired"] for r in base)
    cpi = round(sum(r["cycles"] for r in base) / retired, 3) if retired else None
    ctx.notes["cpi"] = cpi
    worst = max((r["max_cycles_per_insn"] or 0) for r in runs)
    return (f"{len(runs)} runs pass ({len(hexes)} programs x {len(COSIM_MODES)} modes x "
            f"{len(COSIM_SEEDS)} seeds), cpi {cpi}, worst retirement gap {worst}",
            {"runs": runs, "cpi": cpi})


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
    """verif/formal/run_formal.py [--deep] under the heavy lock (writes build/formal.json)."""
    ctx.need(gate, "verif/formal/run_formal.py")
    ctx.need_tool(gate, "lockf")
    out = "build/formal.json"
    ctx.clear(out)
    t0 = time.time()
    cmd = HEAVY_LOCK + [ctx.py, "verif/formal/run_formal.py"] + (["--deep"] if deep else [])
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
    """verif/arch-test/run_arch.py (writes build/arch.json)."""
    ctx.need("arch", "verif/arch-test/run_arch.py")
    out = "build/arch.json"
    ctx.clear(out)
    t0 = time.time()
    rc, lines = ctx.run([ctx.py, "verif/arch-test/run_arch.py"], "arch")
    j = ctx.fresh_json("arch", out, t0)
    tests = j.get("tests") or {}
    failed = sorted(k for k, v in tests.items() if v is not True)
    if rc != 0 or j.get("pass") is not True or not tests or failed:
        why = ", ".join(failed[:10]) if failed else ("no tests reported" if not tests
                                                     else f"exit {rc}")
        raise GateFail(f"arch FAIL: {why} (log {ctx.log_rel('arch')})")
    return f"{len(tests)} riscv-arch-test tests pass", {"tests": len(tests)}


def gate_area(ctx):
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
        raise GateFail(f"gate area: {latches!r} latches (contract v1 section 1: no latches)")
    netlist = j.get("netlist") or "build/synth/netlist.v"
    netlist = netlist if os.path.isabs(netlist) else ctx.path(netlist)
    if not os.path.isfile(netlist):
        raise GateFail(f"gate area: netlist {netlist} missing")
    ctx.notes.update({"area_um2": area, "cells": cells, "netlist": netlist, "area_t0": t0})
    return (f"{area} um2, {cells} cells, {j.get('flops')} flops, 0 latches",
            {"area_um2": area, "cells": cells, "flops": j.get("flops")})


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


def gate_gl(ctx):
    """make -C test GATES=yes on the area netlist with the sky130 cell models (plan Task 7)."""
    ctx.need("gl", "test/Makefile")
    if "netlist" not in ctx.notes:          # standalone `make gl`: synthesize first
        try:
            gate_area(ctx)
        except GateFail as e:
            raise GateFail(f"gate gl: needs the area netlist, and area failed: {e}")
    netlist = ctx.notes["netlist"]
    if os.path.getmtime(netlist) < ctx.notes["area_t0"] - 1:
        raise GateFail(f"gate gl: {os.path.relpath(netlist, ctx.repo)} is older than this run's "
                       f"area step (area.py did not rewrite it)")
    shutil.copyfile(netlist, ctx.path("test", "gate_level_netlist.v"))
    env = dict(ctx.env)
    if not env.get("PDK_ROOT") and os.path.isdir(os.path.expanduser("~/.ciel/sky130A")):
        env["PDK_ROOT"] = os.path.expanduser("~/.ciel")
    env.setdefault("PDK", "sky130A")
    results = "test/results.xml"
    ctx.clear(results)
    t0 = time.time()
    rc, lines = ctx.run(["make", "-C", "test", "GATES=yes", f"GL_NETLIST={netlist}"], "gl", env)
    rpath = ctx.path(results)
    if not os.path.isfile(rpath) or os.path.getmtime(rpath) < t0 - 1:
        raise GateFail(f"gl FAIL: no fresh {results} (exit {rc}; log {ctx.log_rel('gl')})")
    total, bad, skipped = junit_counts(rpath)
    passed = total - bad - skipped
    if rc != 0 or bad or passed < 1:
        raise GateFail(f"gl FAIL: {bad} failed, {passed} passed, {skipped} skipped, exit {rc} "
                       f"(log {ctx.log_rel('gl')})")
    return f"gate-level: {passed} cocotb tests pass on {os.path.relpath(netlist, ctx.repo)}", {}


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
    ap.add_argument("--max-area", type=float, metavar="UM2",
                    help="retire the candidate after the area gate unless area_um2 < UM2")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    ctx = Ctx(args.repo, verbose=args.verbose or bool(args.gate))
    sha = git(ctx.repo, "rev-parse", "HEAD") or "unknown"
    # Untracked scratch files inside a submodule don't change what is evaluated; a moved
    # submodule commit or an edited tracked file does.
    status = git(ctx.repo, "status", "--porcelain", "--ignore-submodules=untracked")
    dirty = None if status is None else bool(status)
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
    doc = {"sha": sha, "dirty": dirty, "pass": False, "gates": {}, "area_um2": None,
           "cells": None, "cpi": None, "timestamp": now_iso(), "order": ORDER,
           "stopped_at": None, "max_area": args.max_area, "no_gain": False,
           "tools": tool_versions(ctx.env)}
    interrupted = False
    skip_why = None
    for name in ORDER:
        res, _ = run_gate(ctx, name)
        doc["gates"][name] = res
        if not res["pass"]:
            doc["stopped_at"] = name
            interrupted = res["detail"] == "interrupted"
            skip_why = f"not run (stopped after {name} failed)"
            break
        if name == "area" and args.max_area is not None \
                and not ctx.notes["area_um2"] < args.max_area:
            doc["stopped_at"], doc["no_gain"] = name, True
            skip_why = f"not run (area {ctx.notes['area_um2']} um2 is not below --max-area " \
                       f"{args.max_area})"
            break
    for name in ORDER:
        doc["gates"].setdefault(name, {"pass": None, "detail": skip_why})
    doc["pass"] = all(doc["gates"][n]["pass"] is True for n in ORDER)
    doc["area_um2"] = ctx.notes.get("area_um2")
    doc["cells"] = ctx.notes.get("cells")
    doc["cpi"] = ctx.notes.get("cpi")
    doc["runtime_s"] = round(time.time() - t0, 1)
    write_json(out, doc)
    verdict = ("PASS" if doc["pass"] else "NO_GAIN (not smaller than --max-area)"
               if doc["no_gain"] else f"FAIL at {doc['stopped_at']}")
    print(f"[eval] {verdict}: sha {sha[:12]}{' (dirty)' if dirty else ''}, "
          f"area {doc['area_um2']} um2, cells {doc['cells']}, cpi {doc['cpi']} -> "
          f"{os.path.relpath(out, ctx.repo)}")
    if interrupted:
        return 130
    if doc["no_gain"]:
        return 3
    return 0 if doc["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
