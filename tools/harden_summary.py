#!/usr/bin/env python3
"""BonettoR32V: classify one tools/harden_tt.sh run and write OUT/summary.json.

Called by tools/harden_tt.sh; kept as its own file so tools/test_physical.py can run it on
fixture run directories.
Arguments: OUT PROJ TOP LL_RC SUB_RC PRE_RC T_LIBRELANE T_PRECHECK LIB [TIMED_OUT]
  LL_RC      LibreLane exit code
  SUB_RC     tt_tool --create-tt-submission exit code (-1: not attempted)
  PRE_RC     precheck exit code (-1: not attempted, -2: --no-precheck)
  LIB        the tt corner liberty file (cell areas for the timing-repair count)
  TIMED_OUT  "", "librelane" or "precheck": the stage harden_tt.sh's --timeout stopped
summary.json "status" is one of
  pass        every check holds
  fail        evidence that the design does not sign off: a signoff metric violation, a
              precheck row that reports a violation, or a design-caused flow error
  tool_error  anything else that went wrong (the tools, not the design)
  timeout     a stage hit --timeout and there is no evidence of a design failure
harden_tt.sh maps them to exit 0, 1, 2 and 3 by reading the file back, so a crash here reads
as a tool error.
"""
import glob, json, re, sys
import xml.etree.ElementTree as ET
from pathlib import Path
out, proj, top = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
ll_rc, sub_rc, pre_rc, t_ll, t_pre = (int(x) for x in sys.argv[4:9])
lib = Path(sys.argv[9])
timed_out = sys.argv[10] if len(sys.argv) > 10 else ""
run = proj / "runs" / "wokwi"
final = run / "final" / "metrics.json"
flow_finished = final.exists()   # all steps ran (deferred checker errors still write final/)
steps = sorted(p.name for p in run.iterdir() if p.is_dir() and re.match(r"^\d+-", p.name)) \
    if run.exists() else []
m = {}
if flow_finished:
    m = json.loads(final.read_text())
elif steps:  # informational only: metrics of the last finished step
    states = sorted(glob.glob(str(run / "*" / "state_out.json")))
    if states:
        m = json.loads(Path(states[-1]).read_text()).get("metrics", {})

def g(k):
    v = m.get(k)
    if isinstance(v, str) and re.match(r"^-?[0-9.eE+-]+$", v):
        v = float(v)
    return v

def count(k):
    v = g(k)
    return None if v is None else int(v)

def read(path):
    return path.read_text(errors="replace") if path.exists() else ""

# --- ERROR-level messages only: LibreLane's per-level error.log/critical.log in the run dir
# (flow.py:661) plus ERROR/CRITICAL entries of the console log, so that design identifiers in
# tool output (yosys, OpenROAD info lines) can never look like evidence.
def console_errors(text):
    entries, cur = [], None
    for line in text.splitlines():
        mm = re.match(r"^(?:\[\d\d:\d\d:\d\d\]| {10})\s+(DEBUG|VERBOSE|INFO|WARNING|ERROR|"
                      r"CRITICAL|SUBPROCESS)\s+(.*)$", line)
        if mm:
            if cur is not None:
                entries.append(cur)
            cur = re.sub(r"\s+\S+\.py:\d+\s*$", "", mm.group(2)) \
                if mm.group(1) in ("ERROR", "CRITICAL") else None
        elif cur is not None and line.startswith(" " * 10):
            cur += " " + line.strip()
        else:
            if cur is not None:
                entries.append(cur)
            cur = None
    if cur is not None:
        entries.append(cur)
    return "\n".join(entries)

errlog = read(run / "error.log") + "\n" + read(run / "critical.log")
error_text = errlog + "\n" + console_errors(read(out / "librelane.log"))

# --- signoff metrics (from final/ only)
fin = (lambda v: v) if flow_finished else (lambda v: None)
magic = fin(count("magic__drc_error__count"))
route_drc = fin(count("route__drc_errors"))
lvs = fin(count("design__lvs_error__count"))
ant_nets = fin(count("antenna__violating__nets"))
ant_pins = fin(count("antenna__violating__pins"))
setup_vio = fin(count("timing__setup_vio__count"))
hold_vio = fin(count("timing__hold_vio__count"))
setup_ws = fin(g("timing__setup__ws"))
hold_ws = fin(g("timing__hold__ws"))
pgrid = fin(count("design__power_grid_violation__count"))
disc = fin(count("design__critical_disconnected_pin__count"))
overlap = fin(count("magic__illegal_overlap__count"))
signoff = {  # name: (metric values, violation?)
    "magic_drc": ([magic], magic is not None and magic > 0),
    "route_drc": ([route_drc], route_drc is not None and route_drc > 0),
    "lvs": ([lvs], lvs is not None and lvs > 0),
    "antenna": ([ant_nets, ant_pins], any(v is not None and v > 0 for v in (ant_nets, ant_pins))),
    "setup_all_corners": ([setup_vio, setup_ws], (setup_vio or 0) > 0 or
                          (setup_ws is not None and setup_ws < 0)),
    "hold_all_corners": ([hold_vio, hold_ws], (hold_vio or 0) > 0 or
                         (hold_ws is not None and hold_ws < 0)),
    "power_grid": ([pgrid], pgrid is not None and pgrid > 0),
    "disconnected_pins": ([disc], disc is not None and disc > 0),
    "illegal_overlap": ([overlap], overlap is not None and overlap > 0),
}

