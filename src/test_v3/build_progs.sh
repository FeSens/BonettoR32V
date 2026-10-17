#!/bin/sh
# Build the seed unit-test programs (needs riscv32-unknown-elf-gcc; run on the Mac).
#   src/test_v3/build_progs.sh          -> build/programs/test_v3/<name>.{elf,bin}
# <name>.bin is the flash image from address 0 (objcopy -O binary: .data's load image included).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
P=$HERE/prog
OUT=$ROOT/build/programs/test_v3
mkdir -p "$OUT"
CC=${CROSS:-riscv32-unknown-elf-}gcc
OBJCOPY=${CROSS:-riscv32-unknown-elf-}objcopy
ARCH="-march=rv32e_zicsr -mabi=ilp32e"
LDF="-nostdlib -nostartfiles -static -Wl,--no-warn-rwx-segments"
CF="$ARCH -Os -ffreestanding -fno-builtin -fno-tree-loop-distribute-patterns -fno-common -I$P -Wall"
cd "$P"
for s in boot csr irq_latency trap_chain; do
    $CC $ARCH $LDF -T link.ld $s.S -o "$OUT/$s.elf"
done
k=1
for t in ecall ebreak jal jalr branch zero ones; do
    $CC $ARCH $LDF -T link.ld -DTRAP_KIND=$k trap.S -o "$OUT/trap_$t.elf"
    k=$((k + 1))
done
for c in irq_timer periph_regs uart_loop uart_echo spi_loop capture pwm irq_gpio; do
    $CC $CF $LDF -T link.ld crt0.S $c.c -lgcc -o "$OUT/$c.elf"
done
CM=$ROOT/verif/programs/coremark
CMS="$CM/core_list_join.c $CM/core_main.c $CM/core_matrix.c $CM/core_state.c $CM/core_util.c $CM/bonetto/core_portme.c"
for it in 1 2; do
    $CC $ARCH -O2 -ffreestanding -fno-builtin -fno-tree-loop-distribute-patterns -fno-common -I$P \
        -I$CM -I$CM/bonetto -DITERATIONS=$it -DTOTAL_DATA_SIZE=2000 -DPERFORMANCE_RUN=1 \
        -Wno-unused-but-set-variable $LDF -T link.ld -Wl,--defsym=_log_ring=0x017F0000 \
        crt0.S $CMS -lgcc -o "$OUT/coremark$it.elf"
done
for s in 1 2 3 4; do
    python3 "$ROOT/verif/programs/gen_random.py" $s 2000 -o "$OUT/random_$s.S"
    $CC $ARCH $LDF -T rand.ld "$OUT/random_$s.S" -o "$OUT/random_$s.elf"
done
for e in "$OUT"/*.elf; do
    $OBJCOPY -O binary "$e" "${e%.elf}.bin"
done
echo "built $(ls "$OUT"/*.bin | wc -l | tr -d ' ') programs in $OUT"
