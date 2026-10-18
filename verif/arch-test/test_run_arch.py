"""Unit tests for run_arch.py's contract v3 flow pieces (no GCC, spike or Verilator needed).

  python3 -m pytest verif/arch-test/test_run_arch.py -q

Test selection (RVTEST_CASE), the flash image, the signature read-back, and the guards that keep
--phase dut from running on a stale or partial manifest. The suite tests need
third_party/riscv-arch-test checked out; they are skipped otherwise.
"""
import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_arch  # noqa: E402

PRIV = run_arch.SUITE / "rv32e_m" / "privilege" / "src"
need_suite = pytest.mark.skipif(not PRIV.exists(), reason="third_party/riscv-arch-test missing")
BASE = ["-DRVTEST_E=True", "-DTEST_CASE_1=True"]
TRAP = ["-Drvtest_mtrap_routine=True"]


def run_arch_cli(*args):
    return subprocess.run([sys.executable, str(HERE / "run_arch.py"), *args], capture_output=True,
                          text=True)


def defines(name: str, d: Path = PRIV) -> list:
    return sorted(run_arch.case_defines((d / f"{name}.S").read_text()))


# ---- the option and test selection ---------------------------------------------------------------
def test_options():
    # contract v3 is the only flow since the swap (plan Task 9): --phase needs no --v3, and --v3
    # is still accepted (a no-op) so old command lines keep working
    p = run_arch_cli("--help")
    assert p.returncode == 0 and "--v3" in p.stdout and "--phase" in p.stdout
    assert "no-op" in p.stdout or "ignored" in p.stdout
    for args in (["--phase", "nope"], ["--v3", "--phase", "nope"]):
        p = run_arch_cli(*args)
        assert p.returncode == 2 and "invalid choice" in p.stderr, args
    assert run_arch.CFLAGS_V3.count("-DBONETTO_V3") == 0  # model_test.h has one model now


@need_suite
def test_trap_tests_get_the_trap_handler():
    # contract v3: ECALL, EBREAK and misaligned jump targets trap to mtvec (case 1: no C, Zicsr)
    for n in ["ecall", "ebreak", "misalign-jal-01", "misalign2-jalr-01"] + [
            f"misalign-{b}-01" for b in ("beq", "bne", "blt", "bge", "bltu", "bgeu")]:
        assert defines(n) == sorted(BASE + TRAP), n


@need_suite
def test_misaligned_data_uses_hardware_support():
    # contract v3 section 3: misaligned loads and stores never trap (case 0)
    for n in ("misalign-lh-01", "misalign-lw-01", "misalign-sh-01", "misalign-sw-01",
              "misalign1-jalr-01"):
        assert defines(n) == sorted(BASE), n
    assert defines("misalign-lhu-01") == sorted(BASE + TRAP)  # its case 0 lists the handler too


@need_suite
def test_every_test_selects_one_case():
    tests, skipped = run_arch.discover_v3()
    assert (len(tests), skipped) == (53, {})
    for name, src in tests.items():
        d = run_arch.case_defines(src.read_text())
        assert set(BASE) <= set(d) <= set(BASE + TRAP), name
    assert defines("add-01", run_arch.SUITE / "rv32e_m" / "E" / "src") == sorted(BASE)


def test_case_selection_rejects_ambiguity_and_unknown_conditions():
    two = ('RVTEST_CASE(0,"//check ISA:=regex(.*E.*);def A=1;",x)\n'
           'RVTEST_CASE(1,"//check ISA:=regex(.*Zicsr.*);def B=1;",x)\n')
    with pytest.raises(RuntimeError, match="2 RVTEST_CASEs apply"):
        run_arch.case_defines(two)
    with pytest.raises(RuntimeError, match="0 RVTEST_CASEs apply"):
        run_arch.case_defines('RVTEST_CASE(0,"//check ISA:=regex(.*C.*);def A=1;",x)\n')
    with pytest.raises(RuntimeError, match="unknown condition"):
        run_arch.case_defines('RVTEST_CASE(0,"//check XLEN:=32;def A=1;",x)\n')


# ---- flash image and signature ------------------------------------------------------------------
def make_elf(segments) -> bytes:
    """A minimal little-endian ELF32 with one PT_LOAD per (load address, bytes)."""
    n = len(segments)
    ident = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9)
    out = struct.pack("<16sHHIIIIIHHHHHH", ident, 2, 0xF3, 1, 0x1000, 52, 0, 0, 52, 32, n, 0, 0,
                      0)
    off = 52 + 32 * n
    data = b""
    for addr, blob in segments:
        out += struct.pack("<8I", 1, off + len(data), addr, addr, len(blob), len(blob), 5, 4)
        data += blob
    return out + data


def test_stub_is_jal_x0_to_the_code():
    w = run_arch.STUB_V3
    imm = (((w >> 31) & 1) << 20 | ((w >> 12) & 0xFF) << 12 | ((w >> 20) & 1) << 11
           | ((w >> 21) & 0x3FF) << 1)
    assert (w & 0x7F, (w >> 7) & 0x1F, imm) == (0x6F, 0, run_arch.CODE_V3)


