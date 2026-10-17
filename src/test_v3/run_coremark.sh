#!/bin/sh
# CoreMark on the seed (rvfi build: ISS lockstep), ITERATIONS = 1 and 2 in parallel; the
# difference of the two cycle counts is the cycles per iteration.
#   src/test_v3/run_coremark.sh [seed]
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
TB=$ROOT/build/test_v3
P=$ROOT/build/programs/test_v3
S=${1:-1}
"$TB/harness_rvfi" "$P/coremark1.bin" --seed "$S" --max-cycles 4000000000 > "$TB/coremark1.json" 2>/dev/null &
"$TB/harness_rvfi" "$P/coremark2.bin" --seed "$S" --max-cycles 4000000000 > "$TB/coremark2.json" 2>/dev/null &
wait
cat "$TB/coremark1.json" "$TB/coremark2.json"
