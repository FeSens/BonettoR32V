#!/bin/sh
# Run every program in both modes under several seeds.
#   src/test_bitserial/run_all.sh <harness_dir> <programs_dir> [seeds...]
TB=$1
PROGS=$2
shift 2
SEEDS=${*:-1 2 3}
fails=0
total=0
for hex in "$PROGS"/*.hex; do
    for mode in rvfi pins; do
        for s in $SEEDS; do
            total=$((total + 1))
            out=$("$TB/harness_$mode" "$hex" --seed "$s" --quiet 2>/dev/null)
            case "$out" in
                *'"pass":true'*) ;;
                *) fails=$((fails + 1)); echo "FAIL $out" ;;
            esac
        done
    done
    echo "$(basename "$hex"): done"
done
echo "runs: $total, failures: $fails"
[ "$fails" = 0 ]
