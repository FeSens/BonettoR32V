#!/usr/bin/env python3
"""BonettoR32V fast area score.

Synthesizes the TT top module exactly the way Tiny Tapeout's flow does (LibreLane 3.0.14,
step Yosys.Synthesis, with the project's src/config.json merged with the user config that
tt_tool --create-user-config writes) and reports the sky130_fd_sc_hd cell area.

The score (area_um2) is the FLATTENED synthesis result, which is the netlist the real flow
places and routes. by_module comes from a second, hierarchical synthesis of the same sources
(SYNTH_HIERARCHY_MODE=keep); it explains where area goes, but its total differs from the
score because flattening optimizes across module boundaries.

Usage:
  tools/area.py --top tt_um_fesens_r32v --src src/
  tools/area.py --top tt_um_x --src /path/src --files a.v b/c.v   (baselines)

Outputs:
  build/area.json        {"area_um2", "cells", "by_type", "by_module", "flops", "latches",
                          "instanced_cells", "netlist", ...}
  build/synth/netlist.v  the flattened gate-level netlist (consumed by the gate-level test)

Cell instances: src/ may instantiate the whitelisted sky130 cells (contract section 1,
CELL_WHITELIST in tools/src_check.py). Only src/*.v is staged, never the behavioral models in
verif/cells, so each instance stays the liberty cell (LibreLane reads the full liberty as
blackboxes). Yosys `stat` reads the synthesis liberty, which lacks the cells synthesis may not map
to (the sdfxtp scan flops, say), so it has no area for them; area_um2 adds their liberty area and
instanced_cells lists them. A netlist cell missing from the synthesis liberty that is not
whitelisted fails.

Engines:
  librelane (default): LibreLane's own Yosys.Synthesis step in the LibreLane 3.0.14 Docker
                       image, so the number is the one the real flow sees.
  yosys:               tools/synth_area.ys, a hand mirror of LibreLane's synthesis script,
                       run with the yosys on PATH. Fallback only (no Docker). Run with
                       LibreLane's yosys 0.62 it reproduces the LibreLane netlist area exactly
                       on NanoV; the oss-cad-suite yosys 0.64 gives 0.48 % less there, so its
                       numbers are not comparable with the default engine's.

Needs the sky130A PDK at the LibreLane 3.0.14 pin under $PDK_ROOT or ~/.ciel
(tools/harden_tt.sh --setup-only fetches it). Deterministic: two runs give the same JSON
(except runtime_s) and a byte-identical netlist.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import src_check  # noqa: E402  (CELL_WHITELIST: the cells src/ may instantiate)

REPO = Path(__file__).resolve().parent.parent
# ghcr.io/librelane/librelane:3.0.14, pinned by digest
LIBRELANE_IMAGE = ("ghcr.io/librelane/librelane@sha256:"
                   "f91b21d75f79871f9ccf37451020b5d2f7b3236881a997ff709c561e8280a30f")
# open_pdks commit LibreLane 3.0.14 pins for sky130 (librelane/pdk_hashes.yaml).
SKY130_PDK_HASH = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"
SCL = "sky130_fd_sc_hd"
LIB_CORNER = "tt_025C_1v80"
# Keys tt_tool --create-user-config adds for a 1x1 sky130A project (tt-support-tools
# 01d5d2814fa9, project.py create_user_config). DIE_AREA and FP_DEF_TEMPLATE only matter for
# floorplanning and are left out here (harden_tt.sh uses tt_tool itself).
TT_USER_CONFIG_STATIC = {"VDD_PIN": "VPWR", "GND_PIN": "VGND", "RT_MAX_LAYER": "met4"}


def die(msg):
    print(f"area.py: error: {msg}", file=sys.stderr)
    sys.exit(2)


def pdk_root():
    """The ciel version directory of the pinned PDK (immune to `ciel enable` of others).
    No fallback: without that exact version the score is not the real flow's."""
    base = Path(os.environ.get("PDK_ROOT", str(Path.home() / ".ciel")))
    pinned = base / "ciel" / "sky130" / "versions" / SKY130_PDK_HASH
    if not (pinned / "sky130A").is_dir():
        die(f"missing {pinned}/sky130A (open_pdks {SKY130_PDK_HASH}, the LibreLane 3.0.14 "
            f"pin); fetch it with tools/harden_tt.sh --setup-only")
    return pinned


