# BonettoR32V test programs.
#
#   make -C verif/programs          build/programs/<name>.{elf,hex,expect.json} for every program
#   make -C verif/programs spike    build/programs/spike/<name>.{elf,hex}: the spike cross-check
#                                   builds of selftest, gpio_demo and coremark (linked at 0x10000)
#
# <name>.hex is the 64 KB RAM image ($readmemh, one byte per line). <name>.expect.json is the
# reference ISS's result (retired count, uo_out trace, halt), checked by mkexpect.py.

ROOT    := $(abspath ../..)
OUT     := $(ROOT)/build/programs
SPIKEOUT := $(OUT)/spike
ISS     := $(ROOT)/build/cosim/iss_main
HERE    := $(abspath .)

CROSS   ?= riscv32-unknown-elf-
CC      := $(CROSS)gcc
CXX     ?= c++
PYTHON  ?= python3

ARCH    := -march=rv32e -mabi=ilp32e
LDFLAGS := -nostdlib -nostartfiles -static -T $(HERE)/link.ld -Wl,--no-warn-rwx-segments
ASFLAGS := $(ARCH) -I$(HERE)
CFLAGS  := $(ARCH) -O2 -ffreestanding -fno-builtin -fno-tree-loop-distribute-patterns \
           -fno-common -I$(HERE) -Wall

RANDOM_SEEDS := 1 2 3 4 5 6
RANDOM_N     := 3000
TRAPS        := ecall jal jalr branch zero ones
TRAP_KIND_ecall  := 1
TRAP_KIND_jal    := 2
TRAP_KIND_jalr   := 3
TRAP_KIND_branch := 4
TRAP_KIND_zero   := 5
TRAP_KIND_ones   := 6

# Static pattern rules below: GNU make 3.81 (macOS) picks the first matching pattern rule.
SPIKE_HEX := $(SPIKEOUT)/selftest.hex $(SPIKEOUT)/gpio_demo.hex $(SPIKEOUT)/coremark.hex
PROGS := selftest gpio_demo coremark $(addprefix trap_,$(TRAPS)) $(addprefix random_,$(RANDOM_SEEDS))

CM_DIR  := $(HERE)/coremark
CM_SRCS := $(CM_DIR)/core_list_join.c $(CM_DIR)/core_main.c $(CM_DIR)/core_matrix.c \
           $(CM_DIR)/core_state.c $(CM_DIR)/core_util.c $(CM_DIR)/bonetto/core_portme.c
CM_FLAGS := -I$(CM_DIR) -I$(CM_DIR)/bonetto -DITERATIONS=1 -DTOTAL_DATA_SIZE=2000 \
            -DPERFORMANCE_RUN=1 -Wno-unused-but-set-variable

all: $(foreach p,$(PROGS),$(OUT)/$(p).expect.json)

spike: $(SPIKE_HEX)

list:
	@echo $(PROGS)

$(OUT) $(SPIKEOUT) $(ROOT)/build/cosim:
	mkdir -p $@

$(ISS): $(ROOT)/verif/cosim/iss_main.cpp $(ROOT)/verif/cosim/iss.h | $(ROOT)/build/cosim
	$(CXX) -std=c++17 -O2 -Wall -Wextra -o $@ $<

# ---- ELF builds -------------------------------------------------------------------------------
$(OUT)/selftest.elf: selftest.S link.ld | $(OUT)
	$(CC) $(ASFLAGS) $(LDFLAGS) $< -o $@

$(OUT)/gpio_demo.elf: gpio_demo.c crt0.S bonetto.h link.ld | $(OUT)
	$(CC) $(CFLAGS) $(LDFLAGS) crt0.S gpio_demo.c -lgcc -o $@

$(OUT)/coremark.elf: $(CM_SRCS) $(CM_DIR)/coremark.h $(CM_DIR)/bonetto/core_portme.h crt0.S link.ld | $(OUT)
	$(CC) $(CFLAGS) $(CM_FLAGS) $(LDFLAGS) crt0.S $(CM_SRCS) -lgcc -o $@

$(OUT)/trap_%.elf: trap.S link.ld | $(OUT)
	$(CC) $(ASFLAGS) -DTRAP_KIND=$(TRAP_KIND_$*) $(LDFLAGS) $< -o $@

$(OUT)/random_%.S: gen_random.py | $(OUT)
	$(PYTHON) gen_random.py $* $(RANDOM_N) -o $@

$(OUT)/random_%.elf: $(OUT)/random_%.S link.ld
	$(CC) $(ASFLAGS) $(LDFLAGS) $< -o $@

# ---- images and expectations ------------------------------------------------------------------
HEXES := $(foreach p,$(PROGS),$(OUT)/$(p).hex)
$(HEXES): $(OUT)/%.hex: $(OUT)/%.elf elf2hex.py
	$(PYTHON) elf2hex.py $< $@

EXPECTS := $(foreach p,$(PROGS),$(OUT)/$(p).expect.json)
$(EXPECTS): $(OUT)/%.expect.json: $(OUT)/%.hex $(ISS) mkexpect.py
	$(PYTHON) mkexpect.py --iss $(ISS) --hex $< --out $@

# ---- spike cross-check builds (RAM_BASE = 0x10000, trampoline at spike's reset vector) --------
SPIKE_LD := -Wl,--defsym=RAM_BASE=0x10000 -Wl,--section-start=.tramp=0x1000
$(SPIKEOUT)/tramp.S: | $(SPIKEOUT)
	printf '    .section .tramp, "ax"\n    j _start\n' > $@

$(SPIKEOUT)/selftest.elf: selftest.S link.ld $(SPIKEOUT)/tramp.S
	$(CC) $(ASFLAGS) -DSPIKE_XCHECK $(LDFLAGS) $(SPIKE_LD) $(SPIKEOUT)/tramp.S $< -o $@

$(SPIKEOUT)/gpio_demo.elf: gpio_demo.c crt0.S bonetto.h link.ld $(SPIKEOUT)/tramp.S
	$(CC) $(CFLAGS) $(LDFLAGS) $(SPIKE_LD) $(SPIKEOUT)/tramp.S crt0.S gpio_demo.c -lgcc -o $@

$(SPIKEOUT)/coremark.elf: $(CM_SRCS) $(CM_DIR)/coremark.h $(CM_DIR)/bonetto/core_portme.h crt0.S link.ld $(SPIKEOUT)/tramp.S
	$(CC) $(CFLAGS) $(CM_FLAGS) $(LDFLAGS) $(SPIKE_LD) $(SPIKEOUT)/tramp.S crt0.S $(CM_SRCS) -lgcc -o $@

$(SPIKE_HEX): $(SPIKEOUT)/%.hex: $(SPIKEOUT)/%.elf elf2hex.py
	$(PYTHON) elf2hex.py $< $@ --base 0x10000

clean:
	rm -rf $(OUT)

.PHONY: all spike list clean
.SECONDARY:
