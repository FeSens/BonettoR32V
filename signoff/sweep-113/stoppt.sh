#!/usr/bin/env bash
# stoppt.sh NAME WHY: stop point NAME through its recorded pid (the docker shim's process group
# under it, as watch.sh does). Never matches processes by name pattern.
cd "$(dirname "$0")/../.."
n=$1; why=${2:-manual}
pidf=build/sw-$n.pid
[ -f "$pidf" ] || { echo "$n: no pid file"; exit 1; }
[ -f build/sw-$n.rc ] && { echo "$n: already done"; exit 0; }
root=$(cat $pidf); todo=($root); shim=""
while [ ${#todo[@]} -gt 0 ]; do p=${todo[0]}; todo=("${todo[@]:1}")
  if tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q "hwe_docker_shim.py run"; then shim=$p; break; fi
  for c in $(cat /proc/$p/task/$p/children 2>/dev/null); do todo+=($c); done; done
if [ -n "$shim" ]; then
  pg=$(ps -o pgid= -p $shim | tr -d ' ')
  kill -TERM -- -$pg && echo "$why" > build/sw-$n.stopped && echo "$n STOPPED ($why) pgid $pg"
else echo "$n: no container under pid $root"; fi
