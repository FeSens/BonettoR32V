# Baselines: the numbers to beat

Every area number below comes from `tools/area.py`: LibreLane 3.0.14 `Yosys.Synthesis`, TT's
`src/config.json` from this repo (50 ns clock, "AREA 0"), sky130_fd_sc_hd `tt_025C_1v80`, and
the flattened netlist. The third-party sources were cloned into the session scratchpad and are
not committed.

## Summary

| Design | What was synthesized | area_um2 | cells | flops | latches | On-die GPRs |
|---|---|---:|---:|---:|---:|---|
| NanoV-1tile | TT top `tt_um_WaiMingLee888_nanov_1tile`, the 8 files in its info.yaml | 14,661.6 | 1,310 | 196 | 0 | none (in SPI RAM) |
| SERV, RV32E-sized | `serv_rf_top`, `WITH_CSR=0`, RF depth cut to 16 registers (our edit) | 30,819.6 | 2,405 | 678 | 0 | 16 x 32, flops |
| SERV, no CSR | `serv_rf_top`, `WITH_CSR=0` | 53,550.1 | 4,169 | 1,190 | 0 | 32 x 32, flops |
| SERV, as shipped | `serv_rf_top`, default parameters | 60,204.0 | 4,672 | 1,336 | 0 | 32 x 32 + 4 CSR, flops |
| Our seed (97153ff) | `tt_um_fesens_r32v`, the full TT top | 47,724.5 | 4,065 | 631 | 0 | 15 x 32, flops |

The TT 1x1 core is 16,493.3 um2: (2.76, 2.72) to (158.24, 108.80) inside a 161 x 111.52 um die.

## NanoV-1tile

- Source: github.com/WaiMingLee888/nanov-1tile-tinytapeout-sky at
  `d0c4a671bbb65e564f9fa4e2acd0beaa3f7018ca`.
- Command:
  `tools/area.py --top tt_um_WaiMingLee888_nanov_1tile --src <clone>/src --info <clone>/info.yaml`.
  The runtime was 3.7 to 7.7 s, and two runs were identical.
- **It's not a like-for-like baseline.**
  - NanoV-1tile keeps x1 to x15 in the external SPI RAM: the last 64 bytes of a 24-bit address
    space, through `nanoV_register_spi`.
  - x3 and x4 are hard-wired to constants.
  - It has no traps, ECALL or EBREAK.
  - Our contract forbids both of the first two (invariant 5) and requires the traps. Read it as
    the area of a bit-serial core with no register file on die. That's a floor more than a
    target.

Top cell types:

| Cell type | Count | Area (um2) | Share |
|---|---:|---:|---:|
| dfxtp_2 | 196 | 4,169.0 | 28.4 % |
| mux2_1 | 224 | 2,522.4 | 17.2 % |
| o211a_2 | 61 | 610.6 | 4.2 % |
| nor2_2 | 97 | 606.8 | 4.1 % |
| a221o_2 | 52 | 585.6 | 4.0 % |
| o221a_2 | 51 | 574.3 | 3.9 % |
| or2_2 | 76 | 475.5 | 3.2 % |
| a22o_2 | 41 | 410.4 | 2.8 % |
| a31o_2 | 43 | 376.6 | 2.6 % |
| nand2_2 | 51 | 319.1 | 2.2 % |

By module (hierarchical synthesis, own cells only; their total is 17,737.0 um2):

| Module | Own area (um2) |
|---|---:|
| nanoV_cpu_external | 7,881.3 |
| nanoV_external_sources | 3,641.0 |
| nanoV_core | 3,216.8 |
| nanoV_register_spi | 2,312.2 |
| nanoV_shift | 479.2 |
| nanoV_alu | 133.9 |
| tt top | 72.6 |

### Hardening (`tools/harden_tt.sh`, NanoV's own config.json)

- **1x1, stock TT flow: FAILS.** Global placement stops with GPL-0301, utilization 104.491 %:
  - 14,661.6 um2 of movable cells plus a 1,972.3 um2 pin-density adjustment, against
    15,919.0 um2 of free core.
  - NanoV's signed-off 1x1 used a patched LibreLane 3.0.5 image (`-disable_pin_density_adjust`
    in `gpl.tcl`, compact CTS flags) in its own workflow. TT's action can't reproduce that.
  - Run time to the failure: 15 s.
- **1x2, stock TT flow: PASSES**, every check including TT's precheck:
  - utilization 0.452 (15,489.9 um2 of placed cells and taps over a 34,255.4 um2 core);
  - setup worst slack 23.94 ns, hold worst slack 0.085 ns at all corners;
  - 0 magic, route and KLayout DRC errors, LVS clean, 0 antenna violations;
  - 964 max-slew and 10 max-cap violations (not gated by TT);
  - LibreLane 186 s plus precheck 43 s.
