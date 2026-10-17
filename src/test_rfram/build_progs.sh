#!/bin/sh
# Build the cosim test programs for the register-file-in-RAM core (hypothesis 002).
#   src/test_rfram/build_progs.sh <verif_programs_dir> <out_dir>
# <verif_programs_dir> is verif/programs from the setup/cosim branch (read only). The programs
# are copied and patched so they never touch the register region 0xFFC0..0xFFFF:
#   - link.ld: the stack starts at 0xFFC0 instead of 0x10000;
#   - selftest.S: section 6 (misaligned accesses that wrap at 64 KB through 0xFFFC..0xFFFF) is
#     left out, as in the spike cross-check build;
#   - gen_random.py: RAM accesses stay below 0xFFC0 (no stores at 0xFFF0..0xFFFF, no wraps).
set -e
SRCP=$(cd "$1" && pwd)
OUT=$2
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT/p"
W="$OUT/p"
cp -R "$SRCP"/. "$W"/
python3 "$HERE/patch_progs.py" "$W"
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
for s in 1 2 3 4 5 6; do
    python3 gen_random.py $s 3000 -o "$OUT/random_$s.S"
    $CC $ARCH -I$W $LDF "$OUT/random_$s.S" -o "$OUT/random_$s.elf"
done
for e in "$OUT"/*.elf; do
    python3 elf2hex.py "$e" "${e%.elf}.hex"
done
echo "built $(ls "$OUT"/*.hex | wc -l | tr -d ' ') programs in $OUT"
