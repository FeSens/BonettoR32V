# Hypothesis backlog (lead-maintained)

The lead keeps this ranked. Estimates are the lead's, not measured, until a hypothesis runs.

**Budget, measured** by Task 8 in TT's stock flow (LibreLane 3.0.14):
- **1x1 geometry.** The free core is 15,919 µm², and global placement adds a pin-density
  adjustment of about 1,972 µm².
- **NanoV doesn't fit.** It is 14,661.6 µm² of cells and fails 1x1 at GPL-0301 (104.5%
  utilization). NanoV's own signed-off 1x1 needed a patched LibreLane.
- **The stock-flow 1x1 cap** is therefore about 13.9k µm² of cells, timing-repair buffers
  included. Routing at 86% density passed in 1x2.
- **Hold repair is not in `area_um2`.** The seed in 3x2 got 607 hold buffers plus 465 clock
  delay buffers, which is +26%. NanoV got none.
- **The on-die register file alone** is 10.9k µm² (512 flops x 21.27). On-die architectural
  state therefore cannot fit a 1x1 microcontroller (H002 explores the register file in RAM).

## Structural (the big wins)

| # | Idea | Est. saving | Notes |
|---|---|---|---|
| 001 | Bit-serial core with an always-rotating flip-flop register file | measured: 47,724.5 -> 17,503.0 µm² (LibreLane engine) | WAITING FOR GATES (hyp/001-bitserial 9a39f1d) |
| B1 | One staging shift register reused as the SPI address buffer (MSB-first 16 bits), the load/store byte reverser, the shift buffer and the instruction immediate, by lifetime | 300-700 µm² | needs the 001 structure |
| B2 | Keep only decoded instruction fields, not the full 32-bit IR: consume immediates serially as they arrive or from the staging register | 200-400 µm² | the fetch bit order is 7..0, 15..8, ...: decode on the fly |
| B3 | PC upper half: a 16-bit rotating register with a lazy carry. Only JALR, AUIPC, JAL links and the 0xFFFF+4 wrap touch it | 0-150 µm² | the full 32-bit PC is architectural; the saving is in the logic, not the flip-flops |
| B4 | Minimal decode under the "reserved = unspecified" rule (if the user keeps it): drop funct7 checks except bit 30, drop reg[4] checks | 100-300 µm² | needs the user's answer; the seed is strict |
| B5 | Merge the SPI bit counter and the 5-bit register phase counter into one free-running counter | 50-150 µm² | |
| B6 | Reset only control (FSM, CS_n, SCK, uo_out); no reset on the datapath, the RF or the PC beyond what the contract needs (PC = 0 at reset) | 5 µm² per flip-flop moved from dfrtp to dfxtp | |
| B7 | Fuse the PC+4 adder and the ALU adder (one serial full adder, time-multiplexed) | 30-80 µm² | serial adders are tiny already |
| B8 | GPIO out register as part of a rotating chain (shift in serially from store data) | logic only | uo_out must hold steady (contract section 3), so it needs a parallel hold register: likely no gain |

## Physical

| # | Idea | Est. saving | Notes |
|---|---|---|---|
| P1 | Placement density sweep inside TT's allowed knobs (`PL_TARGET_DENSITY_PCT` up to 95, as NanoV did) | tile fraction | regular shift-register nets route well |
| P2 | Hand-placed register-file array (rows of rotating flip-flops abutted, taps at one edge) | tile fraction, timing | standard cells only; LVS and gate-level sim cover it |
| P4 | Find a TT-allowed config knob that disables or shrinks GPL's pin-density adjustment (NanoV patched `-disable_pin_density_adjust` into gpl.tcl) | up to ~1,970 µm² of 1x1 capacity | the knob must be in TT's allowed `config.json` set; precheck must pass |
| P5 | Hold-repair minimization: compact clock tree in 1x1, and a design with fewer direct flop-to-flop paths, or ring stages with logic between them | tile fraction | measure with harden: `dlygate4sd3_1` count |
| P3 | Instantiate dfxtp_1 explicitly. The PDK's `no_synth.cells` excludes it, so every inferred flip-flop is dfxtp_2 (21.27 vs 20.02 µm²) | ~730 µm² on H001 | needs contract v1.1 (allowed-cell list + contract-owned models); first find out why the PDK excludes it |

## Exotic (contract v1.1, only if the structural wins run out)

| # | Idea | Est. saving | Notes |
|---|---|---|---|
| X1 | Latch "bubble ring" register file: 36 latches per 32-bit register with shared clock-gated enables | ~1.5k µm² | slow (about 256 steps per word read), latch timing risk; `docs/storage.md` |

## Microcontroller (after the core settles; contract changes go to the user)

GPIO direction on uio[7:4], a timer with compare, UART TX, interrupts (minimal Zicsr), boot from
SPI flash, an optional delta-sigma ADC (external RC). About 4-5.5k µm² estimated for the set.
