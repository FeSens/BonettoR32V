"""Tests for tools/src_check.py: contract section 1's restricted preprocessor and src/ layout."""
import glob
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
HAVE_TOOLS = all(shutil.which(t, path=src_check.tool_env()["PATH"])
                 for t in src_check.PREPROCESSORS)
needs_tools = pytest.mark.skipif(not HAVE_TOOLS, reason="verilator, yosys and iverilog needed")

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


DELAY = "`#` delay control is not allowed"
REPRO_NEEDLES = {
    "fail_attr_from_macro": "attribute instance",
    "fail_cross_file_define": "`define is not allowed",
    "fail_eq_from_macro": "`define is not allowed",
    "fail_eqp_pasted_initial_rvfi": "`P1 is not allowed",
    "fail_include_order": "`include is not allowed",
    "fail_paste_initial": "`P1 is not allowed",
    "fail_simd_define": "`define is not allowed",
    "fail_split_verilator_define": "`define is not allowed",
    "fail_star_space_attr": "attribute instance",
    # review round 3
    "fail_assign_delay": "tt_um_fesens_r32v.v:6: " + DELAY,
    "fail_nb_delay": "tt_um_fesens_r32v.v:6: " + DELAY,
    "fail_net_delay": "tt_um_fesens_r32v.v:6: " + DELAY,
    "fail_net_delay_range": "tt_um_fesens_r32v.v:6: " + DELAY,
    "fail_defparam": "`defparam` is not allowed",
    "fail_hier_self": "hierarchical reference `tt_um_fesens_r32v.ui_in`",
    "fail_hier_upward": "hierarchical reference `rvfi_wrapper.uut`",
    "fail_always_no_event": "`always` must be followed by an event control",
    "fail_sys_random": "system task/function `$random`",
    "fail_sys_time": "system task/function `$time`",
    "fail_sys_finish": "system task/function `$finish`",
    "fail_sys_fopen": "system task/function `$fopen`",
    "fail_specify": "`specify` is not allowed",
    "fail_wand": "`wand` is not allowed",
    "fail_tri0": "`tri0` is not allowed",
    "fail_tri1": "`tri1` is not allowed",
    "fail_event": "`event` is not allowed",
    "fail_force": "`force` is not allowed",
    "fail_real": "`real` is not allowed",
    "fail_escid_backtick": "escaped identifier '\\\\a`ifdef__ICARUS__' is not allowed",
    "fail_function_stale_local": "function f reads t before assigning it on every path",
    "fail_level_sensitivity": "event control must be @(*), @* or a list of posedge/negedge",
}


def test_repro_sets_are_complete():
    assert FAIL_REPROS == sorted(REPRO_NEEDLES)
    assert PASS_SAMPLES == ["pass_bitserial", "pass_generate", "pass_minimal",
                            "pass_param_integer", "pass_star_space_sensitivity"]


@pytest.mark.parametrize("name", sorted(REPRO_NEEDLES))
def test_repros_fail_static(name):
    errs = src_check.check(os.path.join(SC_FIX, name))
    assert any(REPRO_NEEDLES[name] in e for e in errs), errs


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
                                     ("`default_nettype none // trailing comment", True),
                                     ("`default_nettype tri", False),
                                     ("`default_nettype wand", False),
                                     ("`default_nettype NONE", False),
                                     ("`default_nettype", False),
                                     ("`default_nettype none garbage", False),
                                     ("`default_nettype none;", False),
                                     ("`default_nettype none /* block */", False),
                                     ("`default_nettype none `default_nettype wire", False),
                                     ("`timescale 1ns/1ps", True),
                                     ("`timescale 10ns / 100ps", True),
                                     ("`timescale 1 ns/1 ps", True),
                                     ("`timescale 1ns/1ns", True),
                                     ("`timescale 100ps/10fs", True),
                                     ("`timescale 1s/1fs", True),
                                     ("`timescale 3ns/1ps", False),
                                     ("`timescale 1ns", False),
                                     ("`timescale", False),
                                     ("`timescale 1ns/1ps garbage", False),
                                     ("`timescale 1ns/1ps;", False),
                                     ("`timescale 1.0ns/1ps", False),
                                     ("`timescale 1ns/1xs", False),
                                     ("`timescale 1ps/1ns", False)])
def test_default_nettype_and_timescale_forms(src, line, ok):
    top(src, raw=line + "\n" + TOP.format(body="  assign uo_out = 8'd0;"))
    assert (problems(src) == []) is ok, problems(src)


def test_timescale_precision_coarser_than_unit_message(src):
    top(src, raw="`timescale 1ps/1ns\n" + TOP.format(body="  assign uo_out = 8'd0;"))
    one(src, "`timescale precision is coarser than its unit")


@pytest.mark.parametrize("line", ["`default_nettype none", "`timescale 1ns/1ps"])
def test_timescale_and_default_nettype_inside_a_module_rejected(src, line):
    top(src, f"{line}\n  assign uo_out = 8'd0;")
    one(src, "must be outside module ... endmodule")


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


# --- module names ------------------------------------------------------------------------------

SUB = "module {name}(input wire a, output wire y);\n  assign y = ~a;\nendmodule\n"
NAME_MSG = "must be tt_um_fesens_r32v or start with r32v_ (contract section 1)"


@pytest.mark.parametrize("bad", ["rvfi_wrapper", "tb", "spiram_23lc512", "iss"])
def test_checker_module_names_fail(src, bad):
    top(src)
    (src / "extra.v").write_text(SUB.format(name=bad))
    errs = problems(src)
    assert errs == [f"src/extra.v:1: module name '{bad}' {NAME_MSG}"], errs


def test_r32v_module_passes(src):
    top(src)
    (src / "r32v_alu.v").write_text(SUB.format(name="r32v_alu"))
    (src / "regs.v").write_text(SUB.format(name="r32v_") + SUB.format(name="r32v_regfile_2"))
    assert problems(src) == []


@pytest.mark.parametrize("bad", ["R32V_alu", "r32valu", "tt_um_fesens_r32v_core", "r32v_a$b",
                                 "\\r32v_alu ", "\\tt_um_fesens_r32v ", "alu_r32v_"])
def test_near_miss_module_names_fail(src, bad):
    top(src)
    (src / "extra.v").write_text(SUB.format(name=bad))
    one(src, NAME_MSG)


@pytest.mark.parametrize("unit", ["macromodule tb(input wire a); endmodule",
                                  "primitive iss(output y, input a);\n  table 0 : 1 ; 1 : 0 ;\n"
                                  "  endtable\nendprimitive",
                                  "interface tb; endinterface", "program tb; endprogram",
                                  "checker tb; endchecker", "package iss; endpackage",
                                  "class tb; endclass", "config tb; endconfig",
                                  "module automatic r32v_alu; endmodule"])
def test_other_definition_kinds_are_named_too(src, unit):
    top(src)
    (src / "extra.v").write_text(unit + "\n")
    one(src, NAME_MSG)


def test_module_name_checked_inside_rvfi_block_and_after_the_top(src):
    top(src, raw=TOP.format(body="  assign uo_out = 8'd0;") + "`ifdef RISCV_FORMAL\n"
             + SUB.format(name="rvfi_wrapper") + "`endif\n" + SUB.format(name="tb"))
    errs = problems(src)
    assert errs == [f"src/tt_um_fesens_r32v.v:5: module name 'rvfi_wrapper' {NAME_MSG}",
                    f"src/tt_um_fesens_r32v.v:9: module name 'tb' {NAME_MSG}"], errs


# --- timing controls (review round 3) ----------------------------------------------------------

SUB_P = ("module r32v_sub #(parameter W = 2) (input wire [W-1:0] a, output wire [W-1:0] y);\n"
         "  assign y = ~a;\nendmodule\n")


