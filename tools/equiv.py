#!/usr/bin/env python3
"""Equivalence gate: the RVFI build with its RVFI ports stripped equals the plain build.

Contract section 6: logic under `ifdef RISCV_FORMAL` may only compute RVFI outputs; nothing that
drives a pin may depend on it. This gate proves it, so formal (which runs the RVFI build) proves
the design that ships.

  A (gold): read_verilog -mem2reg -DRISCV_FORMAL src/*.v, hierarchy, proc, flatten, remove the
            port flag from every rvfi_* wire, opt_clean (logic that only fed RVFI disappears).
  B (gate): the same without the define.
  Proof:    async2sync, equiv_make gold gate, equiv_simple -seq N, equiv_induct -seq N,
            equiv_status -assert.

How the proof can fail and pass:
- Signals are matched by name. Both builds come from the same RTL, so a register that exists in
  both has the same name in both. A register that exists only in A (RVFI-only state) has no
  partner: if it can reach a pin or a shared register, it is unconstrained and the proof fails.
- Every $equiv cell must be proven. An extra or wrong match can only make the proof fail.
- Every TT output bit must be the output of a $equiv cell (checked from the equiv module).
- Both builds must expose exactly the TT ports of contract section 1.
- `opt_clean` runs without `-purge`. With `-purge`, opt_clean keeps one name per net and may
  pick an RVFI-only alias (for example `wire [7:0] obs = acc;`) in A but the original name in B,
  so the shared register goes unmatched and only `equiv_induct` (whose base case yosys does not
  check) can prove it. Without `-purge`, every source name survives, so shared state stays matched;
  the RVFI-only cells are still removed (they drive nothing once the rvfi ports are gone).
- `-mem2reg` turns arrays into registers with source-derived names (`rf[3]`), which match across
  builds; `equiv_make` rejects designs that still contain memories.

Usage: equiv.py [--src DIR] [--top NAME] [--out JSON] [--work DIR] [--seq N]
Exit 0 on PASS, 1 on FAIL. Writes a JSON summary (default build/equiv.json).
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLCHAIN_BIN = os.path.expanduser("~/bonetto/riscv-autoarch/.toolchain/oss-cad-suite/bin")
RVFI_INCLUDE = os.path.join(REPO, "third_party", "riscv-formal", "checks")
RVFI_DEFINES = ["RISCV_FORMAL", "RISCV_FORMAL_NRET=1", "RISCV_FORMAL_XLEN=32",
                "RISCV_FORMAL_ILEN=32"]

# Contract section 1: name -> (direction, width)
TT_PORTS = {
    "ui_in": ("input", 8),
    "uo_out": ("output", 8),
    "uio_in": ("input", 8),
    "uio_out": ("output", 8),
    "uio_oe": ("output", 8),
    "ena": ("input", 1),
    "clk": ("input", 1),
    "rst_n": ("input", 1),
}
TT_OUTPUT_BITS = {(n, i) for n, (d, w) in TT_PORTS.items() if d == "output" for i in range(w)}


def tool_env():
    env = dict(os.environ)
    path = env.get("PATH", "")
    if os.path.isdir(TOOLCHAIN_BIN) and TOOLCHAIN_BIN not in path.split(os.pathsep):
        env["PATH"] = TOOLCHAIN_BIN + os.pathsep + path
    return env


def module_body(rtlil_text, module):
    """Return the text between `module \\<module>` and its closing top-level `end`."""
    m = re.search(r"^module \\" + re.escape(module) + r"\n(.*?)^end\n", rtlil_text, re.S | re.M)
    return m.group(1) if m else None


def rtlil_ports(path, module):
    """Return {name: (direction, width)} for the ports of `module` in an RTLIL file."""
    with open(path) as f:
        body = module_body(f.read(), module)
    ports = {}
    if body is None:
        return ports
    for line in body.splitlines():
        toks = line.split()
        if not toks or toks[0] != "wire":
            continue
        width, direction, i = 1, None, 1
        while i < len(toks) - 1:
            t = toks[i]
            if t == "width":
                width = int(toks[i + 1])
                i += 2
            elif t in ("input", "output", "inout"):
                direction = t
                i += 2
            elif t == "offset":
                i += 2
            else:
                i += 1
        if direction:
            ports[toks[-1].lstrip("\\")] = (direction, width)
    return ports


def check_tt_ports(ports, label):
    errors = []
    for name, (d, w) in TT_PORTS.items():
        if name not in ports:
            errors.append(f"{label}: missing port {name}")
        elif ports[name] != (d, w):
            errors.append(f"{label}: port {name} is {ports[name][0]} [{ports[name][1]}], "
                          f"contract says {d} [{w}]")
    for name in sorted(set(ports) - set(TT_PORTS)):
        errors.append(f"{label}: extra port {name} ({ports[name][0]} [{ports[name][1]}])")
    return errors


def sigbits(sig):
    """Expand an RTLIL sigspec into (wire, bit) pairs; constants are skipped."""
    sig = sig.strip()
    if sig.startswith("{"):
        parts = re.findall(r"[\\$]\S+(?: \[\d+(?::\d+)?\])?", sig.strip("{} "))
        return [b for p in parts for b in sigbits(p)]
    m = re.match(r"\\(\S+)(?: \[(\d+)(?::(\d+))?\])?$", sig)
    if not m:
        return []
    name = m.group(1)
    if m.group(2) is None:
        return [(name, i) for i in range(TT_PORTS.get(name, ("", 1))[1])]
    hi = int(m.group(2))
    lo = int(m.group(3)) if m.group(3) is not None else hi
    return [(name, i) for i in range(lo, hi + 1)]


def covered_output_bits(equiv_il):
    """TT output bits of module `equiv` that are the Y of a $equiv cell."""
    with open(equiv_il) as f:
        body = module_body(f.read(), "equiv") or ""
    covered = set()
    for cm in re.finditer(r"^  cell \$equiv \S+\n(.*?)^  end\n", body, re.S | re.M):
        y = re.search(r"^    connect \\Y (.+)$", cm.group(1), re.M)
        if y:
            covered.update(b for b in sigbits(y.group(1)) if b in TT_OUTPUT_BITS)
    return covered


def parse_equiv_log(text):
    """Return (found, unproven_count, unproven_names, proved_simple, proved_induct)."""
    def proved_after(pass_name):
        part = text.split(f"Executing {pass_name} pass")
        if len(part) < 2:
            return None
        m = re.search(r"Proved (\d+) previously unproven", part[1].split("Executing ")[0])
        return int(m.group(1)) if m else 0

    last = text.split("Executing EQUIV_STATUS pass")[-1] if "EQUIV_STATUS" in text else ""
    found = unproven = None
    m = re.search(r"Found (\d+) \$equiv cells", last)
    if m:
        found = int(m.group(1))
    m = re.search(r"Of those cells (\d+) are proven and (\d+) are unproven", last)
    if m:
        unproven = int(m.group(2))
    elif found is not None and re.search(r"Equivalence successfully proven", last):
        unproven = 0
    names = []
    for mm in re.finditer(r"Unproven \$equiv .*?: \\(\S+?)_gold(?: \[([\d:]+)\])?", last):
        names.append(mm.group(1) + (f"[{mm.group(2)}]" if mm.group(2) else ""))
    return found, unproven, names, proved_after("EQUIV_SIMPLE"), proved_after("EQUIV_INDUCT")


def tail(path, n=15):
    try:
        with open(path, errors="replace") as f:
            return "\n".join(f.read().splitlines()[-n:])
    except OSError:
        return ""


def run_yosys(script, log, env):
    with open(log + ".stdout", "w") as out:
        p = subprocess.run(["yosys", "-l", log, "-s", script], stdout=out,
                           stderr=subprocess.STDOUT, env=env)
    return p.returncode


def build_script(srcs, top, rvfi, out_il, rename_to, ports_txt, prelude):
    defs = " ".join("-D" + d for d in RVFI_DEFINES) + f" -I{RVFI_INCLUDE} {prelude}" if rvfi else ""
    lines = [
        f"read_verilog -mem2reg {defs} {' '.join(srcs)}",
        f"hierarchy -check -top {top}",
        "proc",
        "flatten",
        f"hierarchy -top {top}",
        f"select -write {ports_txt} {top}/x:*",
    ]
    if rvfi:
        lines.append(f"delete -port {top}/w:rvfi_*")
    lines += ["opt_clean", f"rename {top} {rename_to}", f"write_rtlil {out_il}"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", default=os.path.join(REPO, "src"))
    ap.add_argument("--top", default="tt_um_fesens_r32v")
    ap.add_argument("--out", default=os.path.join(REPO, "build", "equiv.json"))
    ap.add_argument("--work", default=os.path.join(REPO, "build", "equiv"))
    ap.add_argument("--seq", type=int, default=5)
    args = ap.parse_args(argv)

    t0 = time.time()
    env = tool_env()
    result = {"pass": False, "detail": "", "top": args.top,
              "method": f"yosys: equiv_make, equiv_simple -seq {args.seq}, "
                        f"equiv_induct -seq {args.seq}, equiv_status -assert"}

    def finish(ok, detail):
        result["pass"] = bool(ok)
        result["detail"] = detail
        result["runtime_s"] = round(time.time() - t0, 2)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
        print(("equiv: PASS: " if ok else "equiv: FAIL: ") + detail)
        return 0 if ok else 1

    if shutil.which("yosys", path=env["PATH"]) is None:
        return finish(False, "yosys not found on PATH")
    srcs = sorted(glob.glob(os.path.join(os.path.abspath(args.src), "*.v")))
    if not srcs:
        return finish(False, f"no *.v files in {args.src}")
    result["src_files"] = [os.path.basename(s) for s in srcs]
    if not os.path.isfile(os.path.join(RVFI_INCLUDE, "rvfi_macros.vh")):
        return finish(False, "third_party/riscv-formal/checks/rvfi_macros.vh is missing "
                             "(run: git submodule update --init third_party/riscv-formal)")

    work = os.path.abspath(args.work)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    prelude = os.path.join(work, "rvfi_prelude.vh")
    with open(prelude, "w") as f:
        f.write('`include "rvfi_macros.vh"\n')

    labels = {"gold": "A (RISCV_FORMAL, rvfi stripped)", "gate": "B (plain)"}
    il = {}
    for name, rvfi in (("gold", True), ("gate", False)):
        il[name] = os.path.join(work, name + ".il")
        ys = os.path.join(work, name + ".ys")
        with open(ys, "w") as f:
            f.write(build_script(srcs, args.top, rvfi, il[name], name,
                                 os.path.join(work, name + "_ports.txt"), prelude))
        log = os.path.join(work, name + ".log")
        if run_yosys(ys, log, env) != 0 or not os.path.isfile(il[name]):
            return finish(False, f"yosys build {labels[name]} failed (log {log}):\n{tail(log)}")

    with open(os.path.join(work, "gold_ports.txt")) as f:
        rvfi_ports = sorted(p for p in (l.strip().split("/")[-1] for l in f) if p.startswith("rvfi_"))
    result["rvfi_ports"] = len(rvfi_ports)

    errors = []
    for name in ("gold", "gate"):
        errors += check_tt_ports(rtlil_ports(il[name], name), labels[name])
    if errors:
        return finish(False, "port check failed: " + "; ".join(errors))

    ys = os.path.join(work, "equiv.ys")
    equiv_il = os.path.join(work, "equiv.il")
    with open(ys, "w") as f:
        f.write("\n".join([
            f"read_rtlil {il['gold']}",
            f"read_rtlil {il['gate']}",
            "async2sync",
            "equiv_make gold gate equiv",
            "hierarchy -top equiv",
            f"equiv_simple -seq {args.seq}",
            f"equiv_induct -seq {args.seq}",
            f"write_rtlil {equiv_il}",
            "equiv_status -assert",
        ]) + "\n")
    log = os.path.join(work, "equiv.log")
    rc = run_yosys(ys, log, env)
    with open(log, errors="replace") as f:
        found, unproven, names, by_simple, by_induct = parse_equiv_log(f.read())
    covered = covered_output_bits(equiv_il) if os.path.isfile(equiv_il) else set()
    result.update({"equiv_cells": found, "proved_by_simple": by_simple,
                   "proved_by_induct": by_induct, "unproven": names,
                   "output_bits_checked": len(covered)})

    if found is None or unproven is None:
        return finish(False, f"equiv run reported no status (log {log}):\n{tail(log)}")
    if rc != 0 or unproven:
        shown = ", ".join(names[:12]) + (" ..." if len(names) > 12 else "")
        return finish(False, f"{unproven} of {found} $equiv cells unproven ({shown}): the "
                             f"RISCV_FORMAL build differs from the plain build at these points. "
                             f"Log: {log}")
    missing = sorted(TT_OUTPUT_BITS - covered)
    if missing:
        return finish(False, f"{len(missing)} TT output bits have no $equiv cell "
                             f"(e.g. {missing[0][0]}[{missing[0][1]}]); the proof would not "
                             f"constrain every pin")
    return finish(True, f"{found} $equiv cells proven ({by_simple} by equiv_simple, {by_induct} "
                        f"by equiv_induct), {len(covered)}/{len(TT_OUTPUT_BITS)} TT output bits, "
                        f"{len(rvfi_ports)} rvfi ports stripped")


if __name__ == "__main__":
    sys.exit(main())
