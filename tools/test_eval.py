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

V3_ORDER = ["lint", "area", "cosim", "arch", "equiv", "gl"]


def fake_gates(monkeypatch, fail_at=None, boom_at=None, ips=2.5):
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
                if ips is not None:
                    ctx.notes.update({"coremark_ips": ips, "coremark_cycles_per_iter": 16e6,
                                      "clock_period_ns": 25})
            return f"{name} ok", {}
        return gate

    for name in ev.ORDER + [ev.FORMAL_GATE]:
        monkeypatch.setitem(ev.GATES, name, make(name))
    return calls


def eval_doc(repo):
    return json.loads((repo / "build" / "eval.json").read_text())


def test_all_gates_pass(repo, monkeypatch):
    monkeypatch.delenv("BONETTO_GIT_SHA", raising=False)
    monkeypatch.delenv("BONETTO_GIT_DIRTY", raising=False)
    calls = fake_gates(monkeypatch)
    assert ev.main(["--repo", str(repo)]) == 0
    assert calls == ev.ORDER == V3_ORDER
    doc = eval_doc(repo)
    for key in ("sha", "dirty", "pass", "gates", "area_um2", "cells", "coremark_ips", "cpi",
                "formal", "timestamp"):
        assert key in doc
    assert doc["pass"] is True and doc["stopped_at"] is None
    assert (doc["area_um2"], doc["cells"], doc["cpi"]) == (1234.5, 321, 88.0)
    assert (doc["coremark_ips"], doc["coremark_cycles_per_iter"], doc["clock_period_ns"]) \
        == (2.5, 16e6, 25)
    assert all(doc["gates"][g]["pass"] is True for g in ev.ORDER)
    assert doc["sha"] == "unknown" and doc["dirty"] is None   # not a git repo


@pytest.mark.parametrize("dirty_env,dirty", [("0", False), ("1", True), ("", None)])
def test_sha_from_hwe_run_env_without_git(repo, monkeypatch, dirty_env, dirty):
    # A tools/hwe_run.sh mirror has no .git; the script passes the checkout's sha and dirty state.
    monkeypatch.setenv("BONETTO_GIT_SHA", "f" * 40)
    monkeypatch.setenv("BONETTO_GIT_DIRTY", dirty_env)
    fake_gates(monkeypatch)
    assert ev.main(["--repo", str(repo)]) == 0
    doc = eval_doc(repo)
    assert doc["sha"] == "f" * 40 and doc["dirty"] is dirty


def test_v3_order_and_formal_skipped(repo, monkeypatch):
    calls = fake_gates(monkeypatch)
    assert ev.main(["--repo", str(repo)]) == 0
    assert "formal" not in calls and calls == V3_ORDER
    doc = eval_doc(repo)
    assert doc["formal"] == "skipped"
    assert "formal" not in doc["gates"] and doc["order"] == V3_ORDER
    assert doc["pass"] is True                       # a skipped formal does not block a pass


def test_formal_flag_runs_formal_last(repo, monkeypatch):
    calls = fake_gates(monkeypatch)
    assert ev.main(["--repo", str(repo), "--formal"]) == 0
    assert calls == V3_ORDER + ["formal"]
    doc = eval_doc(repo)
    assert doc["formal"] == "pass" and doc["gates"]["formal"]["pass"] is True
    assert doc["order"] == V3_ORDER + ["formal"] and doc["pass"] is True


def test_formal_flag_failure_fails_eval(repo, monkeypatch):
    fake_gates(monkeypatch, fail_at="formal")
    assert ev.main(["--repo", str(repo), "--formal"]) == 1
    doc = eval_doc(repo)
    assert doc["formal"] == "fail" and doc["pass"] is False and doc["stopped_at"] == "formal"


def test_formal_flag_not_reached_is_skipped(repo, monkeypatch):
    calls = fake_gates(monkeypatch, fail_at="cosim")
    assert ev.main(["--repo", str(repo), "--formal"]) == 1
    assert calls == ["lint", "area", "cosim"]
    doc = eval_doc(repo)
    assert doc["formal"] == "skipped"
    assert doc["gates"]["formal"] == {"pass": None, "detail": "not run (stopped after cosim failed)"}