@pytest.mark.parametrize("body", [
    "  assign #5 uo_out = 8'd0;",
    "  assign #(5) uo_out = 8'd0;",
    "  assign #(1, 2) uo_out = 8'd0;",
    "  assign #1000 uo_out = 8'd0;",
    "  wire #1 w = clk;\n  assign uo_out = {7'd0, w};",
    "  wire [7:0] #2 w = 8'd0;\n  assign uo_out = w;",
    "  reg [7:0] q;\n  always @(posedge clk) q <= #1 8'd5;\n  assign uo_out = q;",
    "  reg [7:0] q;\n  always @(posedge clk) q = #(1) 8'd5;\n  assign uo_out = q;",
    "  reg [7:0] q;\n  always @(posedge clk) begin #1 q <= 8'd5; end\n  assign uo_out = q;",
    "  reg [7:0] q;\n  always @(posedge clk) if (clk) #(2) q <= 8'd1;\n  assign uo_out = q;",
    "  reg [7:0] q;\n  always @(posedge clk) ##1 q <= 8'd1;\n  assign uo_out = q;",
    "  wire [1:0] y;\n  r32v_sub #2 u (.a(2'd1), .y(y));\n  assign uo_out = {6'd0, y};",
    "  wire [1:0] y;\n  vendor_cell #(.W(2)) u (.a(2'd1), .y(y));\n  assign uo_out = {6'd0, y};",
])
def test_delay_controls_rejected(src, body):
    top(src, body)
    (src / "r32v_sub.v").write_text(SUB_P)
    one(src, DELAY)


@pytest.mark.parametrize("wrap", ["`ifdef RISCV_FORMAL\n{x}\n`endif",
                                  "`ifndef RISCV_FORMAL\n{x}\n`endif",
                                  "`ifdef RISCV_FORMAL\n`else\n{x}\n`endif"])
def test_construct_rules_cover_both_builds(src, wrap):
    top(src, wrap.format(x="  assign #5 uo_out = 8'd0;"))
    one(src, DELAY)


def test_parameter_hash_forms_allowed(src):
    top(src, raw="module tt_um_fesens_r32v #(parameter X = 1) (input wire clk, "
                 "output wire [7:0] uo_out);\n  wire [1:0] y1;\n  wire [3:0] y2;\n"
                 "  r32v_sub #(.W(2)) u1 (.a(2'd1), .y(y1));\n"
                 "  r32v_sub #(4) u2 (.a(4'd3), .y(y2));\n"
                 "  assign uo_out = {2'd0, y2, y1};\nendmodule\n")
    (src / "r32v_sub.v").write_text(SUB_P)
    assert problems(src) == []


@pytest.mark.parametrize("body", ["  reg q;\n  always q = clk;",
                                  "  reg q;\n  always begin q = clk; end",
                                  "  reg q;\n  always #5 q = ~q;"])
def test_always_needs_an_event_control(src, body):
    top(src, body + "\n  assign uo_out = {7'd0, q};")
    one(src, "`always` must be followed by an event control")


@pytest.mark.parametrize("body", [
    "  reg q;\n  always @(posedge clk) q <= @(negedge clk) ~q;",
    "  reg q;\n  always @(posedge clk) begin @(negedge clk); q <= ~q; end",
    "  reg q;\n  always @(posedge clk) @(negedge clk) q <= ~q;",
])
def test_event_control_only_directly_after_always(src, body):
    top(src, body + "\n  assign uo_out = {7'd0, q};")
    one(src, "`@` event control is only allowed directly after `always`")


@pytest.mark.parametrize("ev", ["@(posedge clk)", "@(negedge clk)", "@(posedge clk or negedge clk)",
                                "@(posedge clk, negedge clk)", "@(*)", "@*", "@ (*)", "@( * )",
                                "@(posedge clk_q[0])"])
def test_event_lists_allowed(src, ev):
    top(src, f"  reg [1:0] clk_q;\n  reg q;\n  always @(posedge clk) clk_q <= {{clk_q[0], clk}};\n"
             f"  always {ev} q <= ~q;\n  assign uo_out = {{6'd0, clk_q[1], q}};")
    assert problems(src) == []


