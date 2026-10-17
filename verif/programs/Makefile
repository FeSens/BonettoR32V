# BonettoR32V test programs, contract v3 (docs/contract-v3.md).
#
#   make -C verif/programs          build/programs/<name>.{elf,hex,expect.json} for every program
#   make -C verif/programs NAME...  only those programs (e.g. `selftest coremark`)
#   make -C verif/programs list     the program names, in harness order
#   make -C verif/programs list-declared   the peripheral programs (EVENT_PROGS, the one list)
#   make -C verif/programs check    test_programs.py: the expect.json contract, the random-stream
#                                   coverage, and progsim (progsim.cpp: a timed peripheral model
#                                   around the ISS) on the peripheral programs at 3 to 2,048 cycles
#                                   per instruction
#
# <name>.hex is a sparse 16 MiB flash image ($readmemh: `@addr` records, unset bytes erased).
# <name>.expect.json (mkexpect.py) is what the cosim harness checks:
#   - deterministic programs: the standalone ISS run (`iss_main --v3`), which must end in the
#     self-loop with GPIO_OUT = 0x5A;
#   - peripheral programs (their outcome depends on peripheral timing, ruling R13): the declared
#     stimulus and expectations (stim.h, stimuli in mkexpect.py), plus <name>.stim for progsim.

ROOT    := $(abspath ../..)
OUT     := $(ROOT)/build/programs
ISS     := $(ROOT)/build/cosim/iss_main
PROGSIM := $(OUT)/progsim
HERE    := $(abspath .)
COSIM   := $(ROOT)/verif/cosim

CROSS   ?= riscv32-unknown-elf-
CC      := $(CROSS)gcc
CXX     ?= c++
PYTHON  ?= python3

ARCH    := -march=rv32e_zicsr -mabi=ilp32e
LDFLAGS := -nostdlib -nostartfiles -static -T $(HERE)/link.ld -Wl,--no-warn-rwx-segments
ASFLAGS := $(ARCH) -I$(HERE)
CFLAGS  := $(ARCH) -O2 -ffreestanding -fno-builtin -fno-tree-loop-distribute-patterns \
           -fno-common -I$(HERE) -Wall

RANDOM_SEEDS := 1 2 3 4 5 6
RANDOM_N     := 3000
TRAPS        := ecall ebreak jal jalr branch zero ones
TRAP_KIND_ecall  := 1
TRAP_KIND_ebreak := 2
TRAP_KIND_jal    := 3
TRAP_KIND_jalr   := 4
TRAP_KIND_branch := 5
TRAP_KIND_zero   := 6
TRAP_KIND_ones   := 7

ASM_PROGS   := selftest csr coherence mem_init_check
C_PROGS     := gpio_demo
ISS_PROGS   := $(ASM_PROGS) $(C_PROGS) coremark $(addprefix trap_,$(TRAPS)) \
               $(addprefix random_,$(RANDOM_SEEDS))
EVENT_ASM   := irq_stress wfi
EVENT_C     := irq_timer uart_echo spi_loop pwm ir_loop gpio_edge
EVENT_PROGS := irq_timer irq_stress wfi uart_echo uart_echo_fast spi_loop pwm ir_loop gpio_edge
PROGS       := $(ISS_PROGS) $(EVENT_PROGS)

CM_DIR  := $(HERE)/coremark
CM_SRCS := $(CM_DIR)/core_list_join.c $(CM_DIR)/core_main.c $(CM_DIR)/core_matrix.c \
           $(CM_DIR)/core_state.c $(CM_DIR)/core_util.c $(CM_DIR)/bonetto/core_portme.c
CM_ITERATIONS := 1
CM_FLAGS := -I$(CM_DIR) -I$(CM_DIR)/bonetto -DITERATIONS=$(CM_ITERATIONS) -DTOTAL_DATA_SIZE=2000 \
            -DPERFORMANCE_RUN=1 -Wno-unused-but-set-variable

all: $(foreach p,$(PROGS),$(OUT)/$(p).expect.json)

