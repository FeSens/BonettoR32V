#!/usr/bin/env python3
"""Cell models: every behavioral model in verif/cells matches the PDK's own Verilog.

src/ may instantiate the sky130 cells of CELL_WHITELIST (tools/src_check.py, contract section 1).
The gates that compile RTL (lint, cosim and arch, equiv, the RTL test, formal) read the behavioral
models verif/cells/<cell>.v; synthesis, harden and the gate-level test use the real cell. This
tool proves the two agree, so no gate sees a different circuit from the netlist.

inventory(models_dir): verif/cells holds exactly one file per whitelisted cell, <cell>.v, which
  defines exactly that module with exactly the whitelist's ports and directions, and nothing else
  (no initial value, no other directive than `default_nettype).

drift(models_dir, pdk_verilog_dir): one Icarus simulation per PDK view instantiates every model
  (renamed model__<cell>) next to the PDK's functional model of the same cell (primitives.v and
  sky130_fd_sc_hd.v of the pinned PDK, compiled with -DFUNCTIONAL -DUNIT_DELAY=#1 like the
  gate-level test), drives both with the same pseudo-random vectors, and compares every output
  with !== (so an X must match an X) at two points per clock cycle: with CLK low after the
  inputs change, and with CLK high after the rising edge (the PDK's unit delay included).
  - Vectors: inputs change only while CLK is low, and CLK is always 0 or 1 (the gate-level test
    never drives an X clock). The first half of the cycles drive 0 and 1 only; the second half
    also drive X on every data and select input (1 in 6). Z is never driven (lint rejects
    undriven nets in src/).
  - Power-up: there is no reset, so every flop starts X in both models and must turn known at
    the same check (the first edge that captures a known value): a model with a power-up value,
    or one that never turns known, fails. The run also requires that each PDK flop starts X and
    turns known, so the comparison is known to exercise both.
  - Both PDK views: without power pins, and with USE_POWER_PINS (VPWR = VPB = 1, VGND = VNB = 0),
    the two netlist forms the gate-level test simulates.

Usage: cell_drift.py [--models DIR] [--pdk-verilog DIR] [--cycles N] [--seed S]
Exit 0 when both checks pass, 1 with one line per problem.
"""
import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import src_check  # noqa: E402

PDK_HASH = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"   # tools/area.py SKY130_PDK_HASH
SCL = "sky130_fd_sc_hd"
CLOCK = "CLK"
POWER = (("VPWR", "1'b1"), ("VGND", "1'b0"), ("VPB", "1'b1"), ("VNB", "1'b0"))


def pdk_verilog_dir():
    base = os.environ.get("PDK_ROOT", os.path.expanduser("~/.ciel"))
    return os.path.join(base, "ciel", "sky130", "versions", PDK_HASH, "sky130A", "libs.ref",
                        SCL, "verilog")


def module_ports(text):
    """[(module, [(direction, port)])] for every module in a model file, from its ANSI header."""
    toks, _ = src_check.tokenize(text)
    out, i = [], 0
    while i < len(toks):
        if toks[i][:2] != ("id", "module"):
            i += 1
            continue
        name = toks[i + 1][1] if i + 1 < len(toks) else ""
        close = src_check.close_of(toks, i + 2) if i + 2 < len(toks) else None
        ports, direction = [], None
        for kind, t, _ in (toks[i + 3:close] if close else []):
            if t in ("input", "output", "inout"):
                direction = t
            elif kind == "id" and t not in ("wire", "reg", "signed") and direction:
                ports.append((direction, t))
        out.append((name, ports))
        i = (close or i) + 1
    return out


def inventory(models_dir):
    """Problems with the set of model files and their headers."""
    errors = []
    want = {c + ".v" for c in src_check.CELL_WHITELIST}
    have = {f for f in os.listdir(models_dir) if not f.startswith(".")}
    for f in sorted(have - want):
        errors.append(f"{models_dir}/{f}: not the model of a whitelisted cell (CELL_WHITELIST in "
                      f"tools/src_check.py)")
    for f in sorted(want - have):
        errors.append(f"{models_dir}/{f}: model missing")
    for cell, (outs, ins) in sorted(src_check.CELL_WHITELIST.items()):
        path = os.path.join(models_dir, cell + ".v")
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            text = f.read()
        try:
            mods = module_ports(text)
            toks, _ = src_check.tokenize(text)
        except src_check.LexError as e:
            errors.append(f"{path}: {e}")
            continue
        expect = [("output", p) for p in outs] + [("input", p) for p in ins]
        if [m for m, _ in mods] != [cell]:
            errors.append(f"{path}: must define exactly module {cell}, defines "
                          f"{[m for m, _ in mods]}")
        elif sorted(mods[0][1]) != sorted(expect):
            errors.append(f"{path}: ports {mods[0][1]} differ from the whitelist's {expect}")
        dirs = {t for k, t, _ in toks if k == "dir"} - {"default_nettype"}
        if dirs:
            errors.append(f"{path}: directives {sorted(dirs)} (only `default_nettype)")
        if any(k == "id" and t == "initial" for k, t, _ in toks):
            errors.append(f"{path}: `initial` (the cell has no power-up value)")
    return errors


