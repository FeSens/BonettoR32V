"""Tests for tools/eval.py: orchestration, missing runners, and each gate adapter's judgment.

Adapters are exercised against fake runners in a temporary repo, so these tests pin how eval reads
each interface (exit code plus json) without needing the real gates.
"""
import json
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import eval as ev  # noqa: E402

FIX = os.path.join(HERE, "test_fixtures", "equiv")
# The real lint and equiv gates run the three-way preprocessor backstop.
HAVE_PP = all(shutil.which(t, path=ev.tool_env()["PATH"])
              for t in ("verilator", "yosys", "iverilog"))
PP_REASON = "verilator, yosys and iverilog needed"


@pytest.fixture(autouse=True)
def private_lock(tmp_path, monkeypatch):
    # Never queue behind a real formal run holding /tmp/bonetto-heavy.lock.
    monkeypatch.setattr(ev, "HEAVY_LOCK", ["lockf", "-k", str(tmp_path / "heavy.lock")])


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    return r


def put(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def fake_runner(repo, rel, out_rel, doc, rc=0, extra=""):
    """A Python runner that writes `doc` to out_rel (unless doc is None) and exits rc."""
    body = ["import json, os, sys"]
    if doc is not None:
        body += [f"os.makedirs(os.path.dirname({out_rel!r}), exist_ok=True)",
                 f"json.dump({doc!r}, open({out_rel!r}, 'w'))"]
    body += [extra, f"sys.exit({rc})"]
    put(repo, rel, "\n".join(body) + "\n")


def run_gate(repo, gate):
    rc = ev.main(["--repo", str(repo), "--gate", gate])
    doc = json.loads((repo / "build" / "gates" / f"{gate}.json").read_text())
    assert doc["pass"] is (rc == 0)
    return doc


def with_src(repo, fixture="pass_observer"):
    for f in os.listdir(os.path.join(FIX, fixture)):
        shutil.copy(os.path.join(FIX, fixture, f), repo / "src" / f)
    os.symlink(os.path.join(REPO, "third_party"), repo / "third_party")


# --- orchestration -----------------------------------------------------------------------------

def fake_gates(monkeypatch, fail_at=None, boom_at=None):
    calls = []

    def make(name):
        def gate(ctx):
            calls.append(name)
            if name == boom_at:
                raise RuntimeError("bug")
            if name == fail_at:
                raise ev.GateFail(f"{name} broke")
            if name == "area":
                ctx.notes.update({"area_um2": 1234.5, "cells": 321})
            if name == "cosim":
                ctx.notes["cpi"] = 88.0
            return f"{name} ok", {}
        return gate

    for name in ev.ORDER:
        monkeypatch.setitem(ev.GATES, name, make(name))
    return calls


def test_all_gates_pass(repo, monkeypatch):
    calls = fake_gates(monkeypatch)
    assert ev.main(["--repo", str(repo)]) == 0
    assert calls == ev.ORDER == ["lint", "area", "cosim", "arch", "equiv", "gl", "formal"]
    doc = json.loads((repo / "build" / "eval.json").read_text())
    for key in ("sha", "pass", "gates", "area_um2", "cells", "cpi", "timestamp"):
        assert key in doc
    assert doc["pass"] is True and doc["stopped_at"] is None
    assert (doc["area_um2"], doc["cells"], doc["cpi"]) == (1234.5, 321, 88.0)
    assert all(doc["gates"][g]["pass"] is True for g in ev.ORDER)
    assert doc["sha"] == "unknown" and doc["dirty"] is None   # not a git repo


def test_stops_at_first_failure(repo, monkeypatch):
    calls = fake_gates(monkeypatch, fail_at="equiv")
    assert ev.main(["--repo", str(repo)]) == 1
    assert calls == ["lint", "area", "cosim", "arch", "equiv"]
    doc = json.loads((repo / "build" / "eval.json").read_text())
    assert doc["pass"] is False and doc["stopped_at"] == "equiv"
    assert doc["gates"]["cosim"]["pass"] is True
    assert doc["gates"]["equiv"] == {"pass": False, "detail": "equiv broke",
                                     "runtime_s": doc["gates"]["equiv"]["runtime_s"]}
    for g in ("gl", "formal"):
        assert doc["gates"][g]["pass"] is None
        assert doc["gates"][g]["detail"] == "not run (stopped after equiv failed)"
    assert doc["area_um2"] == 1234.5 and doc["no_gain"] is False


@pytest.mark.parametrize("bar,rc,calls_after_area", [
    (1234.5, 3, []),                     # equal area is no gain
    (1000.0, 3, []),
    (1234.6, 0, ["cosim", "arch", "equiv", "gl", "formal"]),
])
def test_max_area_retires_early(repo, monkeypatch, bar, rc, calls_after_area):
    calls = fake_gates(monkeypatch)
    assert ev.main(["--repo", str(repo), "--max-area", str(bar)]) == rc
    assert calls == ["lint", "area"] + calls_after_area
    doc = json.loads((repo / "build" / "eval.json").read_text())
    assert doc["max_area"] == bar and doc["area_um2"] == 1234.5
    if rc == 3:
        assert doc["pass"] is False and doc["no_gain"] is True and doc["stopped_at"] == "area"
        assert doc["gates"]["area"]["pass"] is True
        for g in calls_after_area or ["cosim", "formal"]:
            assert doc["gates"][g]["pass"] is None and "--max-area" in doc["gates"][g]["detail"]
    else:
        assert doc["pass"] is True and doc["no_gain"] is False


def test_failing_area_gate_beats_bar_check(repo, monkeypatch):
    calls = fake_gates(monkeypatch, fail_at="area")
    assert ev.main(["--repo", str(repo), "--max-area", "99999"]) == 1
    assert calls == ["lint", "area"]
    doc = json.loads((repo / "build" / "eval.json").read_text())
    assert doc["no_gain"] is False and doc["stopped_at"] == "area"


def test_adapter_exception_fails_gate(repo, monkeypatch):
    fake_gates(monkeypatch, boom_at="lint")
    assert ev.main(["--repo", str(repo)]) == 1
    doc = json.loads((repo / "build" / "eval.json").read_text())
    assert doc["gates"]["lint"]["detail"] == "gate lint: adapter error: RuntimeError: bug"


def test_stale_eval_json_is_replaced(repo, monkeypatch):
    put(repo, "build/eval.json", json.dumps({"pass": True}))
    fake_gates(monkeypatch, fail_at="lint")
    assert ev.main(["--repo", str(repo)]) == 1
    assert json.loads((repo / "build" / "eval.json").read_text())["pass"] is False


def test_sha_and_dirty_flag(repo, monkeypatch):
    import subprocess
    for k, v in {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}.items():
        monkeypatch.setenv(k, v)

    def g(*a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                              text=True).stdout.strip()

    g("init", "-q")
    put(repo, ".gitignore", "build/\n")
    put(repo, "src/top.v", "module top; endmodule\n")
    g("add", "-A")
    g("commit", "-q", "-m", "c")
    sha = g("rev-parse", "HEAD")
    fake_gates(monkeypatch)

    def run():
        assert ev.main(["--repo", str(repo)]) == 0
        return json.loads((repo / "build" / "eval.json").read_text())

    doc = run()                     # build/ (gitignored) now exists: still clean
    assert doc["sha"] == sha and doc["dirty"] is False
    put(repo, "src/top.v", "module top; wire x; endmodule\n")
    assert run()["dirty"] is True   # modified tracked file
    g("checkout", "--", "src/top.v")
    put(repo, "src/extra.v", "module extra; endmodule\n")
    assert run()["dirty"] is True   # untracked file in src/


def test_real_repo_sha():
    assert len(ev.git(REPO, "rev-parse", "HEAD")) == 40


def test_make_gates_alias_runs_the_same_command():
    import subprocess

    def dry(target):
        return subprocess.run(["make", "-n", "-C", REPO, target], capture_output=True,
                              text=True, check=True).stdout.strip()

    assert dry("gates") == dry("eval")
    assert dry("gates").endswith("tools/eval.py")
    assert "lockf" not in dry("gates")              # the full run never takes the heavy lock


# --- missing runners fail loudly ---------------------------------------------------------------

@pytest.mark.parametrize("gate,path", [
    ("cosim", "verif/cosim/run_cosim.py"),
    ("equiv", "tools/equiv.py"),
    ("formal", "verif/formal/run_formal.py"),
    ("formal-deep", "verif/formal/run_formal.py"),
    ("arch", "verif/arch-test/run_arch.py"),
    ("area", "tools/area.py"),
    ("gl", "test/Makefile"),
])
def test_missing_runner_fails(repo, gate, path):
    doc = run_gate(repo, gate)
    assert doc["pass"] is False
    assert doc["detail"] == f"gate {gate}: runner missing ({path})"


def test_lint_without_sources_fails(repo):
    assert "no src/*.v" in run_gate(repo, "lint")["detail"]


# --- lint (real verilator) ---------------------------------------------------------------------

@pytest.mark.skipif(not HAVE_PP, reason=PP_REASON)
def test_lint_clean_fixture_passes(repo):
    with_src(repo)
    doc = run_gate(repo, "lint")
    assert doc["pass"] is True, doc["detail"]
    assert "plain and RISCV_FORMAL" in doc["detail"]


@pytest.mark.skipif(not HAVE_PP, reason=PP_REASON)
@pytest.mark.parametrize("where,label", [("plain", "plain"), ("rvfi", "RISCV_FORMAL")])
def test_lint_warning_fails(repo, where, label):
    with_src(repo)
    top = repo / "src" / "tt_um_fesens_r32v.v"
    text = top.read_text()
    bad = "    wire [3:0] never_used = ui_in[3:0];\n"
    anchor = "    reg [63:0] order_q;\n" if where == "rvfi" else "    wire [7:0] gpio;\n"
    top.write_text(text.replace(anchor, anchor + bad))
    doc = run_gate(repo, "lint")
    assert doc["pass"] is False
    assert f"({label} build)" in doc["detail"] and "UNUSEDSIGNAL" in doc["detail"]


def test_lint_rejects_systemverilog(repo):
    put(repo, "src/top.v", "module top; endmodule\n")
    put(repo, "src/alu.sv", "module alu; endmodule\n")
    assert "SystemVerilog is not allowed" in run_gate(repo, "lint")["detail"]


def test_lint_runs_src_hygiene_before_verilator(repo):
    with_src(repo)
    put(repo, "src/z.v; log INJECTED; log x.v", "module zz; endmodule\n")
    doc = run_gate(repo, "lint")
    assert doc["pass"] is False
    assert doc["detail"].startswith("src hygiene (tools/src_check.py)")
    assert not (repo / "build" / "logs" / "lint_plain.log").exists()   # verilator never ran


@pytest.mark.skipif(not HAVE_PP, reason=PP_REASON)
def test_lint_runs_differential_backstop(repo, monkeypatch):
    # Static rules bypassed: the lint gate must still run the verilator/yosys comparison.
    with_src(repo)
    top = repo / "src" / "tt_um_fesens_r32v.v"
    top.write_text(top.read_text().replace("    assign uo_out  = gpio;\n",
                                           "`ifdef verilator\n    assign uo_out  = ~gpio;\n"
                                           "`else\n    assign uo_out  = gpio;\n`endif\n"))
    monkeypatch.setattr(ev.src_check, "check", lambda path: [])
    doc = run_gate(repo, "lint")
    assert doc["pass"] is False
    assert "differential check (plain)" in doc["detail"], doc["detail"]


@pytest.mark.skipif(not HAVE_PP, reason=PP_REASON)
def test_equiv_runs_differential_backstop(tmp_path, monkeypatch):
    import equiv
    src = tmp_path / "src"
    shutil.copytree(os.path.join(FIX, "pass_observer"), src)
    top = src / "tt_um_fesens_r32v.v"
    top.write_text(top.read_text().replace("    assign uo_out  = gpio;\n",
                                           "`ifdef YOSYS\n    assign uo_out  = gpio;\n"
                                           "`else\n    assign uo_out  = ~gpio;\n`endif\n"))
    monkeypatch.setattr(equiv.src_check, "check", lambda path: [])
    out = tmp_path / "e.json"
    assert equiv.main(["--src", str(src), "--work", str(tmp_path / "w"), "--out", str(out)]) == 1
    assert "differential check" in json.loads(out.read_text())["detail"]


@pytest.mark.skipif(not HAVE_PP, reason=PP_REASON)
def test_bitserial_sample_passes_lint_and_equiv(repo):
    sample = os.path.join(HERE, "test_fixtures", "src_check", "pass_bitserial")
    for f in os.listdir(sample):
        shutil.copy(os.path.join(sample, f), repo / "src" / f)
    os.symlink(HERE, repo / "tools")
    lint = run_gate(repo, "lint")
    assert lint["pass"] is True, lint["detail"]
    eq = run_gate(repo, "equiv")
    assert eq["pass"] is True, eq["detail"]
    assert eq["data"]["proved_by_induct"] == 0


@pytest.mark.skipif(not HAVE_PP, reason=PP_REASON)
@pytest.mark.parametrize("fixture,needle", [
    ("fail_assign_delay", "`#` delay control is not allowed"),
    ("fail_nb_delay", "`#` delay control is not allowed"),
    ("fail_function_stale_local", "function f reads t before assigning it"),
    ("fail_level_sensitivity", "event control must be @(*)"),
])
def test_round3_splits_fail_lint_and_equiv(repo, fixture, needle):
    # Review round 3: each of these passed src_check, lint and equiv while Icarus RTL simulation
    # disagreed with the synthesized netlist. Both gates now stop them at src_check.
    sample = os.path.join(HERE, "test_fixtures", "src_check", fixture)
    for f in os.listdir(sample):
        shutil.copy(os.path.join(sample, f), repo / "src" / f)
    os.symlink(HERE, repo / "tools")
    lint = run_gate(repo, "lint")
    assert lint["pass"] is False and needle in lint["detail"], lint["detail"]
    eq = run_gate(repo, "equiv")
    assert eq["pass"] is False and needle in eq["detail"], eq["detail"]


def test_lint_rejects_macro_split(repo):
    with_src(repo)
    top = repo / "src" / "tt_um_fesens_r32v.v"
    top.write_text(top.read_text().replace("    assign uo_out  = gpio;\n",
                                           "`ifndef SYNTHESIS\n    assign uo_out  = gpio;\n"
                                           "`else\n    assign uo_out  = ~gpio;\n`endif\n"))
    assert "`ifndef SYNTHESIS" in run_gate(repo, "lint")["detail"]


# --- equiv adapter (real tools/equiv.py) -------------------------------------------------------

@pytest.mark.skipif(not HAVE_PP, reason=PP_REASON)
@pytest.mark.parametrize("fixture,ok", [("pass_observer", True), ("fail_rvfi_drives_pin", False)])
def test_equiv_adapter(repo, fixture, ok):
    with_src(repo, fixture)
    os.symlink(HERE, repo / "tools")
    doc = run_gate(repo, "equiv")
    assert doc["pass"] is ok, doc["detail"]


# --- formal adapter ----------------------------------------------------------------------------

FORMAL = "verif/formal/run_formal.py"
GOOD_FORMAL = {"pass": True, "checks": {"insn_add": "PASS", "reg": "PASS"}, "depth": 120,
               "runtime_s": 3.0}


@pytest.mark.parametrize("doc,rc,ok,msg", [
    (GOOD_FORMAL, 0, True, "2 checks PASS, depth 120"),
    ({**GOOD_FORMAL, "checks": {"insn_add": "PASS", "reg": "UNREACHED"}}, 0, False,
     "reg=UNREACHED"),
    ({**GOOD_FORMAL, "checks": {"insn_add": "FAIL"}, "pass": False}, 1, False, "insn_add=FAIL"),
    (GOOD_FORMAL, 1, False, "exit 1"),
    ({**GOOD_FORMAL, "pass": False}, 0, False, "formal FAIL"),
    ({**GOOD_FORMAL, "checks": {}}, 0, False, "no checks reported"),
    ({**GOOD_FORMAL, "checks": {"insn_add": "PASS", "reg": "SKIPPED"}}, 0, False,
     "reg=SKIPPED"),
    (None, 0, False, "runner wrote no build/formal.json"),
])
def test_formal_adapter(repo, doc, rc, ok, msg):
    fake_runner(repo, FORMAL, "build/formal.json", doc, rc)
    res = run_gate(repo, "formal")
    assert res["pass"] is ok and msg in res["detail"], res["detail"]


def test_formal_stale_json_is_not_trusted(repo):
    put(repo, "build/formal.json", json.dumps(GOOD_FORMAL))
    fake_runner(repo, FORMAL, "build/formal.json", None, 0)
    assert run_gate(repo, "formal")["pass"] is False


def test_remote_formal_skips_mac_lock(repo, monkeypatch):
    import subprocess, time as _t
    lock = ev.HEAVY_LOCK[-1]                             # the private lock of this test
    holder = subprocess.Popen(["lockf", "-k", lock, "sleep", "30"])
    try:
        _t.sleep(0.5)                                   # the lock is now held by someone else
        fake_runner(repo, FORMAL, "build/formal.json", GOOD_FORMAL, 0)
        monkeypatch.setenv("BONETTO_FORMAL_HOST", "example-host")
        t0 = _t.time()
        assert run_gate(repo, "formal")["pass"] is True
        assert _t.time() - t0 < 20                      # did not queue behind the held lock
    finally:
        holder.kill()


def test_formal_deep_passes_flag(repo):
    fake_runner(repo, FORMAL, "build/formal.json", GOOD_FORMAL, 0,
                extra="assert sys.argv[1:] == ['--deep'], sys.argv")
    assert run_gate(repo, "formal-deep")["pass"] is True
    assert run_gate(repo, "formal")["pass"] is False      # same runner rejects no --deep


# --- arch adapter ------------------------------------------------------------------------------

@pytest.mark.parametrize("doc,ok", [
    ({"pass": True, "tests": {"add-01": True, "sw-align-01": True}}, True),
    ({"pass": True, "tests": {"add-01": True, "sw-align-01": False}}, False),
    ({"pass": True, "tests": {}}, False),
])
def test_arch_adapter(repo, doc, ok):
    fake_runner(repo, "verif/arch-test/run_arch.py", "build/arch.json", doc, 0)
    assert run_gate(repo, "arch")["pass"] is ok


# --- area adapter ------------------------------------------------------------------------------

def good_area(netlist="build/synth/netlist.v"):
    return {"area_um2": 15432.1, "cells": 987, "by_type": {}, "by_module": {}, "flops": 120,
            "latches": 0, "netlist": netlist, "engine": "librelane"}


@pytest.mark.parametrize("change,ok,msg", [
    ({}, True, "15432.1 um2, 987 cells, 120 flops"),
    ({"latches": 3}, False, "no latches"),
    ({"engine": "yosys"}, False, "librelane engine"),
    ({"engine": None}, False, "librelane engine"),
    ({"latches": None}, False, "no latches"),
    ({"area_um2": 0}, False, "bad area_um2"),
    ({"cells": "many"}, False, "bad cells"),
    ({"netlist": "build/synth/missing.v"}, False, "missing"),
])
def test_area_adapter(repo, change, ok, msg):
    put(repo, "build/synth/netlist.v", "module tt_um_fesens_r32v(); endmodule\n")
    fake_runner(repo, "tools/area.py", "build/area.json", {**good_area(), **change}, 0,
                extra="assert sys.argv[1:] == ['--top', 'tt_um_fesens_r32v', '--src', 'src/']")
    res = run_gate(repo, "area")
    assert res["pass"] is ok and msg in res["detail"], res["detail"]


# --- gl adapter --------------------------------------------------------------------------------

WRITE_NETLIST = ("os.makedirs('build/synth', exist_ok=True); "
                 "open('build/synth/netlist.v', 'w').write('module tt_um_fesens_r32v(); endmodule')")


def gl_makefile(xml):
    return ("all:\n\t@test \"$(GATES)\" = yes\n\t@test -f gate_level_netlist.v\n"
            f"\t@printf '%s' '{xml}' > results.xml\n")


@pytest.mark.parametrize("xml,ok", [
    ('<testsuites><testsuite><testcase name="gpio"/></testsuite></testsuites>', True),
    ('<testsuites><testsuite><testcase name="gpio"><failure/></testcase></testsuite></testsuites>',
     False),
    ('<testsuites><testsuite><testcase name="gpio"><skipped/></testcase></testsuite></testsuites>',
     False),
])
def test_gl_adapter(repo, xml, ok):
    fake_runner(repo, "tools/area.py", "build/area.json", good_area(), 0, extra=WRITE_NETLIST)
    put(repo, "test/Makefile", gl_makefile(xml))
    res = run_gate(repo, "gl")
    assert res["pass"] is ok, res["detail"]
    assert (repo / "test" / "gate_level_netlist.v").exists()


def test_gl_rejects_netlist_older_than_area_step(repo):
    netlist = put(repo, "build/synth/netlist.v", "module tt_um_fesens_r32v(); endmodule\n")
    os.utime(netlist, (1_000_000_000, 1_000_000_000))      # area.py below does not rewrite it
    fake_runner(repo, "tools/area.py", "build/area.json", good_area(), 0)
    put(repo, "test/Makefile", gl_makefile("<testsuites/>"))
    res = run_gate(repo, "gl")
    assert res["pass"] is False and "older than this run's area step" in res["detail"]


def test_gl_fails_when_area_fails(repo):
    put(repo, "test/Makefile", gl_makefile("<testsuites/>"))
    res = run_gate(repo, "gl")
    assert "needs the area netlist" in res["detail"]


# --- cosim adapter -----------------------------------------------------------------------------

PROGRAMS_MAKEFILE = (
    "all:\n"
    "\t@mkdir -p ../../build/programs\n"
    "\t@printf '00\\n' > ../../build/programs/selftest.hex\n"
    "\t@printf '{\"retired\": 10, \"uo_out\": []}' > ../../build/programs/selftest.expect.json\n"
    "\t@printf '00\\n' > ../../build/programs/coremark.hex\n"
    "\t@printf '{\"retired\": 1000}' > ../../build/programs/coremark.expect.json\n")

FAKE_COSIM = """
import argparse, json, os, sys
ap = argparse.ArgumentParser()
for a in ("--mode", "--prog", "--seed"):
    ap.add_argument(a, required=True)
a = ap.parse_args()   # an unexpected --max-cycles is an argparse error (exit 2)
retired = 10 if "selftest" in a.prog else 1000
fail = os.environ.get("FAKE_COSIM_FAIL") == a.mode
gap = int(os.environ.get("FAKE_COSIM_GAP", "100"))
print("building...")
print(json.dumps({"prog": a.prog, "mode": a.mode, "seed": int(a.seed), "pass": not fail,
                  "retired": retired, "cycles": retired * 90, "cpi": 90.0,
                  "max_cycles_per_insn": gap, "error": "rd mismatch" if fail else None}))
sys.exit(1 if fail else 0)
"""


@pytest.fixture
def cosim_repo(repo):
    put(repo, "verif/programs/Makefile", PROGRAMS_MAKEFILE)
    put(repo, "verif/cosim/run_cosim.py", FAKE_COSIM)
    return repo


def test_cosim_adapter_passes(cosim_repo):
    res = run_gate(cosim_repo, "cosim")
    assert res["pass"] is True, res["detail"]
    runs = res["data"]["runs"]
    assert len(runs) == 2 * 2 * 3
    assert runs[0]["prog"] == "selftest"                       # selftest runs first
    assert {(r["mode"], r["seed"]) for r in runs} == {(m, s) for m in ("rvfi", "pins")
                                                      for s in (1, 2, 3)}
    assert res["data"]["cpi"] == 90.0


@pytest.mark.parametrize("env,msg", [
    ({"FAKE_COSIM_FAIL": "pins"}, "rd mismatch"),
    ({"FAKE_COSIM_GAP": "1025"}, "exceeds the latency cap"),
])
def test_cosim_adapter_fails(cosim_repo, monkeypatch, env, msg):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    res = run_gate(cosim_repo, "cosim")
    assert res["pass"] is False and msg in res["detail"], res["detail"]


def test_cosim_needs_expect_count(cosim_repo):
    put(cosim_repo, "verif/programs/Makefile", PROGRAMS_MAKEFILE.replace('"retired": 10, ', ""))
    res = run_gate(cosim_repo, "cosim")
    assert "no retired-instruction count in selftest.expect.json" in res["detail"]
