"""Tests for the pin-level cosim harness itself (verif/cosim/harness.cpp, spiram.h, run_cosim.py).

1. Stand-in SPI driver (mutants/standin): the waveform checker, the reset and constant-pin
   checks, the output-stability check and random power-up state, through `harness --spi-only`.
2. The design in src/ must pass the cosim (both modes, several seeds), and --timing-json and
   --dump-ram must produce what Tasks 5 and 6 consume.
3. Mutants of the seed (mutants/<name>/, listed in mutants/MANIFEST.json) must each fail with
   their specific error.

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
    return any(SRC.glob("*.v")) or any(SRC.glob("*.sv"))


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
                             "max_cycles_per_insn", "error"]
    t = json.loads(out.read_text())
    assert set(t) == {"first_retire_cycle", "max_cycles_per_insn", "per_class"}
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
        assert out.read_bytes() == ref.read_bytes() and len(out.read_bytes()) == 65536


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
    """Each mutant fails in the stated mode(s) with its specific error, and passes where stated
    (the RVFI-hidden mutant passes rvfi mode: only the plain build differs)."""
    m = manifest()
    assert name in m, "mutants/MANIFEST.json is missing (mutants are built from the seed)"
    spec = m[name]
    src = COSIM / "mutants" / name
    prog = PROGS / f"{spec.get('prog', 'selftest')}.hex"
    for mode in spec.get("pass_modes", []):
        s, _ = run_cosim.run_one(mode, prog, 1, src=src)
        assert s["pass"], f"{name} should pass {mode} mode: {s['error']}"
    for mode in spec["fail_modes"]:
        for seed in spec.get("seeds", [1]):
            s, _ = run_cosim.run_one(mode, prog, seed, src=src)
            assert not s["pass"], f"{name} passed {mode} mode (seed {seed})"
            assert spec["error"] in (s["error"] or ""), f"{name} {mode}: {s['error']}"
