# BonettoR32V: smallest correct RV32E for Tiny Tapeout, and the loop that shrinks it

Date: 2026-10-16. Status: draft for the user's approval.

## Intent

Build the smallest correct RISC-V core that runs gcc-compiled programs and drives GPIO on a Tiny
Tapeout (TT) sky130 die. This is step one toward a complete microcontroller (core + GPIO + more
peripherals) inside a single 1x1 TT tile, so every square micron the core saves is room for a
peripheral later.

The core gets small through a loop, like the openTPU work: this Claude Code session is the lead.
It proposes area hypotheses, runs one agent per hypothesis in its own git worktree, re-runs a
fixed evaluation itself, merges winners and logs every result. This document covers the setup
that makes that loop possible: the repo, the contract, the verification, the scoring, the seed
core and the loop procedure. Running the loop comes after.

## Goals

1. A correct RV32E hart (definition below) behind Tiny Tapeout's standard top module and pins,
   with programs and data in an external SPI RAM and memory-mapped GPIO.
2. Minimum silicon area, at both levels:
   - RTL: the fewest and smallest sky130_fd_sc_hd cells;
   - physical: the densest placement and routing that still passes every signoff check.
3. Manufacturable: hardens with TT's own flow (LibreLane 3.0.14, sky130A, the `ttsky26d`
   gds / precheck / gl_test actions) into a 1x1 tile.
4. A loop the lead session can run round after round with no human in it, where an agent can't
   pass a candidate by weakening the evaluation.
5. A private GitHub repo holding all of it.

## Non-goals (for now)

- Performance. It isn't scored at all, beyond a generous cycle cap so tests finish.
- Peripherals beyond 8-in / 8-out GPIO (UART, timer, SPI, interrupts). They're the next phase.
  Pins uio[4..7] stay free for them.
- CSRs, interrupts, privilege modes, M/C/Zicsr extensions.
- Hand-drawn transistor layout or custom cells (the user ruled it out for now).
- Submitting to a shuttle. The repo stays TT-submittable, but submission is a later decision.

## Anti-goals (failure even if every test passes)

- Anything that isn't really RISC-V: hard-wired registers (NanoV ties x3/x4 to constants),
  architectural state kept off the die (registers in the SPI RAM), a missing instruction, or an
  address-space hack that breaks the spec.
- A candidate that wins by changing the evaluation instead of the design.
- An area "win" that only exists in synthesis and disappears in hardening.
- A design that passes RTL simulation but not its own gate-level netlist (for example, logic
  hidden behind `ifdef RISCV_FORMAL`).

## Decisions

### Decided by the user
- ISA: **RV32E** (16 registers, ratified base).
- Registers on the die; only programs and data live off-chip.
- It must execute programs and drive GPIO.
- No hand-drawn transistor layout for now. Custom macros built from standard cells are allowed.
- Space matters most: the eventual target is a full microcontroller in a 1x1 tile.

### My calls (the user can override any of them)

**Pins (TT's recommended SPI pinout, which the demo board's RP2040 RAM emulator uses by default):**

| Pin | Use |
|---|---|
| uio[0] | SPI CS_n (out) |
| uio[1] | SPI MOSI (out) |
| uio[2] | SPI MISO (in) |
| uio[3] | SPI SCK (out) |
| uio[7:4] | unused for now: inputs, `uio_oe = 0` (reserved for peripherals) |
| uo[7:0] | GPIO out |
| ui[7:0] | GPIO in |
| rst_n, clk | reset, clock; `ena` ignored |

**Memory protocol: 23LC512-compatible SPI RAM.** SPI mode 0, READ 0x03 and WRITE 0x02, a 16-bit
address, sequential mode (bytes stream from the start address, wrapping at 64 KB). This is TT's
documented RP2040 emulation and works with a real 23LC512. Each core picks its own SCK rate and
holds CS_n low as long as it likes; the memory models accept any legal mode-0 waveform.

**Memory map:**
- Instruction fetches always read SPI RAM at `pc[15:0]`. The RAM aliases every 64 KB.
- Data accesses with address bit 31 = 0 go to SPI RAM at `addr[15:0]`, wrapping within the 64 KB.
- Data accesses with bit 31 = 1 are GPIO:
  - A byte read at an address with `addr[1:0] == 0` returns `ui_in`. Other IO bytes read 0.
  - A store that writes the byte at an address with `addr[1:0] == 0` sets `uo_out`. Other IO byte
    writes are ignored.
  - `uo_out` is 0 after reset.
