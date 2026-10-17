#!/usr/bin/env bash
# BonettoR32V: harden a Tiny Tapeout project locally, the way TT's GitHub action does.
#
# Mirrors TinyTapeout/tt-gds-action@ttsky26d (pdk: sky130A):
#   1. tt-support-tools (pinned below) `tt_tool.py --create-user-config`
#   2. LibreLane 3.0.14 Classic flow on src/config_merged.json (what `tt_tool.py --harden`
#      runs, inside the same ghcr.io/librelane/librelane:3.0.14 image, pinned by digest)
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
#   (every option also accepts the --option=value form)
#     --project DIR        TT project root with info.yaml and src/ (default: this repo)
#     --out DIR            output directory (default: build/harden); it is wiped first, so it
#                          must not be /, $HOME, the project or this repo or any ancestor of
#                          them, nor inside a checkout outside its build/, nor a non-empty
#                          directory that is not a previous harden_tt.sh output
#     --overlay FILE.json  extra LibreLane config keys merged into the staged src/config.json
#                          (tilefit uses this for FP_OBSTRUCTIONS; never edits the real one)
#     --tiles NxM          override info.yaml's tile count in the staged copy
#     --keep-info-sources  use info.yaml's source_files verbatim (default: regenerate the list
#                          from src/*.v, like tools/area.py does)
#     --no-precheck        skip TT's precheck
#     --setup-only         only fetch tools, venvs and PDKs
#
# Output: OUT/summary.json
#   {"status", "pass", "drc", "lvs", "antenna", "setup_wns", "hold_wns", "utilization",
#    "die_um2", "repair_um2", "repair_cells", "hold_buffers", "info_sources_match", ...}
# Exit status, decided from evidence:
#   0  every signoff check passed
#   1  the design does not sign off: a named signoff check reports a violation (DRC, LVS,
#      antenna, timing, power grid, disconnected pins, overlap, a LibreLane checker, a failing
#      precheck row) or the flow stopped on GPL-0301, a detailed-placement failure or a
#      routing overflow
#   2  tool error: Docker or LibreLane did not run or was killed (rc 125/126/127, >128), no
#      flow steps, a failure with none of the evidence above, a missing signoff metric, a
#      failed --create-tt-submission, or a precheck that wrote no results
#
# Load: this is a heavy job. The script takes the heavy lock itself (lockf -k on
# $BONETTO_HEAVY_LOCK, default /tmp/bonetto-heavy.lock). Do NOT wrap it in lockf yourself
# (flock would deadlock); if you already hold the lock, set BONETTO_HEAVY_LOCKED=1.
set -Eeuo pipefail
# Any unexpected failure is a tool error (2), never a design verdict (1).
on_err() { echo "harden_tt.sh: unexpected failure at line $1 (tool error)" >&2; exit 2; }
errtrap_on() { trap 'on_err $LINENO' ERR; }
errtrap_off() { trap - ERR; }
errtrap_on

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TT_TOOLS_URL="https://github.com/TinyTapeout/tt-support-tools"
TT_TOOLS_SHA="01d5d2814fa9dd61e9d211e0b235a4a592a9316a"   # main HEAD on 2026-10-16 (CI uses ref main)
# ghcr.io/librelane/librelane:3.0.14, pinned by digest
LIBRELANE_IMAGE="ghcr.io/librelane/librelane@sha256:f91b21d75f79871f9ccf37451020b5d2f7b3236881a997ff709c561e8280a30f"
PDK_HASH="8afc8346a57fe1ab7934ba5a6056ea8b43078e71"            # LibreLane 3.0.14 pdk_hashes.yaml
PRECHECK_PDK_HASH="0536d02d875c8f67dd7cca3902ac457e62f20005"   # tt-gds-action/precheck@ttsky26d
CIEL_ROOT="${PDK_ROOT:-$HOME/.ciel}"
ENV_DIR="${BONETTO_TT_ENV:-$REPO/build/tt-env}"
LOCK="${BONETTO_HEAVY_LOCK:-/tmp/bonetto-heavy.lock}"

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
    --*=*) a="$1"; shift; set -- "${a%%=*}" "${a#*=}" "$@"; continue ;;
  esac
  case "$1" in
    --project|--out|--overlay|--tiles)
      [ $# -ge 2 ] || { echo "harden_tt.sh: $1 needs a value" >&2; exit 2; } ;;
  esac
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --overlay) OVERLAY="$2"; shift 2 ;;
    --tiles) TILES="$2"; shift 2 ;;
    --keep-info-sources) KEEP_INFO=1; shift ;;
    --no-precheck) PRECHECK=0; shift ;;
    --setup-only) SETUP_ONLY=1; shift ;;
    -h|--help) sed -n '2,47p' "$0"; exit 0 ;;
    *) echo "harden_tt.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$TILES" in ""|[0-9]x[0-9]) ;; *) echo "harden_tt.sh: bad --tiles $TILES" >&2; exit 2 ;; esac

