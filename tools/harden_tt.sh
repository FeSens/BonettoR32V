#!/usr/bin/env bash
# BonettoR32V: harden a Tiny Tapeout project locally, the way TT's GitHub action does.
#
# Mirrors TinyTapeout/tt-gds-action@ttsky26d (pdk: sky130A):
#   1. tt-support-tools (pinned below) `tt_tool.py --create-user-config`
#   2. LibreLane 3.0.14 Classic flow on src/config_merged.json (what `tt_tool.py --harden`
#      runs, inside the same ghcr.io/librelane/librelane:3.0.14 image)
#   3. `tt_tool.py --create-tt-submission`, then TT's precheck (tt-gds-action/precheck) on the
#      submission GDS, with the precheck's own PDK pin.
# Deviations from CI (all mechanical, none touch the flow config): LibreLane runs in the image
# directly instead of through `librelane --dockerized`, mounting only the stage directory and
# the PDK (not all of $HOME); the precheck's magic and klayout binaries come from the LibreLane
# image (magic 8.3.623, klayout 0.30.7) instead of TT's nix shell (8.3.568, 0.30.8).
#
# Usage:
#   tools/harden_tt.sh [--project DIR] [--out DIR] [--overlay FILE.json] [--tiles 1x2]
#                      [--keep-info-sources] [--no-precheck] [--setup-only]
#     --project DIR        TT project root with info.yaml and src/ (default: this repo)
#     --out DIR            output directory (default: build/harden); it is wiped first
#     --overlay FILE.json  extra LibreLane config keys merged into the staged src/config.json
#                          (tilefit uses this for FP_OBSTRUCTIONS; never edits the real one)
#     --tiles NxM          override info.yaml's tile count in the staged copy
#     --keep-info-sources  use info.yaml's source_files verbatim (default: regenerate the list
#                          from src/**/*.v,*.sv minus test_* paths, like tools/area.py does)
#     --no-precheck        skip TT's precheck
#     --setup-only         only fetch tools, venvs and PDKs
#
# Output: OUT/summary.json
#   {"pass", "drc", "lvs", "antenna", "setup_wns", "hold_wns", "utilization", "die_um2", ...}
# Exit status: 0 when pass is true, 1 when the design does not sign off, 2 on a tool error.
#
# Load: this is a heavy job. The script takes /tmp/bonetto-heavy.lock itself (lockf -k).
# Do NOT wrap it in lockf yourself (flock would deadlock); if you already hold the lock, set
# BONETTO_HEAVY_LOCKED=1.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TT_TOOLS_URL="https://github.com/TinyTapeout/tt-support-tools"
TT_TOOLS_SHA="01d5d2814fa9dd61e9d211e0b235a4a592a9316a"   # main HEAD on 2026-10-16 (CI uses ref main)
LIBRELANE_IMAGE="ghcr.io/librelane/librelane:3.0.14"
PDK_HASH="8afc8346a57fe1ab7934ba5a6056ea8b43078e71"            # LibreLane 3.0.14 pdk_hashes.yaml
PRECHECK_PDK_HASH="0536d02d875c8f67dd7cca3902ac457e62f20005"   # tt-gds-action/precheck@ttsky26d
CIEL_ROOT="${PDK_ROOT:-$HOME/.ciel}"
ENV_DIR="${BONETTO_TT_ENV:-$REPO/build/tt-env}"
LOCK=/tmp/bonetto-heavy.lock

if [ -z "${BONETTO_HEAVY_LOCKED:-}" ]; then
  export BONETTO_HEAVY_LOCKED=1
  exec lockf -k "$LOCK" "$0" "$@"
fi

PROJECT="$REPO"
OUT="$REPO/build/harden"
OVERLAY=""
TILES=""
KEEP_INFO=0
PRECHECK=1
SETUP_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --overlay) OVERLAY="$2"; shift 2 ;;
    --tiles) TILES="$2"; shift 2 ;;
    --keep-info-sources) KEEP_INFO=1; shift ;;
    --no-precheck) PRECHECK=0; shift ;;
    --setup-only) SETUP_ONLY=1; shift ;;
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    *) echo "harden_tt.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done

log() { echo "[harden_tt $(date +%H:%M:%S)] $*" >&2; }

# tt_tool.py imports cairosvg, which dlopens libcairo; on macOS take Homebrew's.
if [ "$(uname -s)" = Darwin ]; then
  for d in /opt/homebrew/lib /usr/local/lib; do
    [ -f "$d/libcairo.2.dylib" ] && export DYLD_FALLBACK_LIBRARY_PATH="$d${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}" && break
  done