# --- precheck results. A failed row is design evidence only when it shows a violation: TT's
# precheck.py turns ANY exception into "Fail: <message>" (precheck.py:580-583), and some
# messages are the same for a violation and a dead tool.
precheck = {"ran": pre_rc >= 0, "skipped": pre_rc == -2, "exit_code": pre_rc}
reports = proj / "tt" / "precheck" / "reports"
res_md = reports / "results.md"
rows = {}
if pre_rc >= 0 and res_md.exists():
    for line in res_md.read_text().splitlines():
        mm = re.match(r"^\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|$", line)
        if mm and mm.group(1) not in ("Check", "-----------"):
            rows[mm.group(1)] = "pass" if "\u2705" in mm.group(2) else mm.group(2)
    precheck["checks"] = rows

def xml_errors(path):
    """{check name: (message, exception type)} from precheck's results.xml."""
    errs = {}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    for tc in root.iter("testcase"):
        e = tc.find("error")
        if e is None:
            continue
        lines = [l for l in (e.text or "").strip().splitlines() if l.strip()]
        mm = re.match(r"^([A-Za-z_][\w.]*)(?::|$)", lines[-1]) if lines else None
        errs[tc.get("name")] = (e.get("message", ""), mm.group(1) if mm else "unknown")
    return errs

def runs_external_tool(name):  # checks whose failure can be a crashed magic/klayout/yosys
    return name in ("Magic DRC", "Verilog syntax check", "urpm/nwell check", "Antenna check") or \
        (name.startswith("KLayout ") and name != "KLayout Checks")

def magic_corroborated():
    hits = []
    mm = re.search(r"\[ERROR\]: (\d+) DRC errors found", read(out / "precheck.log"))
    if mm and int(mm.group(1)) > 0:
        hits.append(f"precheck.log: {mm.group(0)}")
    mm = re.search(r"\[INFO\]: COUNT: (\d+)", read(reports / "magic_drc.txt"))
    if mm and int(mm.group(1)) > 0:
        hits.append(f"magic_drc.txt: {mm.group(0)}")
    if magic is not None and magic > 0:
        hits.append(f"LibreLane magic__drc_error__count = {magic}")
    return hits

design_rows, tool_rows = {}, {}
failed = {k: v for k, v in rows.items() if v != "pass"}
xerrs = xml_errors(reports / "results.xml") if failed else {}
for name, text in failed.items():
    if xerrs is None or name not in xerrs:
        tool_rows[name] = f"{text} (no results.xml entry to confirm it)"
        continue
    msg, etype = xerrs[name]
    if etype.split(".")[-1] != "PrecheckFailure":
        tool_rows[name] = f"{etype}: {msg}"  # an exception, not a check verdict
        continue
    mm = re.search(r"failed with (\d+) DRC violations", msg)
    if mm and int(mm.group(1)) > 0:
        design_rows[name] = msg
    elif name == "Magic DRC" and msg == "Magic DRC failed":
        hits = magic_corroborated()
        if hits:
            design_rows[name] = f"{msg} ({'; '.join(hits)})"
        else:
            tool_rows[name] = f"{msg}, with no DRC count anywhere (magic did not run?)"
    elif not runs_external_tool(name) and "template DEF" not in msg:
        design_rows[name] = msg  # in-process GDS/LEF/netlist content checks
    else:
        tool_rows[name] = msg  # e.g. bare "Klayout feol failed": klayout returned non-zero
precheck["design_failures"] = design_rows
precheck["tool_failures"] = tool_rows
precheck["pass"] = pre_rc == 0 and bool(rows) and not failed
kl = {k: v for k, v in rows.items() if k.startswith("KLayout")}
klayout_ok = (bool(kl) and all(v == "pass" for v in kl.values())) if rows else None