def test_stops_at_first_failure(repo, monkeypatch):
    calls = fake_gates(monkeypatch, fail_at="equiv")
    assert ev.main(["--repo", str(repo)]) == 1
    assert calls == ["lint", "area", "cosim", "arch", "equiv"]
    doc = eval_doc(repo)
    assert doc["pass"] is False and doc["stopped_at"] == "equiv"
    assert doc["gates"]["cosim"]["pass"] is True
    assert doc["gates"]["equiv"] == {"pass": False, "detail": "equiv broke",
                                     "runtime_s": doc["gates"]["equiv"]["runtime_s"]}
    assert doc["gates"]["gl"]["pass"] is None
    assert doc["gates"]["gl"]["detail"] == "not run (stopped after equiv failed)"
    assert "formal" not in doc["gates"] and doc["formal"] == "skipped"
    assert doc["area_um2"] == 1234.5 and doc["no_gain"] is False


@pytest.mark.parametrize("bar,ips,rc,calls_after_cosim", [
    (2.5, 2.5, 0, ["arch", "equiv", "gl"]),   # a tie continues: the area tiebreak may win (R17)
    (2.5000001, 2.5, 3, []),
    (3.0, 2.5, 3, []),
    (0.0, None, 3, []),                  # no CoreMark score: cannot be shown at least as fast
    (2.4, 2.5, 0, ["arch", "equiv", "gl"]),
])
def test_min_ips_stops_after_cosim(repo, monkeypatch, bar, ips, rc, calls_after_cosim):
    calls = fake_gates(monkeypatch, ips=ips)
    assert ev.main(["--repo", str(repo), "--min-ips", str(bar)]) == rc
    assert calls == ["lint", "area", "cosim"] + calls_after_cosim
    doc = eval_doc(repo)
    assert doc["min_ips"] == bar and doc["coremark_ips"] == ips
    assert doc["area_um2"] == 1234.5 and doc["cpi"] == 88.0
    if rc == 3:
        assert doc["pass"] is False and doc["no_gain"] is True and doc["stopped_at"] == "cosim"
        assert doc["gates"]["cosim"]["pass"] is True and doc["formal"] == "skipped"
        for g in ("arch", "equiv", "gl"):
            assert doc["gates"][g]["pass"] is None and "--min-ips" in doc["gates"][g]["detail"]
    else:
        assert doc["pass"] is True and doc["no_gain"] is False


def test_min_ips_with_formal_stops_before_formal(repo, monkeypatch):
    calls = fake_gates(monkeypatch, ips=1.0)
    assert ev.main(["--repo", str(repo), "--formal", "--min-ips", "1.5"]) == 3
    assert calls == ["lint", "area", "cosim"]
    doc = eval_doc(repo)
    assert doc["formal"] == "skipped" and "--min-ips" in doc["gates"]["formal"]["detail"]
    assert "coremark_ips 1.0 is below --min-ips 1.5" in doc["gates"]["formal"]["detail"]


def test_min_ips_tie_runs_every_gate_including_formal(repo, monkeypatch):
    calls = fake_gates(monkeypatch, ips=1.0)
    assert ev.main(["--repo", str(repo), "--formal", "--min-ips", "1.0"]) == 0
    assert calls == V3_ORDER + ["formal"]
    doc = eval_doc(repo)
    assert doc["pass"] is True and doc["no_gain"] is False and doc["formal"] == "pass"


def test_failing_cosim_gate_beats_bar_check(repo, monkeypatch):
    calls = fake_gates(monkeypatch, fail_at="cosim")
    assert ev.main(["--repo", str(repo), "--min-ips", "0"]) == 1
    assert calls == ["lint", "area", "cosim"]
    doc = eval_doc(repo)
    assert doc["no_gain"] is False and doc["stopped_at"] == "cosim"


def test_max_area_is_replaced_by_min_ips(repo, monkeypatch):
    fake_gates(monkeypatch)
    with pytest.raises(SystemExit):
        ev.main(["--repo", str(repo), "--max-area", "5000"])


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


@pytest.mark.skipif(not os.path.exists(os.path.join(REPO, ".git")),
                    reason="no .git (a tools/hwe_run.sh mirror)")
def test_real_repo_sha():
    assert len(ev.git(REPO, "rev-parse", "HEAD")) == 40


def test_make_gates_alias_runs_the_same_command():
    import subprocess

    def dry(target):
        return subprocess.run(["make", "-n", "--no-print-directory", "-C", REPO, target], capture_output=True,
                              text=True, check=True).stdout.strip()

    assert dry("gates") == dry("eval")
    assert dry("gates").endswith("tools/eval.py")
    assert "lockf" not in dry("gates")              # the full run never takes the heavy lock