def liberty_path(root):
    return root / "sky130A" / "libs.ref" / SCL / "lib" / f"{SCL}__{LIB_CORNER}.lib"


def check_pdk(root):
    lib = liberty_path(root)
    if not lib.exists():
        die(f"missing {lib}; fetch the PDK with tools/harden_tt.sh --setup-only")
    sources = root / "sky130A" / "SOURCES"
    try:
        ok = sources.read_text().split() == ["open_pdks", SKY130_PDK_HASH]
    except OSError as e:
        die(f"cannot check the PDK version in {sources}: {e}")
    if not ok:
        die(f"{sources} is not open_pdks {SKY130_PDK_HASH} (the LibreLane 3.0.14 pin)")
    return lib


def parse_liberty(lib):
    """Return {cell: {"area": float, "kind": "flop"|"latch"|"comb"}} for the liberty."""
    cells = {}
    cur = None
    depth = 0
    cell_re = re.compile(r'^\s*cell\s*\(\s*"?([A-Za-z0-9_]+)"?\s*\)')
    with open(lib) as f:
        for line in f:
            if cur is None:
                m = cell_re.match(line)
                if m:
                    cur = m.group(1)
                    cells[cur] = {"area": None, "kind": "comb"}
                    depth = line.count("{") - line.count("}")
                continue
            if depth == 1:
                m = re.match(r"^\s*area\s*:\s*([0-9.eE+-]+)", line)
                if m and cells[cur]["area"] is None:
                    cells[cur]["area"] = float(m.group(1))
            s = line.strip()
            if depth == 1 and re.match(r"^ff\s*\(", s):
                cells[cur]["kind"] = "flop"
            elif depth == 1 and (re.match(r"^latch\s*\(", s)
                                 or s.startswith("clock_gating_integrated_cell")):
                # Clock-gating cells contain a latch; count them as latches (contract v1
                # forbids latches, and an ICG is the only sanctioned way to time one).
                cells[cur]["kind"] = "latch"
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                cur = None
    return cells


# Source names end up in yosys commands (LibreLane's VERILOG_FILES, the rendered
# synth_area.ys), where a name like `z.v; exec -- cmd; y.v` would inject commands. So src/
# files must be plain `name.v`; explicit baseline lists (--files/--info) may add plain
# directory components.
SRC_NAME_RE = re.compile(r"^[A-Za-z0-9_]+\.v$")
SRC_DIR_RE = re.compile(r"^[A-Za-z0-9_]+$")


def check_source_name(rel, allow_dirs):
    parts = Path(rel).parts
    ok = bool(parts) and SRC_NAME_RE.match(parts[-1]) and (
        len(parts) == 1 or (allow_dirs and all(SRC_DIR_RE.match(d) for d in parts[:-1])))
    if not ok or rel != "/".join(parts):
        die(f"refusing source name {rel!r}: src/ files must match {SRC_NAME_RE.pattern}")


def collect_sources(src, files, info):
    if files or info:
        rel = list(files) if files else list(info_source_files(info))
        for r in rel:
            check_source_name(r, allow_dirs=True)
    else:
        rel = []
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            parts = p.relative_to(src).parts
            if any(part.startswith("test_") for part in parts):
                continue  # hypothesis unit tests (src/test_*) are not part of the design
            name = str(p.relative_to(src))
            if p.suffix in (".v", ".sv"):
                check_source_name(name, allow_dirs=False)  # dies on .sv, subdirs, odd names
                rel.append(name)
    for r in rel:
        if not (src / r).is_file():
            die(f"source {src / r} not found")
    if not rel:
        die(f"no Verilog sources under {src}")
    return rel


def info_source_files(info):
    out, inside = [], False
    for line in Path(info).read_text().splitlines():
        if re.match(r"^\s*source_files\s*:", line):
            inside = True
            continue
        if inside:
            m = re.match(r'^\s*-\s*"?([^"#]+?)"?\s*(#.*)?$', line)
            if m:
                out.append(m.group(1).strip())
            elif line.strip() and not line.strip().startswith("#"):
                break
    return out


def read_tt_config(path):
    cfg = json.loads(Path(path).read_text())
    cfg.pop("//", None)  # tt-support-tools config_utils.read_json_config does the same
    return cfg


def stage(synth_dir, src, rel, cfg_path, top, params):
    st = synth_dir / "stage"
    if st.exists():
        shutil.rmtree(st)
    (st / "src").mkdir(parents=True)
    for r in rel:
        (st / "src" / r).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / r, st / "src" / r)
    # copy include files that sit next to the sources (`include "x.vh")
    for p in src.rglob("*"):
        if p.is_file() and p.suffix in (".vh", ".svh", ".vinc"):
            d = st / "src" / p.relative_to(src)
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, d)
    cfg = read_tt_config(cfg_path)
    cfg.update({"DESIGN_NAME": top, "VERILOG_FILES": [f"dir::{r}" for r in rel]})
    cfg.update(TT_USER_CONFIG_STATIC)
    if params:
        cfg["SYNTH_PARAMETERS"] = list(params)  # baselines only (e.g. SERV's WITH_CSR=0)
    flat = dict(cfg)
    hier = dict(cfg, SYNTH_HIERARCHY_MODE="keep")
    (st / "src" / "config_merged.json").write_text(json.dumps(flat, indent=2) + "\n")
    (st / "src" / "config_hier.json").write_text(json.dumps(hier, indent=2) + "\n")
    return st


def docker_cmd(st, cfg_name, tag, root):
    # Mount only the stage directory and the PDK. (LibreLane's own --dockerized mode mounts
    # all of $HOME; on Docker Desktop for Mac that made the virtiofs event injection stall
    # and took the whole Docker VM down once, so we never mount $HOME.)
    st, root = st.resolve(), root.resolve()
    mounts = ["-v", f"{st}:{st}", "-v", f"{root}:{root}:ro"]
    return [
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
        *mounts, "-e", f"PDK_ROOT={root}", "-e", f"HOME={st}", "-w", str(st), LIBRELANE_IMAGE,
        "librelane", "--manual-pdk", "--pdk-root", str(root), "--pdk", "sky130A",
        "--scl", SCL, "--run-tag", tag, "--force-run-dir", str((st / "runs" / tag).resolve()),
        "--only", "Yosys.Synthesis", "--hide-progress-bar",
        str((st / "src" / cfg_name).resolve()),
    ]


def run_librelane(st, root, hier):
    jobs = {"flat": docker_cmd(st, "config_merged.json", "flat", root)}
    if hier:
        jobs["hier"] = docker_cmd(st, "config_hier.json", "hier", root)
    procs = {}
    for k, cmd in jobs.items():
        (st / "runs" / k).mkdir(parents=True, exist_ok=True)
        log = open(st / f"librelane-{k}.log", "w")
        procs[k] = (subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT), log)
    results = {}
    for k, (p, log) in procs.items():
        rc = p.wait()
        log.close()
        if rc != 0:
            tail = (st / f"librelane-{k}.log").read_text().splitlines()[-30:]
            print("\n".join(tail), file=sys.stderr)
            die(f"LibreLane synthesis ({k}) failed, log: {st / f'librelane-{k}.log'}")
        steps = sorted((st / "runs" / k).glob("*-yosys-synthesis"))
        if len(steps) != 1:
            die(f"no yosys-synthesis step dir in {st / 'runs' / k}")
        results[k] = steps[0]
    return results


