#!/usr/bin/env bash
# On hwe-bench: for each point, read the global-routing overflow once it exists; stop points over
# LIMIT (default 400) through their recorded pid (the container's process group, as sweep_stop.sh).
# Prints NAME STATE OVERFLOW. Never matches processes by name.
cd "$(dirname "$0")/../.."
LIMIT=${LIMIT:-400}
for pidf in build/sw-*.pid; do
  n=${pidf#build/sw-}; n=${n%.pid}
  R=build/harden-$n/proj/runs/wokwi
  f=$(ls $R/*-openroad-globalrouting/*.log 2>/dev/null | head -1)
  ov=$(grep -A10 "Final congestion" "$f" 2>/dev/null | awk '/^Total/{print $NF; exit}')
  if [ -f build/sw-$n.rc ]; then echo "$n done rc=$(cat build/sw-$n.rc) overflow=${ov:-?}"; continue; fi
  if [ -z "$ov" ]; then echo "$n running overflow=?"; continue; fi
  if [ "$ov" -gt "$LIMIT" ] && [ ! -f build/sw-$n.stopped ]; then
    root=$(cat $pidf); todo=($root); shim=""
    while [ ${#todo[@]} -gt 0 ]; do p=${todo[0]}; todo=("${todo[@]:1}")
      if tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q "hwe_docker_shim.py run"; then shim=$p; break; fi
      for c in $(cat /proc/$p/task/$p/children 2>/dev/null); do todo+=($c); done; done
    if [ -n "$shim" ]; then pg=$(ps -o pgid= -p $shim | tr -d ' '); kill -TERM -- -$pg; touch build/sw-$n.stopped; echo "$n STOPPED overflow=$ov (pgid $pg)"; else echo "$n overflow=$ov, no container"; fi
  else echo "$n running overflow=$ov"; fi
done
