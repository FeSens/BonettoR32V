"""Tests for tools/equiv.py on the fixtures in tools/test_fixtures/equiv/."""
import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import equiv  # noqa: E402

FIX = os.path.join(HERE, "test_fixtures", "equiv")
HAVE_YOSYS = shutil.which("yosys", path=equiv.tool_env()["PATH"]) is not None
pytestmark = pytest.mark.skipif(not HAVE_YOSYS, reason="yosys not on PATH")


def run_equiv(tmp_path, src):
    out = tmp_path / "equiv.json"
    p = subprocess.run([sys.executable, os.path.join(HERE, "equiv.py"), "--src", str(src),
                        "--work", str(tmp_path / "work"), "--out", str(out)],
                       capture_output=True, text=True)
    return p.returncode, json.loads(out.read_text()), p.stdout


def copy_fixture(tmp_path, name, edits=()):
    dst = tmp_path / "src"
    shutil.copytree(os.path.join(FIX, name), dst)
    for rel, old, new in edits:
        p = dst / rel
        text = p.read_text()
        assert text.count(old) == 1, (rel, old)
        p.write_text(text.replace(old, new))
    return dst


@pytest.mark.parametrize("name", ["pass_observer", "pass_rvfi_alias"])
def test_observer_designs_pass(tmp_path, name):
    rc, j, out = run_equiv(tmp_path, os.path.join(FIX, name))
    assert rc == 0, out
    assert j["pass"] is True
    assert j["rvfi_ports"] == 21
    assert j["output_bits_checked"] == 24
    assert j["unproven"] == []
    # Shared registers keep their names, so no cell depends on equiv_induct's weak base case.
    assert j["proved_by_induct"] == 0
    assert j["proved_by_simple"] == j["equiv_cells"] > 24


@pytest.mark.parametrize("name,expect", [
    ("fail_rvfi_drives_pin", "uo_out[0]"),
    ("fail_rvfi_reg_feeds_pin", "uo_out[0]"),
    ("fail_rvfi_alters_shared_reg", "gpio[0]"),
])
def test_rvfi_leaks_fail(tmp_path, name, expect):
    rc, j, out = run_equiv(tmp_path, os.path.join(FIX, name))
    assert rc == 1, out
    assert j["pass"] is False
    assert "unproven" in j["detail"]        # failed in the proof, not in a build error
    assert expect in j["unproven"], j["unproven"]


def test_extra_port_fails(tmp_path):
    rc, j, out = run_equiv(tmp_path, os.path.join(FIX, "fail_plain_extra_port"))
    assert rc == 1
    assert "port check failed" in j["detail"]
    assert "B (plain): extra port dbg_retire" in j["detail"]


def test_renamed_tt_port_fails(tmp_path):
    src = copy_fixture(tmp_path, "pass_observer", [
        ("tt_um_fesens_r32v.v", "input  wire       ena,", "input  wire       enable,"),
        ("tt_um_fesens_r32v.v", "&{ena,", "&{enable,"),
    ])
    rc, j, _ = run_equiv(tmp_path, src)
    assert rc == 1
    assert "missing port ena" in j["detail"] and "extra port enable" in j["detail"]


def test_wrong_port_width_fails(tmp_path):
    src = copy_fixture(tmp_path, "pass_observer", [
        ("tt_um_fesens_r32v.v", "output wire [7:0] uio_oe,", "output wire [3:0] uio_oe,"),
        ("tt_um_fesens_r32v.v", "assign uio_oe  = 8'b0000_1011;", "assign uio_oe  = 4'b1011;"),
    ])
    rc, j, _ = run_equiv(tmp_path, src)
    assert rc == 1
    assert "port uio_oe is output [4], contract says output [8]" in j["detail"]


def test_rvfi_only_reset_difference_fails(tmp_path):
    # A different reset value under RISCV_FORMAL is a behavior difference at the pins.
    src = copy_fixture(tmp_path, "pass_observer", [
        ("fx_core.v", "            gpio <= 8'd0;\n",
         "`ifdef RISCV_FORMAL\n            gpio <= 8'd1;\n`else\n            gpio <= 8'd0;\n`endif\n"),
    ])
    rc, j, _ = run_equiv(tmp_path, src)
    assert rc == 1
    assert "gpio[0]" in j["unproven"]


def test_no_sources_fails(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc, j, _ = run_equiv(tmp_path, empty)
    assert rc == 1
    assert "no *.v files" in j["detail"]


def test_syntax_error_fails(tmp_path):
    src = copy_fixture(tmp_path, "pass_observer", [
        ("fx_core.v", "endmodule", "endmodul"),
    ])
    rc, j, _ = run_equiv(tmp_path, src)
    assert rc == 1
    assert "yosys build" in j["detail"]


def test_sigbits():
    assert equiv.sigbits("\\uo_out") == [("uo_out", i) for i in range(8)]
    assert equiv.sigbits("\\uo_out [3]") == [("uo_out", 3)]
    assert equiv.sigbits("\\x [2:1]") == [("x", 1), ("x", 2)]
    assert equiv.sigbits("{ \\a [1] 1'0 \\b [0] }") == [("a", 1), ("b", 0)]
