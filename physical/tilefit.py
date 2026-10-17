#!/usr/bin/env python3
"""BonettoR32V physical score: the smallest share of the real TT tile the design signs off in.

Every trial is a full tools/harden_tt.sh run (tt_tool config, the TT tile's die, top-edge pins
and PDN, LibreLane 3.0.14, then TT's precheck) with one extra LibreLane key:

  FP_OBSTRUCTIONS = [[x_cut, die_y0, die_x1, die_y1]]

LibreLane 3.0.14 turns each FP_OBSTRUCTIONS box into a hard placement blockage
(odb::dbBlockage_create in scripts/openroad/floorplan.tcl, before initialize_floorplan); cut_rows
then removes the rows under it, so no standard cell, tap, endcap or filler is placed there.
Routing and the PDN are not obstructed. The blockage runs from x_cut to the die's right edge
over the full die height, so nothing right of x_cut is placeable.

The fraction is measured against the TT template's core for the tile, a fixed geometry: the
die from tt-support-tools' tile_sizes.yaml minus TT's margins (6 sites left and right, 1 row
top and bottom, the "DO NOT CHANGE" block of TT's src/config.json). Neither the project's
config nor an overlay can move it: placeable fraction = (x_cut - core_x0) / core_width, with
x_cut on the 0.46 um site grid.

Every trial is cross-checked, and a mismatch aborts the search as a tool error:
  - the run's core area (summary.json core_um2) must equal the template core's area;
  - after a passing trial, the final DEF's ROWs must span exactly [core_x0, x_cut] by
    [core_y0, core_y1], so a core of the same area shifted by CORE_AREA fails loudly;
  - and no instance of that DEF may reach past x_cut (bounding boxes from the PDK's LEF
    macro sizes).

A trial passes when harden_tt.sh exits 0 (completed flow, magic DRC, route DRC, KLayout DRC via
the precheck decks, LVS, antenna, setup and hold at every corner, power grid, disconnected
pins, overlap, TT's precheck). Exit 1 there is a design failure at that fraction. Exit 3 is a
trial stopped by --trial-timeout: it counts as not signing off at that fraction (the search
moves up), is listed under "timeouts" in tilefit.json and is printed as a WARNING. The limit
applies per harden stage (LibreLane, then the precheck), so one trial can take up to twice it. Exit 2 (tool
error) or anything else aborts the whole search, so a broken tool never reads as a failing
fraction.

Placement density: trials keep the config's PL_TARGET_DENSITY_PCT (60 in TT's template). The
OpenROAD in LibreLane 3.0.14 raises the target itself when the free area is too small for it
(warning GPL-0302, "Automatically adjusting to uniform density"), so density is raised only
when the blockage forces it; each run records the target global placement actually used
(gpl_target_density). GPL-0301 (utilization above 100 %, counting OpenROAD's pin-density area
adjustment) means the cells do not fit at all, a design failure. If a future OpenROAD turned
GPL-0302 back into an error, harden_tt.sh would report a tool error and the search would stop.

Search: the full tile (fraction 1.0) is checked first; the lower bound is the synthesized cell
area over the core area (cells alone would fill the placeable area there, so it cannot pass).
Then --steps bisection steps; min_fraction is the smallest passing fraction found. Bisection
assumes pass/fail is monotonic in the fraction, which PnR noise can violate near the edge.

Usage:
  physical/tilefit.py [--project DIR] [--tiles 1x1] [--steps 6] [--trial-timeout 60]
                      [--out build/tilefit.json]
Output: build/tilefit.json {"min_fraction", "placeable_um2", "density", "timeouts",
                            "runs": [...], ...}
Exit status: 0 with a min_fraction and no timed-out trial; 1 when nothing signs off (not even
the full tile) and no trial timed out; 3 when any trial timed out (min_fraction may be inflated,
or null if the full tile timed out: "min_fraction_tainted": true, never a clean score); 2 on a
tool error or any crash. Each harden_tt.sh run takes the heavy lock itself, so do not wrap
this in lockf. --runs-dir must be inside this repo's build/ (harden_tt.sh's --out rule).
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SITE_W, SITE_H = 0.46, 2.72  # sky130_fd_sc_hd unithd
PDK_HASH = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"  # LibreLane 3.0.14's sky130 pin
CORE_TOL_UM2 = 0.5  # LibreLane rounds design__core__area to 0.1 um2
# TT template margins (src/config.json "DO NOT CHANGE" block: LEFT/RIGHT_MARGIN_MULT 6,
# TOP/BOTTOM_MARGIN_MULT 1), in sites and rows
TT_MARGINS = {"left": 6, "right": 6, "bottom": 1, "top": 1}
STATUS_BY_RC = {0: "pass", 1: "fail", 3: "timeout"}


def die(msg):
    print(f"tilefit.py: error: {msg}", file=sys.stderr)
    sys.exit(2)


def tile_die(tt_tools, tiles):
    for line in (tt_tools / "tech" / "sky130A" / "tile_sizes.yaml").read_text().splitlines():
        m = re.match(rf'^{re.escape(tiles)}:\s*"([^"]+)"', line)
        if m:
            return [float(v) for v in m.group(1).split()]
    die(f"unknown tile size {tiles}")


def template_core(die_box):
    """The TT template's core: LibreLane floorplan.tcl (FP_SIZING absolute, no CORE_AREA)
    with TT's fixed margins."""
    x0, y0, x1, y1 = die_box
    return [x0 + SITE_W * TT_MARGINS["left"], y0 + SITE_H * TT_MARGINS["bottom"],
            x1 - SITE_W * TT_MARGINS["right"], y1 - SITE_H * TT_MARGINS["top"]]


