#!/usr/bin/env bash
# drtprog.sh LOG: one entry per DRT iteration: iter:violations(elapsed of that iteration)
awk '
/Start detail routing/ {it=0}
/DRT-0195\] Start [0-9]+(st|nd|rd|th) / {match($0,/Start [0-9]+/); it=substr($0,RSTART+6,RLENGTH-6)+0}
/Number of violations =/ {v=$NF; sub(/\./,"",v)}
/DRT-0267/ && v!="" {match($0,/elapsed time = [0-9:]+/); e=substr($0,RSTART+15,RLENGTH-15); printf "%s:%s(%s) ", it, v, e; v=""}
/DRT-0198/ {print ""; print "DONE"}
END {print ""}' "$1"
