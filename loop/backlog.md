# Hypothesis backlog (lead-maintained)

The lead keeps this ranked. Estimates are the lead's, not measured, until a hypothesis runs. The
budget comes from `docs/storage.md` and NanoV's signoff:
- a 1x1 tile holds about 14.6k µm² of synthesized cells (routed at 95% density);
- the always-rotating flip-flop register file alone is 10,133 µm².

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
| P3 | Instantiate dfxtp_1 explicitly. The PDK's `no_synth.cells` excludes it, so every inferred flip-flop is dfxtp_2 (21.27 vs 20.02 µm²) | ~730 µm² on H001 | needs contract v1.1 (allowed-cell list + contract-owned models); first find out why the PDK excludes it |

## Exotic (contract v1.1, only if the structural wins run out)

| # | Idea | Est. saving | Notes |
|---|---|---|---|
| X1 | Latch "bubble ring" register file: 36 latches per 32-bit register with shared clock-gated enables | ~1.5k µm² | slow (about 256 steps per word read), latch timing risk; `docs/storage.md` |

## Microcontroller (after the core settles; contract changes go to the user)

GPIO direction on uio[7:4], a timer with compare, UART TX, interrupts (minimal Zicsr), boot from
SPI flash, an optional delta-sigma ADC (external RC). About 4-5.5k µm² estimated for the set.
