"""Tests for tools/src_check.py: contract section 1's restricted preprocessor and src/ layout."""
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import src_check  # noqa: E402

EQUIV_FIX = os.path.join(HERE, "test_fixtures", "equiv")
SC_FIX = os.path.join(HERE, "test_fixtures", "src_check")
SEED_SRC = os.path.join(os.path.dirname(HERE), "src")
HAVE_TOOLS = all(shutil.which(t, path=src_check.tool_env()["PATH"]) for t in ("verilator", "yosys"))
needs_tools = pytest.mark.skipif(not HAVE_TOOLS, reason="verilator and yosys needed")

TOP = "module tt_um_fesens_r32v(input wire clk, output wire [7:0] uo_out);\n{body}\nendmodule\n"


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    return d


def top(src, body="  assign uo_out = 8'd0;", name="tt_um_fesens_r32v.v", raw=None):
    (src / name).write_text(raw if raw is not None else TOP.format(body=body))


def problems(src):
    return src_check.check(str(src))


def one(src, needle):
    errs = problems(src)
    assert any(needle in e for e in errs), errs
    return errs


# --- the reviewer's repros and the samples that must pass --------------------------------------

FAIL_REPROS = sorted(d for d in os.listdir(SC_FIX) if d.startswith("fail_"))
PASS_SAMPLES = sorted(d for d in os.listdir(SC_FIX) if d.startswith("pass_"))


def test_repro_sets_are_complete():
    assert FAIL_REPROS == ["fail_attr_from_macro", "fail_cross_file_define", "fail_eq_from_macro",
                           "fail_eqp_pasted_initial_rvfi", "fail_include_order",
                           "fail_paste_initial", "fail_simd_define",
                           "fail_split_verilator_define", "fail_star_space_attr"]
    assert PASS_SAMPLES == ["pass_bitserial", "pass_generate", "pass_param_integer",
                            "pass_star_space_sensitivity"]


@pytest.mark.parametrize("name,needle", [
    ("fail_attr_from_macro", "attribute instance"),
    ("fail_cross_file_define", "`define is not allowed"),
    ("fail_eq_from_macro", "`define is not allowed"),
    ("fail_eqp_pasted_initial_rvfi", "`P1 is not allowed"),
    ("fail_include_order", "`include is not allowed"),
    ("fail_paste_initial", "`P1 is not allowed"),
    ("fail_simd_define", "`define is not allowed"),
    ("fail_split_verilator_define", "`define is not allowed"),
    ("fail_star_space_attr", "attribute instance"),
])
def test_repros_fail_static(name, needle):
    errs = src_check.check(os.path.join(SC_FIX, name))
    assert any(needle in e for e in errs), errs


def test_split_repro_also_rejects_lowercase_verilator_condition():
    errs = src_check.check(os.path.join(SC_FIX, "fail_split_verilator_define"))
    assert any("got '`ifdef verilator'" in e for e in errs), errs


@pytest.mark.parametrize("name", PASS_SAMPLES)
def test_samples_pass_static(name):
    assert src_check.check(os.path.join(SC_FIX, name)) == []


@needs_tools
@pytest.mark.parametrize("name", PASS_SAMPLES)
def test_samples_pass_differential(name):
    assert src_check.check_differential(os.path.join(SC_FIX, name)) == []


@pytest.mark.parametrize("name", sorted(os.listdir(EQUIV_FIX)))
def test_equiv_fixtures_are_clean(name):
    assert src_check.check(os.path.join(EQUIV_FIX, name)) == []


@needs_tools
def test_seed_is_clean():
    if not os.path.isfile(os.path.join(SEED_SRC, "tt_um_fesens_r32v.v")):
        pytest.skip("no seed in src/")
    assert src_check.check(SEED_SRC) == []
    assert src_check.check_differential(SEED_SRC) == []


# --- differential backstop on its own ----------------------------------------------------------

@needs_tools
def test_differential_catches_verilator_lowercase_predefine(src):
    # Static rules bypassed on purpose: the backstop alone must see the split.
    top(src, "`ifdef verilator\n  assign uo_out = 8'd1;\n`else\n  assign uo_out = 8'd0;\n`endif")
    errs = src_check.check_differential(str(src))
    assert any("(plain): verilator and yosys preprocess src/ differently" in e for e in errs), errs