log() { echo "[harden_tt $(date +%H:%M:%S)] $*" >&2; }
abspath() { (cd "$(dirname "$1")" && echo "$(pwd)/$(basename "$1")"); }

# ---------------------------------------------------------------- resolve and wipe OUT first
# (before anything can fail, so no stale summary.json survives a failed run)
if [ "$SETUP_ONLY" = 0 ]; then
  [ -d "$PROJECT" ] || { echo "harden_tt.sh: no project dir $PROJECT" >&2; exit 2; }
  PROJECT="$(cd "$PROJECT" && pwd -P)"
  # real paths, checked before anything is created or deleted
  python3 -I - "$PROJECT" "$OUT" "$HOME" "$REPO" <<'PY' || exit 2
import os, sys
proj, out, home, repo = (os.path.realpath(os.path.abspath(p)) for p in sys.argv[1:5])
def covers(a, b):  # a is b or an ancestor of b
    return b == a or b.startswith(a.rstrip("/") + "/")
for root in (proj, repo):  # inside a checkout, only under its build/
    if covers(root, out) and not covers(os.path.join(root, "build"), out):
        sys.exit(f"harden_tt.sh: refusing --out {out}: inside {root} it must be under build/")
if out == "/" or covers(out, proj) or covers(out, repo) or covers(out, home):
    sys.exit(f"harden_tt.sh: refusing --out {out} (project {proj}, home {home})")
names = os.listdir(out) if os.path.isdir(out) else []
if names and not ({".harden_tt_out", "summary.json"} & set(names)
                  or os.path.isfile(os.path.join(out, "proj", "info.yaml"))):
    sys.exit(f"harden_tt.sh: refusing to wipe --out {out}: not empty and not a harden_tt.sh "
             f"output directory")
PY
  mkdir -p "$OUT"
  OUT="$(cd "$OUT" && pwd -P)"
  find "$OUT" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  touch "$OUT/.harden_tt_out"
  if [ -n "$OVERLAY" ]; then OVERLAY="$(abspath "$OVERLAY")"; fi
fi

# tt_tool.py imports cairosvg, which dlopens libcairo; on macOS take Homebrew's.
if [ "$(uname -s)" = Darwin ]; then
  for d in /opt/homebrew/lib /usr/local/lib; do
    if [ -f "$d/libcairo.2.dylib" ]; then
      export DYLD_FALLBACK_LIBRARY_PATH="$d${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
      break
    fi
  done
fi

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
[ -f "$PROJECT/info.yaml" ] && [ -f "$PROJECT/src/config.json" ] || {
  echo "harden_tt.sh: $PROJECT needs info.yaml and src/config.json" >&2; exit 2; }
PROJ="$OUT/proj"
mkdir -p "$PROJ" "$OUT/.home"
cp "$PROJECT/info.yaml" "$PROJ/info.yaml"
cp -R "$PROJECT/src" "$PROJ/src"
rm -f "$PROJ/src/user_config.json" "$PROJ/src/config_merged.json"
[ -d "$PROJECT/docs" ] && cp -R "$PROJECT/docs" "$PROJ/docs"
rsync -a --exclude .git "$ENV_DIR/tt-support-tools/" "$PROJ/tt/"

