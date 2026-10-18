#!/usr/bin/env python3
"""Run a subset of the test programs through the cosim harness (both modes, seeds 1-4), the way
`run_cosim.py --all` runs them, for quick checks while developing programs.

    python3 verif/programs/run_subset.py [--jobs N] [--clk-ns 20] PROG...

Prints one JSON line per run and a summary; exit 0 only if every run passes. Verilator: run it on
hwe-bench (tools/hwe_run.sh), never on the Mac. Not a gate: `make cosim` is.
"""
import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "verif" / "cosim"))
import run_cosim  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("progs", nargs="+")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--clk-ns", type=float, default=20.0)
    ap.add_argument("--seeds", default="1,2,3,4")
    a = ap.parse_args()
    subprocess.run(["make", "-s", "-C", str(ROOT / "verif" / "programs"), *a.progs], check=True)
    for m in run_cosim.MODES:
        run_cosim.ensure_built(m, run_cosim.SRC)
    jobs = [(p, m, int(s)) for p in a.progs for m in run_cosim.MODES for s in a.seeds.split(",")]

    def one(j):
        p, m, s = j
        summ, res = run_cosim.run_one(m, run_cosim.PROGS / f"{p}.hex", s, clk_ns=a.clk_ns)
        summ["sim_cycles"] = res.get("sim_cycles")
        summ["sim_seconds"] = res.get("sim_seconds")
        return summ

    ok = True
    with concurrent.futures.ThreadPoolExecutor(a.jobs) as ex:
        for summ in ex.map(one, jobs):
            ok &= bool(summ["pass"])
            print(json.dumps(summ), flush=True)
    print(json.dumps({"pass": ok, "runs": len(jobs)}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
