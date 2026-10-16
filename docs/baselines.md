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
- **Tile fraction:** TILEFIT_NANOV

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

## What this means for the loop

- **A flip-flop register file can't fit in a 1x1 tile.** 512 RF bits as `dfxtp_2` cost
  10,890 um2 before any read mux. In SERV, the RF plus its mux is 23.5k um2. The 1x1 core is
  16.5k um2, and the stock flow's practical ceiling is lower still (see NanoV).
- **The PDK's `no_synth.cells` excludes `dfxtp_1`** (and most `_1` cells), so flops synthesize
  to `dfxtp_2`. Contract v1 forbids instantiating cells.
- **The bar.** A contract-correct core must beat 30,820 um2 (RV32E-sized SERV, without its
  memory interface) to beat the best known bit-serial design. Fitting 1x1 at all needs about
  half that.