@needs_tools
@pytest.mark.parametrize("macro", ["YOSYS", "SYNTHESIS", "VERILATOR", "coverage_block_off",
                                   "verilator3"])
def test_differential_catches_any_tool_macro(src, macro):
    top(src, f"`ifdef {macro}\n  assign uo_out = 8'd1;\n`else\n  assign uo_out = 8'd0;\n`endif")
    assert src_check.check_differential(str(src))


@needs_tools
def test_differential_catches_split_repro():
    errs = src_check.check_differential(os.path.join(SC_FIX, "fail_split_verilator_define"))
    assert any("(plain)" in e and "differently" in e for e in errs), errs


@needs_tools
def test_differential_reports_preprocessor_failure():
    errs = src_check.check_differential(os.path.join(SC_FIX, "fail_include_order"))
    assert any("preprocessor failed" in e for e in errs), errs


@needs_tools
def test_differential_accepts_allowed_directives(src):
    top(src, raw="`default_nettype none\n`timescale 1ns/1ps\n" + TOP.format(
        body="`ifdef RISCV_FORMAL\n  wire f = clk;\n`else\n  wire g = clk;\n`endif\n"
             "`ifndef RISCV_FORMAL\n  wire h = clk;\n`endif\n  assign uo_out = 8'd0;")
        + "`default_nettype wire\n")
    assert problems(src) == []
    assert src_check.check_differential(str(src)) == []


def test_normalized_drops_benign_differences():
    v = "`timescale 1ns/1ps\nmodule m;  wire  a ;\nendmodule\n"
    y = "`file_push \"x.v\"\n\nmodule m; /* c */ wire a;\n\nendmodule\n`file_pop\n"
    assert src_check.normalized(v) == src_check.normalized(y)


# --- directive allowlist -----------------------------------------------------------------------

@pytest.mark.parametrize("line", ["`define W 8", "`undef RISCV_FORMAL", "`include \"x.vh\"",
                                  "`celldefine", "`endcelldefine", "`resetall",
                                  "`undefineall", "`line 1 \"x.v\" 0", "`pragma protect",
                                  "`begin_keywords \"1364-2005\"", "`nounconnected_drive",
                                  "`unconnected_drive pull1", "`RVFI_OUTPUTS", "`__LINE__",
                                  "`__FILE__"])
def test_other_directives_and_macro_uses_rejected(src, line):
    top(src, f"{line}\n  assign uo_out = 8'd0;")
    one(src, "is not allowed (contract section 1")


@pytest.mark.parametrize("cond", ["ifdef YOSYS", "ifndef SYNTHESIS", "ifdef verilator",
                                  "ifdef verilator3", "ifdef coverage_block_off",
                                  "ifdef RISCV_FORMAL_ALTOPS", "ifdef SIMD_WIDTH",
                                  "ifdef \\RISCV_FORMAL ", "ifdef (RISCV_FORMAL)",
                                  "ifdef RISCV_FORMAL wire x;"])
def test_conditions_other_than_riscv_formal_rejected(src, cond):
    top(src, f"`{cond}\n  assign uo_out = 8'd1;\n`else\n  assign uo_out = 8'd0;\n`endif")
    one(src, "is allowed, alone on its line")


def test_elsif_rejected(src):
    top(src, "`ifdef RISCV_FORMAL\n  assign uo_out = 8'd1;\n`elsif RISCV_FORMAL\n"
             "  assign uo_out = 8'd2;\n`endif")
    one(src, "`elsif is not allowed")


@pytest.mark.parametrize("body,needle", [
    ("`ifdef RISCV_FORMAL\n  wire a = clk;", "without `endif"),
    ("`endif", "`endif without `ifdef"),
    ("`else", "`else without `ifdef"),
    ("`ifdef RISCV_FORMAL\n`else\n`else\n`endif", "second `else"),
    ("`ifdef RISCV_FORMAL `endif", "one directive per line"),
    ("`ifdef RISCV_FORMAL\n  wire a = clk; `endif", "`endif must be alone"),
])
def test_ifdef_structure(src, body, needle):
    top(src, body + "\n  assign uo_out = 8'd0;")
    one(src, needle)


