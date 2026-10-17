#!/bin/sh
# Run the seed unit tests (on hwe-bench via tools/hwe_run.sh, after build.sh; the programs come
# from build_progs.sh on the Mac). Each line: test name, program, harness options. Every test
# runs in both builds (rvfi: ISS lockstep; pins: plain build) under seeds 1-4, which cover the
# four Pmod power-up modes. Prints one JSON line per run and a summary; exit 1 on any failure.
#   src/test_v3/run_all.sh [filter]
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
TB=$ROOT/build/test_v3
P=$ROOT/build/programs/test_v3
FILTER=${1:-}
SEEDS=${SEEDS:-1 2 3 4}
fails=0
total=0
run() {
    name=$1
    prog=$2
    shift 2
    case "$name" in *$FILTER*) ;; *) return ;; esac
    for mode in rvfi pins; do
        for s in $SEEDS; do
            total=$((total + 1))
            out=$("$TB/harness_$mode" "$P/$prog.bin" --seed "$s" "$@" 2>/dev/null | tail -1)
            case "$out" in
                *'"pass":true'*) echo "PASS $name $mode $out" ;;
                *) fails=$((fails + 1)); echo "FAIL $name $mode $out" ;;
            esac
        done
    done
}
# boot_from_each_mode
run boot boot
# csr_trap_irq
run csr csr
for t in ecall ebreak jal jalr branch zero ones; do run "trap_$t" "trap_$t" --expect-entries 1; done
# rvfi_intr = 1 on every handler's first instruction: 7 synchronous traps + 1 interrupt
run trap_chain trap_chain --expect-entries 8
run irq_timer irq_timer
run irq_gpio irq_gpio --loop
run irq_latency irq_latency
# periph_regs and the peripheral pin behavior
run periph_regs periph_regs --loop
run uart_loop uart_loop --loop
run uart_echo_p1 uart_echo --uart-div 39 --uart-rx 55aa0001ff7e8133 --uart-rx-start 60000 --uart-rx-gap 30000 --uart-ppm 10000 --expect-tx 55aa0001ff7e8133
run uart_echo_m1 uart_echo --uart-div 39 --uart-rx 0f1e2d3c4b5a6978 --uart-rx-start 60000 --uart-rx-gap 30000 --uart-ppm -10000 --expect-tx 0f1e2d3c4b5a6978
run spi_loop spi_loop --loop
run capture capture --loop
run pwm pwm --pwm 1:99:1:0,2:99:1:50,3:99:1:120,7:65535:0:300
# core datapath: random streams (the EBREAK at the end traps; rvfi mode ends there)
for s in 1 2 3 4; do
    case "random_$s" in *$FILTER*) ;; *) continue ;; esac
    for seed in $SEEDS; do
        total=$((total + 1))
        out=$("$TB/harness_rvfi" "$P/random_$s.bin" --seed "$seed" --end-ebreak 2>/dev/null | tail -1)
        case "$out" in
            *'"pass":true'*) echo "PASS random_$s rvfi $out" ;;
            *) fails=$((fails + 1)); echo "FAIL random_$s rvfi $out" ;;
        esac
    done
done
# liveness fuzz: random flash images (unspecified encodings included), Pmod rules and the cap
for seed in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do
    case "fuzz" in *$FILTER*) ;; *) continue ;; esac
    total=$((total + 1))
    out=$("$TB/harness_rvfi" --fuzz 400000 --seed "$seed" 2>/dev/null | tail -1)
    case "$out" in
        *'"pass":true'*) echo "PASS fuzz rvfi $out" ;;
        *) fails=$((fails + 1)); echo "FAIL fuzz rvfi $out" ;;
    esac
done
echo "runs: $total, failures: $fails"
[ "$fails" = 0 ]
