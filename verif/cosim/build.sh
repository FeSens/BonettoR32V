#!/usr/bin/env bash
# Build the cosim harness against a design.
#
#   verif/cosim/build.sh MODE [SRC_DIR] [OUT_DIR]
#   verif/cosim/build.sh            (no MODE: only the support tools, see below)
#
# MODE is rvfi (design built with RISCV_FORMAL, RVFI compared every retirement) or pins (the plain
# design). SRC_DIR defaults to src/. Every SRC_DIR/*.v file is part of the design (contract
# section 1) and they are read in sorted (C locale) order, like the gates. The RVFI build gets
# exactly one define, RISCV_FORMAL, and nothing is read in front of the design: no prelude, no
# include path. OUT_DIR defaults to build/cosim/MODE. TRACE=1 adds FST tracing (harness --trace
# PATH). VFLAGS adds Verilator arguments; only the harness self-tests use it, on mutant copies.
#
# Verilator runs with --x-initial unique and --x-assign unique; the harness randomizes every
# power-up value at run time from its --seed, so a design that relies on zero-initialized state
# fails.
set -euo pipefail

# Support tools (no MODE argument), built without a design:
#   build/cosim/iss_main   the reference ISS runner (v2 by default, contract v3 with --v3)
if [ $# -eq 0 ]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  mkdir -p "$ROOT/build/cosim"
  "${CXX:-c++}" -std=c++17 -O2 -Wall -Wextra -I"$ROOT/verif/cosim" \
    -o "$ROOT/build/cosim/iss_main" "$ROOT/verif/cosim/iss_main.cpp"
  echo "built $ROOT/build/cosim/iss_main"
  exit 0
fi

MODE="${1:?usage: build.sh rvfi|pins [SRC_DIR] [OUT_DIR]}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COSIM="$ROOT/verif/cosim"
SRC="$(cd "${2:-$ROOT/src}" && pwd)"
OUT="${3:-$ROOT/build/cosim/$MODE}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

export LC_ALL=C  # glob order = byte order
FILES=()
for f in "$SRC"/*.v; do
  [ -f "$f" ] && FILES+=("$f")
done
if [ ${#FILES[@]} -eq 0 ]; then
  echo "build.sh: no Verilog sources in $SRC" >&2
  exit 1
fi

DEFS=()
CDEFS=""
case "$MODE" in
  rvfi)
    DEFS=(+define+RISCV_FORMAL)
    CDEFS="-DRVFI_MODE"
    ;;
  pins) ;;
  *) echo "build.sh: MODE must be rvfi or pins" >&2; exit 1 ;;
esac

# VFLAGS: extra Verilator arguments, used by the harness self-tests (test_cosim_harness.py)
read -r -a EXTRA <<<"${VFLAGS:-}"
DEFS+=(${EXTRA[@]+"${EXTRA[@]}"})

TRACE_ARGS=()
if [ "${TRACE:-0}" = "1" ]; then
  TRACE_ARGS=(--trace-fst)
  CDEFS="$CDEFS -DHARNESS_TRACE"
fi

ARGS=(--cc --exe --build -j 0 -O3
  --x-assign unique --x-initial unique
  -Wno-fatal -Wno-lint -Wno-style
  --top-module tt_um_fesens_r32v
  ${DEFS[@]+"${DEFS[@]}"}
  ${TRACE_ARGS[@]+"${TRACE_ARGS[@]}"}
  "${FILES[@]}"
  "$COSIM/harness.cpp"
  -CFLAGS "-O2 -std=c++17 -I$COSIM $CDEFS"
  -Mdir "$OUT/obj" -o "$OUT/harness")
# the exact command, one argument per line (test_cosim_harness.py checks the design inputs)
printf '%s\n' verilator "${ARGS[@]}" >"$OUT/verilator.args"
verilator "${ARGS[@]}" >"$OUT/build.log" 2>&1 || {
  tail -40 "$OUT/build.log" >&2
  exit 1
}
echo "built $OUT/harness ($MODE, ${#FILES[@]} source files)"
