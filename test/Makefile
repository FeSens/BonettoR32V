# Makefile
# See https://docs.cocotb.org/en/stable/quickstart.html for more info
#
#   make                                  RTL simulation of src/ (info.yaml source_files)
#   make GATES=yes                        gate level on gate_level_netlist.v
#   make GATES=yes GL_NETLIST=<file>      gate level on another netlist file
# The netlist may have power pins (TT's CI) or not (an unpowered synthesis netlist); the Makefile
# looks for VPWR in it and defines USE_POWER_PINS or NO_POWER_PINS. The cell models come from
# $(PDK_ROOT)/$(PDK) (PDK defaults to sky130A; locally PDK_ROOT=~/.ciel).

# defaults
SIM ?= icarus
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(CURDIR)/../src
PDK ?= sky130A
# The design files, from info.yaml's source_files list
PROJECT_SOURCES := $(shell awk '/^[[:space:]]*source_files:/ {f = 1; next} \
  f && /^[[:space:]]*-/ {s = $$0; sub(/^[[:space:]]*-[[:space:]]*/, "", s); sub(/[[:space:]]*\#.*/, "", s); \
  gsub(/"/, "", s); print s; next} f && !/^[[:space:]]*(\#|$$)/ {f = 0}' $(CURDIR)/../info.yaml)

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

else

# Gate level simulation:
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DSIM
COMPILE_ARGS    += -DUNIT_DELAY=\#1
VERILOG_SOURCES += $(PDK_ROOT)/$(PDK)/libs.ref/sky130_fd_sc_hd/verilog/primitives.v
VERILOG_SOURCES += $(PDK_ROOT)/$(PDK)/libs.ref/sky130_fd_sc_hd/verilog/sky130_fd_sc_hd.v

# The netlist: gate_level_netlist.v (copied in by TT's GDS action, or by the eval), or GL_NETLIST.
GL_FILE := $(if $(GL_NETLIST),$(abspath $(GL_NETLIST)),$(CURDIR)/gate_level_netlist.v)
VERILOG_SOURCES += $(GL_FILE)
# Two forms exist: TT's CI netlist has VPWR/VGND on the top module and on every cell; an unpowered
# synthesis netlist (LibreLane's synth output, a plain yosys run) has none. Pick by the content.
ifneq ($(shell grep -l VPWR $(GL_FILE) 2>/dev/null),)
SIM_BUILD				= sim_build/gl
COMPILE_ARGS    += -DUSE_POWER_PINS
else
SIM_BUILD				= sim_build/gl_nopower
COMPILE_ARGS    += -DNO_POWER_PINS
endif

endif

# Include the testbench sources:
VERILOG_SOURCES += $(CURDIR)/spiram_23lc512.v
VERILOG_SOURCES += $(CURDIR)/tb.v
COCOTB_TOPLEVEL = tb

# List test modules to run, separated by commas and without the .py suffix:
COCOTB_TEST_MODULES = test
# test.py, not Python's own "test" package, also under make -C test
export PYTHONPATH := $(CURDIR)$(if $(PYTHONPATH),:$(PYTHONPATH),)

# Rebuild the simulation when the source list or the defines change, not only when a file is newer.
INPUTS_STAMP := $(SIM_BUILD)/inputs.txt
$(shell mkdir -p $(SIM_BUILD) && echo '$(VERILOG_SOURCES) $(COMPILE_ARGS)' > $(INPUTS_STAMP).new && \
  (cmp -s $(INPUTS_STAMP).new $(INPUTS_STAMP) || mv $(INPUTS_STAMP).new $(INPUTS_STAMP)); \
  rm -f $(INPUTS_STAMP).new)
CUSTOM_COMPILE_DEPS += $(INPUTS_STAMP)

# oss-cad-suite's bin/vvp is a wrapper script that points PYTHONHOME at oss-cad-suite's own
# Python, so cocotb's embedded interpreter can't start ("Failed to import encodings module").
# Its real binaries are in ../libexec: use those. Only on macOS (where this repo's toolchain lives);
# other hosts and other Icarus installs (TT's CI) are untouched.
ifeq ($(shell uname -s),Darwin)
ifeq ($(ICARUS_BIN_DIR),)
OSS_LIBEXEC := $(abspath $(dir $(shell command -v vvp 2>/dev/null))../libexec)
ifneq ($(wildcard $(OSS_LIBEXEC)/vvp $(OSS_LIBEXEC)/iverilog),)
ifneq ($(shell grep -l PYTHONHOME $(dir $(shell command -v vvp 2>/dev/null))vvp 2>/dev/null),)
ICARUS_BIN_DIR := $(OSS_LIBEXEC)
endif
endif
endif
endif

# include cocotb's make rules to take care of the simulator setup
include $(shell cocotb-config --makefiles)/Makefile.sim