def test_nested_riscv_formal_blocks_balanced_pass(src):
    top(src, "`ifdef RISCV_FORMAL\n`ifndef RISCV_FORMAL\n  wire d = clk;\n`endif\n`endif\n"
             "  assign uo_out = 8'd0;")
    assert problems(src) == []


@pytest.mark.parametrize("line,ok", [("`default_nettype none", True),
                                     ("`default_nettype wire", True),
                                     ("`default_nettype tri", False),
                                     ("`timescale 1ns/1ps", True),
                                     ("`timescale 10ns / 100ps", True),
                                     ("`timescale 1 ns/1 ps", True),
                                     ("`timescale 3ns/1ps", False),
                                     ("`timescale 1ns", False)])
def test_default_nettype_and_timescale_forms(src, line, ok):
    top(src, raw=line + "\n" + TOP.format(body="  assign uo_out = 8'd0;"))
    assert (problems(src) == []) is ok, problems(src)


def test_backticks_in_comments_and_strings_are_fine(src):
    top(src, "  // `define X would be rejected in code\n  /* `include \"y\" */\n"
             "  assign uo_out = 8'd0;")
    assert problems(src) == []


@pytest.mark.parametrize("seq", ["``", "`\"", "` define"])
def test_stray_backticks_rejected(src, seq):
    top(src, f"  {seq} x\n  assign uo_out = 8'd0;")
    one(src, "stray backtick")


def test_escaped_identifier_cannot_hide_a_directive(src):
    top(src, "  wire \\a//b ; `define X\n  assign uo_out = 8'd0;")
    one(src, "`define is not allowed")


# --- initial and initializers ------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "  reg r;\n  initial r = 1'b0;\n  assign uo_out = {7'd0, r};",
    "`ifdef RISCV_FORMAL\n  initial begin end\n`endif\n  assign uo_out = 8'd0;",
])
def test_initial_rejected(src, body):
    top(src, body)
    one(src, "`initial` is not allowed")


@pytest.mark.parametrize("decl", ["reg r = 1'b0;", "reg [7:0] q = 8'hFF;", "integer i = 3;",
                                  "reg a, b = 1'b1;", "time t = 0;", "real x = 1.0;",
                                  "reg [7:0] m [0:3], z = 8'd0;"])
def test_variable_initializers_rejected(src, decl):
    top(src, f"  {decl}\n  assign uo_out = 8'd0;")
    one(src, "initializer on a")


def test_ansi_port_initializer_rejected(src):
    top(src, raw="module tt_um_fesens_r32v(input wire clk, output reg [7:0] uo_out = 8'd0);\n"
                 "  always @(posedge clk) uo_out <= uo_out + 8'd1;\nendmodule\n")
    one(src, "initializer on a `reg`")


@pytest.mark.parametrize("decl", ["parameter integer N = 4;", "localparam integer DEPTH = 16;",
                                  "parameter [3:0] X = 4'd1;", "localparam W = 8;",
                                  "parameter integer A = 1, B = 2;",
                                  "localparam [1:0] S0 = 2'd0, S1 = 2'd1;",
                                  "wire w = clk;", "wire [7:0] v = {8{clk}};"])
def test_constants_and_continuous_assignments_allowed(src, decl):
    top(src, f"  {decl}\n  assign uo_out = 8'd0;")
    assert problems(src) == []


def test_module_parameter_port_list_allowed(src):
    top(src, raw="module tt_um_fesens_r32v #(parameter integer N = 4, parameter W = 8)\n"
                 "  (input wire clk, output wire [7:0] uo_out);\n"
                 "  reg [7:0] q;\n  integer k;\n"
                 "  always @(posedge clk) for (k = 0; k < N; k = k + 1) q[k] <= ~q[k];\n"
                 "  assign uo_out = (q == 8'd0) ? W[7:0] : q;\nendmodule\n")
    assert problems(src) == []


