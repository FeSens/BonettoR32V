"""Tests for the BonettoR32V formal gate (verif/formal).

  pytest verif/formal/test_formal.py -v

- The rv32e ISA files are up to date with gen_isa_rv32e.py (contract v3: the 40 base
  instructions, the six CSR instructions, MRET and WFI).
- The QSPI chip monitors decode a hand-written Pmod waveform (both power-up modes) into
  exactly the events contract 2.2 and 2.3 imply, violations included.
- The depth policy (depth=short, cpi=N), the Pmod limits at the signoff period, the timing
  summary.
- The verdict aggregation (a group counterexample, SKIPPED tasks).
- Every mutant in mutants/ FAILS the checks its expect.json names, with a
  counterexample, at the champion's depth (proof tasks only); mutants marked "pass"
  PASS ("checks": "all": the whole fast suite). Mutants marked "slow" run only with
  BONETTO_FORMAL_SLOW=1.
- The champion (src/) PASSES the fast suite (checks.cfg). Set BONETTO_SKIP_SEED=1 to skip.

With BONETTO_FORMAL_HOST set, every formal run goes to that host (its own lock).

The formal runs (mutants, seed) take the heavy-job lock /tmp/bonetto-heavy.lock once
for the whole session (the same lock as `lockf -k /tmp/bonetto-heavy.lock`). If you
already run pytest under that lock, set BONETTO_HEAVY_LOCK_HELD=1.
"""
import fcntl
import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RUN = os.path.join(HERE, "run_formal.py")
MUT_DIR = os.path.join(HERE, "mutants")
LOCK_PATH = "/tmp/bonetto-heavy.lock"

sys.path.insert(0, HERE)
import run_formal  # noqa: E402


def mutants():
    if not os.path.isdir(MUT_DIR):
        return []
    return sorted(d for d in os.listdir(MUT_DIR)
                  if os.path.isfile(os.path.join(MUT_DIR, d, "expect.json")))