fi
abspath() { (cd "$(dirname "$1")" && echo "$(pwd)/$(basename "$1")"); }

# ---------------------------------------------------------------- setup (idempotent)
setup() {
  mkdir -p "$ENV_DIR"
  local tt="$ENV_DIR/tt-support-tools"
  if [ ! -d "$tt/.git" ] || [ "$(git -C "$tt" rev-parse HEAD 2>/dev/null)" != "$TT_TOOLS_SHA" ]; then
    log "fetching tt-support-tools $TT_TOOLS_SHA"
    rm -rf "$tt"
    git clone -q "$TT_TOOLS_URL" "$tt"
    git -C "$tt" checkout -q "$TT_TOOLS_SHA"
  fi
  if [ ! -f "$ENV_DIR/venv/.ok-$TT_TOOLS_SHA" ]; then
    log "creating tt-support-tools venv (python 3.11, as in tt-gds-action)"
    rm -rf "$ENV_DIR/venv"
    uv venv -q --python 3.11 "$ENV_DIR/venv"
    VIRTUAL_ENV="$ENV_DIR/venv" uv pip install -q -r "$tt/requirements.txt"
    touch "$ENV_DIR/venv/.ok-$TT_TOOLS_SHA"
  fi
  if [ ! -f "$ENV_DIR/precheck-venv/.ok-$TT_TOOLS_SHA" ]; then
    log "creating precheck venv"
    rm -rf "$ENV_DIR/precheck-venv"
    uv venv -q --python 3.11 "$ENV_DIR/precheck-venv"
    VIRTUAL_ENV="$ENV_DIR/precheck-venv" uv pip install -q -r "$tt/precheck/requirements.txt"
    touch "$ENV_DIR/precheck-venv/.ok-$TT_TOOLS_SHA"
  fi
  mkdir -p "$CIEL_ROOT"
  if [ ! -d "$CIEL_ROOT/ciel/sky130/versions/$PDK_HASH/sky130A" ]; then
    log "fetching sky130 PDK $PDK_HASH via ciel"
    docker run --rm -v "$CIEL_ROOT:$CIEL_ROOT" "$LIBRELANE_IMAGE" \
      ciel enable --pdk-root "$CIEL_ROOT" --pdk-family sky130 "$PDK_HASH"
  fi
  if [ ! -d "$CIEL_ROOT/ciel/sky130/versions/$PRECHECK_PDK_HASH/sky130A" ]; then
    log "fetching precheck PDK $PRECHECK_PDK_HASH via ciel (not enabled)"
    docker run --rm -v "$CIEL_ROOT:$CIEL_ROOT" "$LIBRELANE_IMAGE" \
      ciel fetch --pdk-root "$CIEL_ROOT" --pdk-family sky130 "$PRECHECK_PDK_HASH"
  fi
  # magic / klayout for the precheck: run them from the LibreLane image, same paths inside.
  mkdir -p "$ENV_DIR/shims"
  for tool in magic klayout; do
    cat > "$ENV_DIR/shims/$tool" <<EOF
#!/usr/bin/env bash
mounts=()
for d in \$BONETTO_DOCKER_MOUNTS; do mounts+=(-v "\$d:\$d"); done
exec docker run --rm -i --user "\$(id -u):\$(id -g)" "\${mounts[@]}" -e HOME=/tmp \\
  -e PDK_ROOT="\$PDK_ROOT" -e PDK="\$PDK" -w "\$PWD" "$LIBRELANE_IMAGE" $tool "\$@"
EOF
    chmod +x "$ENV_DIR/shims/$tool"
  done
}

setup
[ "$SETUP_ONLY" = 1 ] && { log "setup done"; exit 0; }

# ---------------------------------------------------------------- stage the project
PROJECT="$(cd "$PROJECT" && pwd)"
[ -f "$PROJECT/info.yaml" ] && [ -f "$PROJECT/src/config.json" ] || {
  echo "harden_tt.sh: $PROJECT needs info.yaml and src/config.json" >&2; exit 2; }
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
case "$OUT" in "$PROJECT"|"$PROJECT/src"*|/) echo "harden_tt.sh: refusing --out $OUT" >&2; exit 2 ;; esac
find "$OUT" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
PROJ="$OUT/proj"
mkdir -p "$PROJ" "$OUT/.home"
cp "$PROJECT/info.yaml" "$PROJ/info.yaml"
cp -R "$PROJECT/src" "$PROJ/src"
rm -f "$PROJ/src/user_config.json" "$PROJ/src/config_merged.json"
[ -d "$PROJECT/docs" ] && cp -R "$PROJECT/docs" "$PROJ/docs"
rsync -a --exclude .git "$ENV_DIR/tt-support-tools/" "$PROJ/tt/"
[ -n "$OVERLAY" ] && OVERLAY="$(abspath "$OVERLAY")"

