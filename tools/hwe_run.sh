#!/usr/bin/env bash
# BonettoR32V: run a heavy command (Verilator builds, cosim, yosys, cocotb) on hwe-bench instead of
# the Mac, which has run out of memory with several agents compiling at once.
#
# Usage (from the root of any checkout or worktree of this repo):
#   tools/hwe_run.sh [--pull PATH]... [--tag NAME] [--third-party] -- <command...>
#
# - Mirrors the working tree (tracked and untracked files, without .git, .claude/, .superpowers/,
#   third_party/ unless --third-party (774 MB; needed by make arch), and build/ except
#   build/programs/) to hwe-bench:~/bonetto-work/<tag>/. <tag> defaults to the checkout's directory
#   name, so each worktree gets its own mirror and its own remote build/ cache.
# - Runs <command...> there with ~/bonetto-venv (pytest, cocotb, the vvp/iverilog shims for
#   cocotb and the LibreLane docker shim), ~/bonetto-tools (spike, dtc), the oss-cad-suite
#   (Verilator, iverilog, yosys, sby, eqy) and /opt/hwe-toolchain (xPack riscv32-unknown-elf-gcc
#   15.2.0, the Mac's release) on PATH.
# - Copies each --pull PATH (relative to the checkout, e.g. build/cosim.json) back into the local
#   checkout, then exits with the remote command's status.
#
# Programs can be built on either host (same xPack GCC release). Only one-off commands belong
# here; long formal runs use verif/formal's own remote runner.
set -euo pipefail

HOST="${BONETTO_HWE_HOST:-hwe-bench}"
pulls=()
tag=""
tp=(--exclude '/third_party/')
while [ $# -gt 0 ]; do
  case "$1" in
    --pull) pulls+=("$2"); shift 2 ;;
    --tag) tag="$2"; shift 2 ;;
    --third-party) tp=(); shift ;;
    --) shift; break ;;
    *) echo "usage: tools/hwe_run.sh [--pull PATH]... [--tag NAME] [--third-party] -- <command...>" >&2; exit 2 ;;
  esac
done
[ $# -gt 0 ] || { echo "hwe_run: no command given" >&2; exit 2; }

root="$(git rev-parse --show-toplevel)"
tag="${tag:-$(basename "$root")}"
case "$tag" in *[!A-Za-z0-9._-]*|"") echo "hwe_run: bad tag '$tag'" >&2; exit 2 ;; esac
remote="bonetto-work/$tag"

ssh -o ConnectTimeout=15 "$HOST" "mkdir -p ~/$remote"
rsync -a --delete \
  --exclude '.git' --exclude '/.claude/' --exclude '/.superpowers/' "${tp[@]+"${tp[@]}"}" \
  --include '/build/' --include '/build/programs/***' --exclude '/build/*' \
  --exclude '__pycache__' --exclude '/test/sim_build' \
  "$root/" "$HOST:$remote/"

# The mirror has no .git: pass the checkout's sha and dirty state for tools/eval.py's provenance.
sha="$(git -C "$root" rev-parse HEAD 2>/dev/null || true)"
dirty=""
if st="$(git -C "$root" status --porcelain --ignore-submodules=untracked 2>/dev/null)"; then
  if [ -n "$st" ]; then dirty=1; else dirty=0; fi
fi
cmd="$(printf '%q ' "$@")"
set +e
ssh -o ConnectTimeout=15 "$HOST" \
  "cd ~/$remote && export BONETTO_GIT_SHA=$sha BONETTO_GIT_DIRTY=$dirty PATH=\$HOME/bonetto-venv/bin:\$HOME/bonetto-tools/bin:/opt/hwe-toolchain/oss-cad-suite/bin:/opt/hwe-toolchain/bin:\$PATH && $cmd"
status=$?
set -e

for p in "${pulls[@]+"${pulls[@]}"}"; do
  mkdir -p "$root/$(dirname "$p")"
  rsync -a "$HOST:$remote/$p" "$root/$(dirname "$p")/" || echo "hwe_run: could not pull $p" >&2
done
exit $status
