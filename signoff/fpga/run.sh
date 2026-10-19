#!/usr/bin/env bash
# Synthesize for the iCE40UP5K (sg48), place and route at 50 MHz, report use and Fmax.
set -euo pipefail
cd "$(dirname "$0")/../.."
mkdir -p build/fpga
yosys -q -l build/fpga/yosys.log -p "read_verilog -sv verif/cells/*.v src/r32v_csr.v src/r32v_mem.v src/r32v_periph.v src/tt_um_fesens_r32v.v signoff/fpga/fpga_top.v; synth_ice40 -top fpga_top -json build/fpga/fpga.json; tee -o build/fpga/stat.txt stat"
nextpnr-ice40 --up5k --package sg48 --json build/fpga/fpga.json --asc build/fpga/fpga.asc --freq 50 --pcf-allow-unconstrained --seed 1 --report build/fpga/report.json > build/fpga/pnr.log 2>&1 || true
grep -E "ICESTORM_LC|ICESTORM_RAM|SB_IO|Max frequency|ERROR" build/fpga/pnr.log | tail -12