python3 -I - "$PROJ" "$KEEP_INFO" "$TILES" "$OVERLAY" <<'PY' || exit 2
import json, re, sys
from pathlib import Path
proj, keep, tiles, overlay = Path(sys.argv[1]), sys.argv[2] == "1", sys.argv[3], sys.argv[4]
info = (proj / "info.yaml").read_text().splitlines()
# Source names reach yosys command strings (tt_tool's port check, LibreLane), where a name
# like `z.v; exec -- cmd; y.v` would inject commands: src/ files must be plain `name.v`.
NAME, DIR = re.compile(r"^[A-Za-z0-9_]+\.v$"), re.compile(r"^[A-Za-z0-9_]+$")
def bad(rel, allow_dirs):
    parts = rel.split("/")
    return not (NAME.match(parts[-1]) and (len(parts) == 1 or
                (allow_dirs and all(DIR.match(d) for d in parts[:-1]))))
if keep:
    m = re.search(r"source_files\s*:\s*\n((?:\s*-.*\n|\s*#.*\n)+)", "\n".join(info) + "\n")
    listed = re.findall(r'^\s*-\s*"?([^"\n#]+?)"?\s*(?:#.*)?$', m.group(1), re.M) if m else []
    for f in listed:
        if bad(f, allow_dirs=True):
            sys.exit(f"harden_tt.sh: refusing source name {f!r} in info.yaml")
if not keep:
    src = proj / "src"
    files = sorted(str(p.relative_to(src)) for p in src.rglob("*")
                   if p.is_file() and p.suffix in (".v", ".sv")
                   and not any(x.startswith("test_") for x in p.relative_to(src).parts))
    for f in files:
        if bad(f, allow_dirs=False):
            sys.exit(f"harden_tt.sh: refusing source name {f!r}: src/ files must match "
                     f"{NAME.pattern}")
    out, skip = [], False
    for line in info:
        if re.match(r"^\s*source_files\s*:", line):
            out.append(line)
            out += [f'    - "{f}"' for f in files]
            skip = True
            continue
        if skip and re.match(r"^\s*-\s", line):
            continue
        if skip and line.strip().startswith("#"):
            continue
        skip = False
        out.append(line)
    info = out
if tiles:
    info = [re.sub(r'^(\s*tiles\s*:\s*)"?[0-9x]+"?', rf'\g<1>"{tiles}"', l) for l in info]
(proj / "info.yaml").write_text("\n".join(info) + "\n")
if overlay:
    cfg_path = proj / "src" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg.update(json.loads(Path(overlay).read_text()))
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
PY

TOP="$(python3 -I -c 'import re,sys; t=open(sys.argv[1]).read(); print(re.search(r"top_module\s*:\s*\"?([A-Za-z0-9_]+)", t).group(1))' "$PROJ/info.yaml")"
log "project $PROJECT top $TOP -> $OUT"

# ---------------------------------------------------------------- 1. create-user-config
T0=$(date +%s)
(cd "$PROJ" && PATH="$ENV_DIR/venv/bin:$PATH" python tt/tt_tool.py --create-user-config) > "$OUT/create-user-config.log" 2>&1 || {
  tail -20 "$OUT/create-user-config.log" >&2; echo "harden_tt.sh: --create-user-config failed" >&2; exit 2; }

# ---------------------------------------------------------------- 2. LibreLane (tt_tool --harden)
mkdir -p "$PROJ/runs/wokwi"
log "LibreLane 3.0.14 Classic flow (log: $OUT/librelane.log)"
set +e
docker run --rm --user "$(id -u):$(id -g)" -v "$OUT:$OUT" -v "$CIEL_ROOT:$CIEL_ROOT" \
  -e PDK_ROOT="$CIEL_ROOT" -e HOME="$OUT/.home" -w "$PROJ" "$LIBRELANE_IMAGE" \
  librelane --pdk-root "$CIEL_ROOT" --run-tag wokwi --force-run-dir runs/wokwi \
  --hide-progress-bar src/config_merged.json > "$OUT/librelane.log" 2>&1
