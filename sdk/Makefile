# BonettoR32V SDK (docs/programming-guide.md).
#
#   make -C sdk                    every example: build/<name>/<name>.{elf,bin,hex,lst}
#   make -C sdk SKETCH=path        one sketch: a directory of .ino / .c / .cpp files, or one file;
#                                  the output goes to build/<name>/
#   make -C sdk test               build, then sdk/tests (pytest): images, the NEC decoder, and the
#                                  examples on the RTL in cosim (those need Verilator: they run on
#                                  hwe-bench through tools/hwe_run.sh and are skipped on macOS)
#   make -C sdk clean
#
# Variables:
#   F_CPU  the clock in Hz, default 50000000 (the 20 ns CLOCK_PERIOD of src/config.json); it sets
#          the timebase, baud rates, tone and IR timing: build for the clock the board runs
#   CROSS  riscv32-unknown-elf- (the xPack GCC 15.2.0)
#   OPT    -O2
#   OUT    the output directory, default sdk/build
#
# Images: <name>.bin is the raw flash image from address 0 (what goes into the QSPI Pmod's
# W25Q128 at offset 0); <name>.hex is the same bytes in the cosim's $readmemh format.
# A .ino file is C++ with Arduino.h included first, like the Arduino IDE (without its automatic
# prototypes: define or declare functions before use).

SDK     := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
OUT     := $(SDK)/build
CORE    := $(SDK)/core
CROSS   ?= riscv32-unknown-elf-
CC      := $(CROSS)gcc
CXX     := $(CROSS)g++
AR      := $(CROSS)ar
OBJDUMP := $(CROSS)objdump
SIZE    := $(CROSS)size
PYTHON  ?= python3
F_CPU   ?= 50000000
OPT     ?= -O2

ARCH     := -march=rv32e_zicsr -mabi=ilp32e
COMMON   := $(ARCH) $(OPT) -g -ffreestanding -fno-builtin -fno-tree-loop-distribute-patterns \
            -ffunction-sections -fdata-sections -fno-common -DF_CPU=$(F_CPU)UL -I$(CORE) \
            -Wall -Wextra
CFLAGS   := $(COMMON) -std=gnu99
CXXFLAGS := $(COMMON) -std=gnu++17 -fno-exceptions -fno-rtti -fno-threadsafe-statics \
            -fno-use-cxa-atexit
LDFLAGS  := $(ARCH) -nostdlib -nostartfiles -static -T $(CORE)/link.ld -Wl,--gc-sections \
            -Wl,--no-warn-rwx-segments

EXAMPLES := blink fade serial_echo button tone ir_remote c_blink
LIBDIR   := $(OUT)/lib-$(F_CPU)
LIB      := $(LIBDIR)/libr32v.a
CRT0     := $(LIBDIR)/crt0.o
CORE_SRC := $(wildcard $(CORE)/*.c)
CORE_OBJ := $(patsubst $(CORE)/%.c,$(LIBDIR)/%.o,$(CORE_SRC)) $(LIBDIR)/trap.o
CORE_HDR := $(wildcard $(CORE)/*.h)

.PHONY: all examples sketch test clean
.SECONDARY:

ifdef SKETCH
all: sketch
else
all: examples
endif

$(LIBDIR):
	mkdir -p $@

$(filter-out $(LIBDIR)/trap.o,$(CORE_OBJ)): $(LIBDIR)/%.o: $(CORE)/%.c $(CORE_HDR) | $(LIBDIR)
	$(CC) $(CFLAGS) -c $< -o $@

$(CRT0): $(CORE)/crt0.S | $(LIBDIR)
	$(CC) $(ARCH) -c $< -o $@

$(LIBDIR)/trap.o: $(CORE)/trap.S | $(LIBDIR)
	$(CC) $(ARCH) -c $< -o $@

$(LIB): $(CORE_OBJ)
	rm -f $@
	$(AR) rcs $@ $^

# $(call sketch_rules,name,source files)
define sketch_rules
$(OUT)/$(1)/$(1).elf: $(2) $(LIB) $(CRT0) $(CORE)/link.ld $(CORE_HDR)
	mkdir -p $(OUT)/$(1)/obj
	set -e; objs=""; for s in $(2); do \
	  b=$$$$(basename $$$$s); o=$(OUT)/$(1)/obj/$$$$b.o; \
	  case $$$$s in \
	    *.ino) $(CXX) $(CXXFLAGS) -x c++ -include Arduino.h -c $$$$s -o $$$$o ;; \
	    *.cpp) $(CXX) $(CXXFLAGS) -c $$$$s -o $$$$o ;; \
	    *.c)   $(CC) $(CFLAGS) -c $$$$s -o $$$$o ;; \
	  esac; objs="$$$$objs $$$$o"; done; \
	$(CC) $(LDFLAGS) -Wl,-Map,$(OUT)/$(1)/$(1).map $(CRT0) $$$$objs \
	  -Wl,--start-group $(LIB) -lgcc -Wl,--end-group -o $$@
	$(PYTHON) $(SDK)/tools/mkimage.py $$@ --bin $(OUT)/$(1)/$(1).bin --hex $(OUT)/$(1)/$(1).hex
	$(OBJDUMP) -d $$@ > $(OUT)/$(1)/$(1).lst
	$(SIZE) $$@
$(1): $(OUT)/$(1)/$(1).elf
.PHONY: $(1)
endef

sources_of = $(sort $(wildcard $(1)/*.ino $(1)/*.c $(1)/*.cpp))
$(foreach e,$(EXAMPLES),$(eval $(call sketch_rules,$(e),$(call sources_of,$(SDK)/examples/$(e)))))

examples: $(EXAMPLES)

ifdef SKETCH
SKETCH_ABS  := $(abspath $(SKETCH))
SKETCH_NAME := $(basename $(notdir $(SKETCH_ABS)))
SKETCH_SRC  := $(if $(wildcard $(SKETCH_ABS)/.),$(call sources_of,$(SKETCH_ABS)),$(SKETCH_ABS))
ifeq ($(filter $(SKETCH_NAME),$(EXAMPLES)),)
$(eval $(call sketch_rules,$(SKETCH_NAME),$(SKETCH_SRC)))
endif
sketch: $(SKETCH_NAME)
endif

test: examples
	$(PYTHON) -m pytest -q $(SDK)/tests

clean:
	rm -rf $(OUT)