# LibreLane 3.0.14 construct_abc_script.py output for SYNTH_STRATEGY "AREA 0" with the
# defaults TT's config leaves in place (no MFS3, no NF, no buffering, no sizing).
_RESYN2 = ("balance; drw -l; drf -l; balance; drw -l; drw -l -z; balance; drf -l -z; "
           "drw -l -z; balance")
ABC_AREA0 = "\n".join([
    "fx", "mfs", "strash", "drf -l", _RESYN2, "retime -M 5", "scleanup",
    f"fraig_store; balance; fraig_store; {_RESYN2}; fraig_store; {_RESYN2}; fraig_store; "
    f"{_RESYN2}; fraig_store; fraig_restore",
    "amap -m -Q 0.1 -F 20 -A 20 -C 5000", "", "retime", "&get -n", "&st", "&dch", "&nf",
    "&put", "", "stime -p", "print_stats -m"]) + "\n"


def trimmed_liberty(lib, root, dst):
    """LibreLane toolbox.remove_cells_from_lib with SYNTH_EXCLUDED_CELL_FILE (no_synth.cells)
    and PNR_EXCLUDED_CELL_FILE (drc_exclude.cells) of the PDK."""
    import fnmatch
    cfgdir = root / "sky130A" / "libs.tech" / "openlane" / SCL
    pats = []
    for name in ("no_synth.cells", "drc_exclude.cells"):
        for line in (cfgdir / name).read_text().splitlines():
            line = line.split("#")[0].strip()
            if line:
                pats.append(line)
    cell_re = re.compile(r'(\s*)cell\s*\("?(.*?)"?\)\s*\{')
    out, state, depth = [], 0, 0
    with open(lib) as f:
        for line in f:
            if state == 0:
                m = cell_re.search(line)
                if m:
                    excl = any(fnmatch.fnmatchcase(m.group(2), p) for p in pats)
                    state, depth = (2 if excl else 1), 1
                    out.append(f"{m.group(1)}/* removed {m.group(2)} */\n" if excl else line)
                else:
                    out.append(line)
            else:
                depth += line.count("{") - line.count("}")
                if state == 1:
                    out.append(line)
                if depth <= 0:
                    state = 0
    dst.write_text("".join(out))
    return dst