def info_field(info, key):
    m = re.search(rf'^\s*{key}\s*:\s*"?([A-Za-z0-9_x]+)"?', info, re.M)
    return m.group(1) if m else None


def lef_sizes(paths):
    """{macro: (width_um, height_um)} from LEF files."""
    sizes, cur = {}, None
    for p in paths:
        for line in Path(p).read_text(errors="replace").splitlines():
            m = re.match(r"^\s*MACRO\s+(\S+)", line)
            if m:
                cur = m.group(1)
                continue
            m = re.match(r"^\s*SIZE\s+([0-9.]+)\s+BY\s+([0-9.]+)", line)
            if m and cur and cur not in sizes:
                sizes[cur] = (float(m.group(1)), float(m.group(2)))
    return sizes


def def_dbu(def_text):
    m = re.search(r"^UNITS DISTANCE MICRONS (\d+)", def_text, re.M)
    return int(m.group(1)) if m else 1000


def def_overreach(def_text, sizes, x_cut_um):
    """Instances of a DEF whose bounding box reaches past x_cut: [(name, macro, right_um)].
    Raises ValueError for an instance whose macro size is unknown."""
    dbu = def_dbu(def_text)
    start, end = def_text.find("\nCOMPONENTS"), def_text.find("\nEND COMPONENTS")
    if start < 0 or end < 0:
        raise ValueError("no COMPONENTS section")
    limit = round(x_cut_um * dbu)
    bad = []
    for c in re.finditer(r"^\s*-\s+(\S+)\s+(\S+)\b[^;]*?\+\s*(?:PLACED|FIXED|COVER)\s*\(\s*(-?\d+)"
                         r"\s+(-?\d+)\s*\)\s*(\S+)", def_text[start:end], re.M):
        name, macro, x, orient = c.group(1), c.group(2), int(c.group(3)), c.group(5)
        if macro not in sizes:
            raise ValueError(f"unknown macro {macro} for instance {name}")
        w, h = sizes[macro]
        width = h if orient in ("E", "W", "FE", "FW") else w
        right = x + round(width * dbu)
        if right > limit:
            bad.append((name, macro, right / dbu))
    return bad


def def_row_extent(def_text, row_height_um=SITE_H):
    """[x0, y0, x1, y1] in um spanned by the DEF's horizontal ROWs (DO n BY 1 STEP s 0).
    Raises ValueError when there are no such rows."""
    dbu = def_dbu(def_text)
    xs0, ys0, xs1, ys1 = [], [], [], []
    for m in re.finditer(r"^ROW\s+\S+\s+\S+\s+(-?\d+)\s+(-?\d+)\s+\S+\s+DO\s+(\d+)\s+BY\s+1\s+"
                         r"STEP\s+(\d+)\s+0\s*;", def_text, re.M):
        x, y, n, step = (int(v) for v in m.groups())
        xs0.append(x)
        ys0.append(y)
        xs1.append(x + n * step)
        ys1.append(y + round(row_height_um * dbu))
    if not xs0:
        raise ValueError("no ROWs")
    return [min(xs0) / dbu, min(ys0) / dbu, max(xs1) / dbu, max(ys1) / dbu]