checks = {"librelane_flow": flow_finished and ll_rc == 0}
for k, (vals, vio) in signoff.items():
    checks[k] = flow_finished and all(v is not None for v in vals) and not vio
checks["klayout_drc"] = klayout_ok is True
checks["precheck"] = precheck["pass"]
if precheck["skipped"]:  # --no-precheck: KLayout DRC comes only from the precheck
    checks["klayout_drc"] = None
    checks["precheck"] = None

# --- evidence that the DESIGN fails (exit 1) versus a tool error (exit 2)
evidence = [k for k, (vals, vio) in signoff.items() if vio]
evidence += [f"precheck: {k}: {v}" for k, v in design_rows.items()]
CHECKERS = ["Unmapped Yosys instances", "Yosys check errors", "Routing DRC errors",
            "Magic DRC errors", "Magic Illegal Overlap errors", "critical disconnected pins",
            "Threshold-surpassing long wires", "XOR differences", "LVS errors",
            "power grid violations", "Lint errors", "Lint warnings", "KLayout DRC errors",
            "KLayout density errors", "KLayout antenna errors"]  # steps/checker.py descriptions
patterns = [  # OpenROAD and LibreLane messages a design causes, matched in ERROR text only
    (r"Utilization [0-9.]+ % exceeds 100%", "global placement: utilization above 100 % (GPL-0301)"),
    (r"\[GPL-\d+\][^\n]*Divergence detected", "global placement diverged"),
    (r"Detailed placement failed", "detailed placement failed"),
    (r"Max buffer count reached", "resizer: max buffer count reached"),
    (r"Unable to repair all hold (?:violations|checks within margin)", "resizer: hold repair failed"),
    (r"\b\d+(?:\.\d+)? (?:" + "|".join(map(re.escape, CHECKERS)) + r")[^\n]*? found",
     "LibreLane checker"),
    (r"(?:Setup|Hold|Max Cap|Max Slew) violations found in the following corners",
     "LibreLane timing checker"),
    (r"Timing constructs found in the RTL", "LibreLane lint: timing constructs"),
    (r"assign statements? found in the netlist", "LibreLane: assign statements in the netlist"),
]
if ll_rc != 0:
    for rx, what in patterns:
        hit = re.search(rx, error_text)
        if hit:
            evidence.append(f"{what}: {hit.group(0)[:200]}")

timeout = None
if timed_out and not evidence:
    timeout = f"{timed_out} stopped by harden_tt.sh --timeout"
tool_error = None
if timeout:
    pass
elif ll_rc in (125, 126, 127) or ll_rc > 128:
    tool_error = f"docker/LibreLane did not run or was killed (exit {ll_rc})"
elif not steps:
    tool_error = f"LibreLane produced no flow steps (exit {ll_rc})"
elif ll_rc != 0 and not evidence:
    tool_error = (f"LibreLane exit {ll_rc} without evidence of a design failure; last step "
                  f"{steps[-1]}: {errlog.strip()[-600:] or 'see librelane.log'}")
elif ll_rc == 0 and not flow_finished:
    tool_error = "LibreLane exited 0 but wrote no final/metrics.json"
elif flow_finished and not evidence:
    missing = [k for k, (vals, vio) in signoff.items() if any(v is None for v in vals)]
    if missing:
        tool_error = f"signoff metrics missing from final/metrics.json: {missing}"
    elif ll_rc == 0 and sub_rc != 0:
        tool_error = f"tt_tool --create-tt-submission failed (exit {sub_rc})"
    elif pre_rc >= 0 and not rows:
        tool_error = f"precheck wrote no results (exit {pre_rc}); see precheck.log"
    elif tool_rows:
        tool_error = f"precheck checks failed without showing a violation: {tool_rows}"
    elif pre_rc > 0:
        tool_error = f"precheck exit {pre_rc} although every results row passed"

failure = None
if timeout:
    status = "timeout"
elif tool_error:
    status = "tool_error"
elif evidence:
    status = "fail"
    failure = "; ".join(evidence)
else:
    status = "pass" if all(v for v in checks.values() if v is not None) else "tool_error"
    if status == "tool_error":
        tool_error = f"no evidence of a violation but checks not all true: {checks}"

# --- timing repair: resizer buffers, final netlist versus the synthesized netlist
def cell_counts(path):
    c = {}
    if path and Path(path).exists():
        for mm in re.finditer(r"^\s*(sky130_fd_sc_hd__\w+)\s+\S+\s*\(", Path(path).read_text(), re.M):
            c[mm.group(1)] = c.get(mm.group(1), 0) + 1
    return c

