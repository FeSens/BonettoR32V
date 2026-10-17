#!/bin/sh
# Build the unit-test harness in both modes (and a traced RVFI build for debugging).
#   src/test_rfram/build.sh <iss_include_dir> <out_dir> [trace]
# iss_include_dir holds iss.h (verif/cosim on the setup/cosim branch).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/.." && pwd)
ISS=$1
OUT=$2
mkdir -p "$OUT"
MODES="rvfi pins"
[ "$3" = trace ] && MODES="trace"
for mode in $MODES; do
    DEF=""
    EXTRA=""
    [ "$mode" = rvfi ] && DEF="-DRISCV_FORMAL"
    [ "$mode" = trace ] && DEF="-DRISCV_FORMAL" && EXTRA="--public-flat-rw -CFLAGS -DTRACE_PUBLIC"
    verilator --cc --exe --build -j 8 -O3 --x-assign unique --x-initial unique \
        -Wno-fatal $DEF $EXTRA -CFLAGS "-O2 -std=c++17 -I$ISS $DEF" \
        --Mdir "$OUT/obj_$mode" -o "$OUT/harness_$mode" \
        --top-module tt_um_fesens_r32v "$SRC"/*.v "$HERE/harness.cpp" >/dev/null
done
echo "built $MODES in $OUT"
