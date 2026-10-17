"""Contract v2 tests of the pin-level cosim harness (verif/cosim/harness_v2.cpp, spiram.h,
run_cosim.py without --v3). They go away at the integration swap (plan Task 9) with
harness_v2.cpp; the contract v3 tests are in test_cosim_harness.py.

1. Stand-in SPI driver (mutants/standin): the waveform checker, the reset and constant-pin
   checks, the output-stability check and random power-up state, through `harness --spi-only`.
2. The design in src/ must pass the cosim (both modes, several seeds), and --timing-json and
   --dump-ram must produce what Tasks 5 and 6 consume.
3. Mutants (mutants/<name>/, listed in mutants/MANIFEST.json; copies of the seed or of the
   contract v2 designs of hyp/002) must each fail with their specific error; the legal variants
   (legal_*) must pass. 3b: the core region of contract v2 (random at reset; both hyp/002
   designs pass the full matrix). 3c: the liveness fuzz (run_cosim.py --fuzz-liveness) passes
   the seed and both hyp/002 designs and fails the hang mutants.
4. The TT test (test/, RTL) passes on the seed and the legal variants and fails on pin-visible
   mutants, the contract section 6 transaction classes included.

Run: pytest verif/cosim/test_cosim_harness.py -v
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COSIM = ROOT / "verif" / "cosim"
PROGS = ROOT / "build" / "programs"
SRC = ROOT / "src"
STANDIN = COSIM / "mutants" / "standin"
MANIFEST = COSIM / "mutants" / "MANIFEST.json"
sys.path.insert(0, str(COSIM))
import run_cosim  # noqa: E402

GOOD_TRACE = [0xDE, 0xAD, 0xBE, 0xEF]


@pytest.fixture(scope="session")
def programs():
    subprocess.run(["make", "-C", str(ROOT / "verif/programs"), "-j8", "all"], check=True,
                   capture_output=True, text=True)
    return PROGS


def standin(bug: str, seed: int = 1, cycles: int = 400):
    exe = run_cosim.ensure_built("pins", STANDIN, vflags=f"+define+{bug}" if bug else "")
    ram = ROOT / "build" / "cosim" / f"standin-{bug or 'ok'}-{seed}.ram"
    p = subprocess.run([str(exe), "--spi-only", str(cycles), "--seed", str(seed), "--dump-ram",
                        str(ram)], capture_output=True, text=True)
    res = json.loads(p.stdout.strip().splitlines()[-1])
    assert (p.returncode == 0) == res["pass"]
    return res, ram.read_bytes()


# ------------------------------------------------------------------------------------------------
# 1. stand-in driver
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_standin_good_driver_passes(seed):
    res, ram = standin("", seed)
    assert res["pass"], res["error"]
    assert res["uo_trace"] == GOOD_TRACE, "the READ must return what the WRITE stored"
    assert ram[0x8000:0x8004] == bytes(GOOD_TRACE)


WAVEFORM_BUGS = {
    "BUG_MOSI_RISE": "MOSI changed on a rising SCK edge",
    "BUG_CS_SCK_HIGH": "CS_n changed while SCK was high",
    "BUG_WRITE_MIDBYTE": "WRITE ended mid-byte",
    "BUG_BAD_CMD": "invalid command 0x05",
    "BUG_SHORT_TXN": "before its command and address were complete",
    "BUG_COMB_OUT": "outputs changed between rising clk edges",
    "BUG_UIO_OUT": "bits [7:4] and [2] must be 0",
    "BUG_OE": "uio_oe = 0x0f",
    "BUG_RESET_CS": "during reset CS_n = 0",
}


@pytest.mark.parametrize("bug", sorted(WAVEFORM_BUGS))
def test_standin_bug_is_reported(bug):
    res, _ = standin(bug)
    assert not res["pass"]
    assert WAVEFORM_BUGS[bug] in res["error"], res["error"]


def test_standin_miso_sampled_low_reads_noise():
    # Sampling MISO while SCK is low breaks no waveform rule, but the harness drives noise there,
    # so the data read back is garbage (with a core, the cosim then reports a mismatch).
    for seed in (1, 2, 3):
        res, _ = standin("BUG_MISO_LOW", seed)
        assert res["uo_trace"] != GOOD_TRACE


def test_standin_zero_init_reliance_fails():
    # A register that is never reset only works if it powers up as 0. Every seed randomizes it.
    traces = [standin("BUG_NO_RESET", seed)[0]["uo_trace"] for seed in (1, 2, 3)]
    assert all(t != GOOD_TRACE for t in traces), traces
    assert len({tuple(t) for t in traces}) > 1, "power-up state must depend on the seed"


# ------------------------------------------------------------------------------------------------
# 2. the design in src/ passes
def have_design():
    return bool(run_cosim.src_files(SRC))


QUICK = ["selftest", "gpio_demo", "trap_ecall", "trap_jal", "trap_jalr", "trap_branch",
         "trap_zero", "trap_ones", "random_1", "random_2"]


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
@pytest.mark.parametrize("prog", QUICK)
def test_design_passes(programs, prog, mode):
    assert have_design(), "no design in src/"
    for seed in (1, 2, 3):
        s, _ = run_cosim.run_one(mode, PROGS / f"{prog}.hex", seed)
        assert s["pass"], f"{prog} {mode} seed {seed}: {s['error']}"


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_design_passes_coremark(programs, mode):
    assert have_design(), "no design in src/"
    s, _ = run_cosim.run_one(mode, PROGS / "coremark.hex", 1)
    assert s["pass"], s["error"]
    assert s["retired"] == json.loads((PROGS / "coremark.expect.json").read_text())["retired"]


def test_timing_json(programs, tmp_path):
    assert have_design(), "no design in src/"
    out = tmp_path / "timing.json"
    p = subprocess.run([sys.executable, str(COSIM / "run_cosim.py"), "--mode", "rvfi", "--prog",
                        str(PROGS / "selftest.hex"), "--timing-json", str(out)],
                       capture_output=True, text=True)
    summary = json.loads(p.stdout.strip().splitlines()[-1])
    assert p.returncode == 0 and summary["pass"], summary
    assert list(summary) == ["prog", "mode", "seed", "pass", "retired", "cycles", "cpi",
                             "max_cycles_per_insn", "coremark_cycles_per_iter", "error"]
    assert summary["coremark_cycles_per_iter"] is None
    t = json.loads(out.read_text())
    assert set(t) == {"pass", "first_retire_cycle", "max_cycles_per_insn", "per_class"}
    assert t["pass"] is True
    classes = {"alu", "alu_imm", "shift", "lui_auipc", "branch_taken", "branch_not_taken", "jal",
               "jalr", "load", "store", "io_load", "io_store", "fence", "trap"}
    assert set(t["per_class"]) == classes
    assert t["max_cycles_per_insn"] == max(t["per_class"].values()) == summary[
        "max_cycles_per_insn"]
    assert 0 < t["first_retire_cycle"] <= 1024 and t["max_cycles_per_insn"] <= 1024


def test_dump_ram_matches_reference(programs, tmp_path):
    assert have_design(), "no design in src/"
    for mode in ("rvfi", "pins"):
        out = tmp_path / f"{mode}.bin"
        s, _ = run_cosim.run_one(mode, PROGS / "selftest.hex", 2, dump_ram=out)
        assert s["pass"], s["error"]
        ref = tmp_path / "ref.bin"
        subprocess.run([str(ROOT / "build/cosim/iss_main"), str(PROGS / "selftest.hex"), "--ui",
                        "rvfi" if mode == "rvfi" else "pins", "--seed", "2", "--dump-ram",
                        str(ref)], capture_output=True, check=True)
        # the whole 64 KB as the design left it; the core region 0xFF80-0xFFFF is the core's own
        # (random at reset, contract v2 section 3), so only the program RAM below it is compared
        got = out.read_bytes()
        assert len(got) == 65536 and got[:0xFF80] == ref.read_bytes()[:0xFF80]


def test_build_key_covers_every_src_file_and_the_tools(tmp_path, monkeypatch):
    # Any file under src/ may matter (a header someone includes by mistake, a second .v file, a
    # test_*.v file, which is part of the design); the unit tests' test_*.py files can't reach
    # the model; a different Verilator must rebuild too.
    d = tmp_path / "src"
    d.mkdir()
    (d / "top.v").write_text("module top; endmodule\n")
    key = lambda: run_cosim.build_key("pins", d, "", False)  # noqa: E731
    k1 = key()
    (d / "defs.vh").write_text("`define X 1\n")
    k2 = key()
    assert k2 != k1
    (d / "defs.vh").write_text("`define X 2\n")
    k3 = key()
    assert k3 != k2
    (d / "sub").mkdir()
    (d / "sub" / "more.svh").write_text("\n")
    k4 = key()
    assert k4 != k3
    (d / "test_unit.py").write_text("x = 1\n")
    assert key() == k4
    (d / "test_part.v").write_text("module r32v_part; endmodule\n")
    k5 = key()
    assert k5 != k4
    assert [p.name for p in run_cosim.src_files(d)] == ["test_part.v", "top.v"]
    monkeypatch.setattr(run_cosim, "_verilator_version", "Verilator 0.000")
    assert key() != k5


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_build_reads_exactly_the_design(mode):
    # Contract sections 1 and 6: every src/*.v file in sorted order, nothing in front of them,
    # and the RVFI build's only define is RISCV_FORMAL (none at all for the plain build).
    assert have_design(), "no design in src/"
    exe = run_cosim.ensure_built(mode)
    args = (exe.parent / "verilator.args").read_text().splitlines()
    verilog = [a for a in args if a.endswith((".v", ".sv", ".vh", ".svh"))]
    assert verilog == [str(p) for p in run_cosim.src_files(SRC.resolve())]
    assert [a for a in args if a.startswith("+define+")] == (
        ["+define+RISCV_FORMAL"] if mode == "rvfi" else [])
    assert not [a for a in args if a.startswith(("+incdir", "-I", "-D", "-y", "-v", "-f"))]


def test_build_lock_is_cross_process(programs):
    # While another process holds build/cosim/pins.lock, ensure_built waits, even for an
    # up-to-date build.
    import fcntl
    run_cosim.ensure_built("pins")
    lock = ROOT / "build" / "cosim" / "pins.lock"
    code = (f"import sys; sys.path.insert(0, {str(COSIM)!r}); import run_cosim; "
            "run_cosim.ensure_built('pins'); print('built')")
    with open(lock, "a") as f:
        fcntl.lockf(f, fcntl.LOCK_EX)
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                p.wait(timeout=3)
        finally:
            fcntl.lockf(f, fcntl.LOCK_UN)
    assert p.wait(timeout=60) == 0 and p.stdout.read().strip() == "built"


def test_latency_cap_boundary(programs, tmp_path):
    """Contract section 4: a gap of exactly 1,024 cycles passes and 1,025 fails, for the first
    retirement after reset release and for later ones, whether the late retirement arrives (the
    check at the retirement edge) or never comes (the watchdog). mutants/stall_gap holds every
    retirement until exactly STALL_GAP cycles after the previous one."""
    src = COSIM / "mutants" / "stall_gap"
    prog = PROGS / "selftest.hex"
    t = tmp_path / "timing.json"

    def run(mode, vflags="", timing=None):
        return run_cosim.run_one(mode, prog, 1, timing_json=timing, src=src, vflags=vflags)[0]

    s = run("rvfi", "+define+STALL_GAP=1024", t)
    assert s["pass"], s["error"]
    tj = json.loads(t.read_text())
    assert tj["pass"] and tj["first_retire_cycle"] == 1024 and tj["max_cycles_per_insn"] == 1024
    assert set(tj["per_class"].values()) == {1024}
    assert run("pins", "+define+STALL_GAP=1024")["pass"]
    # An explicit --max-cycles of 1024 x (retired + 1) fits a core at the cap: the budget covers
    # the program, the windows after the halt don't count
    tight = 1024 * (json.loads((PROGS / "selftest.expect.json").read_text())["retired"] + 1)
    for mode in ("rvfi", "pins"):
        s = run_cosim.run_one(mode, prog, 1, max_cycles=tight, src=src,
                              vflags="+define+STALL_GAP=1024")[0]
        assert s["pass"], f"{mode}: {s['error']}"
    # 1,025: the first retirement is late
    s = run("rvfi", "", t)
    assert not s["pass"]
    assert "latency cap: retirement #0 " in s["error"], s["error"]
    assert "came 1025 cycles after reset release" in s["error"], s["error"]
    assert json.loads(t.read_text())["pass"] is False, "a failed run's timing report says so"
    # 1,025 later on (the first instruction runs at full speed)
    s = run("rvfi", "+define+STALL_SKIP_FIRST")
    assert not s["pass"]
    assert "latency cap: retirement #1 " in s["error"], s["error"]
    assert "came 1025 cycles after the previous retirement" in s["error"], s["error"]
    assert run("rvfi", "+define+STALL_SKIP_FIRST +define+STALL_GAP=1024")["pass"]
    # 1,026: nothing retires at 1,025, so the watchdog reports it at cycle 1,025
    s = run("rvfi", "+define+STALL_GAP=1026")
    assert not s["pass"]
    assert s["error"].startswith("cycle 1025: latency cap: no retirement within 1024 cycles of "
                                 "reset release"), s["error"]


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_core_at_the_cap_passes_as_eval_runs_it(programs, mode):
    """A legal core retiring every instruction exactly 1,024 cycles after the previous one passes
    a ui_in-dependent program, run the way tools/eval.py runs the cosim (no --max-cycles, so the
    harness budget is 1,024 x (its own ISS count for this mode's ui_in schedule + 16)), on every
    random program that retires a different number of instructions in rvfi mode than its expect
    file's pins count (random_3 and random_6 since the contract v2 generator; random_4 before)."""
    dep = []
    for n in range(1, 7):
        exp = json.loads((PROGS / f"random_{n}.expect.json").read_text())
        if exp["rvfi"]["retired"] != exp["retired"]:
            dep.append(f"random_{n}")
    assert dep, "no random program depends on the ui_in schedule any more"
    src = COSIM / "mutants" / "stall_gap"
    for prog, seed in ((p, s) for p in dep for s in (1, 2, 3)):
        p = subprocess.run([sys.executable, str(COSIM / "run_cosim.py"), "--mode", mode, "--prog",
                            str(PROGS / f"{prog}.hex"), "--seed", str(seed), "--src", str(src),
                            "--vflags", "+define+STALL_GAP=1024 +define+STALL_SKIP_FIRST"],
                           capture_output=True, text=True)
        s = json.loads(p.stdout.strip().splitlines()[-1])
        assert p.returncode == 0 and s["pass"], f"{prog} {mode} seed {seed}: {s['error']}"
        if mode == "rvfi":
            assert s["max_cycles_per_insn"] == 1024


def test_coremark_log_ring_is_checked(programs, tmp_path):
    # A RAM image without the validation text must fail the log check, even if the run matched.
    exp = run_cosim.expect_for(PROGS / "coremark.hex")
    assert exp["log_contains"] == ["Correct operation validated"]
    ram = bytearray(65536)
    ram[0xF000:0xF00C] = b"ERROR! crc\0\0"
    assert "Correct operation validated" not in run_cosim.log_ring_text(bytes(ram))
    assert "Correct operation validated" in exp["log"]


# ------------------------------------------------------------------------------------------------
# 3. mutants of the seed
def manifest():
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}


def mutant_ids():
    return sorted(manifest()) or ["<missing: mutants/MANIFEST.json>"]


@pytest.mark.parametrize("name", mutant_ids())
def test_mutant_fails(programs, name):
    """Each mutant fails in every mode listed under "fail" with that mode's error text ("" means
    any error), for every listed seed, and passes the modes listed under "pass" (an RVFI-only bug
    passes pins mode; a bug hidden under RISCV_FORMAL passes rvfi mode). A "fuzz_fail" entry (a
    hang on a reserved encoding, which no program contains) must fail the rvfi-mode liveness fuzz
    in seeds 1-5 with "latency cap" and that text in the error."""
    m = manifest()
    assert name in m, "mutants/MANIFEST.json is missing (mutants are built from the seed)"
    spec = m[name]
    src = COSIM / "mutants" / name
    assert (src / "tt_um_fesens_r32v.v").exists()
    progs = spec.get("progs", [spec.get("prog", "selftest")])
    for prog in (PROGS / f"{p}.hex" for p in progs):
        for mode in spec.get("pass", []):
            s, _ = run_cosim.run_one(mode, prog, 1, src=src)
            assert s["pass"], f"{name} should pass {mode} mode on {prog.name}: {s['error']}"
        for mode, err in spec["fail"].items():
            for seed in spec.get("seeds", [1]):
                s, _ = run_cosim.run_one(mode, prog, seed, src=src)
                assert not s["pass"], f"{name} passed {mode} mode (seed {seed})"
                assert s["error"] and err in s["error"], f"{name} {mode} seed {seed}: {s['error']}"
    if "fuzz_fail" in spec:
        for seed in range(1, 6):
            s = run_cosim.run_fuzz("rvfi", seed, src=src)
            assert not s["pass"] and s["liveness_checked"], f"{name} passed the fuzz (seed {seed})"
            assert "latency cap" in s["error"] and spec["fuzz_fail"] in s["error"], (
                f"{name} fuzz seed {seed}: {s['error']}")


def test_fetch_reading_its_load_bytes_is_named(tmp_path):
    """Contract section 6: a fetch may only read its instruction's bytes. lw x1, 4(x0) at pc 0
    loads the next word; fetch_extra_byte's fetch READ continues into byte 4, one of the load's own
    bytes, so the error names the fetch overrun (not a missing load byte). The seed passes."""
    img = bytearray(65536)
    img[0:8] = (0x00402083).to_bytes(4, "little") + (0x00100073).to_bytes(4, "little")  # ebreak
    hexf = tmp_path / "lw_next.hex"
    hexf.write_text("".join(f"{b:02x}\n" for b in img))
    s, _ = run_cosim.run_one("rvfi", hexf, 1)
    assert s["pass"] and s["retired"] == 2, s["error"]
    s, _ = run_cosim.run_one("rvfi", hexf, 1, src=COSIM / "mutants" / "fetch_extra_byte")
    assert not s["pass"]
    assert ("the fetch READ of retirement #0" in s["error"]
            and "read beyond the instruction's 4 bytes (byte 0x0004)" in s["error"]), s["error"]


def test_trap_retirement_compares_only_the_contract_fields():
    """Contract section 6 (e321614): on a trapping retirement only trap, halt, insn, pc_rdata,
    rd_addr = 0 and no memory write are compared, plus the rs1/rs2 addr and rdata of a trapping
    branch or JALR. legal_trap_fields reports arbitrary rd_wdata and pc_wdata on every trap and
    arbitrary rs fields on the other traps, and must pass; trap_rs_garbage (arbitrary rs fields on
    a trapping branch or JALR too) and trap_writes_rd (rd_addr != 0 on a trap) must fail."""
    m = manifest()
    assert m["legal_trap_fields"]["pass"] == ["rvfi", "pins"] and not m["legal_trap_fields"]["fail"]
    assert {"trap_jalr", "trap_branch", "trap_ecall"} <= set(m["legal_trap_fields"]["progs"])
    assert {"trap_jalr", "trap_branch"} <= set(m["trap_rs_garbage"]["progs"])
    assert m["trap_rs_garbage"]["fail"] == {"rvfi": "RVFI mismatch"}
    assert m["trap_writes_rd"]["fail"] == {"rvfi": "rvfi_rd_addr"}


def test_every_required_mutant_class_is_present():
    m = manifest()
    required = {"miso_sck_low", "mosi_on_rise", "cs_sck_high", "write_midbyte", "rvfi_rd_wrong",
                "rvfi_hidden", "zero_init", "latency", "stall_gap", "ui_at_retire", "uo_glitch",
                "load_own_fetch",
                # contract v2 (section 6): region and program transaction rules
                "region_wrap", "region_straddle", "store_twice", "store_extra_byte",
                "fetch_extra_byte", "load_skip_byte", "region_zero_init",
                # 78472fe: a header shorter than 24 bits still fails; e321614: trapping branch or
                # JALR rs fields; the formal review's C1: liveness on reserved encodings
                "null_read_short", "trap_rs_garbage", "hang_reserved", "hang_funct3"}
    assert required <= set(m), required - set(m)
    assert m["rvfi_hidden"]["pass"] == ["rvfi"] and "pins" in m["rvfi_hidden"]["fail"]
    assert m["null_read_short"]["fail"].keys() == {"rvfi", "pins"}
    for legal in ("legal_split_store", "legal_h002_rf", "legal_h002_pcram", "legal_null_read"):
        assert m[legal]["pass"] == ["rvfi", "pins"] and not m[legal]["fail"], legal


# ------------------------------------------------------------------------------------------------
# 3b. contract v2: the core region
def test_region_starts_random(programs):
    """Contract section 3: the region's contents at reset are arbitrary. region_zero_init (the
    pcram design without its boot write) takes its pc from the region at reset: it passes when the
    region starts zero (the harness's --region-zero test hook) and fails with the random fill."""
    src = COSIM / "mutants" / "region_zero_init"
    exe = run_cosim.ensure_built("rvfi", src)
    for seed in (1, 2, 3):
        runs = {}
        for flag in ([], ["--region-zero"]):
            p = subprocess.run([str(exe), str(PROGS / "selftest.hex"), "--seed", str(seed), *flag],
                               capture_output=True, text=True)
            runs[bool(flag)] = json.loads(p.stdout.strip().splitlines()[-1])
        assert runs[True]["pass"], f"seed {seed}, zero region: {runs[True]['error']}"
        assert not runs[False]["pass"] and "retirement #0" in runs[False]["error"], runs[False]


@pytest.mark.parametrize("name", ["legal_h002_rf", "legal_h002_pcram"])
def test_v2_designs_pass_the_full_matrix(programs, name):
    """The two real contract v2 designs (hyp/002-rf-in-ram fc07cc3: x1..x15 in the region;
    hyp/002-rf-in-ram-pcram b9a5211: the pc too, read before the fetch, SW split in two) pass
    every program in both modes with seeds 1-3, as tools/eval.py's cosim gate runs them."""
    p = subprocess.run([sys.executable, str(COSIM / "run_cosim.py"), "--all", "--src",
                        str(COSIM / "mutants" / name)], capture_output=True, text=True)
    s = json.loads(p.stdout.strip().splitlines()[-1])
    assert p.returncode == 0 and s["pass"] and s["runs"] == 90 and s["failed"] == 0, (
        s, p.stderr[-2000:])


# ------------------------------------------------------------------------------------------------
# 3c. the liveness fuzz (contract section 4: liveness on reserved encodings)
def fuzz_cli(src: Path, seed: int, *extra: str):
    p = subprocess.run([sys.executable, str(COSIM / "run_cosim.py"), "--fuzz-liveness", "--seed",
                        str(seed), "--src", str(src), *extra], capture_output=True, text=True)
    s = json.loads(p.stdout.strip().splitlines()[-1])
    assert list(s) == run_cosim.FUZZ_KEYS, list(s)
    assert (p.returncode == 0) == s["pass"], (p.returncode, s)
    return s


@pytest.mark.parametrize("name", ["(seed)", "legal_h002_rf", "legal_h002_pcram"])
def test_fuzz_liveness_passes(name):
    """The seed (strict decode) and both hyp/002 designs (spec-minimum decode) pass the rvfi-mode
    fuzz in seeds 1-5, as the eval runs it: 2,048 probe sub-runs and 8 walks per seed, each ending
    in a halt or at the retirement cap."""
    src = SRC if name == "(seed)" else COSIM / "mutants" / name
    for seed in range(1, 6):
        s = fuzz_cli(src, seed)
        assert s["pass"] and s["error"] is None, s
        assert s["mode"] == "rvfi" and s["liveness_checked"] is True, s
        assert s["runs"] == run_cosim.FUZZ_RUNS + run_cosim.FUZZ_RUNS // 256 and s["walks"] == 8, s
        assert s["halted"] + s["capped"] == s["runs"], s
        assert s["probes_retired"] > 0 and s["max_cycles_per_insn"] <= 1024, s


def test_fuzz_pins_mode_cannot_check_liveness():
    """pins mode sees no retirement and no halt, so it checks only the pin rules: hang_reserved
    passes it, flagged "liveness_checked": false. The eval runs rvfi mode."""
    s = fuzz_cli(COSIM / "mutants" / "hang_reserved", 1, "--mode", "pins", "--fuzz-runs", "128")
    assert s["pass"] and s["mode"] == "pins" and s["liveness_checked"] is False, s
    assert s["runs"] == 129 and s["halted"] == 0 and s["max_cycles_per_insn"] is None, s
    s = fuzz_cli(COSIM / "mutants" / "hang_reserved", 1, "--fuzz-runs", "128")
    assert not s["pass"] and "opcode 0x0b" in s["error"], s


# ------------------------------------------------------------------------------------------------
# 4. the TT test (test/test.py with the 23LC512 model) on legal variants and on mutants
# It sees only the pins: gpio_demo's uo_out sequence and the RAM model's waveform rules. Mutants
# whose bug only shows against RVFI or the retirement timing (uo_late, cs_open_at_retire,
# load_own_fetch) pass it by design, and so does rvfi_hidden (gpio_demo runs no SLTU/SLTIU); the
# cosim catches those. The RAM model also checks the section 6 transaction classes the pins show
# (region or program, at most 4 bytes per program READ, each store byte once), so a wrap or a
# double write that synthesis introduces fails the gate-level test too.
TT_CASES = {
    "(seed)": None,
    "legal_mosi_echo": None,    # MOSI = X when no bit is due: legal
    "legal_trap_fields": None,
    "legal_split_store": None,  # contract v2: a store split over two WRITEs
    "legal_h002_rf": None,      # contract v2: registers in the core region
    "legal_h002_pcram": None,   # contract v2: registers and pc in the core region
    "legal_null_read": None,    # 78472fe: a READ with its 24-bit header and no data bit
    "null_read_short": "transaction ended after 23 bits, before its command and address",
    "region_wrap": "wraps out of the core region",
    "region_straddle": "runs from program RAM into the core region",
    "store_twice": "written a second time since the last program READ",
    "fetch_extra_byte": "program READ at 0x0000 addresses a fifth byte (0x0004)",
    "mosi_on_rise": "spiram: MOSI changed on a rising SCK edge",
    "cs_sck_high": "spiram: CS_n changed while SCK was high",
    "write_midbyte": "spiram: WRITE ended mid-byte",  # needs gpio_demo's RAM stores
    "zero_init": "spiram: MOSI = x on a rising SCK edge",
    "uo_glitch": "uo_out change #1 is 0x00",
}


@pytest.mark.parametrize("name", sorted(TT_CASES))
def test_tt_test_on_variants(name):
    """make -C test (RTL) passes on the seed and the legal variants and fails on the mutants with
    their specific message. legal_mosi_echo drives MOSI = X when no bit is due, which the contract
    allows (MOSI matters only at rising SCK edges): the TT test must not X-check MOSI."""
    if name == "legal_mosi_echo":
        m = manifest()[name]
        assert m["pass"] == ["rvfi", "pins"] and not m["fail"]
    src = SRC if name == "(seed)" else COSIM / "mutants" / name
    tag = "tt_" + name.strip("()")
    p = subprocess.run(["make", "-C", str(ROOT / "test"), f"SRC_DIR={src}",
                        f"SIM_BUILD=sim_build/{tag}",
                        f"COCOTB_RESULTS_FILE=sim_build/{tag}/results.xml"],
                       capture_output=True, text=True)
    out = p.stdout + p.stderr
    want = TT_CASES[name]
    if want is None:
        assert p.returncode == 0 and "TESTS=1 PASS=1 FAIL=0" in out, out[-3000:]
    else:
        assert p.returncode != 0 and "FAIL=1" in out, f"{name} passed the TT test"
        assert want in out, out[-3000:]