python3 -I - "$PROJ" "$KEEP_INFO" "$TILES" "$OVERLAY" "$OUT/sources.json" <<'PY' || exit 2
import json, re, sys
from pathlib import Path
proj, keep, tiles, overlay, srcjson = (Path(sys.argv[1]), sys.argv[2] == "1", sys.argv[3],
                                       sys.argv[4], Path(sys.argv[5]))
info = (proj / "info.yaml").read_text().splitlines()
# Source names reach yosys command strings (tt_tool's port check, LibreLane), where a name
# like `z.v; exec -- cmd; y.v` would inject commands: src/ files must be plain `name.v`.
NAME, DIR = re.compile(r"^[A-Za-z0-9_]+\.v$"), re.compile(r"^[A-Za-z0-9_]+$")
def bad(rel, allow_dirs):
    parts = rel.split("/")
    return not (NAME.match(parts[-1]) and (len(parts) == 1 or
                (allow_dirs and all(DIR.match(d) for d in parts[:-1]))))
m = re.search(r"source_files\s*:\s*\n((?:[ \t]*-.*\n|[ \t]*#.*\n)+)", "\n".join(info) + "\n")
committed = re.findall(r'^\s*-\s*"?([^"\n#]+?)"?\s*(?:#.*)?$', m.group(1), re.M) if m else []
if keep:
    for f in committed:
        if bad(f, allow_dirs=True):
            sys.exit(f"harden_tt.sh: refusing source name {f!r} in info.yaml")
    used = committed
else:
    src = proj / "src"
    used = sorted(str(p.relative_to(src)) for p in src.rglob("*")
                  if p.is_file() and p.suffix in (".v", ".sv")
                  and not any(x.startswith("test_") for x in p.relative_to(src).parts))
    for f in used:
        if bad(f, allow_dirs=False):
            sys.exit(f"harden_tt.sh: refusing source name {f!r}: src/ files must match "
                     f"{NAME.pattern}")
    out, skip = [], False
    for line in info:
        if re.match(r"^\s*source_files\s*:", line):
            out.append(line)
            out += [f'    - "{f}"' for f in used]
            skip = True
            continue
        if skip and (re.match(r"^\s*-\s", line) or line.strip().startswith("#")):
            continue
        skip = False
        out.append(line)
    info = out
srcjson.write_text(json.dumps({"used": used, "committed": committed,
                               "match": sorted(used) == sorted(committed)}, indent=2) + "\n")
if tiles:
    info = [re.sub(r'^(\s*tiles\s*:\s*)"?[0-9x]+"?', rf'\g<1>"{tiles}"', l) for l in info]
(proj / "info.yaml").write_text("\n".join(info) + "\n")
if overlay:
    cfg_path = proj / "src" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg.update(json.loads(Path(overlay).read_text()))
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
PY

if ! python3 -I -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["match"] else 1)' "$OUT/sources.json"; then
  log "WARNING: info.yaml source_files differ from the src/ files hardened here."
  log "WARNING: TT's CI uses the committed info.yaml list; see $OUT/sources.json"
fi

TOP="$(python3 -I -c 'import re,sys; t=open(sys.argv[1]).read(); print(re.search(r"top_module\s*:\s*\"?([A-Za-z0-9_]+)", t).group(1))' "$PROJ/info.yaml")"
log "project $PROJECT top $TOP -> $OUT"

# ---------------------------------------------------------------- 1. create-user-config
T0=$(date +%s)
(cd "$PROJ" && PATH="$ENV_DIR/venv/bin:$PATH" python tt/tt_tool.py --create-user-config) > "$OUT/create-user-config.log" 2>&1 || {
  tail -20 "$OUT/create-user-config.log" >&2; echo "harden_tt.sh: --create-user-config failed" >&2; exit 2; }

