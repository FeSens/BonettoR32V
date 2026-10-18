#!/bin/sh
# Build and run the SPI master unit test (tb_spi.cpp) on r32v_periph alone, seeds 1-4.
# Run on hwe-bench: tools/hwe_run.sh --tag <tag> -- sh src/test_spi/run.sh
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
OUT=$ROOT/build/test_spi
mkdir -p "$OUT"
verilator --cc --exe --build -j 8 -O2 --x-assign unique --x-initial unique -Wno-fatal \
    --top-module r32v_periph --Mdir "$OUT/obj" -o "$OUT/tb_spi" \
    "$ROOT/src/r32v_periph.v" "$HERE/tb_spi.cpp" > "$OUT/build.log" 2>&1 \
    || { tail -30 "$OUT/build.log"; exit 1; }
fails=0
for s in 1 2 3 4; do
    "$OUT/tb_spi" "$s" || fails=$((fails + 1))
done
echo "seeds failed: $fails"
[ "$fails" = 0 ]
