#!/usr/bin/env bash
# st.sh: status of the full-run (d) points
cd "$(dirname "$0")/../.."
O=signoff/sweep-114
for k in build/sw-*.kind; do n=${k#build/sw-}; n=${n%.kind}; [ "$(cut -d' ' -f1 $k)" = d ] || continue
  R=build/harden-$n/proj/runs/wokwi
  ov=$(grep -A10 "Final congestion" $R/*-openroad-globalrouting/*.log 2>/dev/null | awk '/^Total/{print $NF}')
  hb=$(grep -h "hold buffers" $R/*-openroad-resizertimingpostcts/*.log 2>/dev/null | awk '{print $(NF-2)}')
  l=$(ls $R/*-openroad-detailedrouting/openroad-detailedrouting.log 2>/dev/null | head -1)
  p=""; [ -n "$l" ] && p=$(bash $O/drtprog.sh $l | tr ' ' '\n' | grep : | sed 's/(.*//' | tail -6 | paste -sd' ')
  [ -z "$l" ] && [ -f build/sw-$n.drt.log ] && p=$(bash $O/drtprog.sh build/sw-$n.drt.log | tr ' ' '\n' | grep : | sed 's/(.*//' | tail -4 | paste -sd' ')
  st=running; [ -f build/sw-$n.rc ] && st="rc=$(cat build/sw-$n.rc)"; [ -f build/sw-$n.stopped ] && st="$st stopped($(cat build/sw-$n.stopped))"
  last=$(ls -d $R/*-* 2>/dev/null | tail -1 | xargs -n1 basename 2>/dev/null)
  echo "$n ov=$ov hold=$hb $st step=$last | $p"
done
