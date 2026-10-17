#!/bin/sh
# Build the seed unit-test harness (run on hwe-bench via tools/hwe_run.sh).
#   src/test_v3/build.sh [trace]   -> build/test_v3/harness_rvfi, harness_pins (or harness_trace)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/.." && pwd)
OUT=$(cd "$HERE/../.." && pwd)/build/test_v3
mkdir -p "$OUT"
MODES="rvfi pins"
[ "$1" = trace ] && MODES="trace"
for mode in $MODES; do
    DEF=""
    EXTRA=""
    [ "$mode" = rvfi ] && DEF="-DRISCV_FORMAL"
    [ "$mode" = trace ] && DEF="-DRISCV_FORMAL" && EXTRA="--trace -CFLAGS -DVCD_TRACE"
    verilator --cc --exe --build -j 16 -O3 --x-assign unique --x-initial unique \
        -Wno-fatal $DEF $EXTRA -CFLAGS "-O2 -std=c++17 -I$HERE $DEF" \
        --Mdir "$OUT/obj_$mode" -o "$OUT/harness_$mode" \
        --top-module tt_um_fesens_r32v "$SRC"/*.v "$HERE/harness.cpp" >"$OUT/build_$mode.log" 2>&1 \
        || { tail -30 "$OUT/build_$mode.log"; exit 1; }
done
echo "built $MODES in $OUT"