def vectors(cells, cycles, seed):
    """Per cycle, one $readmemb word: the non-clock inputs of every cell in order, the first
    cell's first input as the MSB. The first half 0/1 only, then X allowed on every input."""
    rng = random.Random(seed)
    rows = []
    for n in range(cycles):
        row = []
        for cell in cells:
            k = len([p for p in src_check.CELL_WHITELIST[cell][1] if p != CLOCK])
            vals = []
            for _ in range(k):
                if n >= cycles // 2 and rng.randrange(6) == 0:
                    vals.append("x")
                else:
                    vals.append(str(rng.randrange(2)))
            row.append("".join(vals))
        rows.append("".join(row))
    return rows


def testbench(cells, cycles, powered):
    """A tb module: model__<cell> and <cell> per cell, shared inputs, the compare."""
    lines = ["`timescale 1ns/1ps", "module tb;", "    reg CLK;"]
    width = sum(len([p for p in src_check.CELL_WHITELIST[c][1] if p != CLOCK]) for c in cells)
    lines += [f"    reg [{width - 1}:0] vec [0:{cycles - 1}];", f"    reg [{width - 1}:0] v;",
              "    integer i, chk;"]
    pos = width
    checks = []
    for ci, cell in enumerate(cells):
        outs, ins = src_check.CELL_WHITELIST[cell]
        conns = []
        for p in ins:
            if p == CLOCK:
                conns.append(f".{p}(CLK)")
            else:
                pos -= 1
                conns.append(f".{p}(v[{pos}])")
        for o in outs:
            lines.append(f"    wire m{ci}_{o}, p{ci}_{o};")
        mconn = ", ".join(conns + [f".{o}(m{ci}_{o})" for o in outs])
        pconn = ", ".join(conns + [f".{o}(p{ci}_{o})" for o in outs]
                          + ([f".{n}({v})" for n, v in POWER] if powered else []))
        lines.append(f"    model__{cell} m{ci} ({mconn});")
        lines.append(f"    {cell} p{ci} ({pconn});")
        for o in outs:
            checks.append((cell, f"m{ci}_{o}", f"p{ci}_{o}", o))
    lines += ["    integer err [0:%d];" % (len(checks) - 1),
              "    integer known [0:%d];" % (len(checks) - 1),
              "    reg [%d:0] x0;" % (len(checks) - 1),
              "    task compare;", "        begin"]
    for k, (cell, m, p, o) in enumerate(checks):
        lines += [f"            if ({m} !== {p}) begin",
                  f"                if (err[{k}] < 5) $display(\"MISMATCH {cell} {o} check=%0d "
                  f"in=%b model=%b pdk=%b\", chk, v, {m}, {p});",
                  f"                err[{k}] = err[{k}] + 1;",
                  "            end",
                  f"            if (known[{k}] < 0 && {p} !== 1'bx) known[{k}] = chk;"]
    lines += ["            chk = chk + 1;", "        end", "    endtask", "    initial begin",
              "        $readmemb(\"vec.txt\", vec);", "        chk = 0;"]
    for k, (_, _, p, _) in enumerate(checks):
        lines += [f"        err[{k}] = 0;", f"        known[{k}] = -1;"]
    lines += ["        CLK = 1'b0;", "        v = vec[0];", "        #1;"]
    for k, (_, _, p, _) in enumerate(checks):
        lines.append(f"        x0[{k}] = ({p} === 1'bx);")
    lines += [f"        for (i = 0; i < {cycles}; i = i + 1) begin",
              "            v = vec[i];", "            #4 compare;",
              "            CLK = 1'b1;", "            #4 compare;",
              "            CLK = 1'b0;", "            #1;", "        end"]
    for k, (cell, _, _, o) in enumerate(checks):
        lines.append(f"        $display(\"RESULT {cell} {o} errors=%0d known=%0d x0=%0d\", "
                     f"err[{k}], known[{k}], x0[{k}]);")
    lines += ["        $finish;", "    end", "endmodule", ""]
    return "\n".join(lines)