LL_RC=$?
set -e
T1=$(date +%s)
log "LibreLane exit $LL_RC after $((T1 - T0)) s"

# tt_tool --harden's post-processing (commit_id.json, pdk.json), needed by --create-tt-submission
PRE_RC=-1
[ "$PRECHECK" = 0 ] && PRE_RC=-2
if [ "$LL_RC" = 0 ]; then
  python3 -I - "$PROJ" "$TT_TOOLS_SHA" <<'PY'
import json, sys
from pathlib import Path
proj, sha = Path(sys.argv[1]), sys.argv[2]
run = proj / "runs" / "wokwi"
(run / "final" / "commit_id.json").write_text(json.dumps(
    {"app": f"Tiny Tapeout main {sha[:8]}", "repo": "N/A", "commit": "local",
     "workflow_url": None}, indent=2) + "\n")
ll = json.loads((run / "resolved.json").read_text())
src, ver = (Path(ll["PDK_ROOT"]) / "sky130A" / "SOURCES").read_text().split()
(run / "pdk.json").write_text(json.dumps(
    {"FLOW_NAME": "LibreLane", "FLOW_VERSION": ll["meta"]["librelane_version"], "PDK": ll["PDK"],
     "PDK_SOURCE": src, "PDK_VERSION": ver}, indent=2))
PY
  (cd "$PROJ" && PATH="$ENV_DIR/venv/bin:$PATH" python tt/tt_tool.py --create-tt-submission) \
    > "$OUT/create-tt-submission.log" 2>&1 || { tail -20 "$OUT/create-tt-submission.log" >&2; LL_RC=97; }

  # -------------------------------------------------------------- 3. TT precheck
  if [ "$PRECHECK" = 1 ] && [ "$LL_RC" = 0 ]; then
    log "TT precheck (log: $OUT/precheck.log)"
    set +e
    (cd "$PROJ/tt/precheck" && mkdir -p reports && \
      PATH="$ENV_DIR/shims:$ENV_DIR/precheck-venv/bin:$PATH" \
      PDK_ROOT="$CIEL_ROOT/ciel/sky130/versions/$PRECHECK_PDK_HASH" PDK=sky130A \
      BONETTO_DOCKER_MOUNTS="$OUT $CIEL_ROOT" \
      python precheck.py --gds "$PROJ/tt_submission/$TOP.gds") > "$OUT/precheck.log" 2>&1
    PRE_RC=$?
    set -e
    log "precheck exit $PRE_RC after $(( $(date +%s) - T1 )) s"
  fi
fi
T2=$(date +%s)

# ---------------------------------------------------------------- summary
python3 -I - "$OUT" "$PROJ" "$TOP" "$LL_RC" "$PRE_RC" "$((T1 - T0))" "$((T2 - T1))" <<'PY'
import glob, json, os, re, sys
from pathlib import Path
out, proj, top = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
ll_rc, pre_rc, t_ll, t_pre = int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6]), int(sys.argv[7])
run = proj / "runs" / "wokwi"
m = {}
final = run / "final" / "metrics.json"
if final.exists():
    m = json.loads(final.read_text())
else:  # failed run: take the metrics of the last step that finished
    states = sorted(glob.glob(str(run / "*" / "state_out.json")))
    if states:
        m = json.loads(Path(states[-1]).read_text()).get("metrics", {})

def g(k, default=None):
    v = m.get(k, default)
    return float(v) if isinstance(v, str) and re.match(r"^-?[0-9.eE+-]+$", v) else v

def count(k):
    v = g(k)
    return None if v is None else int(v)

complete = ll_rc == 0 and final.exists()
failed_step = None
if not complete:
    steps = sorted(p.name for p in run.iterdir() if p.is_dir() and re.match(r"^\d+-", p.name)) \
        if run.exists() else []
    err = (run / "error.log").read_text(errors="replace").strip() if (run / "error.log").exists() else ""
    failed_step = f"last step dir: {steps[-1] if steps else 'none'}; " + \
        (err[-1200:] if err else f"LibreLane exit code {ll_rc}, see librelane.log")