- **Tile fraction** (`physical/tilefit.py --tiles 1x2 --keep-info-sources`, NanoV's config,
  7 runs, 31 min including lock waits):
  - min_fraction 0.5148 of the 1x2 core, which is 17,634.4 um2 placeable. That's more than the
    whole 1x1 core (16,493.3 um2), consistent with the 1x1 failure.
  - Density at that fraction: 0.865 (15,249.6 um2 of placed cells and taps).
  - The next step down, 0.5059 (17,330.4 um2), fails at global placement: GPL-0301, 101.0 %.

  Every run:

  | Fraction | Placeable (um2) | Result | Run time |
  |---:|---:|---|---:|
  | 1.0000 | 34,255.4 | pass | 300 s |
  | 0.7130 | 24,424.7 | pass | 226 s |
  | 0.5710 | 19,560.0 | pass | 282 s |
  | 0.4970 | 17,026.3 | fail, GPL-0301 102.9 % | 11 s |
  | 0.5325 | 18,242.5 | pass | 196 s |
  | 0.5148 | 17,634.4 | pass | 200 s |
  | 0.5059 | 17,330.4 | fail, GPL-0301 101.0 % | 663 s (mostly waiting for the lock) |

  All passing runs are clean on every check, the precheck included. Setup worst slack is about
  23.9 ns and hold worst slack 0.07 to 0.10 ns.

## SERV

- Source: github.com/olofk/serv at `f200eb2ed7b69ac1c6b8eddd47654522aeee5ce8`.
- Top `serv_rf_top`, with `W=1`, `RF_WIDTH=2`, `RESET_STRATEGY="MINI"`, `COMPRESSED=0`,
  `MDU=0` and `PRE_REGISTER=1`.
- Files: `serv_aligner, serv_alu, serv_bufreg, serv_bufreg2, serv_compdec, serv_csr, serv_ctrl,
  serv_debug, serv_decode, serv_immdec, serv_mem_if, serv_rf_if, serv_rf_ram_if, serv_rf_ram,
  serv_rf_top, serv_state, serv_top`.
- The RF is `serv_rf_ram`, a `reg [1:0] memory[]` array. With no SRAM macro, yosys
  `memory_map` turns it into flip-flops and muxes.
- Its ports are Wishbone-style instruction and data buses. So none of the SERV numbers includes
  an SPI memory controller, GPIO or the TT wrapper, which a TT build would need on top.
- Variants:
  - **As shipped**: default parameters (`WITH_CSR=1`, so Zicsr, the timer interrupt and 4 CSR
    registers in the RF). RV32I, 32 GPRs.
  - **No CSR**: `--param WITH_CSR=0`. RV32I, no CSRs, no interrupts: the closest match to our
    EEI, apart from the register count.
  - **RV32E-sized**: no CSR, plus one edit to `serv_rf_top.v`: the `serv_rf_ram` instance gets
    `.depth((16+CSR_REGS)*32/RF_WIDTH)` = 256. The 9-bit RF address then truncates to 8 bits,
    so x16-x31 alias x0-x15 (reserved in RV32E). This one is the fair comparison for our core.

RV32E-sized SERV, top cell types:

| Cell type | Count | Area (um2) | Share |
|---|---:|---:|---:|
| dfxtp_2 | 678 | 14,421.3 | 46.8 % |
| mux2_1 | 690 | 7,770.0 | 25.2 % |
| a22o_2 | 153 | 1,531.5 | 5.0 % |
| or2_2 | 233 | 1,457.6 | 4.7 % |
| a221o_2 | 126 | 1,418.9 | 4.6 % |
| nor2_2 | 102 | 638.1 | 2.1 % |
| buf_2 | 67 | 335.3 | 1.1 % |
| o211a_2 | 28 | 280.3 | 0.9 % |
| o21a_2 | 26 | 227.7 | 0.7 % |
| mux4_2 | 10 | 225.2 | 0.7 % |

By module (own cells): `serv_rf_ram` 23,533.8 um2 (the 512-bit RF and its read mux), then
`serv_bufreg2` 1,671.6, `serv_bufreg` 1,350.0, `serv_immdec` 1,260.0, `serv_ctrl` 1,244.9,
`serv_decode` 869.6, `serv_rf_ram_if` 822.0, `serv_state` 678.2. Hierarchical total: 32,128.3.

SERV as shipped, top cell types: dfxtp_2 1,336 (28,417.3 um2, 47.2 %), mux2_1 1,321 (14,875.5,
24.7 %), nor2_2 638 (3,991.3), a221o_2 315 (3,547.2), a22o_2 300 (3,002.9), or2_2 108 (675.6),
o211a_2 60 (600.6), o21a_2 52 (455.4), a211o_2 40 (400.4), and2_2 47 (352.8).