def renamed_models(models_dir, cells):
    out = []
    for cell in cells:
        with open(os.path.join(models_dir, cell + ".v")) as f:
            text = f.read()
        new, n = re.subn(rf"\bmodule\s+{re.escape(cell)}\b", f"module model__{cell}", text)
        if n != 1:
            raise ValueError(f"{cell}.v: expected one `module {cell}`")
        out.append(new)
    return "\n".join(out)


def drift(models_dir, pdk_dir, cycles=4000, seed=1, env=None):
    """Problems where a model and the PDK's functional model disagree (both PDK views)."""
    env = env or src_check.tool_env()
    if shutil.which("iverilog", path=env["PATH"]) is None:
        return ["drift: iverilog not on PATH"]
    prims = os.path.join(pdk_dir, "primitives.v")
    lib = os.path.join(pdk_dir, f"{SCL}.v")
    for p in (prims, lib):
        if not os.path.isfile(p):
            return [f"drift: PDK model {p} missing"]
    cells = sorted(src_check.CELL_WHITELIST)
    errors = []
    with tempfile.TemporaryDirectory() as work:
        try:
            models = renamed_models(models_dir, cells)
        except (OSError, ValueError) as e:
            return [f"drift: {e}"]
        with open(os.path.join(work, "models.v"), "w") as f:
            f.write(models)
        with open(os.path.join(work, "vec.txt"), "w") as f:
            f.write("\n".join(vectors(cells, cycles, seed)) + "\n")
        for powered in (False, True):
            view = "USE_POWER_PINS" if powered else "no power pins"
            with open(os.path.join(work, "tb.v"), "w") as f:
                f.write(testbench(cells, cycles, powered))
            defs = ["-DFUNCTIONAL", "-DUNIT_DELAY=#1"] + (["-DUSE_POWER_PINS"] if powered else [])
            p = subprocess.run(["iverilog", "-g2005", *defs, "-s", "tb", "-o", "sim.vvp", "tb.v",
                                "models.v", prims, lib], cwd=work, capture_output=True,
                               text=True, env=env)
            if p.returncode != 0:
                errors.append(f"drift ({view}): iverilog failed: "
                              f"{(p.stderr or p.stdout).strip().splitlines()[:3]}")
                continue
            p = subprocess.run(["vvp", "-n", "sim.vvp"], cwd=work, capture_output=True,
                               text=True, env=env)
            results = re.findall(r"RESULT (\S+) (\S+) errors=(\d+) known=(-?\d+) x0=(\d)",
                                 p.stdout)
            if p.returncode != 0 or len(results) != sum(len(src_check.CELL_WHITELIST[c][0])
                                                        for c in cells):
                errors.append(f"drift ({view}): simulation failed (exit {p.returncode}): "
                              f"{(p.stdout + p.stderr).strip()[-400:]}")
                continue
            shown = {}
            for line in p.stdout.splitlines():
                if line.startswith("MISMATCH"):
                    shown.setdefault(line.split()[1], line)
            for cell, out, errs, known, x0 in results:
                flop = CLOCK in src_check.CELL_WHITELIST[cell][1]
                if int(errs):
                    errors.append(f"drift ({view}): {cell}.{out} differs from the PDK model at "
                                  f"{errs} of {2 * cycles} checks; first: {shown.get(cell)}")
                elif flop and (x0 != "1" or int(known) < 0):
                    errors.append(f"drift ({view}): {cell}.{out}: the PDK flop did not start X "
                                  f"and turn known (x0={x0}, known at check {known}); the "
                                  f"vectors do not exercise power-up")
    return errors


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", default=src_check.CELL_MODELS_DIR)
    ap.add_argument("--pdk-verilog", default=pdk_verilog_dir())
    ap.add_argument("--cycles", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    errors = inventory(args.models)
    if not errors:
        errors = drift(args.models, args.pdk_verilog, args.cycles, args.seed)
    for e in errors:
        print("cell_drift: " + e)
    n = len(src_check.CELL_WHITELIST)
    print(f"cell_drift: {'FAIL' if errors else 'PASS'} ({n} cells, {len(errors)} problem(s))")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