- Misaligned RAM loads and stores are supported (no `RISCV_FORMAL_ALIGNED_MEM`). With sequential
  SPI this comes for free, while trapping would cost logic.
- Misaligned IO accesses are unspecified, and no check depends on them.

**Execution environment (the RISC-V "EEI"; I've taken the spec's minimum):**
- Every trap is fatal: the core retires the trapping instruction with `rvfi_trap = 1`, then halts
  for good. Traps are ECALL, EBREAK, a jump or taken branch to an address that isn't 4-byte
  aligned, the all-zeros instruction and the all-ones instruction.
- The spec calls every other non-instruction encoding "reserved", and the behavior of reserved
  encodings is UNSPECIFIED. That includes RV32E encodings that name x16-x31. A core may do anything
  with them, as long as its RVFI trace stays self-consistent (the register, PC and memory
  consistency checks still apply to every retirement). The cheap behavior, ignoring bit 4 of
  register fields, is allowed.
- FENCE (including FENCE.TSO and PAUSE) is a no-op.
- Tests end with EBREAK. The testbench sees the halt and reads results from RAM.

**Clock and signoff:** STA closes at 20 MHz (50 ns, `CLOCK_PERIOD` in TT's `src/config.json`) at
every corner TT's flow checks. A slower target needs fewer upsized cells and leaves timing room for
latch-based register files later. Contract v1 is posedge-only with no negative-edge logic. The RP2040 emulator and the 23LC512 both run comfortably below this.

**Cells:** sky130_fd_sc_hd only (TT's library). Latches are allowed.

### Open: needs the user
- **Reserved encodings.** Above, I took the spec's minimum: only all-zeros, all-ones, ECALL, EBREAK
  and misaligned jumps must trap, and every other reserved encoding is unspecified. The stricter
  alternative is "every encoding that isn't a valid RV32E instruction traps", which is what most
  platforms require. It costs a full decoder, likely a few hundred µm².

## Correctness: what a candidate must pass

All of these gates are fixed and outside what agents may edit. A candidate scores only if every
one passes.

1. **Lint:** Verilator `-Wall` lint plus TT's linter config, both clean.
2. **Formal, at the pins.** riscv-formal, pinned as a submodule, runs on the real TT top module,
   with RVFI ports added only under `ifdef RISCV_FORMAL`:
   - An `rv32e` ISA config, written by us from the rv32i instruction models. Encodings that name
     x16-x31 count as reserved, so the instruction checks skip them.
   - These checks: every instruction check, reg, pc_fwd, pc_bwd, causal, unique, liveness (allowing
     for the halt), cover, and the all-zeros `ill` check.
   - **Bus checks.** The formal wrapper contains a passive SPI-slave monitor that decodes the pin
     traffic into transactions. These feed riscv-formal's bus checks (`rvfi_bus_imem` /
     `rvfi_bus_dmem`), so the proof covers the pins themselves: the fetched bytes are the
     instruction, load data came from the addressed bytes, and stores wrote exactly the claimed
     bytes.
   - A small custom check ties GPIO stores to `uo_out` and GPIO loads to `ui_in`.
   - **Contract-owned forks.** riscv-formal's stock testbench counts cycles in 8 bits and stops at
     255. Its bus checks assume aligned memory and full 32-bit bus addresses. So `verif/formal`
     carries its own testbench and bus checks: misaligned-aware, aware of the 64 KB alias, with GPIO
     handled by its own check. It also adds ECALL, EBREAK and FENCE models and a "silent after halt"
     check.
   - **Depth policy.** Instructions with data-dependent latency mean no BMC depth covers every slow
     instance on the Mac. So per-candidate formal is depth-limited:
     - it proves every instruction instance that retires within depth D, plus liveness up to the
       contract's latency cap where feasible;
     - D is set by measured runtime, and reachability covers prove the checks aren't vacuous;
     - slow instances (for example large shift amounts) are covered by cosim and the architecture
       tests, which hit every shift amount.
     Champions also run a deeper suite; a champion that fails it is rolled back.
3. **Cosim at the pins.** Verilator runs the TT top module against a C++ model of the 23LC512 and
   the GPIO pins, retirement by retirement against a reference ISS (RV32E with this exact memory
   map and trap behavior). Programs:
   - a hand-written selftest;
   - a GPIO program;
   - CoreMark built for rv32e;
   - seeded random instruction streams.
4. **Official architecture tests:** riscv-arch-test, RV32E base subset, with signatures compared
   against the reference.
5. **Gate-level simulation:** the selftest and the GPIO program, run on the synthesized sky130
   netlist. This catches differences between simulation and synthesis.
6. **Equivalence:** the RISCV_FORMAL build, with its RVFI ports stripped and the logic that only
   feeds them removed, must be sequentially equivalent to the plain build (yosys `equiv_*` or eqy).
   Formal then proves the design that ships, and RVFI logic can't influence the pins.
7. **Latency cap:** every instruction retires within 1,024 cycles of the previous retirement, and
   the first within 1,024 cycles of reset (contract section 4). Cosim enforces it on every
   instruction. It bounds tests and formal depth, not speed.
8. **Contract check:** the candidate's diff only touches allowed paths. Contract files are
   fingerprinted before and after the agent runs.

The ISS, the memory models and the formal wrapper are cross-checked once at setup, against each
other and against the seed core.

## Scoring

- **Fast score (every candidate):** total cell area in µm² of the full TT top module, from a fixed
  yosys script (flatten, sky130_fd_sc_hd typical-corner liberty, ABC, `stat -liberty`). It's
  deterministic, takes seconds and is broken down by cell type and module. A smaller number wins.
  Ties go to fewer cells, then fewer cycles on the test programs.
- **Physical score (champions, and physical hypotheses):** measured inside the real TT 1x1 tile
  template (its die, pins and PDN), because that's where the microcontroller has to fit. It's the
  smallest fraction of the tile's core area the design can be squeezed into, by a hard placement
  blockage over the rest. Signoff must stay clean throughout: DRC (magic and klayout), LVS,
  antenna, setup and hold at all corners, and an IR-drop report. The rest of the tile is room for
  peripherals. This number is what "packed" means: it charges for routing overhead, not just
  cells.
- **Manufacturability gate (each new champion):** `tt_tool --harden` into a real 1x1 tile, locally,
  plus TT's gds / precheck / gl_test GitHub Actions on the private repo.
- **Reference points:** NanoV-1tile and SERV, run through the same area script, give the numbers to
  beat.

## Physical design levers (all inside the loop, all under the same signoff)

- Flow knobs: placement density, `GRT_ALLOW_CONGESTION`, and the PDN / tap / decap strategy where
  TT's config allows it.
- Standard-cell macros:
  - hand-placed regular arrays, such as the latch register file with shared write and read lines
    (needs contract v1.1: clock-gated latches with contract-owned cell models for formal and
    cosim, since the PDK's UDP models can't be read by yosys or Verilator);
  - hardened as sub-blocks or placed by script, then integrated.
  LVS plus gate-level simulation cover them exactly, since they're built only from standard cells.
- No custom cells and no hand-drawn layout (non-goal above).

## Repo layout

The repo is shaped like TT's sky130 template, so it's submittable as is. `src/` is the design.

```
BonettoR32V/                    private GitHub repo FeSens/BonettoR32V
  info.yaml                     TT metadata (1x1, tt_um_fesens_r32v)
  src/                          THE design: hypotheses edit only this (and src/test_*)
    config.json                 TT flow config (contract: only the TT-allowed knobs, by the lead)
  test/                         TT cocotb testbench (contract)
  verif/                        contract
    formal/                     wrapper, SPI monitor, rv32e ISA files, check configs, runner
    cosim/                      Verilator harness, 23LC512 + GPIO model, reference ISS
    programs/                   selftest, GPIO demo, CoreMark (rv32e), random-stream generator
    arch-test/                  riscv-arch-test target for this platform
  physical/                     LibreLane configs, in-tile blockage search, macro scripts (lead-owned;
                                physical hypotheses edit here)
  third_party/riscv-formal      pinned submodule
  tools/                        eval: lint, area, harden, score, contract check, ledger (contract)
  loop/
    ledger.jsonl                every hypothesis: id, idea, branch, sha, gates, scores, verdict
    LEADERBOARD.md              champion history
  .claude/skills/area-loop/     the lead's loop procedure (like efficiency-hunt in openTPU)
  CLAUDE.md                     the contract: invariants, don't-touch list, loop rules
  docs/                         protocol, memory map, EEI, results, this spec
  Makefile                      lint / formal / cosim / arch / gl / area / harden / eval
```

## The seed core

- A deliberately plain, correct RV32E: multi-cycle, a register file in flip-flops, and a simple
  SPI shifter.
- Its only job is to prove every gate end to end and to be champion zero.
- The loop does the cleverness. The first rounds will be the large structural wins: bit-serial
  datapath, a latch register file, a fused SPI and datapath shift chain, a minimal decoder.

## The loop (session-driven)

Each round, the lead does six things.

1. **Read the evidence.** The ledger, the champion's area breakdown (by module and cell type,
   flip-flop and latch counts) and its latest physical score.
2. **Propose hypotheses.** List them, ranked by expected µm² saved times confidence. Check them
   against the ledger so known failures aren't repeated.
3. **Spawn agents.** One per hypothesis, Opus only, each in its own worktree on branch
   `hyp/NNN-slug`. Each gets a brief, the contract and `make eval`. The agent implements the
   hypothesis, iterates until the gates pass, commits and reports.
4. **Verify.** The lead re-runs `make eval` itself on the branch's exact sha in a clean worktree
   and runs the contract check. The agent's report is never taken on faith.
5. **Merge.** Winners merge into main one at a time, and each is re-evaluated after merging,
   because two wins can conflict. Then push.
6. **Clean up and log.** Log every hypothesis, failures included. Remove finished worktrees but
   keep their branches.

On top of the rounds:

- **Champion rounds**, when the champion changes: hardening, the in-tile blockage search, TT actions and the
  deep formal configuration.
- **Physical rounds:** hypotheses on floorplan and macros, scored on the in-tile physical score.

The procedure lives in `.claude/skills/area-loop/SKILL.md`, so any later session can pick up the
loop.

## Constraints

- **Hosts.** The Mac (10 cores, 32 GB) runs the gates, at one heavy formal or hardening job at a
  time. GitHub Actions runs TT's flow on the private repo. hwe-bench is an option until it's shut
  down on 2026-10-31, but it hosts other projects, so it isn't planned on.
- **Tools on hand:**
  - yosys 0.64, Verilator 5.047, SBY with bitwuzla;
  - riscv32 gcc 15.2 with rv32e/ilp32e multilibs;
  - the `openroad/orfs` Docker image (sky130hd liberty);
  - Nix, for LibreLane 3.0.14 and the sky130A PDK.
- **Risk: pin-level formal is slow.** A fetch over SPI costs at least 56 SCK cycles, so BMC depths
  reach the hundreds. If the full suite is too slow per candidate, every candidate runs a fast
  subset plus heavy cosim and the full suite gates the champion. The numbers come from the seed.

## Left to the builder

- Module structure of the seed, the exact yosys script, the ISS implementation language, test
  program contents.
- How the SPI monitor feeds riscv-formal's bus checks, and the exact depth formula.
- The blockage search method (which edge, binary search granularity).
- The ledger's schema and the agent brief template.

## Addendum (2026-10-16): measured budget and the microcontroller

- **What a 1x1 tile holds.** NanoV-1tile's signoff gives a die of 161 x 111.52 µm and a core of
  16,493 µm² (instance area). Its 14,653 µm² synthesized design routed at 95% target density.
  With its register file on the die, NanoV needed 21,578 µm² (135%) and didn't fit. It reached 1x1
  only by moving registers into the SPI RAM, which our contract forbids.
- **Our floor.** The architectural state alone (480 register bits, 32 PC bits, 8 GPIO bits) is
  about 520 flip-flops, roughly 10.4-11k µm² depending on the flip-flop size synthesis picks. The
  core will therefore be about 12-14k µm² unless storage gets denser than flip-flops.
- **The microcontroller target** (asked by the user, my estimate, not measured):
  - minimum viable set: GPIO direction on uio[7:4], a timer with compare, UART TX, interrupts
    (minimal Zicsr: mepc, mcause, MIE, mret, fixed mtvec), boot from SPI flash;
  - UART RX and hardware PWM if area allows;
  - SPI and I2C to peripherals bit-banged.
  The set is about 4-5.5k µm². Fitting core plus MCU in 1x1 needs the core around 9-10k µm², which
  likely needs latch storage (contract v1.1) or a better idea. Peripherals and interrupts are
  contract changes (pins, memory map, ISA) and go to the user before they're adopted.
