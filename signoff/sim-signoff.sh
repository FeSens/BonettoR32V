#!/usr/bin/env bash
# On hwe-bench mirror: programs, cosim at 12 / 6.25 / 5 MHz, then the soak.
set -u
make -s -C verif/programs -j8 > build/progs.log 2>&1 || { echo "programs build failed"; exit 2; }
for ns in 83.333 160 200; do
  python3 verif/cosim/run_cosim.py --all --no-make --jobs 8 --clk-ns $ns > build/clk$ns.log 2>&1
  echo "clk $ns rc=$? $(tail -1 build/clk$ns.log | cut -c1-200)"
  cp build/cosim.json build/cosim-clk$ns.json 2>/dev/null
done
JOBS=12 signoff/soak.sh > build/soak.log 2>&1; echo "soak rc=$? $(tail -1 build/soak/summary.txt 2>/dev/null; head -1 build/soak/summary.txt)"