def test_make_eval_formal_and_min_ips_flags():
    import subprocess

    def dry(*args):
        return subprocess.run(["make", "-n", "--no-print-directory", "-C", REPO, *args], capture_output=True, text=True)

    for target in ("eval", "gates"):
        p = dry(target, "FORMAL=1", "MIN_IPS=1.25")
        assert p.returncode == 0, p.stderr
        assert " ".join(p.stdout.split()).endswith("tools/eval.py --formal --min-ips 1.25")
        assert "lockf" not in p.stdout
        bare = " ".join(dry(target).stdout.split())
        assert bare.endswith("tools/eval.py") and "--formal" not in bare
    p = dry("eval", "MAX_AREA=4000")                 # replaced by MIN_IPS: refuse, don't ignore
    assert p.returncode != 0 and "MIN_IPS" in p.stderr


@pytest.mark.parametrize("value,formal", [("1", True), ("0", False), ("", False),
                                          ("yes", False), ("no", False)])
def test_make_formal_only_when_formal_is_1(value, formal):
    import subprocess
    for target in ("eval", "gates"):
        p = subprocess.run(["make", "-n", "--no-print-directory", "-C", REPO, target, f"FORMAL={value}"],
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        cmd = " ".join(p.stdout.split())
        assert cmd.endswith("tools/eval.py --formal" if formal else "tools/eval.py"), cmd


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

ALL53 = {f"t{i:02d}": True for i in range(53)}


@pytest.mark.parametrize("doc,ok,msg", [
    ({"pass": True, "tests": ALL53}, True, "53 riscv-arch-test tests pass"),
    ({"pass": True, "tests": {**ALL53, "t07": False}}, False, "t07"),
    ({"pass": True, "tests": {}}, False, "no tests reported"),
    ({"pass": True, "tests": {"add-01": True, "sw-align-01": True}}, False,
     "2 tests ran, ruling R34 needs all 53"),
    ({"pass": False, "tests": ALL53}, False, "exit 0"),
])
def test_arch_adapter(repo, doc, ok, msg):
    fake_runner(repo, "verif/arch-test/run_arch.py", "build/arch.json", doc, 0,
                extra="assert sys.argv[1] == '--jobs' and int(sys.argv[2]) > 0 "
                      "and len(sys.argv) == 3, sys.argv")
    res = run_gate(repo, "arch")
    assert res["pass"] is ok and msg in res["detail"], res["detail"]


# --- area adapter ------------------------------------------------------------------------------

def good_area(netlist="build/synth/netlist.v"):
    return {"area_um2": 10432.1, "cells": 987, "by_type": {}, "by_module": {}, "flops": 120,
            "latches": 0, "netlist": netlist, "engine": "librelane"}


@pytest.mark.parametrize("change,ok,msg", [
    ({}, True, "10432.1 um2, 987 cells, 120 flops"),
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


def test_fits_is_the_1x2_threshold():
    # contract section 7: 1x2 tile; the seed (20,442 um2) signs off in 1x2 (2026-10-18 sweep)
    assert (ev.FIT_AREA_UM2, ev.FIT_TILES) == (22000.0, "1x2")


@pytest.mark.parametrize("area,ok", [(22001, False), (22000.01, False), (22000, True),
                                     (20288.0, True), (4780.0, True)])
def test_fits_prefilter(repo, area, ok):
    put(repo, "build/synth/netlist.v", "module tt_um_fesens_r32v(); endmodule\n")
    fake_runner(repo, "tools/area.py", "build/area.json", {**good_area(), "area_um2": area}, 0)
    res = run_gate(repo, "area")
    assert res["pass"] is ok, res["detail"]
    if ok:
        assert "fits 1x2 (<= 22,000 um2)" in res["detail"] and res["data"]["fits"] is True
    else:
        assert res["detail"].startswith(f"fits FAIL: synth area {area} um2 exceeds 22,000 um2, "
                                        f"the 1x2 pre-filter")


def test_fits_failure_stops_eval_at_area(repo, monkeypatch):
    put(repo, "build/synth/netlist.v", "module tt_um_fesens_r32v(); endmodule\n")
    fake_runner(repo, "tools/area.py", "build/area.json", {**good_area(), "area_um2": 24001}, 0)
    calls = fake_gates(monkeypatch)
    monkeypatch.setitem(ev.GATES, "area", ev.gate_area)
    assert ev.main(["--repo", str(repo)]) == 1
    doc = eval_doc(repo)
    assert calls == ["lint"] and doc["stopped_at"] == "area" and "fits FAIL" in \
        doc["gates"]["area"]["detail"]
    assert doc["area_um2"] == 24001                 # the measured area is still reported


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


def test_gl_alone_does_not_need_the_fits_prefilter(repo):
    # `make gl` synthesizes for its netlist; whether the design fits is the area gate's verdict.
    fake_runner(repo, "tools/area.py", "build/area.json", {**good_area(), "area_um2": 30000.0},
                0, extra=WRITE_NETLIST)
    put(repo, "test/Makefile",
        gl_makefile('<testsuites><testsuite><testcase name="gpio"/></testsuite></testsuites>'))
    res = run_gate(repo, "gl")
    assert res["pass"] is True, res["detail"]


def test_gl_fails_when_area_fails(repo):
    put(repo, "test/Makefile", gl_makefile("<testsuites/>"))
    res = run_gate(repo, "gl")
    assert "needs the area netlist" in res["detail"]


# --- cosim adapter -----------------------------------------------------------------------------

PROGRAMS_MAKEFILE = (
    "all:\n"
    "\t@mkdir -p ../../build/programs\n"
    "\t@printf '00\\n' > ../../build/programs/selftest.hex\n"
    "\t@printf '00\\n' > ../../build/programs/coremark.hex\n"
    "list:\n"
    "\t@echo selftest coremark\n")

# A stand-in for `run_cosim.py --all --no-make --jobs N`: writes build/cosim.json as the real one
# does (contract v3), shaped by FAKE_* environment variables, and prints its summary line.
FAKE_COSIM = """
import argparse, json, os, sys
ap = argparse.ArgumentParser()
ap.add_argument("--all", action="store_true")
ap.add_argument("--no-make", action="store_true")
ap.add_argument("--jobs", type=int, required=True)
a = ap.parse_args()   # anything else (--mode, --max-cycles, --v3, ...) is an argparse error
assert a.all and a.no_make and a.jobs > 0
env = os.environ.get
progs = ["selftest", "coremark"]
seeds = json.loads(env("FAKE_SEEDS", "[1, 2, 3, 4]"))
cm = env("FAKE_CM_CYCLES")    # unset: no run reports CoreMark cycles
runs = []
for p in progs:
    for m in ("rvfi", "pins"):
        for s in seeds:
            if env("FAKE_DROP") == f"{p}/{m}/{s}":
                continue
            retired = 10 if p == "selftest" else 1000
            fail = env("FAKE_COSIM_FAIL") == m
            r = {"prog": p, "mode": m, "seed": s, "pass": not fail, "retired": retired,
                 "cycles": retired * 90, "cpi": 90.0,
                 "max_cycles_per_insn": int(env("FAKE_COSIM_GAP", "100")),
                 "coremark_cycles_per_iter": None, "error": "rd mismatch" if fail else None}
            if cm is not None and p == "coremark":
                vals = json.loads(cm)          # one value, or one per seed
                r["coremark_cycles_per_iter"] = vals[s - 1] if isinstance(vals, list) else vals
            runs.append(r)
            if env("FAKE_DUP") == f"{p}/{m}/{s}":
                runs.append(dict(r))
bad = env("FAKE_FUZZ")
fuzz = [{"prog": "fuzz_liveness", "mode": "rvfi", "seed": s, "pass": bad != "fail",
         "liveness_checked": bad != "unchecked", "runs": 3 if bad == "short" else 2056,
         "max_cycles_per_insn": 2049 if bad == "gap" else 500,
         "error": "latency cap: no retirement" if bad == "fail" else None}
        for s in json.loads(env("FAKE_FUZZ_SEEDS", "[1, 2, 3, 4, 5]"))]
ok = all(r["pass"] for r in runs) and all(f["pass"] and f["liveness_checked"] for f in fuzz)
if env("FAKE_LIE"):
    ok = True                                   # a runner that claims a pass it didn't earn
doc = {"pass": ok, "contract": env("FAKE_CONTRACT", "v3"), "runs": runs, "fuzz": fuzz,
       "programs": json.loads(env("FAKE_PROGRAMS", json.dumps(progs))), "seeds": seeds,
       "fuzz_seeds": [f["seed"] for f in fuzz], "runtime_s": 1.0}
if env("FAKE_SETUP_ERROR"):
    doc = {"pass": False, "contract": "v3", "error": env("FAKE_SETUP_ERROR")}
if not env("FAKE_NO_JSON"):
    os.makedirs("build", exist_ok=True)
    json.dump(doc, open("build/cosim.json", "w"))
print("building...")
print(json.dumps({"pass": ok, "runs": len(runs), "fuzz": len(fuzz), "failed": 0}))
sys.exit(0 if ok else 1)
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
    assert len(runs) == 2 * 2 * 4
    assert {(r["prog"], r["mode"], r["seed"]) for r in runs} == {
        (p, m, s) for p in ("selftest", "coremark") for m in ("rvfi", "pins") for s in (1, 2, 3, 4)}
    assert [f["seed"] for f in res["data"]["fuzz"]] == [1, 2, 3, 4, 5]
    assert res["data"]["cpi"] == 90.0
    assert "16 runs pass (2 programs x 2 modes x 4 seeds) + liveness fuzz x 5" in res["detail"]


def test_cosim_constants_are_contract_v3():
    assert ev.COSIM_SEEDS == (1, 2, 3, 4) and ev.FUZZ_SEEDS == (1, 2, 3, 4, 5)
    assert ev.LATENCY_CAP == 2048 and ev.FUZZ_MIN_RUNS == 2056


@pytest.mark.parametrize("env,msg", [
    ({"FAKE_COSIM_FAIL": "pins"}, "rd mismatch"),
    ({"FAKE_COSIM_FAIL": "pins", "FAKE_LIE": "1"}, "rd mismatch"),
    ({"FAKE_COSIM_GAP": "2049"}, "exceeds the latency cap 2048"),
    ({"FAKE_FUZZ": "fail"}, "liveness fuzz --seed 1: latency cap"),
    ({"FAKE_FUZZ": "unchecked"}, "liveness was not checked"),
    ({"FAKE_FUZZ": "unchecked", "FAKE_LIE": "1"}, "liveness was not checked"),
    ({"FAKE_FUZZ": "short"}, "only 3 fuzz runs"),
    ({"FAKE_FUZZ": "gap"}, "liveness fuzz --seed 1: max_cycles_per_insn 2049 exceeds"),
    ({"FAKE_SEEDS": "[1, 2, 3]"}, "need [1, 2, 3, 4]"),
    ({"FAKE_FUZZ_SEEDS": "[1, 2, 3]"}, "need [1, 2, 3, 4] and [1, 2, 3, 4, 5]"),
    ({"FAKE_DROP": "coremark/pins/4"}, "missing [('coremark', 'pins', 4)]"),
    ({"FAKE_DUP": "selftest/rvfi/2"}, "extra or repeated [('selftest', 'rvfi', 2)]"),
    ({"FAKE_PROGRAMS": '["selftest"]'}, "differ from `make -C verif/programs list`"),
    ({"FAKE_CONTRACT": "v2"}, "not 'v3'"),
    ({"FAKE_NO_JSON": "1"}, "runner wrote no build/cosim.json"),
    ({"FAKE_SETUP_ERROR": "program build failed: boom"}, "program build failed: boom"),
])
def test_cosim_adapter_fails(cosim_repo, monkeypatch, env, msg):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    res = run_gate(cosim_repo, "cosim")
    assert res["pass"] is False and msg in res["detail"], res["detail"]


def test_cosim_gap_at_the_cap_passes(cosim_repo, monkeypatch):
    monkeypatch.setenv("FAKE_COSIM_GAP", "2048")
    assert run_gate(cosim_repo, "cosim")["pass"] is True


def test_cosim_stale_json_is_not_trusted(cosim_repo, monkeypatch):
    put(cosim_repo, "build/cosim.json", json.dumps({"pass": True, "contract": "v3"}))
    monkeypatch.setenv("FAKE_NO_JSON", "1")
    res = run_gate(cosim_repo, "cosim")
    assert res["pass"] is False and "runner wrote no build/cosim.json" in res["detail"]


def test_cosim_needs_the_program_list(cosim_repo):
    put(cosim_repo, "verif/programs/Makefile", PROGRAMS_MAKEFILE.split("list:")[0])
    res = run_gate(cosim_repo, "cosim")
    assert res["pass"] is False and "make -C verif/programs list" in res["detail"]


def test_eval_jobs(monkeypatch):
    monkeypatch.setenv("BONETTO_EVAL_JOBS", "3")
    assert ev.eval_jobs() == 3
    for bad in ("0", "-2", "x", ""):
        monkeypatch.setenv("BONETTO_EVAL_JOBS", bad)
        assert 1 <= ev.eval_jobs() <= 16


# --- the score: coremark_ips -------------------------------------------------------------------

def config_json(clock_period):
    # TT's src/config.json repeats the "//" comment key; json keeps the last one.
    return ('{\n  "//": "comment",\n  "//": "another",\n  "CLOCK_PERIOD": %s,\n'
            '  "CLOCK_PORT": "clk"\n}\n' % json.dumps(clock_period))


def test_coremark_ips_from_cycles(cosim_repo, monkeypatch):
    assert ev.coremark_ips(1_000_000, 25) == 40.0
    assert ev.coremark_ips(1_000_000, 50) == 20.0
    put(cosim_repo, "src/config.json", config_json(25))
    monkeypatch.setenv("FAKE_CM_CYCLES", "1000000")
    res = run_gate(cosim_repo, "cosim")
    assert res["pass"] is True, res["detail"]
    d = res["data"]
    assert (d["coremark_ips"], d["coremark_cycles_per_iter"], d["clock_period_ns"]) \
        == (40.0, 1000000, 25)
    assert "coremark_ips 40.0" in res["detail"]
    cm = [r for r in d["runs"] if r["prog"] == "coremark"]
    assert cm and all(r["coremark_cycles_per_iter"] == 1000000 for r in cm)


def test_coremark_ips_reaches_eval_json(cosim_repo, monkeypatch):
    put(cosim_repo, "src/config.json", config_json(25))
    monkeypatch.setenv("FAKE_CM_CYCLES", "1000000")
    fake_gates(monkeypatch)
    monkeypatch.setitem(ev.GATES, "cosim", ev.gate_cosim)
    assert ev.main(["--repo", str(cosim_repo), "--min-ips", "39.99"]) == 0
    doc = eval_doc(cosim_repo)
    assert doc["coremark_ips"] == 40.0 and doc["cpi"] == 90.0 and doc["pass"] is True
    assert ev.main(["--repo", str(cosim_repo), "--min-ips", "40"]) == 0     # a tie continues
    assert ev.main(["--repo", str(cosim_repo), "--min-ips", "40.000001"]) == 3


def test_coremark_ips_uses_the_slowest_seed(cosim_repo, monkeypatch):
    put(cosim_repo, "src/config.json", config_json(25))
    monkeypatch.setenv("FAKE_CM_CYCLES", "[1000000, 1000000, 1000000, 1250000]")
    d = run_gate(cosim_repo, "cosim")["data"]
    assert d["coremark_cycles_per_iter"] == 1250000 and d["coremark_ips"] == 32.0


def test_coremark_ips_null_without_cycles(cosim_repo, monkeypatch):
    # No run reports CoreMark cycles (a program without iteration marks, R21): the gate still
    # judges correctness; the score is null (and --min-ips treats it as no gain, R17).
    res = run_gate(cosim_repo, "cosim")
    assert res["pass"] is True and res["data"]["coremark_ips"] is None
    monkeypatch.setenv("FAKE_CM_CYCLES", "null")
    res = run_gate(cosim_repo, "cosim")
    assert res["pass"] is True and res["data"]["coremark_ips"] is None


@pytest.mark.parametrize("cycles,config,msg", [
    ("0", config_json(25), "bad coremark_cycles_per_iter 0"),
    ('"many"', config_json(25), "bad coremark_cycles_per_iter 'many'"),
    ("true", config_json(25), "bad coremark_cycles_per_iter True"),
    ("1000000", None, "src/config.json"),
    ("1000000", config_json(0), "CLOCK_PERIOD"),
    ("1000000", config_json("25"), "CLOCK_PERIOD"),
    ("1000000", '{"CLOCK_PORT": "clk"}', "CLOCK_PERIOD"),
    ("1000000", "not json", "src/config.json"),
], ids=["zero", "string", "bool", "no-config", "period-0", "period-string", "no-period",
        "config-not-json"])
def test_coremark_ips_bad_inputs_fail(cosim_repo, monkeypatch, cycles, config, msg):
    if config is not None:
        put(cosim_repo, "src/config.json", config)
    monkeypatch.setenv("FAKE_CM_CYCLES", cycles)
    res = run_gate(cosim_repo, "cosim")
    assert res["pass"] is False and msg in res["detail"], res["detail"]
