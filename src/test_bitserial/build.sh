#!/bin/sh
# Build the unit-test harness in both modes.
#   src/test_bitserial/build.sh <iss_include_dir> <out_dir>
# iss_include_dir holds iss.h (verif/cosim once the gates are on main).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/.." && pwd)
ISS=$1
OUT=$2
mkdir -p "$OUT"
for mode in rvfi pins; do
    DEF=""
    [ "$mode" = rvfi ] && DEF="-DRISCV_FORMAL"
    verilator --cc --exe --build -j 8 -O3 --x-assign unique --x-initial unique \
        -Wno-fatal $DEF -CFLAGS "-O2 -std=c++17 -I$ISS $DEF" \
        --Mdir "$OUT/obj_$mode" -o "$OUT/harness_$mode" \
        --top-module tt_um_fesens_r32v "$SRC/tt_um_fesens_r32v.v" "$HERE/harness.cpp" >/dev/null
done
echo "built $OUT/harness_rvfi $OUT/harness_pins"