@pytest.fixture(scope="session")
def heavy_lock():
    """Hold the heavy-job lock for every formal run of the session (one acquisition).
    Not needed when the runs go to a remote host (BONETTO_FORMAL_HOST): that host has
    its own lock."""
    if os.environ.get("BONETTO_HEAVY_LOCK_HELD") == "1" or os.environ.get("BONETTO_FORMAL_HOST"):
        yield
        return
    with open(LOCK_PATH, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def formal(args, timeout=4 * 3600):
    """run_formal.py; callers depend on the heavy_lock fixture."""
    cmd = [sys.executable, RUN] + args
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


# ---------------------------------------------------------------------------

def test_isa_generated_up_to_date():
    p = subprocess.run([sys.executable, os.path.join(HERE, "gen_isa_rv32e.py"), "--check"],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    insns = open(os.path.join(HERE, "isa_rv32e.txt")).read().split()
    assert len(insns) == 48 and {"fence", "ecall", "ebreak", "sub", "lw", "csrrw", "csrrci",
                                 "mret", "wfi"} <= set(insns)


def test_depth_policy():
    cfg = {"reset_steps": 1, "depth_margin": 2, "depth_floor": 0, "depth_cap": 2048}
    d, dmin = run_formal.pick_depth(cfg, {"first_retire_cycle": 114, "max_cycles_per_insn": 230})
    assert dmin == 1 + 230 + 230 + 2 and d == dmin
    cfg["depth_floor"] = 600
    assert run_formal.pick_depth(cfg, {"first_retire_cycle": 114, "max_cycles_per_insn": 230})[0] == 600
    assert sorted(sum(run_formal.CLASS_GROUPS, [])) == sorted(run_formal.isa_insns())
    # The cosim's per_class format ({class: cycles}, its own class names).
    t = {"first_retire_cycle": 116, "max_cycles_per_insn": 230,
         "per_class": {"alu": 116, "shift": 116, "load": 230, "io_load": 118, "trap": 116,
                       "branch_taken": 116, "branch_not_taken": 116}}
    assert run_formal.short_depth(cfg, t, ["add", "sll"]) == 1 + 116 + 116 + 2
    assert run_formal.short_depth(cfg, t, ["lw"]) == 1 + 116 + 230 + 2
    assert run_formal.short_depth(cfg, t, ["beq"]) == 1 + 116 + 116 + 2
    assert run_formal.short_depth(cfg, t, ["sw"]) == 1 + 116 + 230 + 2   # store not measured
    assert run_formal.short_depth(cfg, t, ["csrrw"]) == 1 + 116 + 230 + 2   # csr not measured
    t["per_class"]["csr"] = 120
    assert run_formal.short_depth(cfg, t, ["csrrw", "csrrsi"]) == 1 + 116 + 120 + 2
    # cpi=N: reset + first retirement + N x the slowest class + margin; the option picks it.
    assert run_formal.cpi_depth(cfg, t, 4) == 1 + 116 + 4 * 230 + 2
    assert run_formal.base_depth(cfg, t, {"cpi": "2"}, 999) == 1 + 116 + 2 * 230 + 2
    assert run_formal.base_depth(cfg, t, {"depth": "short"}, 999, ["add"]) == 1 + 116 + 116 + 2
    assert run_formal.base_depth(cfg, t, {}, 999) == 999


def test_config_split():
    """Fast: every check at depth=reach (proof depth from its reach task, capped at the short
    depth), coherence and csr with the triggers reachable there (a subset of their own), no
    irq, no liveness, a task timeout that fits a 6-hour run. Deep: every check, coherence and
    csr at cpi=2 and irq at cpi=4 with every trigger, liveness."""
    fast = run_formal.load_config(os.path.join(HERE, "checks.cfg"))
    deep = run_formal.load_config(os.path.join(HERE, "checks-deep.cfg"))
    f = {n: o for n, _, o in fast["checks"]}
    d = {n: o for n, _, o in deep["checks"]}
    assert all(o.get("depth") == "reach" and "cpi" not in o for o in f.values())
    assert all(o.get("depth") != "reach" for o in d.values())
    assert "irq" not in f and "liveness" not in f and fast["timeout_s"] <= 18000
    for name in ("coherence", "csr"):
        labels = f[name]["reach"].split(",")
        assert labels and set(labels) < set(run_formal.REACH[name])
        assert "reach" not in d[name] and d[name]["cpi"] == "2"
    assert d["irq"]["cpi"] == "4" and "liveness" in d
    assert set(f) <= set(d) and deep["timeout_s"] >= fast["timeout_s"]


def test_pmod_limits():
    """tCEM = floor(8000 / T), tRST = ceil(50 / T) cycles (contract 2.1.7, 2.3); a period whose
    2-cycle SCK would beat the slowest command's 33 MHz is refused (the monitor counts edges,
    not SCK phases)."""
    assert run_formal.pmod_limits(20.0) == (400, 3)
    assert run_formal.pmod_limits(25.0) == (320, 2)
    assert run_formal.pmod_limits(15.2) == (526, 4)
    with pytest.raises(SystemExit):
        run_formal.pmod_limits(15.0)


def test_check_verdicts():
    """A check PASSes only if its proof task passed and its reach task reached every
    required trigger; a counterexample is a FAIL; a missing trigger is UNREACHED; a task
    that did not finish (timeout, error, unknown) is a FAIL."""
    c = run_formal.classify
    req = ["reach_insn", "reach_insn_trap"]
    det = lambda *l: {"failed": list(l)}
    both = det("reach_insn", "reach_insn_trap")
    assert c("PASS", det(), "FAIL", both, req)[0] == "PASS"
    assert c("PASS", det(), "FAIL", det("reach_insn"), req)[0] == "UNREACHED"
    assert c("PASS", det(), "PASS", det(), req)[0] == "UNREACHED"
    v, info = c("FAIL", det("insn_rd_wdata"), "FAIL", both, req)
    assert v == "FAIL" and info["assertions"] == ["insn_rd_wdata"]
    assert c("FAIL", det(), "FAIL", both, req)[0] == "FAIL"
    for st in ("TIMEOUT", "UNKNOWN", "ERROR"):
        assert c(st, det(), "FAIL", both, req)[0] == "FAIL"
        assert c("PASS", det(), st, both, req)[0] == "FAIL"
    assert run_formal.insn_reach("jal") == ["reach_insn_jal", "reach_insn_jal_trap"]
    assert run_formal.insn_reach("add") == ["reach_insn_add"]


def test_kill_tree():
    """stop_all/kill_tree reach every process below an SBY run, also those in other
    process groups (SBY runs each engine in its own process group)."""
    import time
    pr = subprocess.Popen(["sh", "-c", "sleep 60 & (setsid sleep 61 || perl -e 'setpgrp; sleep 61') & wait"],
                          start_new_session=True)
    time.sleep(1)
    below = run_formal.descendants(pr.pid)
    assert len(below) >= 2
    run_formal.kill_tree(pr.pid)
    pr.wait(timeout=10)
    time.sleep(0.5)
    alive = []
    for p in below:
        try:
            os.kill(p, 0)
            st = subprocess.run(["ps", "-o", "stat=", "-p", str(p)], capture_output=True,
                                text=True).stdout.strip()
            if st and not st.startswith("Z"):
                alive.append(p)
        except ProcessLookupError:
            pass
    assert alive == []


def test_remote_launch_and_stop(tmp_path):
    """The detached launch returns at once (only the simple command is backgrounded, so no
    shell keeps the ssh channel open), and the stop signals only the run's own process
    group, never a remote.pid process that works elsewhere (a reused pid)."""
    import shlex
    import time
    launch = run_formal.remote_launch_cmd(shlex.quote(str(tmp_path / "run")),
                                          "echo $$ > remote.pid; exec sleep 60")
    assert "&& setsid" not in launch and launch.endswith("&")
    if not shutil.which("setsid") or not os.path.isdir("/proc/self"):
        pytest.skip("needs setsid and /proc (Linux), like the remote host")
    for d in ("run", "fake", "other"):
        (tmp_path / d).mkdir()
    t0 = time.time()
    r = subprocess.run(["sh", "-c", launch], capture_output=True, timeout=20)
    assert r.returncode == 0 and time.time() - t0 < 5
    pidf = tmp_path / "run" / "remote.pid"
    for _ in range(50):
        if pidf.exists() and pidf.read_text().strip():
            break
        time.sleep(0.1)
    p = int(pidf.read_text())
    other = subprocess.Popen(["sleep", "60"], cwd=tmp_path / "other", start_new_session=True)
    try:
        (tmp_path / "fake" / "remote.pid").write_text(f"{other.pid}\n")
        subprocess.run(["sh", "-c", run_formal.remote_stop_cmd(shlex.quote(str(tmp_path / "fake")))],
                       timeout=30)
        assert other.poll() is None          # someone else's process: left alone
        os.kill(p, 0)                        # and the run is untouched
        subprocess.run(["sh", "-c", run_formal.remote_stop_cmd(shlex.quote(str(tmp_path / "run")))],
                       timeout=90)
        with pytest.raises(ProcessLookupError):
            os.kill(p, 0)                    # its own run: stopped
    finally:
        os.killpg(other.pid, 15)
        other.wait()
        try:
            os.killpg(p, 15)
        except OSError:
            pass


def test_mem_jobs(monkeypatch):
    """Jobs are capped by memory at the deepest task's depth (0.4 GB + 9.5 MB per step, the
    v3 champion's worst): the champion's short depth (581, about 5.9 GB) fits 8 jobs in 50 GB,
    its irq depth (1,448, about 14.2 GB) 3, the Mac's 16 GB 1; at least 1."""
    assert run_formal.mem_jobs(581, 18, budget=50.0)[0] == 8
    assert run_formal.mem_jobs(1448, 18, budget=50.0)[0] == 3
    assert run_formal.mem_jobs(1448, 8, budget=16.0)[0] == 1
    assert run_formal.mem_jobs(5000, 8, budget=4.0)[0] == 1
    monkeypatch.setenv("BONETTO_FORMAL_MEM_GB", "13")
    assert run_formal.mem_budget_gb() == 13.0
    # reset counts twice (22 GB seen at depth 1,222 on a v2 core); reach tasks like their check.
    assert abs(run_formal.task_mem_gb("reset", 1222) - 2 * run_formal.task_mem_gb("reg", 1222)) < 1e-9
    assert run_formal.task_mem_gb("reset_reach", 1222) == run_formal.task_mem_gb("reset", 1222)
    # The gate: a task waits while it does not fit next to the running ones.
    import threading
    g = run_formal.MemGate(10.0)
    g.acquire(6.0)
    t = threading.Thread(target=g.acquire, args=(6.0,))
    t.start()
    t.join(0.3)
    assert t.is_alive()          # 6 + 6 > 10: waits
    g.release(6.0)
    t.join(2)
    assert not t.is_alive() and g.used == 6.0
    g.release(6.0)
    g.acquire(20.0)              # alone, a task always runs
    assert g.used == 20.0


def test_task_line():
    """Progress lines: a reach task whose SBY status is FAIL with every required label is
    REACHED (the reach labels are assert(!trigger)); a missing label, or a reach task that
    "passed" (nothing reached), is UNREACHED and names what is missing."""
    t = run_formal.task_line
    req = ["reach_insn_add", "reach_insn_sub"]
    assert "REACHED   2/2 triggers" in t("insn_group0_reach", "FAIL", 9.0, req[::-1], req)
    line = t("insn_group0_reach", "FAIL", 9.0, ["reach_insn_add"], req)
    assert "UNREACHED 1/2" in line and "missing: reach_insn_sub" in line
    assert "UNREACHED 0/2" in t("insn_group0_reach", "PASS", 9.0, [], req)
    assert "TIMEOUT" in t("insn_group0_reach", "TIMEOUT", 9.0, [], req)
    assert "failed assertions: insn_rd_wdata" in t("insn_group0", "FAIL", 9.0, ["insn_rd_wdata"])


def test_reach_depth(tmp_path):
    """depth=reach: parse_task keeps the last step of the solver window where each label
    first failed; the proof depth is one past the latest required one plus the margin, at
    least reset + first retirement + margin, at most the cap; a missing trigger leaves the
    cap."""
    d = tmp_path / "x_reach"
    d.mkdir()
    (d / "status").write_text("FAIL\n")
    (d / "logfile.txt").write_text(
        "##   0:00:01  Checking assertions in steps 261 to 289..\n"
        "##   0:00:02  Assert failed in rvfi_testbench: checker_inst.insn_spec.reach_insn_beq\n"
        "##   0:00:02  Writing trace to VCD file: engine_0/trace0.vcd\n"
        "##   0:00:03  Checking assertions in steps 377 to 405..\n"
        "##   0:00:04  Assert failed in rvfi_testbench: checker_inst.insn_spec.reach_insn_bne\n"
        "##   0:00:04  Assert failed in rvfi_testbench: checker_inst.insn_spec.reach_insn_beq "
        "[failed before]\n")
    st, det = run_formal.parse_task(str(tmp_path), "x_reach")
    assert st == "FAIL" and det["steps"] == {"reach_insn_beq": 289, "reach_insn_bne": 405}
    cfg = {"reset_steps": 1, "depth_margin": 2}
    t = {"first_retire_cycle": 305}
    res = ("x_reach", st, 9.0, det)
    assert run_formal.reach_depth(cfg, t, 613, ["reach_insn_beq", "reach_insn_bne"], res)[0] \
        == 405 + 1 + 2
    assert run_formal.reach_depth(cfg, t, 613, ["reach_insn_beq"], res)[0] == 1 + 305 + 2
    assert run_formal.reach_depth(cfg, t, 400, ["reach_insn_bne"], res)[0] == 400
    assert run_formal.reach_depth(cfg, t, 613, ["reach_insn_blt"], res)[0] == 613
    assert run_formal.reach_depth(cfg, t, 613, ["reach_insn_beq"],
                                  ("x_reach", "TIMEOUT", 9.0, det))[0] == 613
    assert run_formal.base_depth(cfg | {"depth_floor": 0}, t | {"max_cycles_per_insn": 305},
                                 {"depth": "reach"}, 999) == 1 + 305 + 305 + 2


def test_timing_merge():
    """The timing is the max over the cosim programs, per class too."""
    a = {"first_retire_cycle": 115, "max_cycles_per_insn": 230,
         "per_class": {"alu": 116, "load": 230}}
    b = {"first_retire_cycle": 993, "max_cycles_per_insn": 893,
         "per_class": {"alu": 700, "store": 893}}
    m = run_formal.merge_timings([a, b])
    assert m["first_retire_cycle"] == 993 and m["max_cycles_per_insn"] == 893
    assert m["per_class"] == {"alu": 700, "load": 230, "store": 893}


def _cex_vcd(path, insn):
    """A minimal counterexample trace: one retirement of `insn` in step 1."""
    path.write_text("$scope module rvfi_testbench $end\n"
                    "$var wire 1 ! rvfi_valid $end\n$var wire 32 \" rvfi_insn $end\n"
                    "$upscope $end\n$enddefinitions $end\n"
                    f"#0\n0!\nb0 \"\n#10\n1!\nb{insn:032b} \"\n#20\n0!\n")
    return str(path)


def test_aggregate_group_failure(tmp_path):
    """A counterexample in a two-member group: the culprit FAILs with its assertion
    and trace, the other member FAILs as not proven, and nothing crashes (review I2)."""
    vcd = _cex_vcd(tmp_path / "trace.vcd", 0x40000033)   # sub x0, x0, x0
    proof = ("insn_group0", "FAIL", 12.3,
             {"failed": ["insn_rd_wdata"], "traces": {"insn_rd_wdata": vcd}, "errors": []})
    reach = ("insn_group0_reach", "FAIL", 4.5,
             {"failed": ["reach_insn_add", "reach_insn_sub"], "traces": {}, "errors": []})
    units = [("insn_group0", [("insn_add", ["reach_insn_add"]),
                              ("insn_sub", ["reach_insn_sub"])])]
    checks, runtimes, failures, reached, skipped = run_formal.aggregate(
        units, {"insn_group0": proof, "insn_group0_reach": reach})
    assert checks == {"insn_add": "FAIL", "insn_sub": "FAIL"}
    assert failures["insn_sub"]["assertions"] == ["insn_rd_wdata"]
    assert failures["insn_sub"]["trace"] == vcd
    assert "not proven" in failures["insn_add"]["why"] and "assertions" not in failures["insn_add"]
    assert failures["insn_add"]["trace"] == vcd
    assert runtimes["insn_add"] == {"proof": 12.3, "reach": 4.5, "task": "insn_group0"}
    assert skipped == []


def test_aggregate_skipped():
    """Fail-fast: a proof that FAILed stays FAIL even if its reach task was skipped; a
    check whose proof did not run is SKIPPED, or UNREACHED if its finished reach task
    misses a trigger, or FAIL if its reach task timed out or errored."""
    det = lambda *l: {"failed": list(l), "traces": {}, "errors": []}
    res = {
        "qspi": ("qspi", "FAIL", 9.0, det("qspi_rule2")),
        "qspi_reach": ("qspi_reach", "SKIPPED", 0.0, det()),
        "gpio": ("gpio", "SKIPPED", 0.0, det()),
        "gpio_reach": ("gpio_reach", "FAIL", 3.0, det("reach_gpio_load", "reach_gpio_store")),
        "reg": ("reg", "PASS", 20.0, det()),
        "reg_reach": ("reg_reach", "SKIPPED", 0.0, det()),
        "ill": ("ill", "SKIPPED", 0.0, det()),
        "ill_reach": ("ill_reach", "PASS", 5.0, det("reach_ill_zero")),
        "rvfi": ("rvfi", "SKIPPED", 0.0, det()),
        "rvfi_reach": ("rvfi_reach", "TIMEOUT", 99.0, det()),
    }
    units = [("qspi", [("qspi", ["reach_qspi_flash_read"])]),
             ("gpio", [("gpio", ["reach_gpio_load", "reach_gpio_store"])]),
             ("reg", [("reg", ["reach_reg"])]),
             ("ill", [("ill", ["reach_ill_zero", "reach_ill_ones"])]),
             ("rvfi", [("rvfi", ["reach_rvfi"])])]
    checks, _, failures, _, skipped = run_formal.aggregate(units, res)
    assert checks == {"qspi": "FAIL", "gpio": "SKIPPED", "reg": "SKIPPED", "ill": "UNREACHED",
                      "rvfi": "FAIL"}
    assert failures["rvfi"]["why"] == "reach task TIMEOUT"
    assert failures["qspi"]["assertions"] == ["qspi_rule2"]
    assert failures["ill"]["unreached"] == ["reach_ill_ones"]
    assert sorted(skipped) == ["gpio", "reg"]


def test_reap_remote_orphans(tmp_path, monkeypatch):
    """Orphaned remote runs (this checkout's run dirs whose local runner is dead): a
    finished one's result is fetched and its dir deleted (kept if the fetch fails), a
    dead one's dir is deleted, a running one stays. A live runner's dirs, another
    checkout's dirs on the same machine and other machines' dirs are never touched.
    The 'ssh' here runs the commands locally."""
    dead = subprocess.Popen(["true"])
    dead.wait()
    base = tmp_path / "remote"
    done, gone, running, nofetch = (f"run-mac-abc123-{dead.pid}-{i}" for i in (1, 4, 5, 6))
    keep = [f"run-mac-abc123-{os.getpid()}-2", f"run-other-abc123-{dead.pid}-3",
            f"run-mac-def456-{dead.pid}-7", "formal.lock", running, nofetch]
    for n in [done, gone] + keep:
        (base / n).mkdir(parents=True)
    for n in (done, nofetch):
        (base / n / "done").write_text("0\n")
    (base / running / "remote.pid").write_text(f"{os.getpid()}\n")
    monkeypatch.setattr(run_formal, "REMOTE_BASE", str(base))
    ssh = ["sh", "-c"]
    assert sorted(run_formal.remote_orphans(ssh, "mac", "abc123")) == sorted(
        [(done, "done"), (gone, "dead"), (running, "running"), (nofetch, "done")])
    calls = []

    def fake_fetch(host, rdir, out, logs):
        calls.append(rdir)
        if rdir.endswith(nofetch):
            return False
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w").write("{}")
        return True
    monkeypatch.setattr(run_formal, "fetch_remote", fake_fetch)
    got = run_formal.reap_remote(ssh, "host", "mac", str(tmp_path / "local"), "abc123")
    assert sorted(calls) == sorted([f"{base}/{done}", f"{base}/{nofetch}"])
    assert got == [str(tmp_path / "local" / f"orphan-{done}" / "formal.json")]
    assert sorted(os.listdir(base)) == sorted(keep)


def test_stage_takes_every_top_level_v(tmp_path):
    """Contract section 1: every top-level src/*.v is design, test_*.v included;
    subdirectories are not. The whitelisted sky130 cells' behavioral models (verif/cells)
    come after the design."""
    src = tmp_path / "src"
    (src / "test_unit").mkdir(parents=True)
    for n in ("tt_um_fesens_r32v.v", "test_helper.v", "r32v_alu.v"):
        (src / n).write_text("// x\n")
    (src / "test_unit" / "tb.v").write_text("// x\n")
    (src / "notes.md").write_text("x\n")
    _, rtl = run_formal.stage(str(src), str(tmp_path / "work"))
    names = [os.path.basename(f) for f in rtl]
    cells = sorted(f for f in os.listdir(os.path.join(ROOT, "verif", "cells")) if f.endswith(".v"))
    assert names == ["r32v_alu.v", "test_helper.v", "tt_um_fesens_r32v.v"] + cells
    assert "sky130_fd_sc_hd__sdfxtp_1.v" in cells


def test_reach_and_proof_tasks_are_separate(tmp_path):
    """The proof task has no reach statements; the reach task keeps only them."""
    run_formal.setup_path()
    p = run_formal.write_sby(str(tmp_path), "x", "bonetto_qspi_check", 100,
                             "smtbmc bitwuzla step=all", 60,
                             extra_defines=["`define BONETTO_TCEM 400"])
    r = run_formal.write_sby(str(tmp_path), "x_reach", "bonetto_qspi_check", 100,
                             "smtbmc bitwuzla step=29", 60, role="reach",
                             reach=["reach_qspi_flash_read", "reach_qspi_psram_write"])
    pt, rt = open(p).read(), open(r).read()
    assert "chformal -cover -remove" in pt and "BONETTO_REACH_AS_ASSERT" not in pt
    assert "--keep-going" not in pt and "-t 0:100:100" in pt
    assert "`define BONETTO_TCEM 400" in pt
    assert "BONETTO_REACH_AS_ASSERT" in rt
    assert "select -set keep c:*.reach_qspi_flash_read c:*.reach_qspi_psram_write" in rt
    assert "chformal -assert -remove c:* @keep %d" in rt
    assert "smtbmc --keep-going --nopresat bitwuzla -- -t 0:29:100" in rt


# ---------------------------------------------------------------------------
# QSPI chip monitors (wrapper.sv bonetto_qspi_chip) against a hand-written stub driver.
# The expected events come from a Python reading of contract 2.2 and 2.3 (the tables below),
# not from the Verilog.

# command: (address edges (24 single or 6 quad), mode edges, dummy/wait edges, data kind);
# None: no fields. Data kinds: os/oq data out single/quad, is/iq data in single/quad.
QSPI_FLASH = {0x03: (24, 0, 0, "os"), 0x0B: (24, 0, 8, "os"), 0x6B: (24, 0, 8, "oq"),
              0xEB: (6, 2, 4, "oq"), 0xFF: None, 0x02: (24, 0, 0, "is")}
QSPI_PSPI = {0x03: (24, 0, 0, "os"), 0x0B: (24, 0, 8, "os"), 0xEB: (6, 0, 6, "oq"),
             0x02: (24, 0, 0, "is"), 0x38: (6, 0, 0, "iq"), 0x35: None, 0x66: None, 0x99: None}
QSPI_PQPI = {0x0B: (6, 0, 4, "oq"), 0xEB: (6, 0, 6, "oq"), 0x02: (6, 0, 0, "iq"),
             0x38: (6, 0, 0, "iq"), 0xF5: None, 0x66: None, 0x99: None}
QSPI_TCEM, QSPI_TRST = 40, 3     # small, so the stub stays short
QSPI_SIGS = ["dout", "rd", "wb", "fin", "fin_rd", "fin_wr", "bad_cmd", "bad_extra", "bad_wend",
             "bad_trst", "bad_cem"]


class _QspiChip:
    """The contract's view of one chip: its mode, and the events of each transaction."""

    def __init__(self, psram, mode0):
        self.psram = psram
        self.cont = not psram and mode0
        self.qpi = psram and mode0
        self.init = self.rsten = False
        self.reset_end = None      # step a 0x99 ended at, until the next CS_n fall
        self.p = "r_" if psram else "f_"

    def put(self, ev, st, **kw):
        ev.setdefault(st, {}).update({self.p + k: v for k, v in kw.items()})

    def fall(self, ev, st):
        if self.psram and self.reset_end is not None and st - self.reset_end < QSPI_TRST:
            self.put(ev, st, bad_trst=1)
        self.reset_end = None

    def txn(self, ev, edges, end):
        """edges: [(step, nibble)] at the rising edges of one CS_n-low window; end: the step
        CS_n is high again."""
        if not self.psram and self.cont:
            cmd, rest, fields = 0xEB, edges, QSPI_FLASH[0xEB]
        else:
            quad = self.psram and self.qpi
            clen = 2 if quad else 8
            if len(edges) < clen:
                return                                  # ends inside the command: ignored
            cmd = 0
            for _, nb in edges[:clen]:
                cmd = ((cmd << 4) | nb) & 0xFF if quad else ((cmd << 1) | (nb & 1)) & 0xFF
            table = QSPI_FLASH if not self.psram else QSPI_PQPI if self.qpi else QSPI_PSPI
            legal = cmd in table
            if self.psram and legal:
                legal = (cmd == 0x99) == self.rsten and (table[cmd] is None or self.init)
            rest = edges[clen:]
            if not legal:
                self.put(ev, edges[clen - 1][0], bad_cmd=1)
                assert not rest, "the stub ends an illegal command at its last edge"
                fields = "bad"
            else:
                fields = table[cmd]
        kind, nbytes, mval, mseen, partial, short = None, 0, 0, 0, False, False
        if fields is None:
            for st, _ in rest:
                self.put(ev, st, bad_extra=1)
        elif fields != "bad":
            alen, mlen, dlen, kind = fields
            addr = 0
            for _, nb in rest[:alen]:
                addr = ((addr << 4) | nb) if alen == 6 else ((addr << 1) | (nb & 1))
            short = len(rest) < alen
            for _, nb in rest[alen:alen + mlen]:
                mval, mseen = ((mval << 4) | nb) & 0xFF, mseen + 1
            data = rest[alen + mlen + dlen:]
            quad = kind in ("oq", "iq")
            per = 2 if quad else 8
            byte = 0
            for j, (st, nb) in enumerate(data):
                k, idx = divmod(j, per)
                a = (addr + k) & (0x7FFFFF if self.psram else 0xFFFFFF)
                if kind[0] == "o":
                    self.put(ev, st, rd=1, rd_addr=a, rd_quad=int(quad), rd_idx=idx)
                else:
                    byte = ((byte << 4) | nb) & 0xFF if quad else ((byte << 1) | (nb & 1)) & 0xFF
                    if idx == per - 1:
                        self.put(ev, st, wb=1, wb_addr=a, wb_data=byte)
                        nbytes += 1
            if kind[0] == "o" and data:
                for st in range(data[0][0], end):
                    self.put(ev, st, dout=1, dquad=int(quad))
            partial = kind[0] == "i" and len(data) % per != 0
        self.put(ev, end, fin=1, fin_rd=int(kind is not None and kind[0] == "o" and
                                             len(rest) > sum(fields[:3])),
                 fin_wr=int(nbytes > 0),
                 bad_wend=int(kind is not None and kind[0] == "i" and (short or partial)))
        if not self.psram:
            if mseen == 2:
                self.cont = (mval >> 4) & 3 == 2
        else:
            self.rsten = cmd == 0x66
            if cmd == 0x99:
                self.init, self.qpi, self.reset_end = True, False, end
            elif cmd == 0x35:
                self.qpi = True
            elif cmd == 0xF5:
                self.qpi = False


def _qspi_stub(fcont, pqpi):
    """Steps of (cf, cr, sck, nibble) and the expected events {step: {signal: value}}."""
    steps, ev = [], {}
    chips = {"f": _QspiChip(False, fcont), "r": _QspiChip(True, pqpi)}

    def put(cf, cr, sck, nb):
        steps.append((cf, cr, sck, nb))
        return len(steps) - 1

    def idle(n):
        for _ in range(n):
            put(1, 1, 0, 0xA)

    def txn(chip, nibs, high=1, low_only=0):
        """One CS_n-low window clocking `nibs` (one per rising edge); SCK high phases of
        `high` steps; low_only: a window of that many steps with no edge."""
        cf, cr = (0, 1) if chip == "f" else (1, 0)
        st0 = put(cf, cr, 0, nibs[0] if nibs else 0)    # CS_n falls with SCK low
        chips[chip].fall(ev, st0)
        for _ in range(low_only - 1):
            put(cf, cr, 0, 0x5)
        edges = []
        for k, nb in enumerate(nibs):
            edges.append((put(cf, cr, 1, nb), nb))      # the rising edge
            for _ in range(high - 1):
                put(cf, cr, 1, nb)                      # held while SCK is high
            put(cf, cr, 0, nibs[k + 1] if k + 1 < len(nibs) else 0x3)
        end = put(1, 1, 0, 0xC)
        if chip == "r":                                 # tCEM: low for more than QSPI_TCEM steps
            for i in range(QSPI_TCEM, end - st0):
                chips["r"].put(ev, st0 + i, bad_cem=1)
        chips[chip].txn(ev, edges, end)

    def single(byte, other=0xA):
        """8 single edges (MSB first on SD0); the other lines carry `other`'s bits."""
        return [(other & 0xE) | ((byte >> (7 - i)) & 1) for i in range(8)]

    def addr_s(a):
        return [(0x6 & 0xE) | ((a >> (23 - i)) & 1) for i in range(24)]

    def nibs(x, n):
        return [(x >> (4 * (n - 1 - i))) & 0xF for i in range(n)]

    idle(3)
    txn("f", [0xF] * 8)                                         # 2.4 step 1
    idle(1)
    txn("r", [0xF, 0x5])                                        # QPI-form 0xF5
    idle(1)
    txn("r", single(0x66))
    txn("r", single(0x99))
    idle(QSPI_TRST - 1)                                         # high for exactly tRST
    txn("r", single(0x35))                                      # QPI from here
    idle(1)
    txn("r", nibs(0x38, 2) + nibs(0xFFFFFE, 6) + nibs(0xA53C81, 6))   # A[23] ignored, wraps
    idle(1)
    txn("r", nibs(0x0B, 2) + nibs(0x123456, 6) + [0x9] * 4 + nibs(0x5AC37, 5), high=2)
    idle(1)
    txn("r", nibs(0x02, 2) + nibs(0x000010, 6) + nibs(0x123, 3))     # ends mid-byte
    idle(1)
    txn("r", nibs(0x02, 2) + nibs(0x0000, 4))                   # ends in the address
    idle(1)
    txn("f", single(0xEB) + nibs(0x00ABCD, 6) + nibs(0x20, 2) + [0x0] * 4 + nibs(0x1234, 4))
    idle(1)
    txn("f", nibs(0xFFFFFF, 6) + nibs(0x20, 2) + [0x0] * 4 + nibs(0x9876, 4))   # wraps
    idle(1)
    txn("f", nibs(0x000100, 6) + nibs(0xFF, 2) + [0x0] * 2)     # M = 0xFF, ends in dummy
    idle(1)
    txn("f", single(0x03) + addr_s(0x0F00F0) + single(0xC3)[:8] + [0x2, 0x0])   # 10 bits
    idle(1)
    txn("f", single(0x6B) + addr_s(0x000004) + [0x0] * 8 + nibs(0xE1, 2))
    idle(1)
    txn("f", single(0x02) + addr_s(0x000040) + single(0x5A))    # page program, ignored
    idle(1)
    txn("f", single(0x9F))                                      # not listed
    idle(1)
    txn("f", single(0xFF) + [0x1])                              # 0xFF takes no fields
    idle(1)
    txn("r", nibs(0x0C, 2))                                     # not listed in QPI mode
    idle(1)
    txn("r", nibs(0x66, 2) + [0x0])                             # an edge after 0x66
    idle(1)
    txn("r", nibs(0x99, 2))                                     # reset (QPI form): SPI mode
    txn("r", single(0x35))                                      # CS_n high 1 step: tRST
    idle(1)
    txn("r", [], low_only=QSPI_TCEM + 2)                        # CS_n low past tCEM
    idle(1)
    txn("f", [0x1] * 3)                                         # ends in the command
    idle(2)
    return steps, ev


def _qspi_tb(steps, ev, fcont, pqpi):
    lines = ["module stub_tb (input clock);",
             "  reg [15:0] step = 16'd0;",
             "  always @(posedge clock) step <= step + 16'd1;",
             "  reg cf, cr, sck; reg [3:0] nib;",
             "  always @* begin",
             "    {cf, cr, sck, nib} = 7'b1100000;",
             "    case (step)"]
    for i, (cf, cr, s, nb) in enumerate(steps):
        lines.append(f"      {i}: {{cf, cr, sck, nib}} = {{1'b{cf}, 1'b{cr}, 1'b{s}, 4'h{nb:x}}};")
    lines += ["    endcase", "  end",
              "  reg psck = 1'b0;",
              "  always @(posedge clock) psck <= sck;",
              "  wire reset = step == 16'd0, en = step >= 16'd2, rise = en && !psck && sck;"]
    for p, ps, m0 in (("f_", 0, fcont), ("r_", 1, pqpi)):
        lines += [f"  wire {', '.join(p + s for s in QSPI_SIGS)}, {p}dquad, {p}rd_quad;",
                  f"  wire [23:0] {p}rd_addr, {p}wb_addr; wire [2:0] {p}rd_idx; "
                  f"wire [7:0] {p}wb_data;",
                  f"  bonetto_qspi_chip #(.PSRAM({ps})) {p}chip (.clock(clock), .reset(reset), "
                  f".en(en), .cs({'cf' if ps == 0 else 'cr'}), .rise(rise), .nib(nib), "
                  f".mode0(1'b{int(m0)}),",
                  "    " + ", ".join(f".{s}({p}{s})" for s in QSPI_SIGS + [
                      "dquad", "rd_addr", "rd_quad", "rd_idx", "wb_addr", "wb_data"]) + ");"]
    lines += ["  always @* begin", "    if (en) case (step)"]

    def conds(e):
        c = []
        for p in ("f_", "r_"):
            c += [f"{p}{s} == {e.get(p + s, 0)}" for s in QSPI_SIGS]
            if e.get(p + "dout"):
                c.append(f"{p}dquad == {e[p + 'dquad']}")
            if e.get(p + "rd"):
                m = "[22:0]" if p == "r_" else ""
                c += [f"{p}rd_addr{m} == 24'h{e[p + 'rd_addr']:06x}",
                      f"{p}rd_quad == {e[p + 'rd_quad']}", f"{p}rd_idx == {e[p + 'rd_idx']}"]
            if e.get(p + "wb"):
                c += [f"{p}wb_addr == 24'h{e[p + 'wb_addr']:06x}",
                      f"{p}wb_data == 8'h{e[p + 'wb_data']:02x}"]
        return " && ".join(c)
    for st in sorted(ev):
        lines.append(f"      {st}: assert ({conds(ev[st])});")
    lines += [f"      default: assert ({conds({})});", "    endcase", "  end", "endmodule"]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("fcont,pqpi", [(True, True), (False, False)])
def test_qspi_monitor_decodes_stub(tmp_path, fcont, pqpi):
    """Both chip monitors decode a stub waveform (every listed command family, quad and single
    fields, stretched SCK phases, A[23] and burst wrap, M = 0x20 and 0xFF, reads ending
    mid-byte, and one of each violation: unlisted command, edge after a no-field command,
    write ending mid-byte or in its address, tRST, tCEM) into exactly the expected events,
    from both power-up modes."""
    run_formal.setup_path()
    steps, ev = _qspi_stub(fcont, pqpi)
    flags = {k for e in ev.values() for k, v in e.items() if v and k[2:].startswith("bad_")}
    assert flags == {"f_bad_cmd", "f_bad_extra", "r_bad_cmd", "r_bad_extra", "r_bad_wend",
                     "r_bad_trst", "r_bad_cem"}
    assert sum(1 for e in ev.values() if e.get("r_wb")) == 4       # 3 + 1 of the mid-byte write
    (tmp_path / "stub_tb.v").write_text(_qspi_tb(steps, ev, fcont, pqpi))
    (tmp_path / "defs.vh").write_text(f"`define BONETTO_TCEM {QSPI_TCEM}\n"
                                      f"`define BONETTO_TRST {QSPI_TRST}\n")
    (tmp_path / "stub.sby").write_text(f"""[options]
mode bmc
depth {len(steps) + 2}

[engines]
smtbmc bitwuzla

[script]
read -formal -D RISCV_FORMAL -D RISCV_FORMAL_NRET=1 -D RISCV_FORMAL_XLEN=32 -D RISCV_FORMAL_ILEN=32 defs.vh rvfi_macros.vh bonetto_macros.vh wrapper.sv stub_tb.v
hierarchy -top stub_tb
prep -flatten -nordff -top stub_tb

[files]
{tmp_path / "defs.vh"}
{os.path.join(HERE, "checks", "bonetto_macros.vh")}
{os.path.join(HERE, "wrapper.sv")}
{os.path.join(ROOT, "third_party", "riscv-formal", "checks", "rvfi_macros.vh")}
{tmp_path / "stub_tb.v"}
""")
    p = subprocess.run(["sby", "-f", "stub.sby"], cwd=tmp_path, capture_output=True, text=True)
    assert "DONE (PASS" in p.stdout, p.stdout[-3000:]


# ---------------------------------------------------------------------------
# Mutants and the seed.

def _src_with(tmp_path, rtl_dir):
    """A copy of src/ with the top-level design files of rtl_dir on top."""
    src = tmp_path / "src"
    shutil.copytree(os.path.join(ROOT, "src"), src)
    for f in os.listdir(rtl_dir):
        if f.endswith((".v", ".sv", ".vh")):
            shutil.copy(os.path.join(rtl_dir, f), src / f)
    return src


TIMINGS = {}


def design_timing(name, tmp_path_factory):
    """The cosim timing of the seed (name None) or of verif/cosim/mutants/<name>."""
    if name not in TIMINGS:
        d = tmp_path_factory.mktemp("timing")
        args = ["--timing-only", str(d / "timing.json")]
        if name:
            args += ["--src", str(_src_with(d, os.path.join(ROOT, "verif", "cosim", "mutants", name)))]
        rc, text = formal(args, timeout=7200)
        assert rc == 0, text[-3000:]
        TIMINGS[name] = str(d / "timing.json")
    return TIMINGS[name]


@pytest.fixture(scope="session")
def seed_timing(tmp_path_factory, heavy_lock):
    return design_timing(None, tmp_path_factory)


def fast_checks():
    cfg = run_formal.load_config(os.path.join(HERE, "checks.cfg"))
    names = set()
    for name, kind, _ in cfg["checks"]:
        names |= {f"insn_{i}" for i in run_formal.isa_insns()} if kind == "insn" else {name}
    return names | {"structure", "equiv"}


def _expect(mutant):
    return json.load(open(os.path.join(MUT_DIR, mutant, "expect.json")))


@pytest.mark.parametrize("mutant", [
    pytest.param(m, marks=pytest.mark.skipif(
        _expect(m).get("slow") and os.environ.get("BONETTO_FORMAL_SLOW") != "1",
        reason="slow (hours): BONETTO_FORMAL_SLOW=1")) for m in mutants()])
def test_mutant_fails(mutant, tmp_path, tmp_path_factory, seed_timing, heavy_lock):
    """Each mutant FAILS (a counterexample) in the checks its expect.json names, at the
    champion's depth (or its "timing" design's), proof tasks only; mutants marked "pass"
    PASS ("checks": "all": the whole fast suite, reach tasks included). A check that is
    only in the deep config runs with --deep. "rtl" points at a shared cosim mutant."""
    mdir = os.path.join(MUT_DIR, mutant)
    expect = _expect(mutant)
    src = _src_with(tmp_path, os.path.join(ROOT, expect["rtl"]) if expect.get("rtl") else mdir)
    timing = design_timing(expect["timing"], tmp_path_factory) if expect.get("timing") else seed_timing
    out = tmp_path / "formal.json"
    args = ["--src", str(src), "--timing", timing, "--out", str(out), "--keep-going"]
    if expect.get("expect", "fail") == "fail":
        args += ["--tasks", "proof"]
    if expect["checks"] != "all":
        args += ["--only", "^(" + "|".join(expect["checks"]) + ")$"]
        if expect.get("deep") or not set(expect["checks"]) <= fast_checks():
            args.append("--deep")
    rc, text = formal(args, timeout=12 * 3600)
    res = json.load(open(out))
    if expect.get("expect") == "pass":
        ok = rc == 0 and (res["pass"] if expect["checks"] == "all" else
                          all(res["checks"].get(c) == "PASS" for c in expect["checks"]))
        assert ok, f"{mutant}: expected PASS in {expect['checks']}, got " \
                   f"{ {c: v for c, v in res['checks'].items() if v != 'PASS'} }\n" \
                   f"{res.get('failures')}\n{text[-2000:]}"
        print(f"{mutant}: PASS in {expect['checks']} ({len(res['checks'])} checks, "
              f"depths {sorted(set(res.get('per_check_depth', {}).values()))}, "
              f"{res.get('runtime_s')} s)")
        return
    # A FAIL must be a counterexample (a failed assertion), not a timeout or an error;
    # equiv fails in the pre-pass and has no SBY task.
    fails = res.get("failures", {})
    failed = [c for c, v in res["checks"].items() if v == "FAIL" and
              (c == "equiv" or fails.get(c, {}).get("assertions"))]
    assert rc != 0, f"{mutant} passed formal:\n{text[-3000:]}"
    assert set(expect["checks"]) & set(failed), \
        f"{mutant}: expected a counterexample in {expect['checks']}, got {res['checks']}\n" \
        f"{fails}\n{text[-2000:]}"
    print(f"{mutant}: FAIL in {sorted(set(expect['checks']) & set(failed))}: "
          f"{ {c: fails.get(c, {}).get('assertions') for c in failed} }")


@pytest.mark.skipif(os.environ.get("BONETTO_SKIP_SEED") == "1", reason="BONETTO_SKIP_SEED=1")
def test_seed_passes(tmp_path, heavy_lock):
    out = tmp_path / "formal.json"
    rc, text = formal(["--out", str(out)])
    res = json.load(open(out))
    assert rc == 0 and res["pass"], f"{res.get('checks')}\n{res.get('failures')}\n{text[-3000:]}"
    # The fast config: every proof at its reach depth, between reset + first retirement +
    # margin and the short depth (the cap); irq and liveness only in deep.
    pcd = res["per_check_depth"]
    t = res["depth_policy"]["timing"]
    assert res["depth"] == res["depth_policy"]["d_short"]
    assert all(1 + t["first_retire_cycle"] + 2 <= v <= res["depth"] for v in pcd.values())
    assert set(res["reach_depths"]) and all(r["depth"] <= r["cap"]
                                            for r in res["reach_depths"].values())
    assert "irq" not in pcd and "liveness" not in pcd
    assert all(v == "PASS" for v in res["checks"].values())
