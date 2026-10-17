"""Unit tests for run_arch.py's contract v3 test selection (no GCC, spike or Verilator needed).

  python3 -m pytest verif/arch-test/test_run_arch.py -q

The suite tests need third_party/riscv-arch-test checked out; they are skipped otherwise.
"""
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


def defines(name: str, d: Path = PRIV) -> list:
    return sorted(run_arch.case_defines((d / f"{name}.S").read_text()))


def test_v3_option_exists():
    p = subprocess.run([sys.executable, str(HERE / "run_arch.py"), "--v3", "--phase", "nope"],
                       capture_output=True, text=True)
    assert p.returncode == 2 and "invalid choice" in p.stderr


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
def test_unprivileged_tests_and_count():
    assert defines("add-01", run_arch.SUITE / "rv32e_m" / "E" / "src") == sorted(BASE)
    tests, skipped = run_arch.discover_v3()
    assert (len(tests), skipped) == (53, {})


def test_case_selection_rejects_ambiguity_and_unknown_conditions():
    two = ('RVTEST_CASE(0,"//check ISA:=regex(.*E.*);def A=1;",x)\n'
           'RVTEST_CASE(1,"//check ISA:=regex(.*Zicsr.*);def B=1;",x)\n')
    with pytest.raises(RuntimeError, match="2 RVTEST_CASEs apply"):
        run_arch.case_defines(two)
    with pytest.raises(RuntimeError, match="0 RVTEST_CASEs apply"):
        run_arch.case_defines('RVTEST_CASE(0,"//check ISA:=regex(.*C.*);def A=1;",x)\n')
    with pytest.raises(RuntimeError, match="unknown condition"):
        run_arch.case_defines('RVTEST_CASE(0,"//check XLEN:=32;def A=1;",x)\n')
