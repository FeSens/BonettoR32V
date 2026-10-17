#!/usr/bin/env bash
# Build the cosim harness against a design.
#
#   verif/cosim/build.sh MODE [SRC_DIR] [OUT_DIR]
#   verif/cosim/build.sh
#
# With no arguments it builds the standalone C++ pieces instead (no design involved):
#   build/cosim/test_pmod    unit tests of the QSPI Pmod chip models (pmod.h; test_pmod.py runs it)
#   build/cosim/libpmod.so   the Pmod models behind a C API (pmod_capi.cpp), for ctypes
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

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COSIM="$ROOT/verif/cosim"

if [ $# -eq 0 ]; then
  OUT="$ROOT/build/cosim"
  mkdir -p "$OUT"
  CXX="${CXX:-c++}"
  CXXFLAGS=(-O2 -std=c++17 -Wall -Wextra -I"$COSIM")
  "$CXX" "${CXXFLAGS[@]}" "$COSIM/test_pmod.cpp" -o "$OUT/test_pmod"
  "$CXX" "${CXXFLAGS[@]}" -fPIC -shared "$COSIM/pmod_capi.cpp" -o "$OUT/libpmod.so"
  echo "built $OUT/test_pmod $OUT/libpmod.so"
  exit 0
fi

MODE="$1"
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
