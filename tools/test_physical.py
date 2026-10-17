#!/usr/bin/env python3
"""Fixture tests for the scoring tools: tools/area.py, tools/harden_tt.sh (with
tools/harden_summary.py) and physical/tilefit.py.

None of them takes the heavy lock: LibreLane is never run here. harden_summary.py is run on
synthetic run directories (and, when present, on the real runs kept under build/), harden_tt.sh
only up to its argument and --out checks, and tilefit with a stub in place of harden_tt.sh.
Tests that need the local PDK, yosys or Docker skip when it is missing.

  python3 -m unittest tools/test_physical.py -v
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
AREA = REPO / "tools" / "area.py"
HARDEN = REPO / "tools" / "harden_tt.sh"
SUMMARY = REPO / "tools" / "harden_summary.py"
PDK_HASH = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"
PDK = Path(os.environ.get("PDK_ROOT", str(Path.home() / ".ciel"))) / "ciel" / "sky130" \
    / "versions" / PDK_HASH / "sky130A"
TT_ENV = Path(os.environ.get("BONETTO_TT_ENV", REPO / "build" / "tt-env"))
HAVE_PDK = (PDK / "libs.ref" / "sky130_fd_sc_hd" / "lef").is_dir()
HAVE_YOSYS = shutil.which("yosys") is not None


def docker_up():
    try:
        return subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


TT_PORTS = ("input wire [7:0] ui_in, output wire [7:0] uo_out, input wire [7:0] uio_in, "
            "output wire [7:0] uio_out, output wire [7:0] uio_oe, input wire ena, "
            "input wire clk, input wire rst_n")
COMB_LOOP = f"""module tt_um_t ({TT_PORTS});
  wire a, b;
  assign a = b ^ ui_in[0];
  assign b = a & ui_in[1];
  assign uo_out = {{7'b0, a}};
  assign uio_out = 8'b0;
  assign uio_oe = 8'b0;
endmodule
"""
CLEAN = f"""module tt_um_t ({TT_PORTS});
  reg [7:0] q;
  always @(posedge clk) q <= rst_n ? q + ui_in : 8'd0;
  assign uo_out = q;
  assign uio_out = 8'b0;
  assign uio_oe = 8'b0;
endmodule
"""


# ------------------------------------------------------------------------------ area.py
class AreaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="area-test-"))
        self.src = self.tmp / "src"
        self.src.mkdir()
        self.out = self.tmp / "area.json"
        self.out.write_text('{"stale": true}\n')  # must be gone after any failed run

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def area(self, *extra, env=None):
        cmd = [sys.executable, str(AREA), "--top", "tt_um_t", "--src", str(self.src),
               "--out", str(self.out), "--synth-dir", str(self.tmp / "synth"), "--no-hier",
               *extra]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                              env={**os.environ, **(env or {})})

    def assertDies(self, r, needle):
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(needle, r.stderr)
        self.assertFalse(self.out.exists(), "a failed run left area.json behind")

    @unittest.skipUnless(HAVE_PDK, "no local PDK")
    def test_injection_file_name_refused(self):
        (self.src / "tt_um_t.v").write_text(CLEAN)
        (self.src / "z.v; log X; log y.v").write_text("// x\n")
        self.assertDies(self.area(), "refusing source name")

    @unittest.skipUnless(HAVE_PDK, "no local PDK")
    def test_sv_and_subdir_refused(self):
        (self.src / "tt_um_t.v").write_text(CLEAN)
        (self.src / "x.sv").write_text("module x; endmodule\n")
        self.assertDies(self.area(), "refusing source name")
        (self.src / "x.sv").unlink()
        (self.src / "sub").mkdir()
        (self.src / "sub" / "y.v").write_text("module y; endmodule\n")
        self.assertDies(self.area(), "refusing source name")

    @unittest.skipUnless(HAVE_PDK, "no local PDK")
    def test_bad_top_and_param_refused(self):
        (self.src / "tt_um_t.v").write_text(CLEAN)
        r = subprocess.run([sys.executable, str(AREA), "--top", "x; exec -- id",
                            "--src", str(self.src), "--out", str(self.out)],
                           capture_output=True, text=True)
        self.assertDies(r, "bad --top")
        self.assertDies(self.area("--param", "W=1; exec -- id"), "bad --param")

    def test_missing_pinned_pdk_dies(self):
        (self.src / "tt_um_t.v").write_text(CLEAN)
        self.assertDies(self.area(env={"PDK_ROOT": str(self.tmp / "nopdk")}), "missing")

    def test_unreadable_pdk_version_dies(self):
        fake = self.tmp / "pdk" / "ciel" / "sky130" / "versions" / PDK_HASH / "sky130A"
        lib = fake / "libs.ref" / "sky130_fd_sc_hd" / "lib"
        lib.mkdir(parents=True)
        (lib / "sky130_fd_sc_hd__tt_025C_1v80.lib").write_text("library (x) { }\n")
        (self.src / "tt_um_t.v").write_text(CLEAN)
        # no SOURCES file: the version check cannot run, so no score
        self.assertDies(self.area(env={"PDK_ROOT": str(self.tmp / "pdk")}), "cannot check")
        (fake / "SOURCES").write_text("open_pdks 0000000000000000000000000000000000000000\n")
        self.assertDies(self.area(env={"PDK_ROOT": str(self.tmp / "pdk")}), "is not open_pdks")

    @unittest.skipUnless(HAVE_PDK and HAVE_YOSYS, "needs the local PDK and yosys on PATH")
    def test_comb_loop_dies_yosys_engine(self):
        (self.src / "tt_um_t.v").write_text(COMB_LOOP)
        self.assertDies(self.area("--engine", "yosys"), "synthesis checks failed")

    @unittest.skipUnless(HAVE_PDK and HAVE_YOSYS, "needs the local PDK and yosys on PATH")
    def test_clean_design_scores_yosys_engine(self):
        (self.src / "tt_um_t.v").write_text(CLEAN)
        r = self.area("--engine", "yosys")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = json.loads(self.out.read_text())
        self.assertEqual(res["synth_checks"], {"synthesis__check_error__count": 0,
                                               "design__instance_unmapped__count": 0})
        self.assertGreater(res["area_um2"], 0)

    @unittest.skipUnless(HAVE_PDK and docker_up(), "needs the local PDK and Docker")
    def test_comb_loop_dies_librelane_engine(self):
        (self.src / "tt_um_t.v").write_text(COMB_LOOP)
        self.assertDies(self.area(), "synthesis checks failed")


# ------------------------------------------------------------------- harden_summary.py
SIGNOFF_OK = {
    "magic__drc_error__count": 0, "route__drc_errors": 0, "design__lvs_error__count": 0,
    "antenna__violating__nets": 0, "antenna__violating__pins": 0,
    "timing__setup_vio__count": 0, "timing__hold_vio__count": 0,
    "timing__setup__ws": 12.5, "timing__hold__ws": 0.11,
    "design__power_grid_violation__count": 0, "design__critical_disconnected_pin__count": 0,
    "magic__illegal_overlap__count": 0, "design__core__area": 16493.3,
    "design__instance__count__hold_buffer": 2,
}
PRECHECK_OK = ["Magic DRC", "KLayout FEOL", "KLayout BEOL", "KLayout offgrid", "Pin check"]
LIB = """library (t) {
  cell ("sky130_fd_sc_hd__buf_2") {
    area : 3.7536;
  }
  cell ("sky130_fd_sc_hd__dlygate4sd3_1") {
    area : 8.7584;
  }
  cell ("sky130_fd_sc_hd__inv_2") {
    area : 3.7536;
  }
}
"""


def netlist(cells):
    body = "".join(f"  {c} _{i}_ ( .A(x) );\n" for i, c in enumerate(cells))
    return f"module tt_um_t (x);\n{body}endmodule\n"


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="summary-test-"))
        self.out = self.tmp / "out"
        self.proj = self.out / "proj"
        self.run = self.proj / "runs" / "wokwi"
        self.out.mkdir()
        self.lib = self.tmp / "t.lib"
        self.lib.write_text(LIB)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, steps=("01-verilator-lint", "06-yosys-synthesis", "30-openroad-gpl"),
             final=None, errlog="", lllog="", rows=None, match=True):
        for s in steps:
            (self.run / s).mkdir(parents=True, exist_ok=True)
        if steps:
            (self.run / "06-yosys-synthesis").mkdir(parents=True, exist_ok=True)
            (self.run / "06-yosys-synthesis" / "tt_um_t.nl.v").write_text(
                netlist(["sky130_fd_sc_hd__inv_2", "sky130_fd_sc_hd__buf_2"]))
        if errlog:
            (self.run / "error.log").write_text(errlog)
        (self.out / "librelane.log").write_text(lllog)
        (self.out / "sources.json").write_text(json.dumps(
            {"used": ["a.v"], "committed": ["a.v"] if match else ["b.v"], "match": match}))
        if final is not None:
            (self.run / "final" / "nl").mkdir(parents=True)
            (self.run / "final" / "metrics.json").write_text(json.dumps(final))
            (self.run / "final" / "nl" / "tt_um_t.nl.v").write_text(netlist(
                ["sky130_fd_sc_hd__inv_2"] + ["sky130_fd_sc_hd__buf_2"] * 4
                + ["sky130_fd_sc_hd__dlygate4sd3_1"] * 2))
        if rows is not None:
            rep = self.proj / "tt" / "precheck" / "reports"
            rep.mkdir(parents=True)
            (rep / "results.md").write_text(
                "# Tiny Tapeout Precheck Results\n\n| Check | Result |\n|-----------|--------|\n"
                + "".join(f"| {k} | {v} |\n" for k, v in rows.items()))

    def summarize(self, ll, sub=-1, pre=-1, out=None, proj=None):
        out, proj = out or self.out, proj or self.proj
        r = subprocess.run([sys.executable, "-I", str(SUMMARY), str(out), str(proj), "tt_um_t",
                            str(ll), str(sub), str(pre), "10", "5", str(self.lib)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads((out / "summary.json").read_text())

    def test_killed_librelane_is_tool_error(self):
        self.make(errlog="[ERROR GPL-0301] Utilization 104 % exceeds 100%.\n")
        for rc in (125, 126, 127, 137, 143):
            s = self.summarize(rc)
            self.assertEqual(s["status"], "tool_error", rc)  # even with GPL-0301 in the log

    def test_no_steps_is_tool_error(self):  # e.g. the Docker daemon is down (rc 1)
        self.make(steps=(), lllog="Cannot connect to the Docker daemon\n")
        s = self.summarize(1)
        self.assertEqual(s["status"], "tool_error")
        self.assertIn("no flow steps", s["tool_error"])

    def test_gpl0301_is_design_failure(self):
        self.make(errlog="[ERROR GPL-0301] Utilization 104.491 % exceeds 100%.\n")
        s = self.summarize(2)
        self.assertEqual(s["status"], "fail")
        self.assertIn("GPL-0301", s["failure"])

    def test_routing_overflow_is_design_failure(self):
        self.make(errlog="[ERROR GRT-0116] Global routing finished with congestion.\n")
        self.assertEqual(self.summarize(2)["status"], "fail")

    def test_unknown_librelane_failure_is_tool_error(self):
        self.make(errlog="Traceback (most recent call last):\nKeyError: 'x'\n")
        s = self.summarize(1)
        self.assertEqual(s["status"], "tool_error")
        self.assertIn("without signoff evidence", s["tool_error"])

    def test_deferred_magic_drc_is_design_failure(self):
        self.make(final={**SIGNOFF_OK, "magic__drc_error__count": 3},
                  errlog="3 Magic DRC errors found\n")
        s = self.summarize(2)
        self.assertEqual(s["status"], "fail")
        self.assertIn("magic_drc", s["evidence"])
        self.assertFalse(s["checks"]["magic_drc"])

    def test_clean_pass_and_repair_fields(self):
        self.make(final=SIGNOFF_OK, rows={k: "✅" for k in PRECHECK_OK}, match=False)
        s = self.summarize(0, 0, 0)
        self.assertEqual(s["status"], "pass", s)
        self.assertTrue(all(s["checks"].values()), s["checks"])
        # final has 3 more buf_2 and 2 more dlygate4sd3_1 than the synthesized netlist
        self.assertEqual(s["repair_cells"], 5)
        self.assertAlmostEqual(s["repair_um2"], round(3 * 3.7536 + 2 * 8.7584, 2))
        self.assertEqual(s["repair_by_type"], {"sky130_fd_sc_hd__buf_2": 3,
                                               "sky130_fd_sc_hd__dlygate4sd3_1": 2})
        self.assertEqual(s["hold_buffers"], 2)
        self.assertIs(s["info_sources_match"], False)

    def test_no_precheck_pass(self):
        self.make(final=SIGNOFF_OK)
        s = self.summarize(0, 0, -2)
        self.assertEqual(s["status"], "pass")
        self.assertIsNone(s["checks"]["precheck"])

    def test_submission_failure_is_tool_error(self):
        self.make(final=SIGNOFF_OK)
        s = self.summarize(0, 1, -1)
        self.assertEqual(s["status"], "tool_error")
        self.assertIn("create-tt-submission", s["tool_error"])

    def test_precheck_without_results_is_tool_error(self):
        self.make(final=SIGNOFF_OK)
        for pre in (0, 1):
            s = self.summarize(0, 0, pre)
            self.assertEqual(s["status"], "tool_error", pre)
            self.assertIn("precheck wrote no results", s["tool_error"])

    def test_precheck_failing_row_is_design_failure(self):
        rows = {k: "✅" for k in PRECHECK_OK}
        rows["KLayout BEOL"] = "❌ Fail"
        self.make(final=SIGNOFF_OK, rows=rows)
        s = self.summarize(0, 0, 1)
        self.assertEqual(s["status"], "fail")
        self.assertIn("precheck: KLayout BEOL", s["evidence"])

    def test_precheck_nonzero_exit_with_all_rows_passing_is_tool_error(self):
        self.make(final=SIGNOFF_OK, rows={k: "✅" for k in PRECHECK_OK})
        self.assertEqual(self.summarize(0, 0, 1)["status"], "tool_error")

    def test_missing_overlap_metric_fails_its_check(self):
        final = dict(SIGNOFF_OK)
        del final["magic__illegal_overlap__count"]
        self.make(final=final, rows={k: "✅" for k in PRECHECK_OK})
        s = self.summarize(0, 0, 0)
        self.assertFalse(s["checks"]["illegal_overlap"])
        self.assertEqual(s["status"], "tool_error")
        self.assertIn("illegal_overlap", s["tool_error"])

    def test_real_runs_under_build(self):
        """The runs kept from setup, reclassified: NanoV 1x1 (GPL-0301, exit 2) fails; the seed
        in 2x2 (stopped by hand, exit 137) is a tool error; NanoV 1x2 and the seed 3x2 pass."""
        cases = [("harden-nanov", 2, -1, -1, "fail"),
                 ("harden-seed-2x2", 137, -1, -1, "tool_error"),
                 ("harden-nanov-1x2", 0, 0, 0, "pass"),
                 ("harden-seed-3x2", 0, 0, 0, "pass")]
        ran = 0
        for name, ll, sub, pre, want in cases:
            d = REPO / "build" / name
            if not (d / "proj" / "runs" / "wokwi").is_dir():
                continue
            out = self.tmp / name
            out.mkdir()
            shutil.copy(d / "librelane.log", out / "librelane.log")
            top = re.search(r'top_module\s*:\s*"?([A-Za-z0-9_]+)',
                            (d / "proj" / "info.yaml").read_text()).group(1)
            lib = PDK / "libs.ref" / "sky130_fd_sc_hd" / "lib" / "sky130_fd_sc_hd__tt_025C_1v80.lib"
            r = subprocess.run([sys.executable, "-I", str(SUMMARY), str(out), str(d / "proj"),
                                top, str(ll), str(sub), str(pre), "1", "1", str(lib)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            s = json.loads((out / "summary.json").read_text())
            self.assertEqual(s["status"], want, (name, s["tool_error"], s["failure"]))
            if want == "pass":
                self.assertIsNotNone(s["repair_um2"])
                self.assertIsNotNone(s["hold_buffers"])
            print(f"\n  {name}: {s['status']} repair_um2={s['repair_um2']} "
                  f"repair_cells={s['repair_cells']} hold_buffers={s['hold_buffers']} "
                  f"{(s['tool_error'] or s['failure'] or '')[:90]}", end="")
            ran += 1
        if not ran:
            self.skipTest("no kept runs under build/")


# --------------------------------------------------------------- harden_tt.sh arguments
class HardenArgsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harden-args-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def harden(self, *args, env=None):
        # BONETTO_HEAVY_LOCKED=1: these calls all stop before LibreLane, so no lock is needed
        return subprocess.run(["bash", str(HARDEN), *args], capture_output=True, text=True,
                              timeout=300, env={**os.environ, "BONETTO_HEAVY_LOCKED": "1",
                                                **(env or {})})

    def assertRefused(self, r, needle="refusing"):
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(needle, r.stderr)

    def test_out_guard(self):
        home = Path.home()
        for out in ["/", str(home), str(REPO), str(REPO.parent), str(REPO / "docs"),
                    str(REPO / "src" / "x"), str(REPO / "build" / ".." / "src")]:
            self.assertRefused(self.harden("--out", out))
            self.assertRefused(self.harden(f"--out={out}"))
        self.assertTrue((REPO / "src").is_dir() and (REPO / "docs").is_dir())
        self.assertFalse((REPO / "src" / "x").exists())

    def test_out_guard_symlink_to_project(self):
        link = self.tmp / "link"
        link.symlink_to(REPO)
        self.assertRefused(self.harden("--out", str(link)))

    def test_out_guard_unrelated_nonempty_dir(self):
        d = self.tmp / "mine"
        d.mkdir()
        (d / "keep.txt").write_text("precious\n")
        self.assertRefused(self.harden("--out", str(d)), "not a harden_tt.sh output")
        self.assertEqual((d / "keep.txt").read_text(), "precious\n")

    def test_tiles_forms(self):
        self.assertRefused(self.harden("--tiles=3x2x", "--setup-only"), "bad --tiles")
        self.assertRefused(self.harden("--tiles", "3x2; id", "--setup-only"), "bad --tiles")
        self.assertRefused(self.harden("--tiles"), "needs a value")

    @unittest.skipUnless((TT_ENV / "tt-support-tools").is_dir() and HAVE_PDK,
                         "needs tools/harden_tt.sh --setup-only done")
    def test_tiles_equals_form_accepted(self):
        for args in (["--tiles=3x2"], ["--tiles", "3x2"]):
            r = self.harden(*args, "--setup-only")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("setup done", r.stderr)

    @unittest.skipUnless((TT_ENV / "tt-support-tools").is_dir(),
                         "needs tools/harden_tt.sh --setup-only done")
    def test_stale_summary_wiped_before_setup_and_setup_failure_is_tool_error(self):
        out = self.tmp / "out"
        out.mkdir()
        (out / ".harden_tt_out").touch()
        (out / "summary.json").write_text('{"status": "pass"}\n')
        # setup fails creating the PDK root on the read-only system volume
        r = self.harden("--out", str(out), env={"PDK_ROOT": "/nonexistent/pdk"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertFalse((out / "summary.json").exists())


# ------------------------------------------------------------------------- tilefit.py
def load_tilefit():
    spec = importlib.util.spec_from_file_location("tilefit", REPO / "physical" / "tilefit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LEF = """MACRO sky130_fd_sc_hd__inv_2
  CLASS CORE ;
  SIZE 1.380 BY 2.720 ;
END sky130_fd_sc_hd__inv_2
MACRO sky130_fd_sc_hd__tapvpwrvgnd_1
  SIZE 0.460 BY 2.720 ;
END sky130_fd_sc_hd__tapvpwrvgnd_1
"""


def def_text(comps):
    lines = "".join(f"    - {n} {m} + PLACED ( {x} {y} ) {o} ;\n" for n, m, x, y, o in comps)
    return (f"VERSION 5.8 ;\nDESIGN t ;\nUNITS DISTANCE MICRONS 1000 ;\n"
            f"COMPONENTS {len(comps)} ;\n{lines}END COMPONENTS\nEND DESIGN\n")


class TilefitScanTest(unittest.TestCase):
    def setUp(self):
        self.tf = load_tilefit()
        self.tmp = Path(tempfile.mkdtemp(prefix="tilefit-scan-"))
        (self.tmp / "t.lef").write_text(LEF)
        self.sizes = self.tf.lef_sizes([self.tmp / "t.lef"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lef_sizes(self):
        self.assertEqual(self.sizes["sky130_fd_sc_hd__inv_2"], (1.38, 2.72))
        self.assertEqual(self.sizes["sky130_fd_sc_hd__tapvpwrvgnd_1"], (0.46, 2.72))

    def test_overreach(self):
        inv, tap = "sky130_fd_sc_hd__inv_2", "sky130_fd_sc_hd__tapvpwrvgnd_1"
        ok = [("a", inv, 98620, 2720, "N"), ("b", tap, 99540, 5440, "FS")]  # right edge 100.0
        self.assertEqual(self.tf.def_overreach(def_text(ok), self.sizes, 100.0), [])
        bad = ok + [("c", inv, 99080, 2720, "FN")]  # right edge 100.46
        self.assertEqual(self.tf.def_overreach(def_text(bad), self.sizes, 100.0),
                         [("c", inv, 100.46)])
        rot = [("d", inv, 97500, 2720, "E")]  # rotated: width 2.72, right edge 100.22
        self.assertEqual(len(self.tf.def_overreach(def_text(rot), self.sizes, 100.0)), 1)
        with self.assertRaises(ValueError):
            self.tf.def_overreach(def_text([("e", "mystery", 0, 0, "N")]), self.sizes, 100.0)
        with self.assertRaises(ValueError):
            self.tf.def_overreach("VERSION 5.8 ;\n", self.sizes, 100.0)

    @unittest.skipUnless(HAVE_PDK, "no local PDK")
    def test_real_def(self):
        """The kept smoke run (fraction 0.7574, x_cut 120.52): nothing past x_cut, while a
        cut at 60 um finds instances."""
        defs = sorted((REPO / "build" / "tilefit-smoke").glob(
            "*/proj/runs/wokwi/final/def/*.def"))
        if not defs:
            self.skipTest("no kept tilefit run")
        sizes = self.tf.lef_sizes(sorted((PDK / "libs.ref" / "sky130_fd_sc_hd" / "lef").glob(
            "*.lef")))
        text = defs[0].read_text()
        self.assertEqual(self.tf.def_overreach(text, sizes, 120.52), [])
        self.assertGreater(len(self.tf.def_overreach(text, sizes, 60.0)), 100)


@unittest.skipUnless(HAVE_PDK and (TT_ENV / "tt-support-tools").is_dir(),
                     "needs the local PDK and tools/harden_tt.sh --setup-only done")
class TilefitSearchTest(unittest.TestCase):
    """tilefit with a stub in place of harden_tt.sh (subprocess.run patched)."""

    def setUp(self):
        self.tf = load_tilefit()
        self.tmp = Path(tempfile.mkdtemp(prefix="tilefit-search-"))
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stub(self, verdict):
        """verdict(fraction, overlay) -> (rc, summary dict or None, def instances or None)."""
        tf = self.tf

        def run(cmd, **kw):
            out = Path(cmd[cmd.index("--out") + 1])
            overlay = json.loads(Path(cmd[cmd.index("--overlay") + 1]).read_text())
            self.calls.append(overlay)
            obs = overlay.get("FP_OBSTRUCTIONS")
            x_cut = obs[-1][0] if obs else None
            rc, summary, comps = verdict(x_cut, overlay)
            if summary is not None:
                out.mkdir(parents=True, exist_ok=True)
                (out / "summary.json").write_text(json.dumps(summary))
            if comps is not None:
                d = out / "proj" / "runs" / "wokwi" / "final" / "def"
                d.mkdir(parents=True, exist_ok=True)
                (d / "tt_um_t.def").write_text(def_text(comps))
            return subprocess.CompletedProcess(cmd, rc)
        return mock.patch.object(tf.subprocess, "run", side_effect=run)

    def args(self, *extra):
        return ["tilefit.py", "--project", str(REPO), "--runs-dir", str(self.tmp / "runs"),
                "--out", str(self.tmp / "tilefit.json"), "--tiles", "1x1", "--lo", "0.3", *extra]

    def summary(self, ok, core=16493.3):
        return {"status": "pass" if ok else "fail", "checks": {}, "core_um2": core,
                "failure": None if ok else "GPL-0301", "stdcell_area_um2": 5000.0}

    def main(self, verdict, *extra):
        (self.tmp / "tilefit.json").write_text('{"stale": true}\n')
        with self.stub(verdict), mock.patch.object(sys, "argv", self.args(*extra)):
            with self.assertRaises(SystemExit) as cm:
                self.tf.main()
        res = self.tmp / "tilefit.json"
        return cm.exception.code, json.loads(res.read_text()) if res.exists() else None

    def inside(self, x_cut):  # one cell flush against x_cut
        return [("a", "sky130_fd_sc_hd__inv_2", round(x_cut * 1000) - 1380, 2720, "N")]

    def test_obstruction_spans_die_edge_and_search_converges(self):
        def verdict(x_cut, overlay):
            ok = x_cut is None or x_cut >= 110.0
            return (0 if ok else 1), self.summary(ok), (self.inside(x_cut) if ok and x_cut else None)
        code, res = self.main(verdict)
        self.assertEqual(code, 0)
        self.assertIsNone(self.calls[0].get("FP_OBSTRUCTIONS"))  # full tile first
        die = [0.0, 0.0, 161.0, 111.52]
        for ov in self.calls[1:]:
            x0, y0, x1, y1 = ov["FP_OBSTRUCTIONS"][-1]
            self.assertEqual([y0, x1, y1], die[1:], "blockage must reach the die edge")
            sites = (x0 - 2.76) / 0.46
            self.assertAlmostEqual(sites, round(sites), places=4)  # on the site grid
        self.assertGreaterEqual(res["min_fraction"], (110.0 - 2.76) / 155.48 - 1e-4)
        self.assertLess(res["min_fraction"], 0.72)

    def test_user_obstructions_kept(self):
        ov = self.tmp / "ov.json"
        ov.write_text(json.dumps({"FP_OBSTRUCTIONS": [[10, 10, 20, 20]]}))

        def verdict(x_cut, overlay):
            return 0, self.summary(True), self.inside(x_cut) if x_cut else None
        code, _ = self.main(verdict, "--overlay", str(ov), "--steps", "1")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[1]["FP_OBSTRUCTIONS"][0], [10, 10, 20, 20])
        self.assertEqual(len(self.calls[1]["FP_OBSTRUCTIONS"]), 2)

    def test_nothing_signs_off_exits_1(self):
        code, res = self.main(lambda x, o: (1, self.summary(False), None))
        self.assertEqual(code, 1)
        self.assertIsNone(res["min_fraction"])
        self.assertEqual(len(self.calls), 1)

    def test_tool_error_aborts_search(self):
        for rc in (2, 137):
            self.calls.clear()
            code, res = self.main(lambda x, o, rc=rc: (rc, {"status": "tool_error"}, None))
            self.assertEqual(code, 2)
            self.assertIsNone(res, "stale tilefit.json survived")
            self.assertEqual(len(self.calls), 1)

    def test_missing_summary_aborts(self):
        code, res = self.main(lambda x, o: (1, None, None))
        self.assertEqual((code, res), (2, None))

    def test_status_disagreeing_with_exit_aborts(self):
        code, _ = self.main(lambda x, o: (1, self.summary(True), None))
        self.assertEqual(code, 2)

    def test_core_area_mismatch_aborts(self):
        code, _ = self.main(lambda x, o: (0, self.summary(True, core=17000.0), None))
        self.assertEqual(code, 2)

    def test_instance_past_cut_aborts(self):
        def verdict(x_cut, overlay):
            comps = None
            if x_cut:
                comps = [("z", "sky130_fd_sc_hd__inv_2", round(x_cut * 1000) - 920, 2720, "N")]
            return 0, self.summary(True), comps
        code, res = self.main(verdict)
        self.assertEqual((code, res), (2, None))
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
