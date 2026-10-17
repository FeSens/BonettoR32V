#!/usr/bin/env bash
# BonettoR32V: make LibreLane (tools/harden_tt.sh, tools/area.py, physical/tilefit.py) run on
# hwe-bench, which has no docker. Idempotent: run it again after pulling a new pin.
#
# Run it on hwe-bench from a mirror of this repo, e.g. from the Mac:
#   tools/hwe_run.sh --tag hwe-setup -- tools/hwe_harden_setup.sh
#
# What it does (state under $BONETTO_LL_HOME, default ~/bonetto-librelane):
#   1. Installs tools/hwe_docker_shim.py and tools/hwe_lockf_shim.py into shim/, and the
#      wrappers `docker` and `lockf` into ~/bonetto-venv/bin (first on hwe_run.sh's PATH).
#      A wrapper not written by this script is never overwritten.
#   2. `docker pull` of the LibreLane image pinned in tools/harden_tt.sh: the linux/amd64
#      manifest of that multi-arch digest, verified and unpacked into images/.
#   3. libcairo for tt_tool.py (cairosvg dlopens it), only when the host has none: Ubuntu's
#      libcairo2 and libpixman-1-0 via `apt-get download` (hash-checked by apt), unpacked into
#      sysroot/, linked from lib/. harden_tt.sh puts lib/ on LD_LIBRARY_PATH when it exists.
#   4. The flow's sky130 PDK through ciel into ~/.ciel (harden_tt.sh's and area.py's default).
#      A partial version directory (an earlier copy of a few files for the yosys fallback) is
#      replaced by a full ciel fetch after checking every file it had is byte-identical; the
#      old copy is kept in ciel-partial-<hash>/.
#   5. tools/harden_tt.sh --setup-only (precheck PDK, tt-support-tools, both venvs, shims) for
#      this mirror's build/tt-env; every other mirror builds its own on its first harden.
#   6. A smoke check: tool versions inside the image, and tt_tool's cairo import.
set -Eeuo pipefail
trap 'echo "hwe_harden_setup.sh: failed at line $LINENO" >&2' ERR

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LL_HOME="${BONETTO_LL_HOME:-$HOME/bonetto-librelane}"
BIN="${BONETTO_SHIM_BIN:-$HOME/bonetto-venv/bin}"
CIEL_ROOT="${PDK_ROOT:-$HOME/.ciel}"
MARK="# installed by BonettoR32V tools/hwe_harden_setup.sh"
log() { echo "[hwe_setup $(date +%H:%M:%S)] $*" >&2; }

[ "$(uname -s)" = Linux ] || { echo "hwe_harden_setup.sh: run this on hwe-bench (Linux)" >&2; exit 2; }
for t in /usr/bin/bwrap /usr/bin/python3 uv git rsync apt-get dpkg-deb; do
  command -v "$t" >/dev/null || { echo "hwe_harden_setup.sh: missing $t" >&2; exit 2; }
done
unshare -r -m true || { echo "hwe_harden_setup.sh: unprivileged user namespaces are off" >&2; exit 2; }
[ -d "$BIN" ] || { echo "hwe_harden_setup.sh: $BIN missing (the ~/bonetto-venv of hwe_run.sh)" >&2; exit 2; }

IMAGE="$(sed -n 's/^LIBRELANE_IMAGE="\(.*\)".*/\1/p' "$REPO/tools/harden_tt.sh")"
PDK_HASH="$(sed -n 's/^PDK_HASH="\([0-9a-f]*\)".*/\1/p' "$REPO/tools/harden_tt.sh")"
[ -n "$IMAGE" ] && [ -n "$PDK_HASH" ] || { echo "hwe_harden_setup.sh: can't read pins from harden_tt.sh" >&2; exit 2; }
grep -q "${IMAGE##*:}" "$REPO/tools/area.py" || { echo "hwe_harden_setup.sh: area.py pins another image" >&2; exit 2; }

# ---------------------------------------------------------------- 1. shims
mkdir -p "$LL_HOME/shim"
install -m 0644 "$REPO/tools/hwe_docker_shim.py" "$LL_HOME/shim/hwe_docker_shim.py"
install -m 0644 "$REPO/tools/hwe_lockf_shim.py" "$LL_HOME/shim/hwe_lockf_shim.py"
for pair in docker:hwe_docker_shim.py lockf:hwe_lockf_shim.py; do
  name="${pair%%:*}" py="${pair#*:}"
  if [ -e "$BIN/$name" ] && ! grep -qF "$MARK" "$BIN/$name"; then
    echo "hwe_harden_setup.sh: $BIN/$name exists and isn't ours; not touching it" >&2; exit 2
  fi
  tmp="$BIN/.$name.tmp.$$"
  printf '#!/bin/sh\n%s\nexec /usr/bin/python3 -I "%s" "$@"\n' "$MARK" "$LL_HOME/shim/$py" > "$tmp"
  chmod 0755 "$tmp"
  mv -f "$tmp" "$BIN/$name"
done
export PATH="$BIN:$PATH"
log "shims: $(command -v docker), $(command -v lockf)"

