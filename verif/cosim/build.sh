#!/usr/bin/env bash
# Build the cosim harness against a design.
#
#   verif/cosim/build.sh MODE [SRC_DIR] [OUT_DIR]
#
# MODE is rvfi (design built with RISCV_FORMAL, RVFI compared every retirement) or pins (the plain
# design). SRC_DIR defaults to src/ and is globbed (*.v, *.sv, minus test_* files), since a design
# may add, rename or split files. OUT_DIR defaults to build/cosim/MODE. TRACE=1 adds FST tracing
# (harness --trace PATH).
#
# Verilator runs with --x-initial unique and --x-assign unique; the harness randomizes every
# power-up value at run time from its --seed, so a design that relies on zero-initialized state
# fails.
set -euo pipefail

MODE="${1:?usage: build.sh rvfi|pins [SRC_DIR] [OUT_DIR]}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COSIM="$ROOT/verif/cosim"
SRC="$(cd "${2:-$ROOT/src}" && pwd)"
OUT="${3:-$ROOT/build/cosim/$MODE}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

FILES=()
for f in "$SRC"/*.v "$SRC"/*.sv; do
  [ -e "$f" ] || continue
  case "$(basename "$f")" in test_*) continue ;; esac
  FILES+=("$f")
done
if [ ${#FILES[@]} -eq 0 ]; then
  echo "build.sh: no Verilog sources in $SRC" >&2
  exit 1
fi

DEFS=()
CDEFS=""
case "$MODE" in
  rvfi)
    DEFS=(+define+RISCV_FORMAL +define+RISCV_FORMAL_NRET=1 +define+RISCV_FORMAL_XLEN=32
          +define+RISCV_FORMAL_ILEN=32 "+incdir+$ROOT/third_party/riscv-formal/checks"
          "$ROOT/third_party/riscv-formal/checks/rvfi_macros.vh")
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

verilator --cc --exe --build -j 0 -O3 \
  --x-assign unique --x-initial unique \
  -Wno-fatal -Wno-lint -Wno-style -Wno-DEFOVERRIDE \
  --top-module tt_um_fesens_r32v \
  "+incdir+$SRC" \
  ${DEFS[@]+"${DEFS[@]}"} \
  ${TRACE_ARGS[@]+"${TRACE_ARGS[@]}"} \
  "${FILES[@]}" \
  "$COSIM/harness.cpp" \
  -CFLAGS "-O2 -std=c++17 -I$COSIM $CDEFS" \
  -Mdir "$OUT/obj" -o "$OUT/harness" >"$OUT/build.log" 2>&1 || {
    tail -40 "$OUT/build.log" >&2
    exit 1
  }
echo "built $OUT/harness ($MODE, ${#FILES[@]} source files)"
