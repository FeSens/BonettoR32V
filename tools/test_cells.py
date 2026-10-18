"""Tests for sky130 cell instances in src/ (contract section 1): src_check rule 13, the behavioral
models in verif/cells (inventory and drift against the PDK's Verilog), the gates that read them
(lint, equiv) and tools/area.py's accounting of an instance's liberty area.

The cosim mutants (wrong select polarity, reliance on a cell flop's power-up value) are in
verif/cosim/test_cosim_harness.py, next to the harness's other mutants.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import area  # noqa: E402
import cell_drift  # noqa: E402
import src_check  # noqa: E402

CELLS = os.path.join(REPO, "verif", "cells")
SDF = "sky130_fd_sc_hd__sdfxtp_1"
TOP = """`default_nettype none
module tt_um_fesens_r32v (
    input  wire [7:0] ui_in,  output wire [7:0] uo_out,
    input  wire [7:0] uio_in, output wire [7:0] uio_out, output wire [7:0] uio_oe,
    input  wire ena, input wire clk, input wire rst_n
);
{body}
    assign uio_out = 8'd0;
    assign uio_oe = 8'd0;
endmodule
`default_nettype wire
"""
# An 8-bit register of scan flops used as enable flops: SCE = load, SCD = new data, D = Q.
SDF_REG = """    wire [7:0] q;
    wire       ld = ~rst_n | ui_in[0];
    wire [7:0] d  = rst_n ? uio_in : 8'd0;
    genvar i;
    generate
        for (i = 0; i < 8; i = i + 1) begin : g_q
            sky130_fd_sc_hd__sdfxtp_1 u_ff (.CLK(clk), .D(q[i]), .SCD(d[i]), .SCE(ld), .Q(q[i]));
        end
    endgenerate
    assign uo_out = q;
    wire _unused = &{ui_in[7:1], ena, 1'b0};"""
HAVE_PP = all(shutil.which(t, path=src_check.tool_env()["PATH"])
              for t in src_check.PREPROCESSORS)
needs_pp = pytest.mark.skipif(not HAVE_PP, reason="verilator, yosys and iverilog needed")
PDK_V = cell_drift.pdk_verilog_dir()
HAVE_DRIFT = (shutil.which("iverilog", path=src_check.tool_env()["PATH"]) is not None
              and os.path.isfile(os.path.join(PDK_V, "sky130_fd_sc_hd.v")))
needs_drift = pytest.mark.skipif(not HAVE_DRIFT, reason="iverilog and the pinned PDK needed")


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    return d


def design(src, body):
    (src / "tt_um_fesens_r32v.v").write_text(TOP.format(body=body))


def inst(cell, conns, name="u0"):
    return f"    {cell} {name} ({conns});"


def full_conns(cell, sig="ui_in[0]", out="o"):
    outs, ins = src_check.CELL_WHITELIST[cell]
    return ", ".join([f".{p}({sig})" for p in ins] + [f".{p}({out})" for p in outs])


def errs(src):
    return src_check.check(str(src))


def one(src, needle):
    e = errs(src)
    assert any(needle in x for x in e), e
    return e


# --- the whitelist and rule 13 -------------------------------------------------------------------

def test_whitelist_has_the_cells_that_matter():
    for c in ("sky130_fd_sc_hd__sdfxtp_1", "sky130_fd_sc_hd__edfxtp_1",
              "sky130_fd_sc_hd__dfxtp_1", "sky130_fd_sc_hd__mux2_1"):
        assert c in src_check.CELL_WHITELIST
    for cell, (outs, ins) in src_check.CELL_WHITELIST.items():
        assert cell.startswith("sky130_fd_sc_hd__") and outs and ins
        assert not set(outs + ins) & set(src_check.POWER_PINS)


def test_whitelist_avoids_the_pdk_exclusion_lists():
    """No whitelisted cell is on drc_exclude.cells (PnR's dont_use list: cells with DRC errors).
    The sdfxtp cells are on no_synth.cells (synthesis may not map logic onto them), which a
    direct instance does not need; the others are synthesizable with src/config.json."""
    sky130a = os.path.dirname(os.path.dirname(os.path.dirname(PDK_V)))
    cfg = os.path.join(sky130a, "libs.tech", "openlane", "sky130_fd_sc_hd")
    drc = os.path.join(cfg, "drc_exclude.cells")
    if not os.path.isfile(drc):
        pytest.skip("pinned PDK not installed")
    import fnmatch
    for name in ("drc_exclude.cells",):
        with open(os.path.join(cfg, name)) as f:
            pats = [ln.split("#")[0].strip() for ln in f if ln.split("#")[0].strip()]
        hit = [c for c in src_check.CELL_WHITELIST if any(fnmatch.fnmatchcase(c, p)
                                                           for p in pats)]
        assert hit == [], (name, hit)
    with open(os.path.join(cfg, "no_synth.cells")) as f:
        nosynth = {ln.strip() for ln in f}
    assert {c for c in src_check.CELL_WHITELIST if c in nosynth} == {
        "sky130_fd_sc_hd__dfxtp_1", "sky130_fd_sc_hd__edfxtp_1", "sky130_fd_sc_hd__sdfxtp_1",
        "sky130_fd_sc_hd__sdfxtp_2"}


@pytest.mark.parametrize("cell", sorted(src_check.CELL_WHITELIST))
def test_each_whitelisted_cell_with_named_ports_passes(src, cell):
    design(src, "    wire o;\n" + inst(cell, full_conns(cell)) + "\n    assign uo_out = {7'd0, o};")
    assert errs(src) == []


def test_generate_loop_of_scan_flops_passes(src):
    design(src, SDF_REG)
    assert errs(src) == []


def test_instance_in_rvfi_build_only_passes(src):
    design(src, "`ifdef RISCV_FORMAL\n    wire o;\n" + inst(SDF, full_conns(SDF))
           + "\n`endif\n    assign uo_out = 8'd0;")
    assert errs(src) == []


def test_cell_name_in_a_comment_is_fine(src):
    design(src, "    // sky130_fd_sc_hd__conb_1 would tie this off\n    assign uo_out = 8'd0;")
    assert errs(src) == []


@pytest.mark.parametrize("cell", ["sky130_fd_sc_hd__dfxtp_4", "sky130_fd_sc_hd__conb_1",
                                  "sky130_fd_sc_hd__dlxtp_1", "sky130_fd_sc_hs__dfxtp_1",
                                  "sky130_ef_sc_hd__decap_12", "SKY130_fd_sc_hd__dfxtp_1",
                                  "sky130_fd_sc_hd__sdfxtp_4", "sky130_fd_sc_hd__mux2i_1"])
def test_non_whitelisted_cell_fails(src, cell):
    design(src, "    wire o;\n" + inst(cell, ".CLK(clk), .D(ui_in[0]), .Q(o)")
           + "\n    assign uo_out = {7'd0, o};")
    one(src, f"cell {cell} is not in the cell whitelist")


def test_non_whitelisted_cell_in_rvfi_build_only_fails(src):
    design(src, "`ifdef RISCV_FORMAL\n    wire o;\n"
           + inst("sky130_fd_sc_hd__dfxtp_4", ".CLK(clk), .D(ui_in[0]), .Q(o)")
           + "\n`endif\n    assign uo_out = 8'd0;")
    one(src, "cell sky130_fd_sc_hd__dfxtp_4 is not in the cell whitelist")


def test_positional_ports_fail(src):
    design(src, "    wire o;\n" + inst(SDF, "o, clk, ui_in[0], ui_in[1], ui_in[2]")
           + "\n    assign uo_out = {7'd0, o};")
    one(src, "positional connections are not allowed")


def test_mixed_positional_and_named_ports_fail(src):
    design(src, "    wire o;\n" + inst(SDF, ".Q(o), clk, .D(ui_in[0]), .SCD(ui_in[1]), "
                                         ".SCE(ui_in[2])") + "\n    assign uo_out = {7'd0, o};")
    one(src, "positional connections are not allowed")


def test_parameters_fail(src):
    design(src, f"    wire o;\n    {SDF} #(.W(1)) u0 ({full_conns(SDF)});\n"
                "    assign uo_out = {7'd0, o};")
    one(src, f"an instance of {SDF} takes no parameters")


@pytest.mark.parametrize("pin", src_check.POWER_PINS)
def test_power_pins_fail(src, pin):
    design(src, "    wire o;\n" + inst(SDF, full_conns(SDF) + f", .{pin}(1'b1)")
           + "\n    assign uo_out = {7'd0, o};")
    one(src, f"power pin {pin} is not allowed in RTL")


def test_unknown_port_fails(src):
    design(src, "    wire o;\n" + inst(SDF, ".CLK(clk), .D(ui_in[0]), .SCD(ui_in[1]), "
                                         ".SE(ui_in[2]), .SCE(ui_in[3]), .Q(o)")
           + "\n    assign uo_out = {7'd0, o};")
    one(src, f"the cell has no port SE")


def test_duplicate_port_fails(src):
    design(src, "    wire o;\n" + inst(SDF, full_conns(SDF) + ", .D(ui_in[5])")
           + "\n    assign uo_out = {7'd0, o};")
    one(src, "port D connected twice")


def test_missing_port_fails(src):
    design(src, "    wire o;\n" + inst(SDF, ".CLK(clk), .D(ui_in[0]), .SCD(ui_in[1]), .Q(o)")
           + "\n    assign uo_out = {7'd0, o};")
    one(src, "every port must be connected, missing SCE")


def test_empty_connection_fails(src):
    design(src, inst(SDF, ".CLK(clk), .D(ui_in[0]), .SCD(ui_in[1]), .SCE(ui_in[2]), .Q()")
           + "\n    assign uo_out = 8'd0;")
    one(src, "port Q is left unconnected")


def test_wildcard_and_implicit_ports_fail(src):
    design(src, "    wire Q, CLK, D, SCD, SCE;\n" + inst(SDF, ".*") + "\n    assign uo_out = 8'd0;")
    one(src, "malformed connection")
    design(src, "    wire Q, CLK, D, SCD, SCE;\n" + inst(SDF, ".Q, .CLK, .D, .SCD, .SCE")
           + "\n    assign uo_out = 8'd0;")
    one(src, "malformed connection")


def test_instance_array_fails(src):
    design(src, f"    wire [1:0] o;\n    {SDF} u0 [1:0] (.CLK(clk), .D(ui_in[1:0]), "
                ".SCD(ui_in[3:2]), .SCE(ui_in[4]), .Q(o));\n    assign uo_out = {6'd0, o};")
    one(src, "instance arrays are not allowed")


def test_two_instances_in_one_statement_fail(src):
    design(src, f"    wire o, p;\n    {SDF} u0 ({full_conns(SDF)}),\n"
                f"        u1 ({full_conns(SDF, out='p')});\n    assign uo_out = {{6'd0, o, p}};")
    one(src, "one instance per statement")


def test_cell_name_used_as_a_net_fails(src):
    design(src, f"    wire {SDF};\n    assign {SDF} = ui_in[0];\n    assign uo_out = 8'd0;")
    one(src, f"{SDF} may only be the module type of an instance")


@pytest.mark.parametrize("cell", [SDF, "sky130_fd_sc_hd__mux2_1", "sky130_fd_sc_hd__conb_1"])
def test_src_module_named_like_a_cell_fails(src, cell):
    """No shadowing: src/ never defines a module with a cell's name (rule 6)."""
    design(src, "    assign uo_out = 8'd0;")
    (src / "cells.v").write_text(f"module {cell} (output wire Q, input wire CLK, input wire D,"
                                 f" input wire SCD, input wire SCE);\n"
                                 f"    assign Q = D;\nendmodule\n")
    one(src, f"module name '{cell}' must be tt_um_fesens_r32v or start with r32v_")


# --- the models: inventory and drift against the PDK ---------------------------------------------

def test_cell_model_files_follow_the_whitelist():
    files = src_check.cell_model_files()
    assert [os.path.basename(f) for f in files] == sorted(c + ".v"
                                                          for c in src_check.CELL_WHITELIST)
    assert all(os.path.dirname(f) == CELLS for f in files)


def test_models_inventory_is_exact():
    assert cell_drift.inventory(CELLS) == []


def copy_models(tmp_path, edits=()):
    dst = tmp_path / "cells"
    shutil.copytree(CELLS, dst)
    for name, old, new in edits:
        p = dst / name
        text = p.read_text()
        assert text.count(old) == 1, (name, old)
        p.write_text(text.replace(old, new))
    return dst


def test_inventory_catches_extra_missing_and_wrong_models(tmp_path):
    d = copy_models(tmp_path)
    (d / "sky130_fd_sc_hd__dfxtp_4.v").write_text("module sky130_fd_sc_hd__dfxtp_4; endmodule\n")
    os.remove(d / "sky130_fd_sc_hd__mux2_2.v")
    e = cell_drift.inventory(str(d))
    assert any("dfxtp_4.v: not the model of a whitelisted cell" in x for x in e), e
    assert any("mux2_2.v: model missing" in x for x in e), e
    d = copy_models(tmp_path / "b", [(SDF + ".v", "input  wire SCE", "input  wire SE")])
    assert any("differ from the whitelist" in x for x in cell_drift.inventory(str(d)))
    d = copy_models(tmp_path / "c", [(SDF + ".v", "endmodule",
                                      "    initial q = 1'b0;\nendmodule")])
    assert any("`initial`" in x for x in cell_drift.inventory(str(d)))


@needs_drift
def test_models_match_the_pdk():
    assert cell_drift.drift(CELLS, PDK_V) == []


@needs_drift
@pytest.mark.parametrize("name,old,new,needle", [
    (SDF + ".v", "SCE ? SCD : D", "SCE ? D : SCD", SDF),                    # SCE polarity
    ("sky130_fd_sc_hd__sdfxtp_2.v", "SCE ? SCD : D", "~SCE ? SCD : D", "sdfxtp_2"),
    ("sky130_fd_sc_hd__edfxtp_1.v", "DE ? D : q", "DE ? q : D", "edfxtp_1"),   # DE polarity
    ("sky130_fd_sc_hd__mux2_1.v", "X = S ? A1 : A0;", "X = S ? A0 : A1;", "mux2_1"),
    ("sky130_fd_sc_hd__dfxtp_1.v", "posedge CLK", "negedge CLK", "dfxtp_1"),
    # a power-up value (the PDK flop starts X) and an X-hiding select
    ("sky130_fd_sc_hd__dfxtp_2.v", "endmodule", "    initial q = 1'b0;\nendmodule", "dfxtp_2"),
    ("sky130_fd_sc_hd__mux2_2.v", "X = S ? A1 : A0;", "X = (S === 1'b1) ? A1 : A0;", "mux2_2"),
])
def test_drift_catches_a_wrong_model(tmp_path, name, old, new, needle):
    d = copy_models(tmp_path, [(name, old, new)])
    e = cell_drift.drift(str(d), PDK_V)
    assert e and all(needle in x for x in e), e


# --- the gates read the models ------------------------------------------------------------------

def gate_repo(tmp_path, body):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    design(r / "src", body)
    os.symlink(HERE, r / "tools")
    return r


def run_gate(repo, gate):
    import eval as ev
    rc = ev.main(["--repo", str(repo), "--gate", gate])
    doc = json.loads((repo / "build" / "gates" / f"{gate}.json").read_text())
    assert doc["pass"] is (rc == 0)
    return doc


@needs_pp
def test_lint_and_equiv_pass_a_design_with_scan_flops(tmp_path):
    repo = gate_repo(tmp_path, SDF_REG)
    lint = run_gate(repo, "lint")
    assert lint["pass"] is True, lint["detail"]
    assert f"{len(src_check.CELL_WHITELIST)} cell models" in lint["detail"]
    eq = run_gate(repo, "equiv")
    assert eq["pass"] is True, eq["detail"]
    j = json.loads((repo / "build" / "equiv.json").read_text())
    assert j["cell_models"] == sorted(c + ".v" for c in src_check.CELL_WHITELIST)
    assert j["src_files"] == ["tt_um_fesens_r32v.v"]


@needs_pp
def test_lint_stops_a_non_whitelisted_cell_at_src_check(tmp_path):
    repo = gate_repo(tmp_path, SDF_REG.replace(SDF, "sky130_fd_sc_hd__sdfxtp_4"))
    doc = run_gate(repo, "lint")
    assert doc["pass"] is False
    assert "is not in the cell whitelist" in doc["detail"], doc["detail"]


@needs_pp
def test_equiv_proves_cell_flops(tmp_path):
    """An RVFI-only scan flop that alters a pin is caught: the models are flattened into both
    builds, so cell state is matched and proven like any register."""
    body = SDF_REG.replace("    assign uo_out = q;",
                           "`ifdef RISCV_FORMAL\n    wire t;\n"
                           f"    {SDF} u_t (.CLK(clk), .D(t), .SCD(~t), .SCE(rst_n), .Q(t));\n"
                           "    assign uo_out = q ^ {7'd0, t};\n`else\n"
                           "    assign uo_out = q;\n`endif")
    repo = gate_repo(tmp_path, body)
    assert run_gate(repo, "lint")["pass"] is True
    eq = run_gate(repo, "equiv")
    assert eq["pass"] is False and "uo_out[0]" in eq["detail"], eq["detail"]


# --- area: an instance keeps its liberty area -------------------------------------------------------

LIBCELLS = {"sky130_fd_sc_hd__inv_1": {"area": 3.7536, "kind": "comb"},
            "sky130_fd_sc_hd__dfxtp_1": {"area": 20.0192, "kind": "flop"},
            SDF: {"area": 26.2752, "kind": "flop"},
            "sky130_fd_sc_hd__dlxtp_1": {"area": 15.0144, "kind": "latch"},
            "sky130_fd_sc_hd__sdfxtp_4": {"area": 33.7824, "kind": "flop"}}


def stat_dir(tmp_path, cells, stat_area):
    d = tmp_path / "step"
    (d / "reports").mkdir(parents=True)
    (d / "reports" / "stat.json").write_text(json.dumps({"design": {
        "num_cells": sum(cells.values()), "num_cells_by_type": cells, "area": stat_area}}))
    return d


def test_area_adds_the_liberty_area_yosys_lacks(tmp_path):
    synth = {"sky130_fd_sc_hd__inv_1", "sky130_fd_sc_hd__dfxtp_1"}   # no sdfxtp: no_synth.cells
    cells = {"sky130_fd_sc_hd__inv_1": 3, "sky130_fd_sc_hd__dfxtp_1": 2, SDF: 16}
    stat_area = 3 * 3.7536 + 2 * 20.0192            # what yosys stat reports
    res, _ = area.summarize(stat_dir(tmp_path, cells, stat_area), "t", LIBCELLS, synth)
    assert res["area_um2"] == round(stat_area + 16 * 26.2752, 4)
    assert res["instanced_cells"] == {SDF: {"count": 16, "area_um2": 420.4032}}
    assert res["by_type"][SDF] == {"count": 16, "area_um2": 420.4032}
    assert res["flops"] == 18


def test_area_without_instances_is_stat_area_exactly(tmp_path):
    synth = set(LIBCELLS)
    cells = {"sky130_fd_sc_hd__inv_1": 3, SDF: 1}
    res, _ = area.summarize(stat_dir(tmp_path, cells, 37.5360), "t", LIBCELLS, synth)
    assert res["area_um2"] == 37.536 and res["instanced_cells"] == {}


def summarize_dies(step, synth):
    """area.summarize in a child process (it exits 2 through die()); returns its stderr."""
    code = ("import sys, json; sys.path.insert(0, %r); import area; from pathlib import Path\n"
            "area.summarize(Path(%r), 't', json.loads(%r), set(json.loads(%r)))"
            % (HERE, str(step), json.dumps(LIBCELLS), json.dumps(sorted(synth))))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    return r.stderr


def test_area_dies_on_a_non_whitelisted_cell_missing_from_the_synth_liberty(tmp_path):
    cells = {"sky130_fd_sc_hd__inv_1": 1, "sky130_fd_sc_hd__sdfxtp_4": 1}
    e = summarize_dies(stat_dir(tmp_path, cells, 3.7536), {"sky130_fd_sc_hd__inv_1"})
    assert "neither in the synthesis liberty nor in the cell whitelist" in e, e


def test_area_dies_when_stat_disagrees_with_the_synth_liberty(tmp_path):
    cells = {"sky130_fd_sc_hd__inv_1": 2}
    e = summarize_dies(stat_dir(tmp_path, cells, 3.7536), {"sky130_fd_sc_hd__inv_1"})
    assert "stat area" in e, e


def test_by_module_adds_instanced_area_through_the_hierarchy():
    """Submodule types appear as `sub` (module `\\sub`) or `$paramod\\...` (the same key)."""
    pm = "$paramod\\r32v_sff\\W=s32'00000000000000000000000000000010"
    stat = {"modules": {
        "\\top": {"num_cells_by_type": {"sky130_fd_sc_hd__inv_1": 1, "sub": 2, pm: 1},
                  "area": 3.7536 + 2 * 20.0192},
        "\\sub": {"num_cells_by_type": {"sky130_fd_sc_hd__dfxtp_1": 1, SDF: 1},
                  "area": 20.0192},
        pm: {"num_cells_by_type": {SDF: 2}}}}
    synth = {"sky130_fd_sc_hd__inv_1", "sky130_fd_sc_hd__dfxtp_1"}
    mods = area.by_module(stat, LIBCELLS, synth)
    assert mods["sub"]["area_um2"] == round(20.0192 + 26.2752, 4)
    assert mods["sub"]["self_area_um2"] == round(20.0192 + 26.2752, 4)
    assert mods["r32v_sff"]["area_um2"] == round(2 * 26.2752, 4)
    assert mods["top"]["area_um2"] == round(3.7536 + 2 * (20.0192 + 26.2752) + 2 * 26.2752, 4)


def docker_up():
    try:
        return subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


AREA_PDK = Path(os.environ.get("PDK_ROOT", str(Path.home() / ".ciel"))) / "ciel" / "sky130" / \
    "versions" / area.SKY130_PDK_HASH / "sky130A"


@pytest.mark.skipif(not (AREA_PDK / "libs.ref").is_dir() or not docker_up(),
                    reason="needs the pinned PDK and Docker (or the hwe-bench shim)")
def test_area_librelane_keeps_the_instance_as_the_liberty_cell(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    design(d, SDF_REG)
    out = tmp_path / "area.json"
    r = subprocess.run([sys.executable, os.path.join(HERE, "area.py"), "--top",
                        "tt_um_fesens_r32v", "--src", str(d), "--out", str(out),
                        "--synth-dir", str(tmp_path / "synth")],
                       capture_output=True, text=True, timeout=1800)
    assert r.returncode == 0, r.stdout + r.stderr
    j = json.loads(out.read_text())
    lib = area.parse_liberty(area.liberty_path(AREA_PDK.parent))
    a = round(8 * lib[SDF]["area"], 4)
    assert j["instanced_cells"] == {SDF: {"count": 8, "area_um2": a}}
    assert j["by_type"][SDF] == {"count": 8, "area_um2": a}
    assert j["flops"] == 8
    assert j["by_module_total_um2"] == j["area_um2"]
    netlist = (tmp_path / "synth" / "netlist.v").read_text()
    assert netlist.count(f"{SDF} ") == 8          # the cell, not a model's flattened logic
