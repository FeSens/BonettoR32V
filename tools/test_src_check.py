"""Tests for tools/src_check.py (src/ hygiene: names, layout, macros, includes, initial values)."""
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import src_check  # noqa: E402

FIX = os.path.join(HERE, "test_fixtures", "equiv")
TOP = "module tt_um_fesens_r32v(input wire clk, output wire [7:0] uo_out);\n{body}\nendmodule\n"


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    return d


def top(src, body="  assign uo_out = 8'd0;", name="tt_um_fesens_r32v.v"):
    (src / name).write_text(TOP.format(body=body))


def problems(src):
    return src_check.check(str(src))


def one(src, needle):
    errs = problems(src)
    assert any(needle in e for e in errs), errs
    return errs


@pytest.mark.parametrize("name", sorted(os.listdir(FIX)))
def test_all_equiv_fixtures_are_clean(name):
    assert src_check.check(os.path.join(FIX, name)) == []


def test_clean_design_with_extras(src):
    top(src)
    (src / "config.json").write_text("{}\n")
    (src / "test_alu.py").write_text("def test_x(): pass\n")
    (src / "test_alu").mkdir()
    (src / "test_alu" / "prog.hex").write_text("00\n")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "test_alu.cpython-313-pytest-9.0.3.pyc").write_bytes(b"\0")
    assert problems(src) == []


# --- names and layout --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["z.v; log INJECTED; log x.v", "a b.v", "a;b.v", 'a"b.v',
                                 "a'b.v", "a\\b.v", "-Wno-fatal.v", ".hidden", "caf\u00e9.v",
                                 "a\tb.v", "a\x01b.v", "+define+X.v", "a$b.v"])
def test_odd_names_rejected(src, bad):
    top(src)
    (src / bad).write_text("module m; endmodule\n")
    one(src, "name must match")


@pytest.mark.parametrize("bad", ["my-mod.v", "a.b.v", "1-x.v"])
def test_verilog_file_name_rule(src, bad):
    top(src)
    (src / bad).write_text("module m; endmodule\n")
    one(src, "Verilog file names must match")


def test_systemverilog_rejected(src):
    top(src)
    (src / "alu.sv").write_text("module alu; endmodule\n")
    one(src, "SystemVerilog is not allowed")


def test_subdirectory_rejected(src):
    top(src)
    (src / "rtl").mkdir()
    (src / "rtl" / "alu.v").write_text("module alu; endmodule\n")
    one(src, "subdirectories are not allowed")


@pytest.mark.parametrize("link", ["evil.v", "test_link"])
def test_symlinks_rejected_even_test_ones(src, tmp_path, link):
    top(src)
    target = tmp_path / "outside.v"
    target.write_text("module outside; endmodule\n")
    os.symlink(target, src / link)
    one(src, "symlinks are not allowed")


def test_symlink_inside_test_dir_rejected(src, tmp_path):
    top(src)
    (src / "test_x").mkdir()
    os.symlink(tmp_path, src / "test_x" / "up")
    one(src, "symlinks are not allowed")


def test_odd_name_inside_test_dir_rejected(src):
    top(src)
    (src / "test_x").mkdir()
    (src / "test_x" / "a b.txt").write_text("x\n")
    one(src, "name must match")


def test_src_itself_symlink_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, tmp_path / "src")
    assert any("real directory" in e for e in src_check.check(str(tmp_path / "src")))


# --- macros ------------------------------------------------------------------------------------

@pytest.mark.parametrize("cond", ["ifdef YOSYS", "ifndef SYNTHESIS", "ifdef VERILATOR",
                                  "ifdef FORMAL", "ifdef GL_TEST", "ifdef COCOTB_SIM",
                                  "ifdef RISCV_FORMAL_ALTOPS", "ifdef UNDEFINED_ANYWHERE",
                                  "ifdef \\YOSYS ", "ifdef (RISCV_FORMAL)"])
def test_foreign_conditions_rejected(src, cond):
    top(src, f"`{cond}\n  assign uo_out = 8'd1;\n`else\n  assign uo_out = 8'd0;\n`endif")
    assert problems(src), cond


def test_elsif_checked(src):
    top(src, "`ifdef RISCV_FORMAL\n  assign uo_out = 8'd1;\n`elsif YOSYS\n"
             "  assign uo_out = 8'd2;\n`else\n  assign uo_out = 8'd0;\n`endif")
    one(src, "`elsif YOSYS")


def test_riscv_formal_and_local_macros_allowed(src):
    (src / "defs.vh").write_text("`define WIDE_ALU\n`define W 8\n")
    top(src, '`include "defs.vh"\n`ifdef WIDE_ALU\n  assign uo_out = `W\'d1;\n`endif\n'
             "`ifdef RISCV_FORMAL\n  wire x = 1'b0;\n`endif\n`ifndef WIDE_ALU\n`endif")
    assert problems(src) == []


@pytest.mark.parametrize("d", ["define SYNTHESIS", "define YOSYS 1", "define VERILATOR",
                               "define RISCV_FORMAL", "define RISCV_FORMAL_ALIGNED_MEM",
                               "define RVFI_OUTPUTS", "define COCOTB_SIM", "define GL_TEST",
                               "undef RISCV_FORMAL", "undef SYNTHESIS", "define __ICARUS__",
                               "define SIM_FAST"])
def test_tool_macro_defines_rejected(src, d):
    top(src, f"`{d}\n  assign uo_out = 8'd0;")
    one(src, "tool macros may not be defined or undefined")


def test_tool_macro_define_cannot_unlock_condition(src):
    top(src, "`define YOSYS\n`ifdef YOSYS\n  assign uo_out = 8'd1;\n`endif")
    errs = problems(src)
    assert any("`define YOSYS" in e for e in errs) and any("`ifdef YOSYS" in e for e in errs)


def test_undefineall_and_token_pasting_rejected(src):
    top(src, "`undefineall\n`define CAT(a,b) a``b\n  assign uo_out = 8'd0;")
    errs = problems(src)
    assert any("undefineall" in e for e in errs) and any("token pasting" in e for e in errs)


def test_directive_hidden_in_define_body_rejected(src):
    top(src, "`define SNEAK `ifdef YOSYS\n  assign uo_out = 8'd0;")
    one(src, "`ifdef YOSYS")


def test_comments_are_ignored(src):
    top(src, "  // `ifdef YOSYS would be bad\n  /* `define SYNTHESIS\n initial */\n"
             "  assign uo_out = 8'd0; // initial value is reset")
    assert problems(src) == []


def test_escaped_identifier_cannot_hide_a_directive(src):
    # Tools read `\a//b` as one identifier, so the `ifdef that follows is real.
    top(src, "  wire \\a//b ; `ifdef YOSYS\n  assign uo_out = 8'd1;\n  `endif\n"
             "  assign uo_out = 8'd0;")
    one(src, "`ifdef YOSYS")


# --- includes ----------------------------------------------------------------------------------

@pytest.mark.parametrize("inc", ['"rvfi_macros.vh"', '"../verif/x.vh"', '"/etc/passwd"',
                                 "<defs.vh>", "`INC", '"sub/defs.vh"', '"missing.vh"'])
def test_bad_includes_rejected(src, inc):
    (src / "defs.vh").write_text("`define W 8\n")
    top(src, f"`include {inc}\n  assign uo_out = 8'd0;")
    one(src, "`include")


def test_included_file_is_checked(src):
    (src / "defs.vh").write_text("`ifdef VERILATOR\n`define W 8\n`endif\n")
    top(src, '`include "defs.vh"\n  assign uo_out = 8\'d0;')
    one(src, "src/defs.vh:1: `ifdef VERILATOR")


# --- initial values, attributes, pragmas -------------------------------------------------------

@pytest.mark.parametrize("body", [
    "  reg r;\n  initial r = 1'b0;\n  assign uo_out = {7'd0, r};",
    "`ifdef RISCV_FORMAL\n  initial begin end\n`endif\n  assign uo_out = 8'd0;",
])
def test_initial_rejected(src, body):
    top(src, body)
    one(src, "`initial` is not allowed")


@pytest.mark.parametrize("decl", ["reg r = 1'b0;", "reg [7:0] q = 8'hFF;", "integer i = 3;",
                                  "reg a, b = 1'b1;"])
def test_declaration_initializers_rejected(src, decl):
    top(src, f"  {decl}\n  assign uo_out = 8'd0;")
    one(src, "declaration initializer")


def test_harmless_lookalikes_allowed(src):
    top(src, "  reg [7:0] initial_pc;\n  reg q;\n  wire w = clk;\n"
             "  always @(*) begin q = (initial_pc == 8'd0); end\n"
             "  always @(posedge clk) initial_pc <= initial_pc + 8'd1;\n"
             "  reg [7:0] cmp;\n  always @(posedge clk) cmp <= (initial_pc >= 8'd3) ? 8'd1 : 8'd0;\n"
             "  assign uo_out = initial_pc ^ {7'd0, q} ^ cmp ^ {7'd0, w};")
    assert problems(src) == []


@pytest.mark.parametrize("attr", ["full_case", "parallel_case", "init = 1", "nosync", "nolatches",
                                  "blackbox", "whitebox", "nomem2reg", "keep, full_case"])
def test_bad_attributes_rejected(src, attr):
    top(src, f"  (* {attr} *) reg r;\n  assign uo_out = 8'd0;")
    one(src, "makes synthesis differ from simulation")


def test_keep_attribute_and_star_sensitivity_allowed(src):
    top(src, "  (* keep *) wire k = clk;\n  reg [7:0] q;\n  always @(*) q = 8'd0;\n"
             "  assign uo_out = q;")
    assert problems(src) == []


@pytest.mark.parametrize("pragma", ["// synopsys translate_off", "// synthesis translate_on",
                                    "/* synopsys full_case parallel_case */"])
def test_pragma_comments_rejected(src, pragma):
    top(src, f"  {pragma}\n  assign uo_out = 8'd0;")
    one(src, "pragma comment")


def test_non_ascii_code_rejected(src):
    top(src, "  wire \u00e9 = clk;\n  assign uo_out = 8'd0;")
    one(src, "non-ASCII")


def test_cli(src):
    top(src)
    script = os.path.join(HERE, "src_check.py")
    p = subprocess.run([sys.executable, script, "--src", str(src)], capture_output=True, text=True)
    assert p.returncode == 0 and "PASS" in p.stdout
    shutil.copy(src / "tt_um_fesens_r32v.v", src / "bad name.v")
    p = subprocess.run([sys.executable, script, "--src", str(src)], capture_output=True, text=True)
    assert p.returncode == 1 and "FAIL" in p.stdout
