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
             final=None, errlog="", lllog="", rows=None, match=True, xml=None, prelog="",
             magic_txt=None):
        """rows: results.md; xml: {row: (message, exception type)} for results.xml, by
        default a PrecheckFailure with the row's message for every failing row ("none": no
        results.xml at all)."""
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
            if xml != "none":
                errs = xml if xml is not None else {
                    k: (v.split("Fail: ", 1)[-1], "precheck_failure.PrecheckFailure")
                    for k, v in rows.items() if "\u2705" not in v}
                cases = ""
                for k in rows:
                    if k in errs:
                        msg, etype = errs[k]
                        tb = ("Traceback (most recent call last):\n  File \"precheck.py\", "
                              f"line 575, in main\n    check[\"check\"]()\n{etype}: {msg}")
                        cases += (f'    <testcase name="{k}" time="1.0">\n      <error '
                                  f'message="{msg}">{tb}</error>\n    </testcase>\n')
                    else:
                        cases += f'    <testcase name="{k}" time="1.0" />\n'
                (rep / "results.xml").write_text(
                    '<testsuites>\n  <testsuite name="Tiny Tapeout Prechecks">\n'
                    f"{cases}  </testsuite>\n</testsuites>\n")
            if magic_txt is not None:
                (rep / "magic_drc.txt").write_text(magic_txt)
        (self.out / "precheck.log").write_text(prelog)

    def summarize(self, ll, sub=-1, pre=-1, out=None, proj=None, timed_out=""):
        out, proj = out or self.out, proj or self.proj
        r = subprocess.run([sys.executable, "-I", str(SUMMARY), str(out), str(proj), "tt_um_t",
                            str(ll), str(sub), str(pre), "10", "5", str(self.lib), timed_out],
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

    def test_grt0116_is_not_evidence(self):  # never emitted under GRT_ALLOW_CONGESTION
        self.make(errlog="[GRT-0116] Global routing finished with congestion.\n")
        self.assertEqual(self.summarize(2)["status"], "tool_error")

    def test_design_caused_openroad_and_checker_errors(self):
        cases = [
            "[GPL-0307] Divergence detected between consecutive iterations",
            "[DPL-0036] Detailed placement failed.",
            "[RSZ-0060] Max buffer count reached.",
            "[RSZ-0064] Unable to repair all hold violations.",
            "Timing constructs found in the RTL. Please remove them or wrap them around an "
            "ifdef.",
            "Setup violations found in the following corners:\n* max_ss_100C_1v60",
            "2 Lint errors found.",
            "7 power grid violations (as reported by OpenROAD PSM- you may ignore these if LVS "
            "passes) found. - deferred",
        ]
        for msg in cases:
            shutil.rmtree(self.out)
            self.out.mkdir()
            self.make(errlog=msg + "\n")
            s = self.summarize(2)
            self.assertEqual(s["status"], "fail", (msg, s["tool_error"]))

    def test_evidence_only_from_error_level_text(self):
        # design identifiers and INFO lines in the console log are never evidence
        lllog = ("[INFO GPL-0019] Utilization: 104.491 %\n"
                 "module Detailed_placement_failed; // Max buffer count reached\n"
                 "[05:16:52] INFO     2 Lint errors found in a comment        flow.py:1\n"
                 "Utilization 104.491 % exceeds 100%.\n")
        self.make(lllog=lllog)
        self.assertEqual(self.summarize(1)["status"], "tool_error")

    def test_console_error_entry_counts_without_error_log(self):
        lllog = ("[05:16:52] ERROR    [GPL-0301] Utilization 104.491 % exceeds     "
                 "openroad.py:312\n                    100%.                \n"
                 "Error: gpl.tcl, 82 GPL-0301\n")
        self.make(lllog=lllog)
        s = self.summarize(2)
        self.assertEqual(s["status"], "fail", s["tool_error"])
        self.assertIn("GPL-0301", s["failure"])

    def test_timeout(self):
        self.make()
        s = self.summarize(124, timed_out="librelane")
        self.assertEqual(s["status"], "timeout")
        self.assertIn("librelane", s["timeout"])
        # a precheck timeout after a flow that already shows a violation is still a failure
        shutil.rmtree(self.out)
        self.out.mkdir()
        self.make(final={**SIGNOFF_OK, "route__drc_errors": 4})
        self.assertEqual(self.summarize(0, 0, 124, timed_out="precheck")["status"], "fail")

    def test_unknown_librelane_failure_is_tool_error(self):
        self.make(errlog="Traceback (most recent call last):\nKeyError: 'x'\n")
        s = self.summarize(1)
        self.assertEqual(s["status"], "tool_error")
        self.assertIn("without evidence of a design failure", s["tool_error"])

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

    def precheck_case(self, row, text, **kw):
        shutil.rmtree(self.out, ignore_errors=True)
        self.out.mkdir()
        rows = {k: "\u2705" for k in PRECHECK_OK}
        rows[row] = f"\u274c Fail: {text}"
        self.make(final=kw.pop("final", SIGNOFF_OK), rows=rows, **kw)
        return self.summarize(0, 0, 1)

    def test_precheck_violation_rows_are_design_failures(self):
        s = self.precheck_case("KLayout BEOL", "Klayout beol failed with 3 DRC violations")
        self.assertEqual(s["status"], "fail")
        self.assertIn("precheck: KLayout BEOL: Klayout beol failed with 3 DRC violations",
                      s["evidence"])
        s = self.precheck_case("Pin check", "Some ports are missing or have wrong dimensions, "
                                            "see uo_out[0] above")
        self.assertEqual(s["status"], "fail")

    def test_precheck_tool_failures_are_tool_errors(self):
        """The re-review's repro (scratchpad/rr8/repro_precheck.py), plus variants."""
        cases = [
            ("Magic DRC", "Magic DRC failed", {}),  # dead magic shim, no count anywhere
            ("KLayout FEOL", "Klayout feol failed", {}),  # klayout returned non-zero
            ("KLayout BEOL", "[Errno 2] No such file or directory: 'reports/drc_beol.xml'",
             {"xml": {"KLayout BEOL": ("[Errno 2] No such file or directory: "
                                       "'reports/drc_beol.xml'", "FileNotFoundError")}}),
            ("Verilog syntax check", "Verilog syntax check failed", {}),
            ("KLayout BEOL", "Klayout beol failed with 3 DRC violations", {"xml": "none"}),
            ("Pin check", "Duplicate pin in template DEF", {}),
        ]
        for row, text, kw in cases:
            s = self.precheck_case(row, text, **kw)
            self.assertEqual(s["status"], "tool_error", (row, text, s["failure"]))
            self.assertIn(row, s["precheck"]["tool_failures"])

    def test_magic_drc_failed_needs_corroboration(self):
        s = self.precheck_case("Magic DRC", "Magic DRC failed",
                               prelog="[INFO]: DRC Checking DONE\n[ERROR]: 12 DRC errors found\n")
        self.assertEqual(s["status"], "fail")
        self.assertIn("12 DRC errors found", s["failure"])
        s = self.precheck_case("Magic DRC", "Magic DRC failed",
                               magic_txt="top\n[INFO]: COUNT: 8\n[INFO]: Should be divided by 3 or 4\n")
        self.assertEqual(s["status"], "fail")
        s = self.precheck_case("Magic DRC", "Magic DRC failed",
                               magic_txt="top\n[INFO]: COUNT: 0\n")
        self.assertEqual(s["status"], "tool_error")

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
        (REPO / "build").mkdir(exist_ok=True)
        self.btmp = Path(tempfile.mkdtemp(prefix="test-harden-args-", dir=REPO / "build"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.btmp, ignore_errors=True)

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
                    str(REPO / "src" / "x"), str(REPO / "build" / ".." / "src"),
                    str(REPO / "build"), str(self.tmp / "out")]:
            self.assertRefused(self.harden("--out", out))
            self.assertRefused(self.harden(f"--out={out}"))
        self.assertTrue((REPO / "src").is_dir() and (REPO / "docs").is_dir())
        self.assertFalse((REPO / "src" / "x").exists())

    def test_out_guard_symlink_to_project(self):
        link = self.tmp / "link"
        link.symlink_to(REPO)
        self.assertRefused(self.harden("--out", str(link)))

    def test_out_guard_unrelated_nonempty_dir(self):
        d = self.btmp / "mine"
        d.mkdir()
        (d / "keep.txt").write_text("precious\n")
        self.assertRefused(self.harden("--out", str(d)), "not a harden_tt.sh output")
        self.assertEqual((d / "keep.txt").read_text(), "precious\n")
        (d / "summary.json").write_text("{}\n")  # summary.json alone is not enough
        self.assertRefused(self.harden("--out", str(d)), "not a harden_tt.sh output")
        self.assertEqual((d / "keep.txt").read_text(), "precious\n")

    @unittest.skipUnless((TT_ENV / "tt-support-tools").is_dir(),
                         "needs tools/harden_tt.sh --setup-only done")
    def test_out_guard_accepts_legacy_output(self):
        d = self.btmp / "legacy"
        (d / "proj").mkdir(parents=True)
        (d / "summary.json").write_text("{}\n")
        r = self.harden("--out", str(d), env={"PDK_ROOT": "/nonexistent/pdk"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("refusing", r.stderr)
        self.assertEqual(sorted(p.name for p in d.iterdir()), [".harden_tt_out"])

    def test_timeout_value_checked(self):
        self.assertRefused(self.harden("--timeout", "10m", "--setup-only"), "bad --timeout")
        self.assertRefused(self.harden("--timeout=-1", "--setup-only"), "bad --timeout")

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
        out = self.btmp / "out"
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


def def_text(comps, rows=()):
    """rows: [(x_dbu, y_dbu, sites)] as horizontal unithd ROWs."""
    rowtext = "".join(f"ROW ROW_{i} unithd {x} {y} N DO {n} BY 1 STEP 460 0 ;\n"
                      for i, (x, y, n) in enumerate(rows))
    lines = "".join(f"    - {n} {m} + PLACED ( {x} {y} ) {o} ;\n" for n, m, x, y, o in comps)
    return (f"VERSION 5.8 ;\nDESIGN t ;\nUNITS DISTANCE MICRONS 1000 ;\n{rowtext}"
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
        # 1x2 template core (2.76, 2.72)-(158.24, 223.04), rows cut at x_cut
        self.assertEqual(self.tf.def_row_extent(text), [2.76, 2.72, 120.52, 223.04])

    def test_row_extent(self):
        rows = [(2760, 2720 + 2720 * i, 100) for i in range(3)]
        self.assertEqual(self.tf.def_row_extent(def_text([], rows)), [2.76, 2.72, 48.76, 10.88])
        with self.assertRaises(ValueError):
            self.tf.def_row_extent(def_text([]))


@unittest.skipUnless(HAVE_PDK and (TT_ENV / "tt-support-tools").is_dir(),
                     "needs the local PDK and tools/harden_tt.sh --setup-only done")
class TilefitSearchTest(unittest.TestCase):
    """tilefit with a stub in place of harden_tt.sh (subprocess.run patched). The 1x1 TT
    template core is (2.76, 2.72) to (158.24, 108.80): 338 sites by 39 rows."""
    CORE = (2.76, 2.72, 158.24, 108.80)

    def setUp(self):
        self.tf = load_tilefit()
        self.tmp = Path(tempfile.mkdtemp(prefix="tilefit-search-"))
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stub(self, verdict):
        """verdict(x_cut or None, overlay) -> (rc, summary or None, def or None), where def
        is "auto" (rows of the template core cut at x_cut, one cell flush against it) or
        (rows, comps) for def_text."""
        tf = self.tf

        def run(cmd, **kw):
            out = Path(cmd[cmd.index("--out") + 1])
            overlay = json.loads(Path(cmd[cmd.index("--overlay") + 1]).read_text())
            self.calls.append({"overlay": overlay, "timeout": cmd[cmd.index("--timeout") + 1]})
            obs = [o for o in overlay.get("FP_OBSTRUCTIONS") or [] if o[1] == 0.0]
            x_cut = obs[-1][0] if obs else None
            rc, summary, d = verdict(x_cut, overlay)
            if summary is not None:
                out.mkdir(parents=True, exist_ok=True)
                (out / "summary.json").write_text(json.dumps(summary))
            if d is not None:
                if d == "auto":
                    d = (self.rows(x_cut), self.inside(x_cut or self.CORE[2]))
                p = out / "proj" / "runs" / "wokwi" / "final" / "def"
                p.mkdir(parents=True, exist_ok=True)
                (p / "tt_um_t.def").write_text(def_text(d[1], d[0]))
            return subprocess.CompletedProcess(cmd, rc)
        return mock.patch.object(tf.subprocess, "run", side_effect=run)

    def rows(self, x_cut, dx=0.0, dy=0.0):
        x0, y0, x1, _ = self.CORE
        n = round(((x_cut or x1) - x0) / 0.46)
        return [(round((x0 + dx) * 1000), round((y0 + dy + 2.72 * i) * 1000), n)
                for i in range(39)]

    def args(self, *extra):
        return ["tilefit.py", "--project", str(REPO), "--runs-dir", str(self.tmp / "runs"),
                "--out", str(self.tmp / "tilefit.json"), "--tiles", "1x1", "--lo", "0.3", *extra]

    def summary(self, status, core=16493.3):
        return {"status": status, "checks": {}, "core_um2": core if status != "timeout" else None,
                "failure": "GPL-0301" if status == "fail" else None,
                "timeout": "librelane stopped" if status == "timeout" else None,
                "stdcell_area_um2": 5000.0}

    def main(self, verdict, *extra, entry=False):
        (self.tmp / "tilefit.json").write_text('{"stale": true}\n')
        with self.stub(verdict), mock.patch.object(sys, "argv", self.args(*extra)):
            with self.assertRaises(SystemExit) as cm:
                (self.tf.entry if entry else self.tf.main)()
        res = self.tmp / "tilefit.json"
        return cm.exception.code, json.loads(res.read_text()) if res.exists() else None

    def inside(self, x_cut):  # one cell flush against x_cut
        return [("a", "sky130_fd_sc_hd__inv_2", round(x_cut * 1000) - 1380, 2720, "N")]

    def passing_above(self, threshold):
        def verdict(x_cut, overlay):
            ok = x_cut is None or x_cut >= threshold
            return (0, self.summary("pass"), "auto") if ok else (1, self.summary("fail"), None)
        return verdict

    def test_obstruction_spans_die_edge_and_search_converges(self):
        code, res = self.main(self.passing_above(110.0))
        self.assertEqual(code, 0)
        self.assertIsNone(self.calls[0]["overlay"].get("FP_OBSTRUCTIONS"))  # full tile first
        self.assertEqual(self.calls[0]["timeout"], "3600")  # --trial-timeout 60 min default
        die = [0.0, 0.0, 161.0, 111.52]
        for c in self.calls[1:]:
            x0, y0, x1, y1 = c["overlay"]["FP_OBSTRUCTIONS"][-1]
            self.assertEqual([y0, x1, y1], die[1:], "blockage must reach the die edge")
            sites = (x0 - 2.76) / 0.46
            self.assertAlmostEqual(sites, round(sites), places=4)  # on the site grid
        self.assertGreaterEqual(res["min_fraction"], (110.0 - 2.76) / 155.48 - 1e-4)
        self.assertLess(res["min_fraction"], 0.72)
        self.assertEqual(res["timeouts"], [])

    def test_core_is_the_template_whatever_the_overlay(self):
        ov = self.tmp / "ov.json"
        ov.write_text(json.dumps({"LEFT_MARGIN_MULT": 12, "TOP_MARGIN_MULT": 4}))
        code, res = self.main(self.passing_above(110.0), "--overlay", str(ov))
        self.assertEqual(code, 0)
        self.assertEqual(res["core_box_um"], list(self.CORE))
        # a real run with those margins reports a smaller core and aborts on the area check
        code, _ = self.main(lambda x, o: (0, self.summary("pass", core=15000.0), "auto"),
                            "--overlay", str(ov))
        self.assertEqual(code, 2)

    def test_shifted_core_of_equal_area_aborts(self):
        def verdict(x_cut, overlay):  # same area, rows shifted 0.46 um right (CORE_AREA)
            return 0, self.summary("pass"), (self.rows(x_cut, dx=0.46),
                                             self.inside((x_cut or self.CORE[2]) - 1.0))
        code, res = self.main(verdict)
        self.assertEqual((code, res), (2, None))
        self.assertEqual(len(self.calls), 1)  # caught on the full tile already

    def test_user_obstructions_kept(self):
        ov = self.tmp / "ov.json"
        ov.write_text(json.dumps({"FP_OBSTRUCTIONS": [[10, 10, 20, 20]]}))
        code, _ = self.main(self.passing_above(0.0), "--overlay", str(ov), "--steps", "1")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[1]["overlay"]["FP_OBSTRUCTIONS"][0], [10, 10, 20, 20])
        self.assertEqual(len(self.calls[1]["overlay"]["FP_OBSTRUCTIONS"]), 2)

    def test_nothing_signs_off_exits_1(self):
        code, res = self.main(lambda x, o: (1, self.summary("fail"), None))
        self.assertEqual(code, 1)
        self.assertIsNone(res["min_fraction"])
        self.assertEqual(len(self.calls), 1)

    def test_timeout_counts_as_not_signing_off_and_is_listed(self):
        def verdict(x_cut, overlay):
            if x_cut is not None and x_cut < 120.0:
                return 3, self.summary("timeout"), None
            return 0, self.summary("pass"), "auto"
        code, res = self.main(verdict, "--trial-timeout", "5")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[0]["timeout"], "300")
        self.assertTrue(res["timeouts"])
        self.assertIn("timeout", res["warning"])
        self.assertTrue(all(r["status"] == "timeout" for r in res["runs"]
                            if r["tag"] in res["timeouts"]))
        self.assertGreaterEqual(res["min_fraction"], (120.0 - 2.76) / 155.48 - 1e-4)

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
        for rc, status in ((1, "pass"), (0, "timeout"), (3, "fail")):
            code, _ = self.main(lambda x, o: (rc, self.summary(status), "auto"))
            self.assertEqual(code, 2, (rc, status))

    def test_core_area_mismatch_aborts(self):
        code, _ = self.main(lambda x, o: (0, self.summary("pass", core=17000.0), "auto"))
        self.assertEqual(code, 2)

    def test_instance_past_cut_aborts(self):
        def verdict(x_cut, overlay):
            comps = [("z", "sky130_fd_sc_hd__inv_2",
                      round((x_cut or self.CORE[2]) * 1000) - (920 if x_cut else 1380), 2720,
                      "N")]
            return 0, self.summary("pass"), (self.rows(x_cut), comps)
        code, res = self.main(verdict)
        self.assertEqual((code, res), (2, None))
        self.assertEqual(len(self.calls), 2)

    def test_crash_is_a_tool_error(self):
        def verdict(x_cut, overlay):
            raise RuntimeError("boom")
        code, res = self.main(verdict, entry=True)
        self.assertEqual((code, res), (2, None))


if __name__ == "__main__":
    unittest.main()