list:
	@echo $(PROGS)

list-declared:
	@echo $(EVENT_PROGS)

# `make -C verif/programs selftest csr`: build those programs only.
$(PROGS): %: $(OUT)/%.expect.json

check: all $(PROGSIM)
	$(PYTHON) -m pytest $(HERE)/test_programs.py -q

$(OUT) $(ROOT)/build/cosim:
	mkdir -p $@

$(ISS): $(COSIM)/iss_main.cpp $(COSIM)/iss.h $(COSIM)/iss3.h | $(ROOT)/build/cosim
	$(CXX) -std=c++17 -O2 -Wall -Wextra -o $@ $<

$(PROGSIM): progsim.cpp $(COSIM)/iss3.h | $(OUT)
	$(CXX) -std=c++17 -O2 -Wall -Wextra -I$(COSIM) -o $@ $<

# ---- ELF builds (static pattern rules: GNU make 3.81 on macOS picks the first matching rule) ----
COMMON_DEPS := crt0.S bonetto.h link.ld

$(foreach p,$(ASM_PROGS) $(EVENT_ASM),$(OUT)/$(p).elf): $(OUT)/%.elf: %.S $(COMMON_DEPS) stim.h | $(OUT)
	$(CC) $(ASFLAGS) $(LDFLAGS) crt0.S $< -o $@

$(foreach p,$(C_PROGS) $(EVENT_C),$(OUT)/$(p).elf): $(OUT)/%.elf: %.c $(COMMON_DEPS) stim.h | $(OUT)
	$(CC) $(CFLAGS) $(LDFLAGS) crt0.S $< -lgcc -o $@

# The same program at the other UART rate: only its expect.json differs (uart_rate_ppm).
$(OUT)/uart_echo_fast.elf: $(OUT)/uart_echo.elf
	cp $< $@

$(OUT)/coremark.elf: $(CM_SRCS) $(CM_DIR)/coremark.h $(CM_DIR)/bonetto/core_portme.h $(COMMON_DEPS) | $(OUT)
	$(CC) $(CFLAGS) $(CM_FLAGS) $(LDFLAGS) crt0.S $(CM_SRCS) -lgcc -o $@

$(foreach t,$(TRAPS),$(OUT)/trap_$(t).elf): $(OUT)/trap_%.elf: trap.S $(COMMON_DEPS) | $(OUT)
	$(CC) $(ASFLAGS) -DTRAP_KIND=$(TRAP_KIND_$*) $(LDFLAGS) crt0.S $< -o $@

$(foreach s,$(RANDOM_SEEDS),$(OUT)/random_$(s).S): $(OUT)/random_%.S: gen_random.py | $(OUT)
	$(PYTHON) gen_random.py $* $(RANDOM_N) -o $@

$(foreach s,$(RANDOM_SEEDS),$(OUT)/random_$(s).elf): $(OUT)/random_%.elf: $(OUT)/random_%.S link.ld
	$(CC) $(ASFLAGS) $(LDFLAGS) $< -o $@

# ---- images and expectations -----------------------------------------------------------------
HEXES := $(foreach p,$(PROGS),$(OUT)/$(p).hex)
$(HEXES): $(OUT)/%.hex: $(OUT)/%.elf elf2hex.py
	$(PYTHON) elf2hex.py $< $@

EXPECTS := $(foreach p,$(PROGS),$(OUT)/$(p).expect.json)
MKEXPECT_coremark := --coremark-iters $(CM_ITERATIONS)
$(foreach p,$(EVENT_PROGS),$(eval MKEXPECT_$(p) := --declared))
$(EXPECTS): $(OUT)/%.expect.json: $(OUT)/%.hex $(OUT)/%.elf $(ISS) mkexpect.py stim.h
	$(PYTHON) mkexpect.py --iss $(ISS) --hex $< --elf $(OUT)/$*.elf --out $@ $(MKEXPECT_$*)

clean:
	rm -rf $(OUT)

.PHONY: all list list-declared check clean $(PROGS)
.SECONDARY:
