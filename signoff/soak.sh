#!/usr/bin/env bash
# Sign-off soak (run on hwe-bench: tools/hwe_run.sh --tag soak --pull build/soak -- signoff/soak.sh).
# Far more random programs than the cosim gate's six, both modes, the four power-up seeds, at the
# signoff clock, plus many more liveness fuzz seeds. Results: build/soak/{runs.jsonl,summary.txt}.
#   SHORT seeds: RANDOM_N = 3000 instructions (the gate's size); LONG seeds: 12000 (code must stay below the .fwindow block at 64 KiB).
set -uo pipefail
cd "$(dirname "$0")/.."
SHORT=${SHORT:-$(seq 101 400)}
LONG=${LONG:-$(seq 1001 1050)}
FUZZ=${FUZZ:-$(seq 6 55)}
JOBS=${JOBS:-16}
CLK=$(python3 -c 'import json; print(json.load(open("src/config.json"))["CLOCK_PERIOD"])')
OUT=build/soak
rm -rf $OUT && mkdir -p $OUT
make -s -C verif/programs -j$JOBS $(for s in $SHORT; do echo random_$s; done) RANDOM_SEEDS="$SHORT" > $OUT/build_short.log 2>&1 || { echo "build failed (short)"; exit 2; }
make -s -C verif/programs -j$JOBS $(for s in $LONG; do echo random_$s; done) RANDOM_SEEDS="$LONG" RANDOM_N=12000 > $OUT/build_long.log 2>&1 || { echo "build failed (long)"; exit 2; }
{
  for s in $SHORT $LONG; do for m in rvfi pins; do for seed in 1 2 3 4; do
    echo "--mode $m --prog build/programs/random_$s.hex --seed $seed --clk-ns $CLK"
  done; done; done
  for f in $FUZZ; do echo "--fuzz-liveness --seed $f --clk-ns $CLK"; done
} > $OUT/jobs.txt
# Build the harnesses once, serially, so the parallel jobs don't race on the Verilator builds.
first=$(echo $SHORT | cut -d' ' -f1)
for m in rvfi pins; do
  python3 verif/cosim/run_cosim.py --mode $m --prog build/programs/random_$first.hex --seed 1 --clk-ns $CLK > $OUT/warm_$m.log 2>&1
done
python3 verif/cosim/run_cosim.py --fuzz-liveness --seed 1 --fuzz-runs 1 --clk-ns $CLK > $OUT/warm_fuzz.log 2>&1
# Each job prints one JSON line (run_cosim.py's summary); a failure exits nonzero.
xargs -P $JOBS -L 1 sh -c 'python3 verif/cosim/run_cosim.py "$@" 2>&1 | tail -1' _ < $OUT/jobs.txt > $OUT/runs.jsonl
python3 - "$OUT" <<'EOF'
import json, sys
out = sys.argv[1]
n = ok = 0
bad = []
cycles = 0
for line in open(f"{out}/runs.jsonl"):
    try:
        d = json.loads(line)
    except ValueError:
        bad.append(line.strip()[:200]); n += 1; continue
    n += 1
    if d.get("pass"):
        ok += 1
        cycles += d.get("cycles") or 0
    else:
        bad.append(f"{d.get('prog')} {d.get('mode')} seed {d.get('seed')}: {(d.get('error') or '')[:160]}")
jobs = sum(1 for _ in open(f"{out}/jobs.txt"))
s = f"soak: {ok}/{jobs} runs pass ({n} reported), {cycles:,} simulated cycles in passing runs\n"
s += "".join(f"FAIL {b}\n" for b in bad[:50])
open(f"{out}/summary.txt", "w").write(s)
print(s, end="")
sys.exit(0 if ok == jobs == n else 1)
EOF
