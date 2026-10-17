#!/usr/bin/env python3
"""BonettoR32V: classify one tools/harden_tt.sh run and write OUT/summary.json.

Called by tools/harden_tt.sh; kept as its own file so tools/test_physical.py can run it on
fixture run directories. Arguments: OUT PROJ TOP LL_RC SUB_RC PRE_RC T_LIBRELANE T_PRECHECK LIB
  LL_RC   LibreLane exit code
  SUB_RC  tt_tool --create-tt-submission exit code (-1: not attempted)
  PRE_RC  precheck exit code (-1: not attempted, -2: --no-precheck)
  LIB     the tt corner liberty file (cell areas for the timing-repair count)
summary.json "status" is "pass", "fail" (evidence that the design does not sign off) or
"tool_error"; harden_tt.sh maps them to exit 0, 1 and 2 by reading the file back, so a crash
here reads as a tool error.
"""
import glob, json, re, sys
from pathlib import Path
out, proj, top = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
ll_rc, sub_rc, pre_rc, t_ll, t_pre = (int(x) for x in sys.argv[4:9])
lib = Path(sys.argv[9])
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

errlog = (run / "error.log").read_text(errors="replace") if (run / "error.log").exists() else ""
lllog = (out / "librelane.log").read_text(errors="replace") if (out / "librelane.log").exists() else ""
logs = errlog + "\n" + lllog

# --- precheck results
precheck = {"ran": pre_rc >= 0, "skipped": pre_rc == -2, "exit_code": pre_rc}
res_md = proj / "tt" / "precheck" / "reports" / "results.md"
if pre_rc >= 0 and res_md.exists():
    rows = {}
    for line in res_md.read_text().splitlines():
        mm = re.match(r"^\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|$", line)
        if mm and mm.group(1) not in ("Check", "-----------"):
            rows[mm.group(1)] = "pass" if "\u2705" in mm.group(2) else mm.group(2)
    precheck["checks"] = rows
rows = precheck.get("checks", {})
precheck["pass"] = pre_rc == 0 and bool(rows) and all(v == "pass" for v in rows.values())
kl = {k: v for k, v in rows.items() if k.startswith("KLayout")}
klayout_ok = (bool(kl) and all(v == "pass" for v in kl.values())) if rows else None

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
if rows and not precheck["pass"]:
    evidence += [f"precheck: {k}" for k, v in rows.items() if v != "pass"]
patterns = [
    (r"GPL-0301", "global placement: utilization above 100 %"),
    (r"DPL-0036|DPL-0044|[Dd]etailed placement failed", "detailed placement failed"),
    (r"ERROR\s+GRT-0116|\[GRT-0116\]", "global routing overflow"),
    (r"\d+(?:\.\d+)? (?:Routing DRC errors|Magic DRC errors|Magic Illegal Overlap errors|"
     r"critical disconnected pins|LVS errors|power grid violations[^\n]*|Lint errors|"
     r"Lint Timing Errors|KLayout DRC errors|Unmapped Yosys instances|Yosys check errors) found",
     "LibreLane checker"),
]
if ll_rc != 0:
    for rx, what in patterns:
        hit = re.search(rx, errlog) or re.search(rx, lllog)
        if hit:
            evidence.append(f"{what}: {hit.group(0)[:200]}")

tool_error = None
if ll_rc in (125, 126, 127) or ll_rc > 128:
    tool_error = f"docker/LibreLane did not run or was killed (exit {ll_rc})"
elif not steps:
    tool_error = f"LibreLane produced no flow steps (exit {ll_rc})"
elif ll_rc != 0 and not evidence:
    tool_error = (f"LibreLane exit {ll_rc} without signoff evidence; last step "
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
    elif pre_rc > 0 and rows and all(v == "pass" for v in rows.values()):
        tool_error = f"precheck exit {pre_rc} although every results row passed"

failure = None
if tool_error:
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
