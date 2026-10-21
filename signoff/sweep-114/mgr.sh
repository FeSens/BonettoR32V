#!/usr/bin/env bash
# mgr.sh: sweep manager on hwe-bench (the 113 manager, signoff/sweep-113/mgr.sh, adapted for 114).
# Run from the mirror: setsid nohup bash signoff/sweep-114/mgr.sh > build/mgr.out 2>&1 < /dev/null &
# Queues (one point per line, '#' comments):
#   squeue.txt  NAME              screen: run to global routing, record overflow, stop
#   dqueue.txt  NAME TIMEOUT CURVE   full run; CURVE "it:max,it:max,..." (or "-"): stop when the
#               fewest violations so far, once DRT iteration >= it, exceed max (largest such it)
# Overlay: signoff/sweep-114/NAME.json (merged over src/config.json, which already holds c1).
# Records: signoff/sweep-114/results.txt. Stop: touch signoff/sweep-114/mgr.stop.
# Never matches processes by name: every stop goes through a point's own pid file (stoppt.sh).
cd "$(dirname "$0")/../.."
O=signoff/sweep-114
export PATH=$HOME/bonetto-venv/bin:/opt/hwe-toolchain/oss-cad-suite/bin:$PATH
export BONETTO_HWE_HEAVY_SLOTS=9
mkdir -p build
echo $$ > build/mgr.pid
rdir() { echo build/harden-$1/proj/runs/wokwi; }
grtov() { f=$(ls $(rdir $1)/*-openroad-globalrouting/*.log 2>/dev/null | head -1); grep -A10 "Final congestion" "$f" 2>/dev/null | awk '/^Total/{print $NF; exit}'; }
grtwl() { f=$(ls $(rdir $1)/*-openroad-globalrouting/*.log 2>/dev/null | head -1); grep -m1 "Total wirelength" "$f" 2>/dev/null | awk '{print $(NF-1)}'; }
grtlay() { f=$(ls $(rdir $1)/*-openroad-globalrouting/*.log 2>/dev/null | head -1); grep -A8 "Final congestion" "$f" 2>/dev/null | awk '/^met[1-4] /{printf "%s:%s/%s,", $1, $3, $2}' | sed 's/,$//'; }
holdb() { grep -h "hold buffers" $(rdir $1)/*-openroad-resizertimingpostcts/*.log 2>/dev/null | awk '{print $(NF-2)}' | tail -1; }
drtlog() { ls $(rdir $1)/*-openroad-detailedrouting/openroad-detailedrouting.log 2>/dev/null | head -1; }
drtlast() { l=$(drtlog $1); [ -n "$l" ] && bash $O/drtprog.sh "$l" | tr ' ' '\n' | grep -v '^$' | grep -v DONE | tail -1; }
drtbest() { l=$(drtlog $1); bash $O/drtprog.sh "$l" | tr ' ' '\n' | grep : | sed 's/^[0-9]*://; s/(.*//' | sort -n | head -1; }
drtcurve() { l=$(drtlog $1); [ -z "$l" ] && [ -f build/sw-$1.drt.log ] && l=build/sw-$1.drt.log; [ -n "$l" ] && bash $O/drtprog.sh "$l" | tr ' ' '\n' | grep : | sed 's/^[0-9]*://; s/(.*//' | paste -sd, -; }
summ() { f=build/harden-$1/summary.json; [ -f $f ] || return
  python3 -I -c 'import json,sys; s=json.load(open(sys.argv[1])); c=s.get("checks") or {}
print("status=%s setup=%s hold=%s util=%s hb=%s slew=%s cap=%s fails=%s" % (s.get("status"), s.get("setup_ws"), s.get("hold_ws"), s.get("utilization"), s.get("hold_buffers"), s.get("max_slew_violations"), s.get("max_cap_violations"), ",".join(k for k,v in c.items() if v is not True) or "-"))' $f 2>/dev/null; }
launch() { n=$1; to=$2
  rm -f build/sw-$n.rc build/sw-$n.stopped
  setsid nohup bash -c "tools/harden_tt.sh --overlay $O/$n.json --timeout $to --out build/harden-$n > build/sw-$n.log 2>&1; echo \$? > build/sw-$n.rc" < /dev/null > /dev/null 2>&1 &
  echo $! > build/sw-$n.pid; echo "$(date +%T) launch $n $(cat $O/$n.json)" >> $O/mgr.log; }
record() { n=$1; kind=$2
  [ -f build/sw-$n.recorded ] && return
  extra=""; [ "$kind" = d ] && extra="curve=$(drtcurve $n) $(summ $n)"
  echo "$n kind=$kind rc=$(cat build/sw-$n.rc) ov=$(grtov $n) wl=$(grtwl $n) lay=$(grtlay $n) hold=$(holdb $n) drt=$(drtlast $n) why=$(cat build/sw-$n.stopped 2>/dev/null) $extra $(cat $O/$n.json)" >> $O/results.txt
  touch build/sw-$n.recorded
  if [ "$kind" = s ]; then rm -rf build/harden-$n; fi
  if [ "$kind" = d ] && [ -f build/sw-$n.stopped ]; then l=$(drtlog $n); [ -n "$l" ] && cp "$l" build/sw-$n.drt.log; rm -rf build/harden-$n; fi; }
pop() { q=$O/$1; line=$(grep -v '^#' $q 2>/dev/null | grep -v '^ *$' | head -1); [ -z "$line" ] && return 1
  grep -vxF "$line" $q > $q.tmp; mv $q.tmp $q; echo "$line"; }
while true; do
  [ -f $O/mgr.stop ] && { echo "$(date +%T) mgr stop" >> $O/mgr.log; exit 0; }
  ns=0; nd=0; MAXD=$(cat $O/maxd 2>/dev/null || echo 3); MAXS=$(cat $O/maxs 2>/dev/null || echo 4)
  for k in build/sw-*.kind; do [ -f "$k" ] || continue
    n=${k#build/sw-}; n=${n%.kind}; kind=$(cut -d' ' -f1 $k)
    if [ -f build/sw-$n.rc ]; then record $n $kind; continue; fi
    if [ "$kind" = s ]; then ns=$((ns+1)); ov=$(grtov $n)
      [ -n "$ov" ] && [ ! -f build/sw-$n.stopped ] && bash $O/stoppt.sh $n "screened ov=$ov" >> $O/mgr.log
    else nd=$((nd+1)); curve=$(cut -d' ' -f2 $k); [ "$curve" = "-" ] && continue
      [ -f build/sw-$n.stopped ] && continue
      last=$(drtlast $n); [ -z "$last" ] && continue
      it=${last%%:*}; v=$(drtbest $n); lim=""
      for c in ${curve//,/ }; do ci=${c%%:*}; [ "$it" -ge "$ci" ] && lim=${c#*:}; done
      if [ -n "$lim" ] && [ "$v" -gt "$lim" ]; then bash $O/stoppt.sh $n "drt it$it v$v>$lim" >> $O/mgr.log; fi
    fi
  done
  mem=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
  if [ "$mem" -ge 12 ]; then
    if [ $nd -lt $MAXD ] && line=$(pop dqueue.txt); then set -- $line; echo "d ${3:--}" > build/sw-$1.kind; rm -f build/sw-$1.recorded; launch $1 ${2:-14400}
    elif [ $ns -lt $MAXS ] && line=$(pop squeue.txt); then set -- $line; echo "s -" > build/sw-$1.kind; rm -f build/sw-$1.recorded; launch $1 3600; fi
  fi
  sleep 30
done
