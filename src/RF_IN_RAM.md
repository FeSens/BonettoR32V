# Hypothesis 002: the register file in the SPI RAM (exploratory spike, contract v2 candidate)

This branch breaks invariant 5 of contract v1 ("registers live on the die") on purpose, to
measure what it buys. It is not mergeable under contract v1. Every number below comes from a
command run on this branch; projected numbers are labelled.

## 1. Result

| | 001 (bit-serial, rings) | 002 strict decode (12a5186) | 002 loose decode (head) |
|---|---|---|---|
| Area (LibreLane 3.0.14 engine) | 17,503.0 µm² | 7,339.5 µm² | 7,238.2 µm² |
| Cells | 1,150 | 649 | 641 |
| Flops | 582 | 132 | 132 |
| CoreMark CPI | 372.7 | 508.5 | 508.5 |
| Max cycles between retirements | 736 | 805 | 805 |

- Engine: setup/physical `tools/area.py --engine librelane` (LibreLane 3.0.14 Yosys.Synthesis,
  sky130_fd_sc_hd, tt_025C_1v80), with this branch's `src/config.json`. The same command
  reproduces 001 at 17,503.0368 µm², 1,150 cells, 582 flops.
- NanoV (registers in SPI RAM, x3/x4 hard-wired), measured by the lead with the same engine:
  14,661.6 µm², 1,310 cells, 196 flops. 002 is 2.03x smaller than NanoV and 2.42x smaller than
  001.
- Against the ~13.9k µm² of cells that TT's stock flow fits in a 1x1 tile, 002 uses about 52%
  of the cell budget (cell area only, before hardening buffers), leaving about 6.7k µm²
  (projected) for GPIO, timer, UART and interrupts.
- "Loose decode" is independent of the register-file move: it traps only what contract v1
  requires (SYSTEM, 0x00000000, 0xFFFFFFFF, misaligned JAL/JALR/branch targets) and lets every
  other reserved encoding execute as the class its opcode bits select, which contract v1
  section 4 allows. It saves about 100 µm² and would apply to 001 too.
- Synthesis sensitivity: ABC's result moves by up to about ±100 µm² for equivalent rewrites of
  the same logic (measured: an equivalent rewrite of one 5-bit expression gave +86, removing a
  dead wire gave +8.8). Differences below about 100 µm² between variants are not meaningful.

## 2. Where the area goes (head, 7,238.2 µm²)

- Flops: 132 x dfxtp_2 = 2,807.7 µm² (39%). Logic: 4,430.5 µm² (61%).

| Flops | Count | Purpose |
|---|---|---|
| `bfr` | 33 | operand/result buffer ring |
| `pcr` | 33 | pc ring |
| `irs` | 32 | instruction register (hold muxes: about 360 µm² of logic) |
| `gpo` | 8 | uo_out (contract) |
| `ph` | 6 | phase counter, 0..32 |
| `j` | 6 | SPI bit counter, shift pre-roll counter, shift amount |
| `ep`, `sl` | 4 + 3 | engine phase and slot type (binary, used bitwise) |
| `cs_n`, `sck`, `mosi` | 3 | registered SPI pins |
| `mem`, `cy`, `cmp`, `c4` | 4 | RAM/IO flag, carry/sign, compare/flags, pc + 4 carry |
| **Total** | **132** | |

- Knockout measurements (each feature removed, area change, broken designs used only to size
  the logic): immediate selection up to 576 µm²; uo_out with its load path 332 µm² (8 flops
  included); W-slot data paths (loads, SLT, shift fill) 249 µm²; register-address emission
  221 µm²; IO load mux 96 µm²; the per-slot start phases 83 µm²; shifts as a whole 236 µm²;
  all store support 390 µm² (uo_out included).

## 3. Architecture

### What is where

- RAM (23LC512): x_r (r = 1..15) at 0xFFC0 + 4r. 0xFFC0..0xFFC3 (the x0 slot) is core scratch:
  RAM stores spill the pc there. The layout is private: a register is stored LSB first on the
  wire (bit 8n + i of x_r is bit 7 - i of byte 4r + n), so register traffic needs no reversal.
  Register addresses are constants from the instruction fields (address bits 15..6 = 1, bits 5..2
  = r, bits 1..0 = 0): no adder.
- Die: pc ring, instruction register, one buffer ring, the SPI engine, uo_out. x0 is never
  stored.

### Rings and phases

- `pcr` and `bfr` are 33-bit rings that rotate right on every clock, as in 001. `ph` counts 0..32;
  the bit at ring position i shows phase ph + i (mod 33).
- Logical bit k sits at phase 2k (mod 33): bits 0..16 at even phases, bits 17..31 at odd phases,
  phase 31 unused.
- SPI data runs at SCK = clk/2, one bit every 2 clocks. A data phase whose first bit meets phase
  0 meets bit k at phase 2k, so READ data streams straight into a ring (write mux at the ring
  input only) and WRITE data streams straight out of a fixed tap. Every ring flop is a plain
  dfxtp with no enable. A 32-bit ring would put bits k and k + 16 on the same phase; 33 is odd,
  so all 32 bits get distinct phases.
- 33 being odd also makes a time offset of 2s clocks equal to a shift by s bits: shifts are a
  delayed emission (below).
- MSB-first fields (fetch address, load/store address, store data, and the byte-reversed load
  data) come out of a ring through an 8:1 tap mux selected by `j[2:0]`. Consecutive MSB-first
  bits are 4 positions apart, so 8 positions per ring cover every byte. A one-clock gap between
  the two address bytes keeps both bytes on the same 8 positions (4 x 8 = 32 = -1 mod 33).

### Transactions (slots)

Every SPI transaction has one frame: CS_n low, 8 command bits, address high byte, one-clock gap,
address low byte, data, one clock with SCK low, CS_n high, all bits at SCK = clk/2. Each slot
type starts at its own phase, chosen so that every tap is a fixed position:

| Slot | Start phase | Transaction | Data |
|---|---|---|---|
| F | 18 | READ pc[15:0] (pc tap mux) | 32 bits into `irs` |
| A | 16 | READ 0xFFC0 + 4*rs1 | serial ALU with the immediate, into `bfr`; pc + 4 into `pcr` |
| B | 16 | READ 0xFFC0 + 4*rs2 | serial ALU with `bfr` (or rs2 into `pcr` for RAM stores, or the shift amount into `j`) |
| S | 21 | WRITE 0xFFC0 (RAM stores only) | the pc, from the pc ring |
| M | 2 | READ/WRITE the data address (buffer tap mux) | load data into `bfr`; store data from the pc tap mux |
| R | 16 | READ 0xFFC0 (RAM stores only) | back into `pcr` |
| W | 5 (+ shift offset) | WRITE 0xFFC0 + 4*rd | from `bfr`: forward, byte-reversed (loads), or delayed (shifts) |
| U | 16 | none | branch pc update |

- A slot whose register is x0 (or that has none) keeps CS_n high and runs the same timing; the
  SCK pin is `sck & ~cs_n`, so it stays low.
- The serial ALU is 001's (SERV formulas), one bit per strobe (the SCK-high clock of a data bit),
  with its carry held between strobes.

### Slot sequences

| Class | Slots |
|---|---|
| ALU immediate (not shift), SLTI(U), LUI, AUIPC, JAL | F, A, W |
| ALU register, SLT(U) | F, A, B, W |
| Shift | F, A, B (shift amount), pre-roll, W |
| Branch | F, A, B (compare), U |
| JALR | F, A (target), B (swap: pc <- target, buffer <- pc + 4, no SPI), W |
| FENCE | F, A (pc + 4, no SPI) |
| RAM load | F, A (address), M, W |
| IO load | F, A, W (from ui_in) |
| RAM store | F, A (address), S (spill pc), B (rs2 into the pc ring), M, R (restore pc) |
| IO store | F, A, B (rs2 into the buffer), retire (uo_out <- buffer bits) |
| Decode trap | F |

- RAM stores need 16 address bits and up to 32 data bits on the die at once; the single buffer
  holds 33. Spilling the pc to the x0 slot frees the pc ring for rs2. This costs two slots (about
  264 clocks) and no flops.
- Loads capture the data bytes as they arrive (MSB first) and W emits them through the buffer tap
  mux in reverse within each byte, with the sign or zero fill after the last byte.
- Shifts: B captures the 5-bit shift amount s into `j` (from rs2, or from the immediate). After B
  a pre-roll counts `j` to 63, so W starts 2s clocks later (right) or earlier modulo 33 (left)
  than an unshifted W. The emission tap then shows source bit k + s (or k - s) when result bit k
  goes out. The fill boundary is a fixed phase (source bit 32, the unused phase, for right shifts;
  source bit 0 for left shifts), so no comparator with s is needed. SRA fills with the last real
  bit emitted (held in `cy`).

### Traps

- Decode traps retire right after F. JALR target[1] is known after A, a taken branch's target[1]
  after B; both retire without writes. Then the core halts with no SPI traffic.

### RVFI

- rs1/rs2 values are snooped from MISO during A/B, rd_wdata from the W emission, load data from
  MISO during M, pc_rdata from the pc ring after F, pc_wdata from the pc ring at retirement. RVFI
  logic only observes: the equivalence gate (main's `tools/equiv.py`) passes.

## 4. Cycle counts

Measured with `harness_rvfi` (per-retirement gaps, stderr), on selftest, the trap programs and
CoreMark, head design. The cycle behavior of 12a5186 and head is identical.

| Class | Min / avg / max cycles between retirements |
|---|---|
| ALU immediate, LUI, AUIPC, JAL | 367 / 390 / 416 |
| ALU register, SLT | 499 / 520 / 530 |
| Branch taken / not taken | 510 / 539 / 542 |
| JALR | 517 / 518 / 528 |
| Shift, by amount range | 503 / 520-590 / 594 |
| RAM load byte / half / word | 433-462 / 484-495 / 512-530 |
| IO load / IO store | 385-396 / 396-407 |
| RAM store byte / half / word | 725-737 / 759-770 / 774-805 |
| FENCE | 264 / 265 / 275 |
| Decode trap | 134-145 |

- Worst case: SW at 805 cycles (CoreMark max). All slot start phases are fixed, so the only
  variable wait in a store is before its fetch (0..32 clocks); the analytic bound is about 806.
  That leaves about 218 cycles of margin under the 1,024 cap.
- CoreMark: 402,600,302 cycles, 791,682 retirements, CPI 508.5 (001: 372.7, so 1.36x slower).
- selftest: 1,093,097 cycles, 2,116 retirements (section 6 left out, see below), CPI 516.6.

## 5. Tests

- `src/test_rfram/`: 001's harness, adapted.
  - The register region starts with random bytes (per seed).
  - RVFI mode: every retirement is compared with the ISS (all RVFI fields). The SPI transactions
    of each retirement must be: the fetch first, any number of 4-byte register-region
    transactions, and the data access of a RAM load or store (address, direction and every byte
    checked). After every retirement the region must hold the ISS value of every register
    written so far (layout-aware). Programs that access the region fail.
  - Pins mode: the final RAM below 0xFFC0, the final register values in the region and the
    uo_out trace are compared with the ISS.
  - The 1,024-cycle cap is checked per retirement (RVFI mode).
- Programs: setup/cosim's verif/programs, patched by `build_progs.sh` / `patch_progs.py` so no
  program touches the region: the stack starts at 0xFFC0, selftest's section 6 (misaligned
  accesses that wrap at 64 KB through 0xFFFC..0xFFFF) is left out as in the spike build, and the
  random streams keep RAM accesses below 0xFFC0.
- Runs: 14 programs (selftest, gpio_demo, trap x6, random x6) x 2 modes x 5 seeds = 140, plus
  CoreMark x 2 modes x 2 seeds = 4: 144 runs, 0 failures, at 12a5186 and at 15df205 (head's
  logic; head only changes RVFI-only code and lint, re-tested with the fast set).
- Gates run on this branch: main's `tools/src_check.py` PASS; `verilator --lint-only -Wall`,
  plain and RISCV_FORMAL builds, clean; main's `tools/equiv.py` PASS (346 $equiv cells, 24/24 TT
  output bits).
- Not run: riscv-formal, riscv-arch-test, the gate-level test and main's cosim gate. They all
  assume contract v1 (no register traffic on the pins), so they need the v2 changes below first.

## 6. Contract v2 text I would propose

**Section 3, memory map.**
- 0xFFC0..0xFFFF (64 bytes) is the register region, reserved for the core. Its contents and bit
  layout are private to the core. It holds x1..x15 and core scratch.
- A program access to the region is UNSPECIFIED. That covers fetches and data accesses whose
  bytes `(addr[15:0] + k) mod 2^16` fall in 0xFFC0..0xFFFF, in any 64 KB alias. The core may
  return or change register state, but it must still retire or trap (liveness applies) and keep
  its RVFI trace consistent. Checkers never generate such accesses. Trapping instead is possible
  but costs a 10-bit comparator on every data and fetch address; I don't recommend it.
- The test layout moves the stack top to 0xFFC0.

**Section 4, EEI.**
- Registers x1..x15 start with whatever the region holds at reset (unspecified, as today).

**Invariant 5.**
- Registers live on the die or in the register region; no hard-wired GPRs; no other core state
  in RAM visible to programs.

**Section 6, transaction rules.**
- Every SPI transaction belongs to the instruction that retires next, and finishes before its
  `rvfi_valid` with CS_n high at that cycle.
- Per instruction, in this order:
  - exactly one fetch READ covering its 4 bytes, first;
  - then any number of register-region transactions (READ or WRITE, each entirely inside the
    region; a WRITE still ends on a byte boundary);
  - interleaved with those, at most one data transaction: one READ of all bytes of a RAM load,
    or one WRITE of all bytes of a RAM store. It never touches the region.
- IO accesses never appear on the pins.

**Section 6, RVFI.**
- `rvfi_rs1_rdata` and `rvfi_rs2_rdata` report the values the core actually read. RVFI
  consistency (a register read returns the last value written) is then a property of the core
  plus the RAM, and the checkers verify it.

**Checkers.**
- **Formal.** The SPI RAM model must be a real memory for the region: per-byte state, an
  arbitrary (anyconst) initial value, WRITEs stored, READs return the stored bytes. Outside the
  region the existing model stays. The SPI monitor must classify region transactions separately
  and not match them against `rvfi_mem_*`. Without a real region memory, the reg check and every
  instruction check that reads a register are vacuous, so this model is required, not optional.
- **Cosim.** Exclude the region from the RAM image compare. Rely on RVFI for register values, or
  add an implementation-provided layout hook if register values must also be checked at the pins.
- **Gate level.** Same memory model as cosim.

## 7. Risks and what didn't work

- Formal has not been run. The v1 checkers can't model the region, so this design has only cosim
  and equivalence evidence.
- Loose decode is exercised only on legal instructions; its reserved-encoding behavior is checked
  by reasoning, not tests. Reported register indices are 4 bits (what the core actually uses).
- Hardening is not done. The two rings and `irs` are shift chains (direct flop-to-flop paths),
  which can attract hold buffers at hardening; 001 has the same structure at 5x the scale.
- Speed: CPI 508.5, 1.36x 001; every instruction moves 2-6 SPI transactions.
- One-hot re-encoding: yosys re-encoded `ep` and `sl` one-hot (142 flops). Binary codes used
  bitwise prevent it (132 flops, -245 µm²).
- Micro-optimizations within ABC's noise did not pay: indexing the immediate by `j` (+71),
  splitting the immediate index by phase parity (+60), simpler slot masks (+194 alone), always
  reloading `j` in IDLE (+130 alone). All reverted.

## 8. The pc in RAM too (optional second variant)

To be filled after the attempt.