class Search:
    def __init__(self, a):
        self.a = a
        self.project = Path(a.project).resolve()
        self.outdir = Path(a.runs_dir).resolve()
        info = (self.project / "info.yaml").read_text()
        self.top = info_field(info, "top_module")
        self.tiles = a.tiles or info_field(info, "tiles") or "1x1"
        cfg = json.loads((self.project / "src" / "config.json").read_text())
        cfg.pop("//", None)
        self.base_density = float(cfg.get("PL_TARGET_DENSITY_PCT", 60))
        tt_tools = Path(os.environ.get("BONETTO_TT_ENV", REPO / "build" / "tt-env")) / "tt-support-tools"
        if not tt_tools.exists():
            subprocess.run([str(REPO / "tools" / "harden_tt.sh"), "--setup-only"], check=True)
        self.die = tile_die(tt_tools, self.tiles)
        self.core = template_core(self.die)
        self.core_w = self.core[2] - self.core[0]
        self.core_h = self.core[3] - self.core[1]
        pdk = Path(os.environ.get("PDK_ROOT", str(Path.home() / ".ciel"))) / "ciel" / "sky130" \
            / "versions" / PDK_HASH / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "lef"
        lefs = sorted(pdk.glob("*.lef"))
        if not lefs:
            die(f"no LEF files in {pdk}; run tools/harden_tt.sh --setup-only")
        self.sizes = lef_sizes(lefs)
        self.runs = []

    def geometry(self, fraction):
        sites = math.floor(fraction * self.core_w / SITE_W + 1e-9)
        width = min(self.core_w, sites * SITE_W)
        return self.core[0] + width, width / self.core_w, width * self.core_h

    def check_final_def(self, tag, run, x_cut):
        defs = sorted((run / "final" / "def").glob("*.def"))
        if len(defs) != 1:
            die(f"{tag}: expected one final DEF, found {defs}")
        text = defs[0].read_text()
        try:
            rows = def_row_extent(text)
            bad = def_overreach(text, self.sizes, x_cut)
        except ValueError as e:
            die(f"{tag}: cannot check the final DEF: {e}")
        want = [self.core[0], self.core[1], x_cut, self.core[3]]
        if any(abs(r - w) > 0.001 for r, w in zip(rows, want)):
            die(f"{tag}: final DEF rows span {rows}, expected {[round(v, 3) for v in want]} "
                f"(the TT template core cut at x_cut); the core moved or the blockage did not "
                f"hold")
        if bad:
            die(f"{tag}: {len(bad)} instances reach past x_cut={x_cut:.3f} um, e.g. "
                f"{bad[:3]}; the blockage did not hold")

    def harden(self, fraction, tag):
        x_cut, frac, placeable = self.geometry(fraction)
        overlay = {}
        if self.a.overlay:
            overlay.update(json.loads(Path(self.a.overlay).read_text()))
        if frac < 1.0:
            # from x_cut to the DIE edge, full die height: nothing right of x_cut is placeable
            # (appended to any obstructions a physical hypothesis's overlay already sets)
            overlay["FP_OBSTRUCTIONS"] = list(overlay.get("FP_OBSTRUCTIONS") or []) + [
                [round(x_cut, 3), round(self.die[1], 3), round(self.die[2], 3),
                 round(self.die[3], 3)]]
        out = self.outdir / tag
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").unlink(missing_ok=True)
        ov_path = self.outdir / f"{tag}.overlay.json"
        ov_path.write_text(json.dumps(overlay, indent=2) + "\n")
        cmd = [str(REPO / "tools" / "harden_tt.sh"), "--project", str(self.project),
               "--out", str(out), "--tiles", self.tiles, "--overlay", str(ov_path),
               "--timeout", str(round(self.a.trial_timeout * 60))]
        if self.a.keep_info_sources:
            cmd.append("--keep-info-sources")
        if self.a.no_precheck:
            cmd.append("--no-precheck")
        t0 = time.time()
        with open(self.outdir / f"{tag}.log", "w") as log:
            rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
        dt = round(time.time() - t0)
        sfile = out / "summary.json"
        if rc not in STATUS_BY_RC or not sfile.exists():
            die(f"harden_tt.sh tool error (exit {rc}) in {tag}; search aborted, see "
                f"{self.outdir / f'{tag}.log'} and {sfile}")
        s = json.loads(sfile.read_text())
        status = STATUS_BY_RC[rc]
        if s.get("status") != status:
            die(f"{tag}: harden exit {rc} disagrees with summary status {s.get('status')!r}")
        core = s.get("core_um2")
        want = self.core_w * self.core_h
        if core is None and rc == 0:
            die(f"{tag}: passing run reports no core area")
        if core is not None and abs(float(core) - want) > CORE_TOL_UM2:
            die(f"{tag}: run core area {core} um2 != TT template core {want:.2f} um2 (does the "
                f"config or overlay move the core?); the placeable fraction would be wrong")
        run = out / "proj" / "runs" / "wokwi"
        if rc == 0:
            self.check_final_def(tag, run, x_cut)
        gpl_density = None
        for gl in sorted(run.glob("*-openroad-globalplacement/openroad-globalplacement.log")):
            dens = re.findall(r"Placement target density:\s*([0-9.]+)", gl.read_text(errors="replace"))
            if dens:
                gpl_density = float(dens[0])
        rec = {
            "tag": tag, "fraction": round(frac, 5), "x_cut_um": round(x_cut, 3),
            "placeable_um2": round(placeable, 2), "target_density_pct": self.base_density,
            "status": status, "pass": rc == 0, "checks": s.get("checks"),
            "failure": s.get("failure") or s.get("timeout"), "evidence": s.get("evidence"),
            "utilization": s.get("utilization"), "stdcell_area_um2": s.get("stdcell_area_um2"),
            "repair_um2": s.get("repair_um2"), "hold_buffers": s.get("hold_buffers"),
            "setup_ws": s.get("setup_ws"), "hold_ws": s.get("hold_ws"),
            "gpl_target_density": gpl_density, "core_um2": core,
            "runtime_s": dt, "summary": str(sfile),
        }
        self.runs.append(rec)
        print(f"[tilefit] {tag}: fraction={rec['fraction']:.4f} status={status} ({dt} s)"
              + ("" if rec["pass"] else f" failure={str(rec['failure'])[:160]!r}"), flush=True)
        if status == "timeout":
            print(f"[tilefit] WARNING: {tag} hit the {self.a.trial_timeout} min trial timeout; "
                  f"it counts as not signing off at fraction {rec['fraction']:.4f}, but nothing "
                  f"proves the design fails there. See {sfile}", file=sys.stderr, flush=True)
        if not self.a.keep_runs and not rec["pass"]:
            shutil.rmtree(out / "proj" / "runs", ignore_errors=True)
        return rec

    def trial(self, fraction):
        return self.harden(fraction, f"f{fraction:.4f}")

    def run(self):
        lo = self.a.lo
        if lo is None:
            area = self.synth_area()
            lo = area / (self.core_w * self.core_h)
        hi = 1.0
        full = self.trial(hi)
        best = full if full["pass"] else None
        if best is None:
            print("[tilefit] the design does not sign off in the full tile", flush=True)
        else:
            for _ in range(self.a.steps):
                mid = (lo + hi) / 2
                if self.geometry(mid)[1] >= self.geometry(hi)[1]:
                    break  # below site-grid resolution
                rec = self.trial(mid)
                if rec["pass"]:
                    hi, best = mid, rec
                else:
                    lo = mid
        return best, lo

    def synth_area(self):
        cmd = [sys.executable, str(REPO / "tools" / "area.py"), "--top", self.top,
               "--src", str(self.project / "src"), "--no-hier",
               "--out", str(self.outdir / "area.json"),
               "--synth-dir", str(self.outdir / "synth")]
        if self.a.keep_info_sources:
            cmd += ["--info", str(self.project / "info.yaml")]
        if subprocess.run(cmd, stdout=subprocess.DEVNULL).returncode != 0:
            die("tools/area.py failed, cannot set the lower bound")
        return json.loads((self.outdir / "area.json").read_text())["area_um2"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", default=str(REPO))
    ap.add_argument("--tiles", help="tile size (default: info.yaml's, normally 1x1)")
    ap.add_argument("--steps", type=int, default=6, help="bisection steps after the full tile")
    ap.add_argument("--lo", type=float, help="known-failing fraction (default: synth area/core)")
    ap.add_argument("--overlay", help="extra LibreLane keys for every trial (physical hypotheses)")
    ap.add_argument("--out", default=str(REPO / "build" / "tilefit.json"))
    ap.add_argument("--runs-dir", default=str(REPO / "build" / "tilefit"))
    ap.add_argument("--trial-timeout", type=float, default=60.0,
                    help="minutes per trial, counted once harden_tt.sh holds the lock "
                         "(default 60)")
    ap.add_argument("--keep-info-sources", action="store_true",
                    help="use info.yaml's source list (default: src/ glob, like area.py)")
    ap.add_argument("--no-precheck", action="store_true", help="skip TT's precheck per trial")
    ap.add_argument("--keep-runs", action="store_true",
                    help="keep every run's LibreLane directory (default: only the min-fraction "
                         "run's; each is about 200 MB)")
    a = ap.parse_args()
    if a.trial_timeout <= 0:
        die("--trial-timeout must be positive")

    out = Path(a.out)
    out.unlink(missing_ok=True)  # no stale score survives a failed or aborted search
    t0 = time.time()
    s = Search(a)
    s.outdir.mkdir(parents=True, exist_ok=True)
    best, lo = s.run()
    if not a.keep_runs:  # keep the LibreLane run of the min-fraction trial only
        for r in s.runs:
            if best is None or r["tag"] != best["tag"]:
                shutil.rmtree(s.outdir / r["tag"] / "proj" / "runs", ignore_errors=True)
    density = None
    if best is not None and best.get("stdcell_area_um2"):
        density = round(float(best["stdcell_area_um2"]) / best["placeable_um2"], 4)
    timeouts = [r["tag"] for r in s.runs if r["status"] == "timeout"]
    res = {
        "min_fraction": best["fraction"] if best else None,
        "placeable_um2": best["placeable_um2"] if best else None,
        "density": density,
        "density_definition": "standard-cell area of the min-fraction run (LibreLane "
                              "design__instance__area__stdcell) / placeable_um2",
        "target_density_pct": best["target_density_pct"] if best else None,
        "repair_um2": best.get("repair_um2") if best else None,
        "hold_buffers": best.get("hold_buffers") if best else None,
        "highest_failing_fraction": max((r["fraction"] for r in s.runs if not r["pass"]),
                                        default=None),
        "timeouts": timeouts,
        "min_fraction_tainted": bool(timeouts),
        "warning": (f"{len(timeouts)} trial(s) hit the {a.trial_timeout} min timeout and were "
                    f"counted as not signing off: {timeouts}") if timeouts else None,
        "lower_bound_fraction": round(lo, 5),
        "tiles": s.tiles, "die_box_um": s.die, "core_box_um": [round(v, 3) for v in s.core],
        "core_um2": round(s.core_w * s.core_h, 2),
        "core_definition": "TT template core: die minus 6 sites left/right and 1 row top/bottom",
        "blockage": "FP_OBSTRUCTIONS hard placement blockage from x_cut to the die's right "
                    "edge, full die height",
        "top": s.top, "project": str(s.project), "steps": a.steps,
        "trial_timeout_min": a.trial_timeout,
        "precheck": not a.no_precheck,
        "runs": s.runs,
        "runtime_s": round(time.time() - t0),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2) + "\n")
    print(f"[tilefit] min_fraction={res['min_fraction']} placeable_um2={res['placeable_um2']} "
          f"density={density} runs={len(s.runs)} runtime_s={res['runtime_s']}")
    if timeouts:
        print(f"[tilefit] WARNING: {res['warning']}", file=sys.stderr)
    print(f"wrote {out}")
    if timeouts:
        sys.exit(3)         # a timeout may have moved the bisection up: never a clean score
    sys.exit(0 if best is not None else 1)


def entry():
    """Any crash is a tool error (2), never a verdict (1 means nothing signs off)."""
    try:
        main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 (KeyboardInterrupt included)
        traceback.print_exc()
        print("tilefit.py: error: crashed (tool error)", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    entry()
