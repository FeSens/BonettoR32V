#!/usr/bin/env python3
"""BonettoR32V physical score: the smallest share of the real TT tile the design signs off in.

Every trial is a full tools/harden_tt.sh run (tt_tool config, the TT tile's die, top-edge pins
and PDN, LibreLane 3.0.14, then TT's precheck) with one extra LibreLane key:

  FP_OBSTRUCTIONS = [[x_cut, core_y0, core_x1, core_y1]]

LibreLane 3.0.14 turns each FP_OBSTRUCTIONS box into a hard placement blockage
(odb::dbBlockage_create in scripts/openroad/floorplan.tcl, before initialize_floorplan); cut_rows
then removes the rows under it, so no standard cell, tap, endcap or filler is ever placed there.
Routing and the PDN are not obstructed. The blockage covers the right-hand part of the core;
the placeable fraction is (x_cut - core_x0) / core_width, with x_cut on the 0.46 um site grid.

A trial passes when harden_tt.sh's summary says pass: completed flow, magic DRC, route DRC,
KLayout DRC (precheck decks), LVS, antenna, setup and hold at every corner, power grid,
disconnected pins, and TT's precheck.

Placement density: TT's config sets PL_TARGET_DENSITY_PCT (60). When a trial's global placement
stops with GPL-0302 (target density below the utilization of the remaining area), the trial is
rerun once with the density OpenROAD suggests plus one point (capped at 100); GPL-0301
(utilization above 100 %) means the cells do not fit at all. So density is raised only when the
blockage forces it, and every run records the density it used.

Search: the full tile (fraction 1.0) is checked first; the lower bound is the synthesized cell
area over the core area (cells alone would fill the placeable area there, so it cannot pass).
Then --steps bisection steps; min_fraction is the smallest passing fraction found. Bisection
assumes pass/fail is monotonic in the fraction, which PnR noise can violate near the edge.

Usage:
  physical/tilefit.py [--project DIR] [--tiles 1x1] [--steps 6] [--out build/tilefit.json]
Output: build/tilefit.json {"min_fraction", "placeable_um2", "density", "runs": [...], ...}
Each harden_tt.sh run takes /tmp/bonetto-heavy.lock itself, so do not wrap this in lockf.
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
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SITE_W, SITE_H = 0.46, 2.72  # sky130_fd_sc_hd unithd


def die(msg):
    print(f"tilefit.py: error: {msg}", file=sys.stderr)
    sys.exit(2)


def tile_die(tt_tools, tiles):
    for line in (tt_tools / "tech" / "sky130A" / "tile_sizes.yaml").read_text().splitlines():
        m = re.match(rf'^{re.escape(tiles)}:\s*"([^"]+)"', line)
        if m:
            return [float(v) for v in m.group(1).split()]
    die(f"unknown tile size {tiles}")


def core_box(die_box, cfg):
    """LibreLane floorplan.tcl for FP_SIZING absolute without CORE_AREA."""
    x0, y0, x1, y1 = die_box
    return [x0 + SITE_W * cfg.get("LEFT_MARGIN_MULT", 12),
            y0 + SITE_H * cfg.get("BOTTOM_MARGIN_MULT", 4),
            x1 - SITE_W * cfg.get("RIGHT_MARGIN_MULT", 12),
            y1 - SITE_H * cfg.get("TOP_MARGIN_MULT", 4)]


def info_field(info, key):
    m = re.search(rf'^\s*{key}\s*:\s*"?([A-Za-z0-9_x]+)"?', info, re.M)
    return m.group(1) if m else None


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
        if a.overlay:
            cfg.update(json.loads(Path(a.overlay).read_text()))
        self.cfg = cfg
        self.base_density = float(cfg.get("PL_TARGET_DENSITY_PCT", 60))
        tt_tools = Path(os.environ.get("BONETTO_TT_ENV", REPO / "build" / "tt-env")) / "tt-support-tools"
        if not tt_tools.exists():
            subprocess.run([str(REPO / "tools" / "harden_tt.sh"), "--setup-only"], check=True)
        self.die = tile_die(tt_tools, self.tiles)
        self.core = core_box(self.die, cfg)
        self.core_w = self.core[2] - self.core[0]
        self.core_h = self.core[3] - self.core[1]
        self.runs = []

    def geometry(self, fraction):
        sites = math.floor(fraction * self.core_w / SITE_W + 1e-9)
        width = min(self.core_w, sites * SITE_W)
        return self.core[0] + width, width / self.core_w, width * self.core_h

    def harden(self, fraction, density, tag):
        x_cut, frac, placeable = self.geometry(fraction)
        overlay = {}
        if frac < 1.0:
            overlay["FP_OBSTRUCTIONS"] = [[round(x_cut, 3), round(self.core[1], 3),
                                          round(self.core[2], 3), round(self.core[3], 3)]]
        if density is not None:
            overlay["PL_TARGET_DENSITY_PCT"] = density
        if self.a.overlay:
            overlay = {**json.loads(Path(self.a.overlay).read_text()), **overlay}
        out = self.outdir / tag
        out.mkdir(parents=True, exist_ok=True)
        ov_path = self.outdir / f"{tag}.overlay.json"
        ov_path.write_text(json.dumps(overlay, indent=2) + "\n")
        cmd = [str(REPO / "tools" / "harden_tt.sh"), "--project", str(self.project),
               "--out", str(out), "--tiles", self.tiles, "--overlay", str(ov_path)]
        if self.a.keep_info_sources:
            cmd.append("--keep-info-sources")
        if self.a.no_precheck:
            cmd.append("--no-precheck")
        t0 = time.time()
        with open(self.outdir / f"{tag}.log", "w") as log:
            rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
        dt = round(time.time() - t0)
        sfile = out / "summary.json"
        if rc == 2 or not sfile.exists():
            die(f"harden_tt.sh tool error in {tag}, see {self.outdir / f'{tag}.log'}")
        s = json.loads(sfile.read_text())
        err = ""
        errlog = out / "proj" / "runs" / "wokwi" / "error.log"
        if errlog.exists():
            err = errlog.read_text(errors="replace")
        rec = {
            "tag": tag, "fraction": round(frac, 5), "x_cut_um": round(x_cut, 3),
            "placeable_um2": round(placeable, 2), "target_density_pct": density
            if density is not None else self.base_density,
            "pass": bool(s["pass"]), "checks": s["checks"], "failure": s.get("failure"),
            "utilization": s.get("utilization"), "stdcell_area_um2": s.get("stdcell_area_um2"),
            "setup_ws": s.get("setup_ws"), "hold_ws": s.get("hold_ws"),
            "runtime_s": dt, "summary": str(sfile),
        }
        self.runs.append(rec)
        print(f"[tilefit] {tag}: fraction={rec['fraction']:.4f} density={rec['target_density_pct']}"
              f" pass={rec['pass']} ({dt} s)" + ("" if rec["pass"] else
              f" failure={str(rec['failure'])[:160]!r}"), flush=True)
        if not self.a.keep_runs and not rec["pass"]:
            shutil.rmtree(out / "proj" / "runs", ignore_errors=True)
        return rec, err

    def trial(self, fraction):
        """One fraction; retry once with OpenROAD's suggested density on GPL-0302."""
        tag = f"f{fraction:.4f}"
        rec, err = self.harden(fraction, None, tag)
        if rec["pass"]:
            return rec
        m = re.search(r"[Ss]uggested target density:\s*([0-9.]+)", err)
        if "GPL-0302" in err and m:
            need = min(100, math.ceil(float(m.group(1)) * 100) + 1)
            if need > self.base_density:
                rec, _ = self.harden(fraction, need, f"{tag}-d{need}")
        return rec

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
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
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
    ap.add_argument("--keep-info-sources", action="store_true",
                    help="use info.yaml's source list (default: src/ glob, like area.py)")
    ap.add_argument("--no-precheck", action="store_true", help="skip TT's precheck per trial")
    ap.add_argument("--keep-runs", action="store_true", help="keep failing runs' LibreLane dirs")
    a = ap.parse_args()

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
    res = {
        "min_fraction": best["fraction"] if best else None,
        "placeable_um2": best["placeable_um2"] if best else None,
        "density": density,
        "density_definition": "standard-cell area of the min-fraction run (LibreLane "
                              "design__instance__area__stdcell) / placeable_um2",
        "target_density_pct": best["target_density_pct"] if best else None,
        "highest_failing_fraction": max((r["fraction"] for r in s.runs if not r["pass"]),
                                        default=None),
        "lower_bound_fraction": round(lo, 5),
        "tiles": s.tiles, "die_box_um": s.die, "core_box_um": [round(v, 3) for v in s.core],
        "core_um2": round(s.core_w * s.core_h, 2),
        "blockage": "FP_OBSTRUCTIONS hard placement blockage over the right part of the core",
        "top": s.top, "project": str(s.project), "steps": a.steps,
        "precheck": not a.no_precheck,
        "runs": s.runs,
        "runtime_s": round(time.time() - t0),
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2) + "\n")
    print(f"[tilefit] min_fraction={res['min_fraction']} placeable_um2={res['placeable_um2']} "
          f"density={density} runs={len(s.runs)} runtime_s={res['runtime_s']}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
