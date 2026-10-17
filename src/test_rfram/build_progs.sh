#!/bin/sh
# Build the cosim test programs for the register-file-and-pc-in-RAM core (hypotheses 002 and 005).
#   src/test_rfram/build_progs.sh <verif_programs_dir> <out_dir>
# <verif_programs_dir> is main's verif/programs (read only), which contract v2 already keeps out
# of the core region 0xFF80..0xFFFF (stack top 0xFF80). Adds the directed v2_*.S tests.
set -e
SRCP=$(cd "$1" && pwd)
OUT=$2
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT/p"
W="$OUT/p"
cp -R "$SRCP"/. "$W"/
CC=${CROSS:-riscv32-unknown-elf-}gcc
ARCH="-march=rv32e -mabi=ilp32e"
LDF="-nostdlib -nostartfiles -static -T $W/link.ld -Wl,--no-warn-rwx-segments"
CF="$ARCH -O2 -ffreestanding -fno-builtin -fno-tree-loop-distribute-patterns -fno-common -I$W -Wall"
CM="$W/coremark"
CMS="$CM/core_list_join.c $CM/core_main.c $CM/core_matrix.c $CM/core_state.c $CM/core_util.c $CM/bonetto/core_portme.c"
CMF="-I$CM -I$CM/bonetto -DITERATIONS=1 -DTOTAL_DATA_SIZE=2000 -DPERFORMANCE_RUN=1 -Wno-unused-but-set-variable"
cd "$W"
$CC $ARCH -I$W $LDF selftest.S -o "$OUT/selftest.elf"
$CC $CF $LDF crt0.S gpio_demo.c -lgcc -o "$OUT/gpio_demo.elf"
$CC $CF $CMF $LDF crt0.S $CMS -lgcc -o "$OUT/coremark.elf"
k=1
for t in ecall jal jalr branch zero ones; do
    $CC $ARCH -I$W -DTRAP_KIND=$k $LDF trap.S -o "$OUT/trap_$t.elf"
    k=$((k + 1))
done
for t in first_sw first_jalr first_btaken first_bnot first_auipc first_jal first_io; do
    $CC $ARCH -I$W $LDF "$HERE/v2_$t.S" -o "$OUT/v2_$t.elf"
done
for s in 1 2 3 4 5 6; do
    python3 gen_random.py $s 3000 -o "$OUT/random_$s.S"
    $CC $ARCH -I$W $LDF "$OUT/random_$s.S" -o "$OUT/random_$s.elf"
done
for e in "$OUT"/*.elf; do
    python3 elf2hex.py "$e" "${e%.elf}.hex"
done
echo "built $(ls "$OUT"/*.hex | wc -l | tr -d ' ') programs in $OUT"