def test_flash_image_v3(tmp_path):
    elf, hexf = tmp_path / "t.elf", tmp_path / "t.hex"
    elf.write_bytes(make_elf([(0x1000, bytes.fromhex("13000000" * 2)), (0x1100, b"\xaa\xbb")]))
    assert run_arch.flash_image_v3(elf, hexf) == 0x1102
    lines = hexf.read_text().splitlines()
    assert lines[:2] == ["@00000000", "6f 10 00 00"], "the stub at flash byte 0"
    i = lines.index("@00001000")
    assert lines[i + 1] == "13 00 00 00 13 00 00 00"
    assert lines[lines.index("@00001100") + 1] == "aa bb"
    elf.write_bytes(make_elf([(0x800, bytes(4)), (0x1000, bytes(4))]))
    with pytest.raises(RuntimeError, match="below the code at 0x1000"):
        run_arch.flash_image_v3(elf, hexf)
    elf.write_bytes(make_elf([(0x1000, bytes(4)), (0xFFFFFE, bytes(4))]))
    with pytest.raises(RuntimeError, match="flash image"):
        run_arch.flash_image_v3(elf, hexf)


def test_sig_words_v3():
    psram = bytearray(0x10020)
    struct.pack_into("<3I", psram, run_arch.SIG_V3 - run_arch.PSRAM_CPU_V3, 0xDEADBEEF, 1,
                     0x6F5CA309)
    assert run_arch.sig_words_v3(bytes(psram), run_arch.SIG_V3, run_arch.SIG_V3 + 12) == \
        ["deadbeef", "00000001", "6f5ca309"]
    assert run_arch.first_diff(["a", "b", "c"], ["a", "x", "c"]) == 1


# ---- --phase dut guards --------------------------------------------------------------------------
@pytest.fixture
def staged(tmp_path, monkeypatch):
    """A staged manifest with one test "t" in tmp_path; building or running the cosim fails the
    test (the guards must refuse before that)."""
    for name in ("STAGE_V3", "WORK_V3", "OUT"):
        monkeypatch.setattr(run_arch, name, tmp_path / name.lower())
    monkeypatch.setattr(run_arch, "SUMMARY", tmp_path / "arch.json")
    run_arch.STAGE_V3.mkdir()

    def no_cosim(*a, **k):
        raise AssertionError("the guards should have refused before any cosim")

    monkeypatch.setattr(run_arch.run_cosim, "ensure_built", no_cosim)
    monkeypatch.setattr(run_arch.run_cosim, "run_one", no_cosim)
    hexf = run_arch.STAGE_V3 / "t.hex"
    hexf.write_text("@00000000\n6f 10 00 00\n")
    man = {"contract": "v3", "flow_key": run_arch.flow_key_v3(),
           "suite_key": run_arch.suite_key_v3() if run_arch.SUITE.exists() else None,
           "suite_commit": None, "only": None, "skipped": {},
           "tests": {"t": {"hex": "t.hex", "sha256": hashlib.sha256(hexf.read_bytes()).hexdigest(),
                           "sig": [run_arch.SIG_V3, run_arch.SIG_V3 + 4], "ref": ["00000000"]}}}

    def write(**changes):
        m = dict(man, **changes)
        (run_arch.STAGE_V3 / "manifest.json").write_text(json.dumps(m))
        return m

    return write


def refusal(only=None) -> str:
    assert run_arch.dut_v3(only, Path("src"), [1], 1) == 1
    report = json.loads((run_arch.OUT / "report.json").read_text())
    out = run_arch.OUT / "only.json" if only else run_arch.SUMMARY
    assert json.loads(out.read_text()) == {"pass": False, "tests": {}}
    return report["error"]


def test_dut_refuses_a_missing_manifest(staged):
    assert "run --phase prep first" in refusal()


def test_dut_refuses_another_flow(staged):
    staged(flow_key="0" * 64)
    assert "another flow" in refusal()


def test_dut_refuses_a_partial_manifest_for_a_full_run(staged, monkeypatch):
    staged(only=["t"])
    assert "covers only ['t']" in refusal()
    # an --only run of the tests it has gets past the guards (to the cosim build)
    def build(*a, **k):
        raise RuntimeError("build reached")

    monkeypatch.setattr(run_arch.run_cosim, "ensure_built", build)
    assert refusal(only=["t"]) == "build reached"
    assert "tests not in the manifest: ['u']" in refusal(only=["u"])


@pytest.mark.skipif(not run_arch.SUITE.exists(), reason="third_party/riscv-arch-test missing")
def test_dut_refuses_a_suite_change(staged):
    staged(suite_key="0" * 64, suite_commit="abc")
    assert "the suite changed since prep (manifest suite commit abc" in refusal()
    staged()  # a full manifest of one test where the suite has 53
    assert "the manifest's tests differ from the suite's" in refusal()


def test_dut_refuses_a_changed_image(staged):
    m = staged()
    ent = m["tests"]["t"]
    (run_arch.STAGE_V3 / "t.hex").write_text("@00000000\n6f 10 00 00\n13 00 00 00\n")
    ok, err, _ = run_arch.dut_one_v3("t", ent, Path("src"), [1])
    assert not ok and "t.hex is missing or differs from the manifest" in err
    (run_arch.STAGE_V3 / "t.hex").unlink()
    ok, err, _ = run_arch.dut_one_v3("t", ent, Path("src"), [1])
    assert not ok and "missing or differs" in err
    ok, err, _ = run_arch.dut_one_v3("t", {"error": "build failed: x"}, Path("src"), [1])
    assert not ok and err == "prep: build failed: x"