precheck_skipped = pre_rc == -2
precheck = {"ran": pre_rc >= 0, "pass": pre_rc == 0, "skipped": precheck_skipped}
res_md = proj / "tt" / "precheck" / "reports" / "results.md"
if res_md.exists():
    rows = {}
    for line in res_md.read_text().splitlines():
        mm = re.match(r"^\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|$", line)
        if mm and mm.group(1) not in ("Check", "-----------"):
            rows[mm.group(1)] = "pass" if "✅" in mm.group(2) else mm.group(2)
    precheck["checks"] = rows
klayout_ok = None
if "checks" in precheck:
    kl = {k: v for k, v in precheck["checks"].items() if k.startswith("KLayout")}
    klayout_ok = bool(kl) and all(v == "pass" for v in kl.values())

magic = count("magic__drc_error__count")
route_drc = count("route__drc_errors")
lvs = count("design__lvs_error__count")
ant_nets = count("antenna__violating__nets")
ant_pins = count("antenna__violating__pins")
setup_vio = count("timing__setup_vio__count")
hold_vio = count("timing__hold_vio__count")
setup_ws = g("timing__setup__ws")
hold_ws = g("timing__hold__ws")
pgrid = count("design__power_grid_violation__count")
disc = count("design__critical_disconnected_pin__count")
overlap = count("magic__illegal_overlap__count")

drc_clean = (magic == 0 and route_drc == 0 and klayout_ok is True)
# Every check needs a completed flow: metrics from a run that died early (for example the
# pre-PnR STA) say nothing about signoff.
checks = {k: complete and v for k, v in {
    "librelane_flow": True,
    "magic_drc": magic == 0,
    "route_drc": route_drc == 0,
    "klayout_drc": klayout_ok is True,
    "lvs": lvs == 0,
    "antenna": ant_nets == 0 and ant_pins == 0,
    "setup_all_corners": setup_vio == 0 and setup_ws is not None and setup_ws >= 0,
    "hold_all_corners": hold_vio == 0 and hold_ws is not None and hold_ws >= 0,
    "power_grid": pgrid == 0,
    "disconnected_pins": disc == 0,
    "illegal_overlap": overlap in (0, None),
    "precheck": precheck["pass"],
}.items()}
if precheck_skipped:  # --no-precheck: KLayout DRC comes only from the precheck
    checks["klayout_drc"] = None
    checks["precheck"] = None
summary = {
    "pass": all(v for v in checks.values() if v is not None),
    "complete": complete,
    "drc": {"clean": drc_clean, "magic_errors": magic, "route_errors": route_drc,
            "klayout_clean": klayout_ok,
            "klayout_source": "TT precheck KLayout FEOL/BEOL/offgrid decks (TT's config sets RUN_KLAYOUT_DRC=0)"},
    "lvs": {"clean": lvs == 0, "errors": lvs},
    "antenna": {"clean": ant_nets == 0 and ant_pins == 0, "violating_nets": ant_nets,
                "violating_pins": ant_pins},
    "setup_wns": None if setup_ws is None else min(0.0, setup_ws),
    "hold_wns": None if hold_ws is None else min(0.0, hold_ws),
    "setup_ws": setup_ws, "hold_ws": hold_ws,
    "setup_violations": setup_vio, "hold_violations": hold_vio,
    "utilization": g("design__instance__utilization"),
    "utilization_stdcell": g("design__instance__utilization__stdcell"),
    "die_um2": g("design__die__area"),
    "core_um2": g("design__core__area"),
    "instance_area_um2": g("design__instance__area"),
    "stdcell_area_um2": g("design__instance__area__stdcell"),
    "instance_count": count("design__instance__count"),
    "max_slew_violations": count("design__max_slew_violation__count"),
    "max_cap_violations": count("design__max_cap_violation__count"),
    "ir_drop_worst_v": g("ir__drop__worst"),
    "power_grid_violations": pgrid,
    "disconnected_pins": disc,
    "wirelength_um": g("route__wirelength"),
    "precheck": precheck,
    "checks": checks,
    "failure": failed_step,
    "top": top,
    "flow": "TT tt-gds-action@ttsky26d: tt-support-tools 01d5d281, LibreLane 3.0.14, sky130A",
    "runtime_s": {"librelane": t_ll, "precheck": t_pre},
    "run_dir": str(run),
}
(out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps({k: summary[k] for k in ("pass", "checks", "setup_ws", "hold_ws",
                  "utilization", "die_um2", "runtime_s")}, indent=1))
if failed_step:
    print("failure:", failed_step[-600:])
PY

PASS="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["pass"])' "$OUT/summary.json")"
[ "$PASS" = "True" ] && exit 0 || exit 1
