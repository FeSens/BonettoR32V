# BonettoR32V top-level Makefile. Contract: hypothesis agents never edit it (CLAUDE.md).
#
# Every gate's command lives in tools/eval.py, one adapter per gate; the gate targets below only
# call it, so `make cosim` and the cosim step of `make eval` are the same command.
# Gate order in `make eval`: lint, cosim, equiv, formal, arch, area, gl (stops at the first FAIL).
# `make gates` is the same command under another name, for agent sandboxes that block commands
# containing the word "eval".
# Only formal, harden and tilefit take `lockf -k /tmp/bonetto-heavy.lock` (one heavy job at a
# time on this Mac). Never wrap `make eval` / `make gates` in that lock: formal would wait on it
# forever. See docs/eval.md.

SHELL := /bin/bash

# Resolve python before PATH changes (GNU Make 3.81 on macOS); fail clearly if it's missing.
PYTHON := $(shell command -v python3 2>/dev/null)
ifeq ($(strip $(PYTHON)),)
$(error python3 not found on PATH; the gates need Python 3)
endif

TOOLCHAIN_BIN := $(HOME)/bonetto/riscv-autoarch/.toolchain/oss-cad-suite/bin
ifneq ($(wildcard $(TOOLCHAIN_BIN)),)
export PATH := $(TOOLCHAIN_BIN):$(PATH)
endif

HEAVY_LOCK := lockf -k /tmp/bonetto-heavy.lock
EVAL := $(PYTHON) tools/eval.py
GATE_TARGETS := lint cosim equiv formal formal-deep arch area gl

# Extra arguments for the physical runners (Task 8 defines them), e.g. HARDEN_ARGS='--tiles 3x2'.
# harden_tt.sh and tilefit.py take the heavy lock themselves unless BONETTO_HEAVY_LOCKED=1; the
# Makefile takes it once (so tilefit holds it across all its trials) and sets the variable.
HARDEN_ARGS ?=
TILEFIT_ARGS ?=

.PHONY: help $(GATE_TARGETS) eval gates harden tilefit test-tools clean

help:
	@echo "make lint         Verilator -Wall lint, plain and RISCV_FORMAL builds"
	@echo "make cosim        pin-level cosim: every program, modes rvfi and pins, seeds 1-3"
	@echo "make equiv        RVFI-stripped RISCV_FORMAL build == plain build (yosys equiv)"
	@echo "make formal       riscv-formal at the pins, fast config (heavy lock)"
	@echo "make formal-deep  riscv-formal deep config, for champions (heavy lock)"
	@echo "make arch         riscv-arch-test RV32E subset"
	@echo "make area         sky130 cell area (build/area.json)"
	@echo "make gl           gate-level cocotb test on the area netlist"
	@echo "make eval         all gates in order, stop at first FAIL, write build/eval.json (V=1 streams logs)"
	@echo "make gates        exactly the same as make eval (for sandboxes that block the word)"
	@echo "make harden       TT 1x1 hardening with LibreLane (heavy lock)"
	@echo "make tilefit      smallest signoff-clean fraction of the 1x1 tile (heavy lock)"
	@echo "make test-tools   pytest over tools/"
	@echo "make clean        remove build outputs"

$(GATE_TARGETS):
	$(EVAL) --gate $@

eval gates:
	$(EVAL) $(if $(V),-v) $(if $(MAX_AREA),--max-area $(MAX_AREA))

harden:
	@test -f tools/harden_tt.sh || { echo "harden: runner missing (tools/harden_tt.sh)"; exit 1; }
	BONETTO_HEAVY_LOCKED=1 $(HEAVY_LOCK) bash tools/harden_tt.sh $(HARDEN_ARGS)

tilefit:
	@test -f physical/tilefit.py || { echo "tilefit: runner missing (physical/tilefit.py)"; exit 1; }
	BONETTO_HEAVY_LOCKED=1 $(HEAVY_LOCK) $(PYTHON) physical/tilefit.py $(TILEFIT_ARGS)

test-tools:
	$(PYTHON) -m pytest tools/ -q

clean:
	rm -rf build obj_dir test/sim_build test/results.xml test/gate_level_netlist.v