def lib_areas(path):
    areas, cur = {}, None
    if path.exists():
        for line in open(path):
            mm = re.match(r'^\s*cell\s*\(\s*"?([A-Za-z0-9_]+)"?\s*\)', line)
            if mm:
                cur = mm.group(1)
                continue
            mm = re.match(r"^\s*area\s*:\s*([0-9.eE+-]+)", line)
            if mm and cur and cur not in areas:
                areas[cur] = float(mm.group(1))
    return areas

REPAIR = re.compile(r"^sky130_fd_sc_hd__(buf_\d+|dlygate\w+|clkdlybuf\w+|dlymetal\w+)$")
synth_nl = next(iter(sorted(glob.glob(str(run / "*-yosys-synthesis" / f"{top}.nl.v")))), None)
final_nl = run / "final" / "nl" / f"{top}.nl.v"
repair = None
if flow_finished and synth_nl and final_nl.exists():
    s_c, f_c, areas = cell_counts(synth_nl), cell_counts(final_nl), lib_areas(lib)
    added = {t: n - s_c.get(t, 0) for t, n in f_c.items() if REPAIR.match(t) and n > s_c.get(t, 0)}
    repair = {"cells": sum(added.values()),
              "um2": round(sum(areas.get(t, 0.0) * n for t, n in added.items()), 2),
              "by_type": dict(sorted(added.items(), key=lambda kv: -kv[1])),
              "hold_dlygate": sum(n for t, n in added.items() if "dlygate" in t)}

srcs = json.loads((out / "sources.json").read_text()) if (out / "sources.json").exists() else {}
summary = {
    "status": status,
    "pass": status == "pass",
    "tool_error": tool_error,
    "timeout": timeout,
    "failure": failure,
    "evidence": evidence,
    "flow_finished": flow_finished,
    "librelane_exit": ll_rc, "submission_exit": sub_rc,
    "drc": {"clean": checks["magic_drc"] and checks["route_drc"] and klayout_ok is True,
            "magic_errors": magic, "route_errors": route_drc, "klayout_clean": klayout_ok,
            "klayout_source": "TT precheck KLayout FEOL/BEOL/offgrid decks (TT's config sets RUN_KLAYOUT_DRC=0)"},
    "lvs": {"clean": checks["lvs"], "errors": lvs},
    "antenna": {"clean": checks["antenna"], "violating_nets": ant_nets, "violating_pins": ant_pins},
    "setup_wns": None if setup_ws is None else min(0.0, setup_ws),
    "hold_wns": None if hold_ws is None else min(0.0, hold_ws),
    "setup_ws": setup_ws, "hold_ws": hold_ws,
    "setup_violations": setup_vio, "hold_violations": hold_vio,
    "utilization": fin(g("design__instance__utilization")),
    "die_um2": fin(g("design__die__area")),
    "core_um2": fin(g("design__core__area")),
    "instance_area_um2": fin(g("design__instance__area")),
    "stdcell_area_um2": fin(g("design__instance__area__stdcell")),
    "instance_count": fin(count("design__instance__count")),
    "repair_um2": repair["um2"] if repair else None,
    "repair_cells": repair["cells"] if repair else None,
    "repair_by_type": repair["by_type"] if repair else None,
    "hold_buffers": fin(count("design__instance__count__hold_buffer")),
    "repair_librelane": {"timing_repair_buffer_um2": fin(g("design__instance__area__class:timing_repair_buffer")),
                         "timing_repair_buffer_cells": fin(count("design__instance__count__class:timing_repair_buffer"))},
    "max_slew_violations": fin(count("design__max_slew_violation__count")),
    "max_cap_violations": fin(count("design__max_cap_violation__count")),
    "ir_drop_worst_v": fin(g("ir__drop__worst")),
    "power_grid_violations": pgrid,
    "disconnected_pins": disc,
    "wirelength_um": fin(g("route__wirelength")),
    "precheck": precheck,
    "checks": checks,
    "info_sources_match": srcs.get("match"),
    "sources_used": srcs.get("used"),
    "last_step": steps[-1] if steps else None,
    "top": top,
    "flow": "TT tt-gds-action@ttsky26d: tt-support-tools 01d5d281, LibreLane 3.0.14, sky130A",
    "runtime_s": {"librelane": t_ll, "precheck": t_pre},
    "run_dir": str(run),
}
(out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps({k: summary[k] for k in ("status", "checks", "setup_ws", "hold_ws",
                  "utilization", "repair_um2", "hold_buffers", "info_sources_match",
                  "runtime_s")}, indent=1))
if failure:
    print("failure:", failure[-600:])
if tool_error:
    print("TOOL ERROR:", tool_error[-800:], file=sys.stderr)
if timeout:
    print("TIMEOUT:", timeout, file=sys.stderr)