@pytest.mark.parametrize("ev", ["@(clk)", "@(clk or q)", "@(clk, q)", "@(posedge clk or q)",
                                "@(posedge)", "@()", "@(posedge clk or)",
                                "@(posedge (clk))", "@(posedge clk iff q)", "@(edge clk)"])
def test_level_sensitive_and_malformed_event_lists_rejected(src, ev):
    top(src, f"  reg q;\n  always {ev} q <= ~q;\n  assign uo_out = {{7'd0, q}};")
    one(src, "event control must be @(*), @* or a list of posedge/negedge signals")


@pytest.mark.parametrize("gate", sorted(src_check.GATE_PRIMITIVES))
def test_gate_primitives_rejected(src, gate):
    top(src, f"  wire w;\n  {gate} g0 (w, clk, clk);\n  assign uo_out = {{7'd0, w}};")
    one(src, f"`{gate}` is not allowed (gate primitive")


def test_or_outside_an_event_list_rejected(src):
    top(src, "  reg q;\n  always @(posedge clk or negedge clk) q <= ~q;\n"
             "  assign uo_out = {7'd0, q or clk};")
    errs = problems(src)
    assert len(errs) == 1 and "`or` is not allowed" in errs[0] and ":4:" in errs[0], errs


# --- system tasks, keywords, hierarchy, literals (review round 3) ------------------------------

@pytest.mark.parametrize("name", ["$display", "$random", "$time", "$finish", "$stop", "$fopen",
                                  "$clog2", "$urandom", "$test$plusargs", "$readmemh", "$bits",
                                  "$past", "$root", "$realtime", "$fatal"])
def test_system_tasks_and_functions_rejected(src, name):
    top(src, f"  wire [31:0] w = {name}(8'd1);\n  assign uo_out = w[7:0];")
    one(src, f"system task/function `{name}` is not allowed")


def test_signed_and_unsigned_allowed(src):
    top(src, "  wire signed [7:0] s = $signed(8'd3);\n  assign uo_out = $unsigned(s);")
    assert problems(src) == []


@pytest.mark.parametrize("kw", sorted(src_check.FORBIDDEN_KEYWORDS))
def test_forbidden_keywords_rejected(src, kw):
    top(src, f"  {kw} x;\n  assign uo_out = 8'd0;")
    one(src, f"`{kw}` is not allowed")


def test_event_trigger_rejected(src):
    top(src, "  reg q;\n  always @(posedge clk) -> q;\n  assign uo_out = {7'd0, q};")
    one(src, "`->` event trigger is not allowed")


@pytest.mark.parametrize("expr,ref", [("u_core.gpio", "u_core.gpio"),
                                      ("g_lane[0].q", "].q"),
                                      ("tt_um_fesens_r32v.clk", "tt_um_fesens_r32v.clk"),
                                      ("rvfi_wrapper.uut.clk", "rvfi_wrapper.uut"),
                                      ("f(1).x", ").x")])
def test_hierarchical_references_rejected(src, expr, ref):
    top(src, f"  assign uo_out = {{7'd0, {expr}}};")
    one(src, f"hierarchical reference `{ref}` is not allowed")


@pytest.mark.parametrize("body", ["  assign uo_out = {.a(1)};",
                                  "  wire [1:0] y;\n  r32v_sub u (.*);\n  assign uo_out = 8'd0;",
                                  "  wire [1:0] y;\n  r32v_sub u (.a, .y);\n"
                                  "  assign uo_out = 8'd0;"])
def test_dot_outside_a_named_connection_rejected(src, body):
    top(src, body)
    (src / "r32v_sub.v").write_text(SUB_P)
    one(src, "`.` is only allowed in a named connection")


def test_named_connections_allowed(src):
    top(src, "  wire [1:0] y;\n  r32v_sub #(.W(2)) u (\n    .a(2'd1),\n"
             "`ifdef RISCV_FORMAL\n    .y(y)\n`else\n    .y(y)\n`endif\n  );\n"
             "  assign uo_out = {6'd0, y};")
    (src / "r32v_sub.v").write_text(SUB_P)
    assert problems(src) == []


@pytest.mark.parametrize("ident", ["\\a`ifdef__ICARUS__ ", "\\b#5 ", "\\c ", "\\r32v_x ",
                                   "\\a.b ", "\\\n"])
def test_escaped_identifiers_rejected(src, ident):
    top(src, f"  wire {ident} = 1'b0;\n  assign uo_out = 8'd0;")
    one(src, "escaped identifier")


def test_backslash_line_continuation_rejected(src):
    top(src, "  assign uo_out = 8'd0 | \\\n                  8'd1;")
    one(src, "escaped identifier '\\\\' is not allowed")


@pytest.mark.parametrize("s", ['"a`b"', '"`__ICARUS__"', '"c*/d"', '"e/*f"', '"g//h"', '"\\""',
                               '""'])
def test_string_literals_rejected(src, s):
    top(src, f"  localparam S = {s};\n  assign uo_out = 8'd0;")
    one(src, "string literal")


@pytest.mark.parametrize("s", ['"/*"', '"*/"', '"//"', '"\\"/*"'])
def test_code_after_a_string_is_still_checked(src, s):
    # The string must not open a comment that would hide the `initial` after it.
    top(src, f"  localparam S = {s}; initial begin end\n  assign uo_out = 8'd0;")
    errs = problems(src)
    assert any("string literal" in e for e in errs) and \
        any("`initial` is not allowed" in e for e in errs), errs


@pytest.mark.parametrize("lit,bad", [("1.5", True), ("2.0e3", True), ("1e3", True),
                                     ("0.5", True), ("1E-2", True), ("8'hE5", False),
                                     ("3", False), ("1_000", False), ("'b1", False),
                                     ("16'd1e3", False)])
def test_real_literals_rejected(src, lit, bad):
    top(src, f"  localparam integer P = {lit};\n  assign uo_out = P[7:0];")
    errs = problems(src)
    assert any("real literal" in e for e in errs) is bad, errs


# --- tokenizer sweep (review round 3) ----------------------------------------------------------

def texts(code):
    return [t for _, t, _ in src_check.tokenize(code)[0]]


def test_block_comments_do_not_nest():
    assert texts("/* a /* b */ c */") == ["c", "*", "/"]


@pytest.mark.parametrize("body", ["  /* a /* b */ initial begin end",
                                  "  /* x // y */ initial begin end",
                                  "  // x /* y\n  initial begin end",
                                  "  // x */ y\n  initial begin end"])
def test_comment_corners_cannot_hide_code(src, body):
    top(src, body + "\n  assign uo_out = 8'd0;")
    one(src, "`initial` is not allowed")


CORNERS = ("module tt_um_fesens_r32v(input wire clk, output wire [7:0] uo_out);\n"
           "  /* a /* b */ wire x1 = clk;\n  /* c // d */ wire x2 = clk;\n  // e /* f\n"
           "  wire x3 = clk;\n  // g */ h\n  localparam S1 = \"a`b\";\n"
           "  localparam S2 = \"c*/d\";\n  localparam S3 = \"e/*f\";\n"
           "  localparam S4 = \"g//h\";\n  localparam S5 = \"q\\\"r\";\n"
           "  assign uo_out = {x1, x2, x3, 5'd0};\nendmodule\n")


@needs_tools
def test_tokenizer_matches_all_three_preprocessors_on_corners(src, tmp_path):
    # Static rules reject the strings; what matters here is that src_check's tokenizer reads the
    # comment and string corners exactly as Verilator, yosys and Icarus do.
    top(src, raw=CORNERS)
    files = [str(src / "tt_um_fesens_r32v.v")]
    env = src_check.tool_env()
    mine = src_check.normalized(CORNERS)
    for text, err in (src_check.verilator_pp(files, False, env),
                      src_check.yosys_ppdump(files, False, env, str(tmp_path)),
                      src_check.iverilog_pp(files, False, env, str(tmp_path))):
        assert err is None, err
        assert src_check.normalized(text) == mine
    assert src_check.check_differential(str(src)) == []


@needs_tools
def test_differential_catches_icarus_macro_inside_escaped_identifier(src):
    # Icarus expands `__ICARUS__ inside an escaped identifier (\r`__ICARUS__ becomes \r1); the
    # third stream sees it even with the static rules bypassed.
    top(src, "  wire \\r`__ICARUS__ = 1'b1;\n  assign uo_out = {7'd0, \\r`__ICARUS__ };")
    errs = src_check.check_differential(str(src))
    assert any("verilator and iverilog preprocess src/ differently" in e for e in errs), errs


@needs_tools
def test_differential_reports_icarus_warnings(src):
    top(src, "  wire \\a`ifdef__ICARUS__ = 1'b1;\n  assign uo_out = {7'd0, \\a`ifdef__ICARUS__ };")
    errs = src_check.check_differential(str(src))
    assert any("preprocessor failed: iverilog" in e for e in errs), errs


IFDEF_IN_GENERATE = (
    "module tt_um_fesens_r32v(input wire clk, output wire [7:0] uo_out\n`ifdef RISCV_FORMAL\n"
    "  , output wire rvfi_valid\n`endif\n);\n  genvar i;\n  wire [7:0] q;\n"
    "  generate\n    for (i = 0; i < 8; i = i + 1) begin : g_bit\n"
    "      reg r;\n      always @(posedge clk) r <= ~r;\n      assign q[i] = r;\n"
    "`ifdef RISCV_FORMAL\n      wire seen = r;\n`endif\n    end\n  endgenerate\n"
    "`ifdef RISCV_FORMAL\n  assign rvfi_valid = q[0];\n`endif\n  assign uo_out = q;\nendmodule\n")
IFDEF_AROUND_HEADER = (
    "`ifdef RISCV_FORMAL\nmodule tt_um_fesens_r32v(input wire clk, output wire [7:0] uo_out,\n"
    "    output wire rvfi_valid);\n  assign rvfi_valid = clk;\n`else\n"
    "module tt_um_fesens_r32v(input wire clk, output wire [7:0] uo_out);\n`endif\n"
    "  assign uo_out = 8'd0;\nendmodule\n")


@pytest.mark.parametrize("text", [IFDEF_IN_GENERATE, IFDEF_AROUND_HEADER],
                         ids=["inside_generate", "around_module_header"])
def test_riscv_formal_blocks_in_generate_and_around_header_pass_static(src, text):
    top(src, raw=text)
    assert problems(src) == []


@needs_tools
@pytest.mark.parametrize("text", [IFDEF_IN_GENERATE, IFDEF_AROUND_HEADER],
                         ids=["inside_generate", "around_module_header"])
def test_riscv_formal_blocks_in_generate_and_around_header_pass_differential(src, text):
    top(src, raw=text)
    assert src_check.check_differential(str(src)) == []


# --- functions: no read before a definite assignment (review round 3) --------------------------

FN_TOP = ("module tt_um_fesens_r32v(input wire clk, input wire [7:0] ui_in, "
          "output wire [7:0] uo_out);\n{fn}\n  assign uo_out = f(ui_in);\nendmodule\n")
FN_FAIL = {
    "uninitialized": "reg [7:0] t; begin f = a ^ t; end",
    "stale_between_calls": "reg [7:0] t; begin f = t; t = a; end",
    "if_without_else": "reg [7:0] t; begin if (a[0]) t = 1; f = t; end",
    "case_without_default": "reg [7:0] t; begin case (a[1:0]) 2'd0: t = 1; 2'd1: t = 2; "
                            "endcase f = t; end",
    "own_result": "begin f = f + a; end",
    "partial_then_read": "reg [7:0] t; begin t[0] = a[0]; f = t; end",
    "loop_carried": "reg [7:0] t; integer i; begin f = 0; for (i = 0; i < 2; i = i + 1) "
                    "begin f = t; t = a; end end",
    "index_variable": "integer i; begin f = a[i]; end",
    "automatic": "reg [7:0] t; begin f = t; end",
    "else_branch_only": "reg [7:0] t; begin if (a[0]) f = 0; else t = 1; f = t; end",
}
FN_PASS = {
    "if_else": "reg [7:0] t; begin if (a[0]) t = 1; else t = 2; f = t; end",
    "case_with_default": "reg [7:0] t; begin case (a[1:0]) 2'd0: t = 1; 2'd1: t = 2; "
                         "default: t = 3; endcase f = t; end",
    "accumulate": "integer i; begin f = 0; for (i = 0; i < 8; i = i + 1) f = f + a[i]; end",
    "bit_reverse": "integer k; begin for (k = 0; k < 8; k = k + 1) f[k] = a[7-k]; end",
    "concat_target": "reg [3:0] h, l; begin {h, l} = a; f = {l, h}; end",
    "named_block": "begin : blk reg [7:0] t; t = a; f = t; end",
    "nested_if": "reg [7:0] t; begin if (a[0]) begin if (a[1]) t = 1; else t = 2; end "
                 "else t = 3; f = t; end",
    "while_loop": "reg [7:0] t; begin t = a; while (t > 8'd3) t = t - 8'd3; f = t; end",
}


def fn_src(src, name, body):
    head = ("function automatic [7:0] f; input [7:0] a;" if name == "automatic"
            else "function [7:0] f; input [7:0] a;")
    top(src, raw=FN_TOP.format(fn=f"  {head} {body} endfunction"))


@pytest.mark.parametrize("name", sorted(FN_FAIL))
def test_function_reads_before_definite_assignment_fail(src, name):
    fn_src(src, name, FN_FAIL[name])
    one(src, "before assigning it on every path")


@pytest.mark.parametrize("name", sorted(FN_PASS))
def test_function_definite_assignment_patterns_pass(src, name):
    fn_src(src, name, FN_PASS[name])
    assert problems(src) == []


def test_function_ansi_header_and_message(src):
    top(src, raw=FN_TOP.format(fn="  function [7:0] f(input [7:0] a); reg [7:0] t;\n"
                                  "    begin f = t; t = a; end\n  endfunction"))
    assert problems(src) == ["src/tt_um_fesens_r32v.v:3: function f reads t before assigning "
                             "it on every path (an uninitialized or stale value; contract "
                             "section 1)"]


def test_function_body_the_walker_cannot_follow_fails(src):
    fn_src(src, "x", "begin f = a; f; end")
    one(src, "src_check cannot follow its body")


SWAP_N = """module tt_um_fesens_r32v;
    function [31:0] swap_n;
        input [1:0]  size;   // funct3[1:0]: 00 byte, 01 half, 10 word
        input [31:0] x;
        begin
            case (size)
                2'b00:   swap_n = {24'd0, x[7:0]};
                2'b01:   swap_n = {16'd0, x[7:0], x[15:8]};
                default: swap_n = {x[7:0], x[15:8], x[23:16], x[31:24]};
            endcase
        end
    endfunction
endmodule
"""


def test_seed_function_passes():
    # swap_n from the contract v2 seed (main 11890e5, src/tt_um_fesens_r32v.v; the v3 seed has no
    # function): a case with default assigning the result on every branch.
    toks, _ = src_check.tokenize(SWAP_N)
    assert any(t[1] == "function" for t in toks)
    assert src_check.check_functions("tt_um_fesens_r32v.v", toks) == []


def test_src_functions_pass():
    # whatever functions the design in src/ has (none, in the v3 seed) pass the rule
    for f in sorted(glob.glob(os.path.join(SEED_SRC, "*.v"))):
        toks, _ = src_check.tokenize(open(f).read())
        assert src_check.check_functions(os.path.basename(f), toks) == [], f


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
