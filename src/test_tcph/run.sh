#!/bin/sh
# Build and run the pin timing probe (hypothesis 113) on hwe-bench through tools/hwe_run.sh:
#   tools/hwe_run.sh -- sh src/test_tcph/run.sh [prog ...]
# Programs come from build/programs (make -C verif/programs). Each run prints the probe's lines.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
SRC=${SRC:-$ROOT/src}
OUT=${OUT:-$ROOT/build/test_tcph}
mkdir -p "$OUT"
verilator --cc --exe --build -j 8 -O3 --x-assign unique --x-initial unique -Wno-fatal \
    -Wno-lint -Wno-style --top-module tt_um_fesens_r32v \
    "$SRC"/*.v "$ROOT"/verif/cells/*.v "$ROOT"/verif/cosim/noinline.vlt "$HERE/tb.cpp" \
    -CFLAGS "-O2 -std=c++17 -I$ROOT/verif/cosim" --Mdir "$OUT/obj" -o "$OUT/tb" \
    >"$OUT/build.log" 2>&1 || { tail -30 "$OUT/build.log"; exit 1; }
CYCLES=${CYCLES:-3000000}
for p in ${@:-selftest coherence csr coremark}; do
    for s in ${SEEDS:-1 2 3 4}; do
        "$OUT/tb" "$ROOT/build/programs/$p.hex" --seed "$s" --cycles "$CYCLES" || true
    done
done