SERV without CSRs, top cell types: dfxtp_2 1,190 (25,311.8 um2, 47.3 %), mux2_1 1,196
(13,467.9, 25.2 %), a221o_2 267 (3,006.6), or2_2 447 (2,796.4), a22o_2 272 (2,722.6), nor2_2
182 (1,138.6), o211a_2 72 (720.7), nand2_2 89 (556.8), o21a_2 56 (490.5), buf_2 67 (335.3).

## The seed (setup/seed 97153ff, merged here)

- Command: `tools/area.py --top tt_um_fesens_r32v --src src/`, 9 to 10 s per run.
- Two runs gave identical JSON (`runtime_s` aside) and byte-identical netlists.
- Result: **47,724.5 um2, 4,065 cells, 631 flops, 0 latches.** One module, so by_module equals
  the score.
- The `--engine yosys` fallback (yosys 0.64) gives 47,505.6 um2 (0.46 % less), as on NanoV.
- The seed agent's estimate of 38.7k um2 came from a different yosys script. The real flow's
  number is the one above.

| Cell type | Count | Area (um2) | Share |
|---|---:|---:|---:|
| dfxtp_2 | 631 | 13,421.6 | 28.1 % |
| mux2_1 | 893 | 10,055.9 | 21.1 % |
| mux4_2 | 182 | 4,098.9 | 8.6 % |
| nor2_2 | 335 | 2,095.8 | 4.4 % |
| or2_2 | 201 | 1,257.5 | 2.6 % |
| nand2_2 | 198 | 1,238.7 | 2.6 % |
| xnor2_2 | 73 | 1,187.4 | 2.5 % |
| a21o_2 | 135 | 1,182.4 | 2.5 % |
| o211a_2 | 116 | 1,161.1 | 2.4 % |
| o21ai_2 | 101 | 884.6 | 1.9 % |

Hardening the seed (`tools/harden_tt.sh --tiles NxM`, the repo's `src/config.json`):

- **2x2: unproven, probably doesn't fit.** `harden_tt.sh` now classifies this run (exit 137,
  stopped by hand) as a tool error, not a measured failure.
  - Global placement auto-raised the target density from 0.60 to 0.76 (utilization 75.9 %,
    including a 5,885.7 um2 pin-density adjustment).
  - The resizer added 575 hold buffers, which took the standard-cell area to 62,931.6 um2.
  - Global routing finished congested: demand 58,147 against 42,898 resources (135.6 %), total
    overflow 17,713. TT's config allows that (`GRT_ALLOW_CONGESTION`).
  - Detailed routing was still in its first iteration after 29 min, with 76,388 violations at
    60 %. I stopped it there, so the 2x2 failure is inferred from the routing overflow, not from
    a finished run.
- **3x2: PASSES**, every check including TT's precheck. So 3x2 is the smallest tile it's
  proven to fit.
  - utilization 0.573: 63,547.2 um2 of placed cells and taps in a 110,874 um2 core;
  - target density stayed at 0.60 (GPL utilization 49.5 %), and global routing ran at 46.6 %
    with no overflow;
  - setup worst slack 20.56 ns, hold worst slack 0.115 ns, 0 violations at every corner;
  - 0 magic, route and KLayout DRC errors, LVS clean, 0 antenna violations;
  - 2,252 max-slew and 24 max-cap violations (not gated by TT);
  - LibreLane 253 s plus precheck 59 s.
- **Timing repair grows the seed by a quarter.** The resizer inserts 1,285 timing-repair
  buffers worth 12,238 um2 (LibreLane's `timing_repair_buffer` metrics; `harden_tt.sh`'s
  netlist difference, `repair_um2`, counts 1,185 cells and 11,531.06 um2), on top of the
  47,725 um2 of logic: 607 `dlygate4sd3_1` hold
  buffers ("Inserted 607 hold buffers", hold margin 0.1 ns from TT's
  `PL_RESIZER_HOLD_SLACK_MARGIN`), 465 `clkdlybuf4s25_1`, and `buf_2`, `buf_4` and `buf_6`.
  NanoV in 1x2 needed no hold buffers. `area.py` doesn't see this overhead; tilefit does.

## What this means for the loop

- **A flip-flop register file can't fit in a 1x1 tile.** 512 RF bits as `dfxtp_2` cost
  10,890 um2 before any read mux. In SERV, the RF plus its mux is 23.5k um2. The 1x1 core is
  16.5k um2, and the stock flow's practical ceiling is lower still (see NanoV).
- **The PDK's `no_synth.cells` excludes `dfxtp_1`** (and most `_1` cells), so flops synthesize
  to `dfxtp_2`. Contract v1 forbids instantiating cells.
- **The bar.** A contract-correct core must beat 30,820 um2 (RV32E-sized SERV, without its
  memory interface) to beat the best known bit-serial design. Fitting 1x1 at all needs about
  half that.