def test_initial_lookalikes_allowed(src):
    top(src, "  reg [7:0] initial_pc;\n  always @(posedge clk) initial_pc <= initial_pc + 8'd1;\n"
             "  // initial value comes from reset\n  assign uo_out = initial_pc;")
    assert problems(src) == []


# --- attributes ---------------------------------------------------------------------------------

@pytest.mark.parametrize("sens", ["@(*)", "@( * )".replace("( ", "("), "@(* )", "@(*  )",
                                  "@ (*)", "@*"])
def test_star_sensitivity_allowed(src, sens):
    top(src, f"  reg [7:0] q;\n  always {sens} q = 8'd0;\n  assign uo_out = q;")
    assert problems(src) == []


@pytest.mark.parametrize("attr", ["(* keep *) wire k = clk;", "(* full_case *)",
                                  "(*keep*) wire k = clk;", "(* *) wire k = clk;",
                                  "always @(* ) begin (* parallel_case *) end"])
def test_attribute_instances_rejected(src, attr):
    top(src, f"  {attr}\n  assign uo_out = 8'd0;")
    one(src, "attribute instance")


# --- metacomments, encoding --------------------------------------------------------------------

@pytest.mark.parametrize("c", ["/* verilator lint_off UNUSED */", "/*verilator public*/",
                               "// synopsys translate_off", "// synthesis keep",
                               "// pragma protect begin", "// yosys: keep this",
                               "// Verilator would zero this", "/* SYNTHESIS: no */",
                               "// see translate_on below", "// lint_off is handy",
                               "// a full_case here", "/* parallel_case */",
                               "/* * verilator lint_off WIDTH */"])
def test_metacomments_rejected(src, c):
    top(src, f"  {c}\n  assign uo_out = 8'd0;")
    one(src, "tool metacomment")


def test_ordinary_comments_allowed(src):
    top(src, "  // the synthesized netlist is checked at gate level\n"
             "  /* uses no pragmas; lint is clean */\n  assign uo_out = 8'd0;")
    assert problems(src) == []


@pytest.mark.parametrize("data,needle", [
    (b"module m;\r\nendmodule\r\n", "carriage return"),
    (b"module m; // caf\xc3\xa9\nendmodule\n", "non-ASCII"),
    (b"module m;\x0cendmodule\n", "control character"),
    (b"module m; /* open\nendmodule\n", "unterminated /* comment"),
    (b"module m; initial $display(\"x\nendmodule\n", "unterminated string"),
])
def test_encoding_and_lexing_errors(src, data, needle):
    (src / "m.v").write_bytes(data)
    one(src, needle)


# --- names and layout --------------------------------------------------------------------------

def test_clean_design_with_extras(src):
    top(src)
    (src / "config.json").write_text("{}\n")
    (src / "test_alu.py").write_text("def test_x(): pass\n")
    (src / "test_alu").mkdir()
    (src / "test_alu" / "prog.hex").write_text("00\n")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "test_alu.cpython-313-pytest-9.0.3.pyc").write_bytes(b"\0")
    assert problems(src) == []


@pytest.mark.parametrize("bad", ["z.v; log INJECTED; log x.v", "a b.v", "a;b.v", 'a"b.v',
                                 "a'b.v", "a\\b.v", "-Wno-fatal.v", ".hidden", "caf\u00e9.v",
                                 "a\tb.v", "a\x01b.v", "+define+X.v", "a$b.v", "a+b.v"])
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


def test_src_itself_symlink_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, tmp_path / "src")
    assert any("real directory" in e for e in src_check.check(str(tmp_path / "src")))


@needs_tools
def test_cli(src):
    top(src)
    script = os.path.join(HERE, "src_check.py")
    p = subprocess.run([sys.executable, script, "--src", str(src)], capture_output=True, text=True)
    assert p.returncode == 0 and "PASS" in p.stdout
    shutil.copy(src / "tt_um_fesens_r32v.v", src / "bad name.v")
    p = subprocess.run([sys.executable, script, "--src", str(src)], capture_output=True, text=True)
    assert p.returncode == 1 and "FAIL" in p.stdout