# ---------------------------------------------------------------- 2. image
docker pull "$IMAGE"

# ---------------------------------------------------------------- 3. libcairo
if /sbin/ldconfig -p 2>/dev/null | grep -q 'libcairo\.so\.2 '; then
  log "libcairo: the host has one"
elif [ -f "$LL_HOME/lib/libcairo.so.2" ]; then
  log "libcairo: $LL_HOME/lib"
else
  log "libcairo: fetching Ubuntu's libcairo2 and libpixman-1-0"
  dl="$(mktemp -d "$LL_HOME/debs.XXXXXX")"
  (cd "$dl" && apt-get download -q libcairo2 libpixman-1-0)
  rm -rf "$LL_HOME/sysroot"
  mkdir -p "$LL_HOME/sysroot" "$LL_HOME/lib.tmp"
  for deb in "$dl"/*.deb; do dpkg-deb -x "$deb" "$LL_HOME/sysroot"; done
  ls "$dl" > "$LL_HOME/sysroot/PACKAGES"
  rm -rf "$dl"
  for so in libcairo.so.2 libpixman-1.so.0; do
    f="$(find "$LL_HOME/sysroot" -name "$so" | head -1)"
    [ -n "$f" ] || { echo "hwe_harden_setup.sh: $so not in the debs" >&2; exit 2; }
    ln -sf "$f" "$LL_HOME/lib.tmp/$so"
  done
  rm -rf "$LL_HOME/lib"
  mv "$LL_HOME/lib.tmp" "$LL_HOME/lib"
fi

# ---------------------------------------------------------------- 4. flow PDK (ciel)
VDIR="$CIEL_ROOT/ciel/sky130/versions/$PDK_HASH"
complete() {
  [ -f "$1/sky130A/SOURCES" ] && [ -f "$1/sky130A/libs.tech/magic/sky130A.tech" ] && \
    [ -f "$1/sky130A/libs.ref/sky130_fd_sc_hd/lef/sky130_fd_sc_hd.lef" ] && \
    [ -f "$1/sky130A/libs.tech/openlane/config.tcl" ]
}
if [ -d "$VDIR" ] && ! complete "$VDIR"; then
  log "PDK: $VDIR is partial ($(find "$VDIR" -type f | wc -l) files); fetching the full one"
  stage="$LL_HOME/ciel-stage"
  rm -rf "$stage"
  mkdir -p "$stage"
  docker run --rm -v "$stage:$stage" "$IMAGE" \
    ciel fetch --pdk-root "$stage" --pdk-family sky130 "$PDK_HASH"
  new="$stage/ciel/sky130/versions/$PDK_HASH"
  complete "$new" || { echo "hwe_harden_setup.sh: ciel fetch gave an incomplete PDK" >&2; exit 2; }
  (cd "$VDIR" && find . -type f) | while read -r f; do
    cmp -s "$VDIR/$f" "$new/$f" || { echo "hwe_harden_setup.sh: $f differs from ciel's" >&2; exit 2; }
  done
  bak="$LL_HOME/ciel-partial-$PDK_HASH"
  rm -rf "$bak"
  mv "$VDIR" "$bak"
  mv "$new" "$VDIR"
  rm -rf "$stage"
  log "PDK: replaced; the partial copy (all files identical to ciel's) is in $bak"
fi
mkdir -p "$CIEL_ROOT"
if [ ! -d "$VDIR" ] || [ "$(cat "$CIEL_ROOT/ciel/sky130/current" 2>/dev/null)" != "$PDK_HASH" ]; then
  log "PDK: ciel enable $PDK_HASH"
  docker run --rm -v "$CIEL_ROOT:$CIEL_ROOT" "$IMAGE" \
    ciel enable --pdk-root "$CIEL_ROOT" --pdk-family sky130 "$PDK_HASH"
fi
complete "$VDIR" || { echo "hwe_harden_setup.sh: $VDIR incomplete after ciel" >&2; exit 2; }

# ---------------------------------------------------------------- 5. harden_tt.sh setup
BONETTO_HEAVY_LOCKED=1 bash "$REPO/tools/harden_tt.sh" --setup-only

# ---------------------------------------------------------------- 6. smoke
log "smoke: tools in the image"
docker run --rm "$IMAGE" sh -c 'librelane --version | head -1; yosys -V; echo "OpenROAD $(openroad -version)"; echo "magic $(magic --version)"; klayout -v; ciel --version | head -1'
ENV_DIR="${BONETTO_TT_ENV:-$REPO/build/tt-env}"
if [ -d "$LL_HOME/lib" ]; then export LD_LIBRARY_PATH="$LL_HOME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"; fi
"$ENV_DIR/venv/bin/python" -c 'import cairosvg, klayout.db; print("tt venv: cairosvg and klayout.db import")'
"$ENV_DIR/precheck-venv/bin/python" -c 'import klayout.db, gdstk; print("precheck venv: klayout.db and gdstk import")'
docker info | head -3
log "done. PDK $(cat "$VDIR/sky130A/SOURCES" | tr '\n' ' ')"