def run_yosys(st, rel, top, cfg, lib, root, params, hier):
    """Fallback: tools/synth_area.ys rendered for this design, run with the yosys on PATH."""
    if not shutil.which("yosys"):
        die("yosys not on PATH")
    libsyn = trimmed_liberty(lib, root, st / "lib_synth.lib")
    (st / "AREA_0.abc").write_text(ABC_AREA0)
    (st / "synthesis.abc.sdc").write_text(
        "set_driving_cell sky130_fd_sc_hd__inv_2/Y\nset_load 33.442\n")
    maps = root / "sky130A" / "libs.tech" / "openlane" / SCL
    defines = "-DPDK_sky130A -DSCL_sky130_fd_sc_hd -D__librelane__ -D__pnr__"
    template = (REPO / "tools" / "synth_area.ys").read_text()
    out = {}
    for k, flatten in [("flat", True)] + ([("hier", False)] if hier else []):
        d = st / "runs" / k / "01-yosys-synthesis"
        (d / "reports").mkdir(parents=True, exist_ok=True)
        fields = {
            "LIB_FULL": str(lib), "LIB": str(libsyn), "TOP": top,
            # names were validated in collect_sources; quoted as defense in depth
            "READ_VERILOG": "\n".join(f"read_verilog -defer -noautowire -sv {defines} "
                                      f"\"{st / 'src' / r}\"" for r in rel),
            "CHPARAM": "\n".join("chparam -set {} {} {}".format(*p.split("=", 1), top)
                                 for p in params),
            "FLATTEN": "flatten" if flatten else "",
            "REPORTS": str(d / "reports"), "TRIBUF_MAP": str(maps / "tribuff_map.v"),
            "LATCH_MAP": str(maps / "latch_map.v"), "ABC_SCRIPT": str(st / "AREA_0.abc"),
            "CLOCK_PS": str(float(cfg["CLOCK_PERIOD"]) * 1000),
            "ABC_SDC": str(st / "synthesis.abc.sdc"), "OUT": str(d / f"{top}.nl.v"),
        }
        text = template
        for key, val in fields.items():
            text = text.replace(f"@{key}@", val)
        script = d / "synth_area.rendered.ys"
        script.write_text(text)
        log = d / "yosys-synthesis.log"
        rc = subprocess.run(["yosys", "-q", "-l", str(log), "-s", str(script)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT).returncode
        if rc != 0:
            die(f"yosys synthesis ({k}) failed, log: {log}")
        out[k] = d
    return out


SYNTH_CHECK_METRICS = ("synthesis__check_error__count", "design__instance_unmapped__count")


def synth_checks(step_dir, engine):
    """TT's flow stops on these (Checker.YosysSynthChecks, Checker.YosysUnmappedCells), so a
    design that fails them has no valid score. LibreLane engine: the step's own metrics.
    Yosys fallback: the same reports, counted the same way (approximation of LibreLane's
    _parse_yosys_check: every check warning not about tristates is an error)."""
    if engine == "librelane":
        state = step_dir / "state_out.json"
        try:
            metrics = json.loads(state.read_text())["metrics"]
        except (OSError, KeyError, ValueError) as e:
            die(f"cannot read synthesis metrics from {state}: {e}")
        vals = {k: metrics.get(k) for k in SYNTH_CHECK_METRICS}
    else:
        errors = 0
        for rpt in ("pre_synth_chk.rpt", "chk.rpt"):
            text = (step_dir / "reports" / rpt).read_text(errors="replace")
            for block in re.split(r"\n(?=Warning:)", text):
                if block.startswith("Warning:") and "tribuf" not in block and "ebuf" not in block:
                    errors += 1
        stat = json.loads((step_dir / "reports" / "stat.json").read_text())["design"]
        unmapped = sum(n for c, n in stat["num_cells_by_type"].items()
                       if c.startswith("$") and c != "$assert")
        vals = {SYNTH_CHECK_METRICS[0]: errors, SYNTH_CHECK_METRICS[1]: unmapped}
    bad = {k: v for k, v in vals.items() if v is None or int(v) != 0}
    if bad:
        log = step_dir / "yosys-synthesis.log"
        die(f"synthesis checks failed in {step_dir.parent.name} run: {bad} "
            f"(see {step_dir / 'reports'} and {log}); TT's flow would stop here, no score")
    return {k: int(v) for k, v in vals.items()}


def synth_lib_cells(step_dir, engine, st):
    """Cell names of the liberty synthesis (and its `stat`) used: LibreLane's trimmed copy without
    the excluded cells (extra.json "libs_synth"), or the fallback engine's lib_synth.lib."""
    if engine == "librelane":
        try:
            libs = json.loads((step_dir / "extra.json").read_text())["libs_synth"]
        except (OSError, KeyError, ValueError) as e:
            die(f"cannot read the synthesis liberty list from {step_dir / 'extra.json'}: {e}")
    else:
        libs = [str(st / "lib_synth.lib")]
    cells = set()
    for lib in libs:
        if not Path(lib).is_file():
            die(f"synthesis liberty {lib} missing")
        cells.update(parse_liberty(lib))
    return cells


def summarize(step_dir, top, libcells, synth_cells):
    """The score from the flattened stat.json. Yosys `stat` reads the synthesis liberty, which
    lacks the cells synthesis may not map to (no_synth.cells and friends), so it has no area for
    such a cell. Only a direct instance of a whitelisted cell (contract section 1) can put one in
    the netlist: its liberty area is added to stat's area and listed under instanced_cells. Any
    other cell missing from the synthesis liberty fails."""
    stat = json.loads((step_dir / "reports" / "stat.json").read_text())
    design = stat["design"]
    by_type, instanced = {}, {}
    flops = latches = 0
    total = known = extra = 0.0
    for cell, n in sorted(design["num_cells_by_type"].items()):
        info = libcells.get(cell)
        if info is None:
            die(f"cell {cell} is not in the {SCL} liberty (unmapped logic?)")
        a = info["area"] * n
        total += a
        if cell in synth_cells:
            known += a
        elif cell in src_check.CELL_WHITELIST:
            extra += a
            instanced[cell] = {"count": n, "area_um2": round(a, 4)}
        else:
            die(f"cell {cell} is in the netlist but neither in the synthesis liberty nor in the "
                f"cell whitelist (CELL_WHITELIST in tools/src_check.py)")
        by_type[cell] = {"count": n, "area_um2": round(a, 4)}
        if info["kind"] == "flop":
            flops += n
        elif info["kind"] == "latch":
            latches += n
    area = float(design.get("area", known))
    if abs(area - known) > 0.01:
        die(f"stat area {area} != sum over the synthesis liberty's cell types {known}")
    area += extra
    if abs(area - total) > 0.01:
        die(f"area {area} != sum over cell types {total}")
    by_type = dict(sorted(by_type.items(), key=lambda kv: (-kv[1]["area_um2"], kv[0])))
    return {"area_um2": round(area, 4), "cells": int(design["num_cells"]),
            "by_type": by_type, "flops": flops, "latches": latches,
            "instanced_cells": instanced}, stat


def short_module_name(mod):
    """'$paramod$<hash>\\name' or '$paramod\\name\\P=v' -> 'name'."""
    name = mod.lstrip("\\")
    m = re.match(r"^\$paramod(?:\$[0-9a-f]+)?\\([^\\]+)", name)
    return m.group(1) if m else name


def unknown_area(stat, libcells, synth_cells):
    """Per module, the liberty area yosys `stat` left out (cells missing from the synthesis
    liberty: whitelisted instances), submodules included."""
    mods = stat.get("modules", {})
    memo = {}

    def miss(mod):
        if mod not in memo:
            memo[mod] = 0.0
            total = 0.0
            for c, n in mods[mod]["num_cells_by_type"].items():
                # a submodule type is `name` for module `\name`, or `$paramod\...` as is
                sub = c if c in mods else "\\" + c
                if c in libcells and c not in synth_cells:
                    total += libcells[c]["area"] * n
                elif sub in mods and sub != mod:
                    total += miss(sub) * n
            memo[mod] = total
        return memo[mod]

    return {mod: miss(mod) for mod in mods}


def by_module(stat, libcells, synth_cells=None):
    """Per module: self_area_um2 (its own cells only), area_um2 (yosys' inclusive figure,
    submodule instances included, plus the liberty area of whitelisted instances yosys has no
    area for) and its own cell count. Hierarchical synthesis."""
    out = {}
    missing = unknown_area(stat, libcells, synth_cells if synth_cells is not None else libcells)
    for mod, s in stat.get("modules", {}).items():
        own = {c: n for c, n in s["num_cells_by_type"].items() if c in libcells}
        self_area = sum(libcells[c]["area"] * n for c, n in own.items())
        incl = s.get("area", self_area - missing[mod]) + missing[mod]
        name = short_module_name(mod)
        if name in out:
            name = mod.lstrip("\\")
        out[name] = {"self_area_um2": round(self_area, 4), "area_um2": round(float(incl), 4),
                     "cells": int(sum(own.values()))}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["self_area_um2"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--top", required=True)
    ap.add_argument("--src", default=str(REPO / "src"))
    ap.add_argument("--files", nargs="+", help="explicit source list, relative to --src")
    ap.add_argument("--info", help="take the source list from this info.yaml")
    ap.add_argument("--config", default=str(REPO / "src" / "config.json"),
                    help="TT flow config (default: the repo's src/config.json)")
    ap.add_argument("--out", default=str(REPO / "build" / "area.json"))
    ap.add_argument("--synth-dir", default=str(REPO / "build" / "synth"))
    ap.add_argument("--engine", choices=["librelane", "yosys"], default="librelane")
    ap.add_argument("--no-hier", action="store_true", help="skip the by_module synthesis")
    ap.add_argument("--param", action="append", default=[],
                    help="top-level parameter KEY=VALUE (baselines only)")
    a = ap.parse_args()
    t0 = time.time()
    src = Path(a.src).resolve()
    synth_dir = Path(a.synth_dir).resolve()
    out = Path(a.out).resolve()
    # No stale result may survive a failed run (eval and the gate-level test read these).
    out.unlink(missing_ok=True)
    (synth_dir / "netlist.v").unlink(missing_ok=True)
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", a.top):
        die(f"bad --top {a.top!r}")
    for p in a.param:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=[A-Za-z0-9_']+$", p):
            die(f"bad --param {p!r}")
    root = pdk_root()
    lib = check_pdk(root)
    libcells = parse_liberty(lib)
    rel = collect_sources(src, a.files, a.info)
    st = stage(synth_dir, src, rel, a.config, a.top, a.param)
    cfg = read_tt_config(a.config)

    if a.engine == "librelane":
        steps = run_librelane(st, root, not a.no_hier)
    else:
        steps = run_yosys(st, rel, a.top, cfg, lib, root, a.param, not a.no_hier)

    checks = {k: synth_checks(d, a.engine) for k, d in steps.items()}
    synth_cells = synth_lib_cells(steps["flat"], a.engine, st)
    res, _ = summarize(steps["flat"], a.top, libcells, synth_cells)
    res["synth_checks"] = checks["flat"]
    netlist = synth_dir / "netlist.v"
    shutil.copy2(steps["flat"] / f"{a.top}.nl.v", netlist)
    hier_total = None
    mods = {}
    if "hier" in steps:
        hstat = json.loads((steps["hier"] / "reports" / "stat.json").read_text())
        hcells = synth_lib_cells(steps["hier"], a.engine, st)
        mods = by_module(hstat, libcells, hcells)
        extra = sum(libcells[c]["area"] * n
                    for c, n in hstat["design"]["num_cells_by_type"].items()
                    if c in libcells and c not in hcells)
        hier_total = round(float(hstat["design"].get("area", 0.0)) + extra, 4)

    srchash = hashlib.sha256()
    for r in rel:
        srchash.update(r.encode() + b"\0" + (src / r).read_bytes() + b"\0")
    res.update({
        "by_module": mods,
        "by_module_total_um2": hier_total,
        "netlist": os.path.relpath(netlist, REPO) if str(netlist).startswith(str(REPO))
        else str(netlist),
        "top": a.top,
        "engine": a.engine,
        "flow": "LibreLane 3.0.14 Yosys.Synthesis, SYNTH_HIERARCHY_MODE=flatten"
        if a.engine == "librelane" else "tools/synth_area.ys (mirror, not the real flow)",
        "liberty": f"{SCL}__{LIB_CORNER}",
        "pdk": f"sky130A open_pdks {SKY130_PDK_HASH}",
        "clock_period_ns": cfg.get("CLOCK_PERIOD"),
        "synth_strategy": cfg.get("SYNTH_STRATEGY", "AREA 0"),
        "sources": rel,
        "parameters": a.param,
        "sources_sha256": srchash.hexdigest(),
        "runtime_s": round(time.time() - t0, 1),
    })
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2) + "\n")
    top10 = list(res["by_type"].items())[:10]
    print(f"area_um2={res['area_um2']} cells={res['cells']} flops={res['flops']} "
          f"latches={res['latches']} runtime_s={res['runtime_s']}")
    for cell, v in top10:
        print(f"  {cell:34s} {v['count']:6d} {v['area_um2']:12.3f}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