# ---------------------------------------------------------------- 2. LibreLane (tt_tool --harden)
mkdir -p "$PROJ/runs/wokwi"
log "LibreLane 3.0.14 Classic flow (log: $OUT/librelane.log)"
set +e; errtrap_off
docker run --rm --user "$(id -u):$(id -g)" -v "$OUT:$OUT" -v "$CIEL_ROOT:$CIEL_ROOT" \
  -e PDK_ROOT="$CIEL_ROOT" -e HOME="$OUT/.home" -w "$PROJ" "$LIBRELANE_IMAGE" \
  librelane --pdk-root "$CIEL_ROOT" --run-tag wokwi --force-run-dir runs/wokwi \
  --hide-progress-bar src/config_merged.json > "$OUT/librelane.log" 2>&1
LL_RC=$?
set -e; errtrap_on
T1=$(date +%s)
log "LibreLane exit $LL_RC after $((T1 - T0)) s"

# ---------------------------------------------------------------- 3. submission and precheck
# Only for a flow that ran to the end without errors (deferred checker errors still write
# final/ but make LibreLane exit non-zero; those are design failures, judged from metrics).
SUB_RC=-1   # -1: not attempted
PRE_RC=-1   # -1: not attempted, -2: --no-precheck
[ "$PRECHECK" = 0 ] && PRE_RC=-2
if [ "$LL_RC" = 0 ] && [ -f "$PROJ/runs/wokwi/final/metrics.json" ]; then
  # tt_tool --harden's post-processing (commit_id.json, pdk.json), needed by --create-tt-submission
  set +e; errtrap_off
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
  SUB_RC=$?
  if [ "$SUB_RC" = 0 ]; then
    (cd "$PROJ" && PATH="$ENV_DIR/venv/bin:$PATH" python tt/tt_tool.py --create-tt-submission) \
      > "$OUT/create-tt-submission.log" 2>&1
    SUB_RC=$?
  fi
  set -e; errtrap_on
  [ "$SUB_RC" = 0 ] || { tail -20 "$OUT/create-tt-submission.log" >&2 2>/dev/null || true; }

  if [ "$PRECHECK" = 1 ] && [ "$SUB_RC" = 0 ]; then
    log "TT precheck (log: $OUT/precheck.log)"
    rm -rf "$PROJ/tt/precheck/reports"
    set +e; errtrap_off
    (cd "$PROJ/tt/precheck" && mkdir -p reports && \
      PATH="$ENV_DIR/shims:$ENV_DIR/precheck-venv/bin:$PATH" \
      PDK_ROOT="$CIEL_ROOT/ciel/sky130/versions/$PRECHECK_PDK_HASH" PDK=sky130A \
      BONETTO_DOCKER_MOUNTS="$OUT $CIEL_ROOT" \
      python precheck.py --gds "$PROJ/tt_submission/$TOP.gds") > "$OUT/precheck.log" 2>&1
    PRE_RC=$?
    set -e; errtrap_on
    log "precheck exit $PRE_RC after $(( $(date +%s) - T1 )) s"
  fi
fi
T2=$(date +%s)

# ---------------------------------------------------------------- summary and exit status
LIB="$CIEL_ROOT/ciel/sky130/versions/$PDK_HASH/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"
set +e; errtrap_off
python3 -I "$REPO/tools/harden_summary.py" "$OUT" "$PROJ" "$TOP" "$LL_RC" "$SUB_RC" "$PRE_RC" \
  "$((T1 - T0))" "$((T2 - T1))" "$LIB"
set -e; errtrap_on
# The exit status comes from the summary's status, never from the summarizer's own exit code:
# a crash there must read as a tool error (2), not as a design failure (1).
STATUS="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$OUT/summary.json" 2>/dev/null || echo missing)"
case "$STATUS" in
  pass) exit 0 ;;
  fail) exit 1 ;;
  *) echo "harden_tt.sh: tool error (status: $STATUS), see $OUT/summary.json" >&2; exit 2 ;;
esac
