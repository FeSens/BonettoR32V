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
    # The differential preprocessor check (yosys parses too) now stops it before the build.
    assert "preprocessor failed" in j["detail"] or "yosys build" in j["detail"]


def test_file_name_injection_blocked_before_yosys(tmp_path):
    # Review repro: a file named like this used to inject yosys commands into the build script.
    src = copy_fixture(tmp_path, "pass_observer")
    (src / "z.v; log INJECTED_CMD_RAN; log x.v").write_text("module zz; endmodule\n")
    rc, j, out = run_equiv(tmp_path, src)
    assert rc == 1
    assert "src hygiene" in j["detail"] and "name must match" in j["detail"]
    assert not (tmp_path / "work").exists()          # nothing was built, no yosys ran
    assert "INJECTED_CMD_RAN" not in [l.strip() for l in out.splitlines()]


def test_macro_split_blocked(tmp_path):
    src = copy_fixture(tmp_path, "pass_observer", [
        ("tt_um_fesens_r32v.v", "    assign uo_out  = gpio;\n",
         "`ifdef YOSYS\n    assign uo_out  = gpio;\n`else\n    assign uo_out  = ~gpio;\n`endif\n"),
    ])
    rc, j, _ = run_equiv(tmp_path, src)
    assert rc == 1 and "`ifdef YOSYS" in j["detail"]


def test_rvfi_only_initial_value_blocked(tmp_path):
    src = copy_fixture(tmp_path, "pass_observer", [
        ("fx_core.v", "    reg [1:0] ptr;\n",
         "    reg [1:0] ptr;\n`ifdef RISCV_FORMAL\n    initial ptr = 2'd0;\n`endif\n"),
    ])
    rc, j, _ = run_equiv(tmp_path, src)
    assert rc == 1 and "`initial` is not allowed" in j["detail"]


def test_prestrip_init_on_rvfi_only_state_fails(tmp_path, monkeypatch, capsys):
    # Review repro (eqp): a macro-pasted `initial` on RVFI-only state. After the rvfi strip that
    # state and its init attribute are gone, so only the pre-strip check can see it. src_check
    # rejects the macros first; bypass it here to test the second line on its own.
    src = copy_fixture(tmp_path, "pass_observer", [
        ("tt_um_fesens_r32v.v", "    reg [63:0] order_q;\n",
         "    reg [63:0] order_q;\n`define P1 ini\n`define P2 tial\n"
         "    `P1`P2 valid_q = 1'b1;\n"),
    ])
    monkeypatch.setattr(equiv.src_check, "check", lambda path: [])
    monkeypatch.setattr(equiv.src_check, "check_differential", lambda path, env=None: [])
    out = tmp_path / "equiv.json"
    rc = equiv.main(["--src", str(src), "--work", str(tmp_path / "work"), "--out", str(out)])
    detail = json.loads(out.read_text())["detail"]
    assert rc == 1
    assert "A before the rvfi strip: valid_q" in detail, detail
    assert "B (plain)" not in detail and "A (RISCV_FORMAL, rvfi stripped)" not in detail


def test_init_wires_reads_yosys_output(tmp_path):
    # The init-attribute check is the second line behind src_check; pin its parsing to what
    # yosys actually writes for `reg r = 1` and `initial s = 0`.
    v = tmp_path / "m.v"
    v.write_text("module m(input wire clk, input wire d, output wire q, output wire p);\n"
                 "  reg r = 1'b1;\n  reg s;\n  initial s = 1'b0;\n"
                 "  always @(posedge clk) begin r <= d; s <= ~s; end\n"
                 "  assign q = r; assign p = s;\nendmodule\n")
    il = tmp_path / "m.il"
    subprocess.run(["yosys", "-q", "-p", f"read_verilog -mem2reg {v}; hierarchy -top m; proc; "
                    f"flatten; opt_clean; write_rtlil {il}"], check=True, env=equiv.tool_env())
    assert sorted(equiv.init_wires(str(il))) == ["r", "s"]
    clean = tmp_path / "clean.il"
    clean.write_text("module \\m\n  attribute \\src \"x\"\n  wire input 1 \\a\nend\n")
    assert equiv.init_wires(str(clean)) == []


@pytest.mark.parametrize("bad", ["/tmp/a b/x.v", "/tmp/a;b.v", '/tmp/a"b.v', "/tmp/a#b.v",
                                 "/tmp/a\nlog x.v", "/tmp/caf\u00e9.v"])
def test_ys_path_rejects_unsafe(bad):
    with pytest.raises(equiv.UnsafePath):
        equiv.ys_path(bad)


def test_ys_path_quotes_safe():
    assert equiv.ys_path("/tmp/ok_dir/x-1.v") == '"/tmp/ok_dir/x-1.v"'
    assert equiv.ys_path("/tmp/ok_dir", quote=False) == "/tmp/ok_dir"


def test_unsafe_work_dir_fails(tmp_path):
    out = tmp_path / "equiv.json"
    p = subprocess.run([sys.executable, os.path.join(HERE, "equiv.py"), "--src",
                        os.path.join(FIX, "pass_observer"), "--work", str(tmp_path / "w x"),
                        "--out", str(out)], capture_output=True, text=True)
    assert p.returncode == 1
    assert "unsafe in a yosys script" in json.loads(out.read_text())["detail"]


def test_sigbits():
    assert equiv.sigbits("\\uo_out") == [("uo_out", i) for i in range(8)]
    assert equiv.sigbits("\\uo_out [3]") == [("uo_out", 3)]
    assert equiv.sigbits("\\x [2:1]") == [("x", 1), ("x", 2)]
    assert equiv.sigbits("{ \\a [1] 1'0 \\b [0] }") == [("a", 1), ("b", 0)]
