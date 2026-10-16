# BonettoR32V loop setup: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every gate, the scoring and the loop procedure needed to run a session-driven area loop
on a correct RV32E core for Tiny Tapeout sky130, plus a seed core that passes all of it (champion
zero).

**Architecture:**
- **The contract is at the pins.** It's the TT top module, the 23LC512 SPI protocol and the GPIO
  map, with RVFI under `ifdef RISCV_FORMAL`.
- **Three independent models check the pins:**
  - a C++ SPI RAM and GPIO model in the Verilator cosim, compared retirement by retirement against
    a C++ RV32E ISS;
  - an SV SPI monitor in the riscv-formal wrapper that feeds riscv-formal's bus checks;
  - a Verilog SPI RAM model in TT's cocotb testbench, which also runs on the gate-level netlist.
- **Scoring.** A fast LibreLane or yosys synthesis area for every candidate. For champions, an
  in-tile LibreLane fit: the smallest share of the real 1x1 TT tile they sign off in.

**Tech Stack:**
- Verilog-2005 RTL (yosys, Verilator and Icarus all accept it), Verilator 5.047, C++17.
- Python 3.13 and cocotb 2.1.
- riscv-formal (submodule `third_party/riscv-formal`, pinned c992aa6), SBY with bitwuzla.
- riscv32 gcc 15.2 (rv32e/ilp32e multilib), spike 1.1.1-dev.
- LibreLane 3.0.14 (Docker `ghcr.io/librelane/librelane:3.0.14`, native arm64), sky130A via ciel,
  tt-support-tools.

**Spec:** `docs/superpowers/specs/2026-10-16-bonettor32v-loop-setup-design.md`, plus the
interface in `docs/contract.md`. Every task reads both.

## Global Constraints

- **Top module:** `tt_um_fesens_r32v`. Pins are exactly as in contract section 1, with
  `uio_oe = 8'b0000_1011`.
- **SPI:** mode 0, 0x03 READ / 0x02 WRITE, 16-bit address, sequential with a 64 KB wrap. MISO is
  valid only while SCK is high (checkers drive noise while SCK is low). MOSI doesn't change on a
  rising SCK edge. CS_n changes only while SCK is low. WRITEs end on byte boundaries.
- **Memory map:** fetch from `pc[15:0]`; data `addr[31] = 0` goes to RAM `addr[15:0]` (misaligned
  legal); `addr[31] = 1` is GPIO (byte at `X[1:0] = 0` reads `ui_in`, other bytes 0; a store to that
  byte sets `uo_out`).
- **EEI:** RV32E. ECALL, EBREAK, a misaligned jump or branch target (`target[1] = 1`), `0x00000000`
  and `0xFFFFFFFF` trap with `rvfi_trap = rvfi_halt = 1` and no side effects, then the core halts.
  Other non-instruction encodings are unspecified. FENCE is a no-op.
- **RVFI:** NRET 1, riscv-formal's misaligned convention (exact address, LSB-aligned masks), all of
  an instruction's SPI traffic before its `rvfi_valid`, no prefetch.
- **Physical:** one posedge clock, sky130_fd_sc_hd only, signoff at 50 ns, no reliance on power-up
  values.
- **Git:** `git -c commit.gpgsign=false commit`. Agents commit on their own branch and never merge.
  No force pushes.
- **Mac load:** one heavy job at a time (formal, LibreLane). Agents use the scratchpad or `build/`
  for outputs; `build/` is gitignored.

## Review Focus

1. **A core samples MISO while SCK is low.** The cosim's noise on MISO must make it fail, and the
   formal bus check must fail. Pinned by Task 3 (noise test with a deliberately broken DUT variant)
   and Task 5 (a mutation that samples on the wrong phase).
2. **Logic hidden under `ifdef RISCV_FORMAL`.** The non-RVFI build must fail. Pinned by Task 3's
   end-to-end mode (final RAM image and GPIO trace) and Task 7's gate-level test.
3. **A slow core passes formal vacuously.** BMC depth shorter than one fetch plus execute, or a
   check that's never reachable, must be reported as a failure. Pinned by Task 5's per-check
   reachability covers.
4. **The design relies on zero-initialized flip-flops** (Verilator zeroes them by default). Pinned
   by Task 3, which runs each program under 3 `+verilator+seed` values with `--x-initial unique`.
5. **Misaligned accesses that wrap.** A LW at 0xFFFE or 0xFFFF and a SH at 0xFFFF must wrap within
   the 64 KB RAM. Pinned by Task 2's selftest cases, checked in Tasks 3 and 5.

---

### Task 1: Reference ISS

**Files:**
- Create: `verif/cosim/iss.h`, `verif/cosim/iss_main.cpp` (standalone runner),
  `verif/cosim/test_iss.py`.

**Interfaces:**
- Produces:
  ```cpp
  struct Retire {
    uint32_t pc, npc, insn;
    bool trap, halt;
    uint8_t rs1, rs2, rd;
    uint32_t rs1v, rs2v, rdv;
    uint32_t maddr;
    uint8_t rmask, wmask;
    uint32_t mrdata, mwdata;
    bool reserved;
  };
  class Iss {
   public:
    Iss();
    void load_image(const uint8_t* ram64k);
    Retire step(uint8_t ui_in);
    uint8_t uo_out() const;
    const uint8_t* ram() const;
    bool halted() const;
  };
  ```
  `reserved = true` means the encoding is unspecified: the cosim reports an error, since programs
  never contain one.
- `iss_main <prog.hex> [--max N] [--log]` prints one JSON line per retirement and exits 0 on an
  EBREAK halt.

- [ ] **Step 1:** Write `test_iss.py`. For `selftest`, `gpio_demo` and `coremark` from Task 2, it
  compares `iss_main --log` with spike's `--log-commits` (spike flags: `--isa=rv32e`, memory map
  mirrored with `-m`, `--pc`). Fields: pc, rd write and memory write. Programs only use canonical
  addresses, so the aliasing and IO differences don't show. Expect a FAIL (no `iss_main`).
- [ ] **Step 2:** Implement `iss.h` per contract sections 3 and 4. RAM is a 64 KB array; data
  bytes wrap at `addr[15:0] + k`; IO uses `addr[31]`.
- [ ] **Step 3:** Run `pytest verif/cosim/test_iss.py -v`. Expect a PASS. Add unit cases for the
  traps (ECALL, EBREAK, misaligned JALR, `0x0`, `0xFFFFFFFF`), wrap at `0xFFFF`, and IO
  read/write lanes.
- [ ] **Step 4:** Commit.

### Task 2: Test programs

**Files:**
- Create: `verif/programs/`, with:
  - `Makefile`, `crt0.S`, `link.ld`, `bonetto.h` (the `GPIO` define at `0x80000000`);
  - `selftest.S`, `gpio_demo.c`;
  - `coremark/` (copied from `~/bonetto/auto-arch-tournament/bench/programs/coremark` with a
    port for this platform);
  - `gen_random.py`.

**Interfaces:**
- Produces: `make -C verif/programs` builds `build/programs/<name>.elf` and `<name>.hex`, a 64 KB
  byte image read by `$readmemh` (one byte per line, 65,536 lines). `<name>.expect.json` holds the
  ISS's final `uo_out` trace and its retired-instruction count.
- Flags: `-march=rv32e -mabi=ilp32e`. Memory: RAM `0x0`-`0xFFFF`, stack at the top, IO at
  `0x80000000`. Programs end with `ebreak`.

- [ ] **Step 1: selftest.S.** Every RV32E instruction at least once; every branch taken and not
  taken; x0 writes; JALR with bit 0 set; misaligned LW/LH/SW/SH, including the wraps at
  `0xFFFE`/`0xFFFF`; GPIO read and write; FENCE. It ends with a pass/fail byte written to GPIO
  (`0xA5` pass, `0xEE` fail) and then EBREAK.
- [ ] **Step 2: gpio_demo.c.** Counts on `uo_out`, reads `ui_in` and echoes it XOR a pattern,
  under 2,000 instructions. This is the program the gate-level test runs.
- [ ] **Step 3: coremark.** ITERATIONS=1, TOTAL_DATA_SIZE=2000, `-O2`, with the results printed to
  GPIO bytewise or written to a RAM signature area. It must print "Correct operation validated"
  through a RAM log buffer that the harness dumps.
- [ ] **Step 4: gen_random.py SEED N.** Emits a `.S` with N valid-only RV32E instructions: random
  ALU ops, loads and stores into a reserved window `0x8000`-`0x8FFF`, misaligned included, forward
  branches, GPIO accesses, then EBREAK. It's deterministic per seed.
- [ ] **Step 5:** Build everything. Running `iss_main` on each hex halts, and selftest leaves
  `uo_out = 0xA5`. Commit.

### Task 3: Pin-level cosim harness

**Files:**
- Create: `verif/cosim/`, with:
  - `spiram.h` (23LC512 model plus waveform rule checker);
  - `harness.cpp` (Verilator main);
  - `build.sh`;
  - `run_cosim.py`;
  - `test_cosim_harness.py`;
  - `mutants/` (broken DUT variants for the harness's own tests).

**Interfaces:**
- Consumes: `Iss` (Task 1), `build/programs/*.hex` (Task 2), the DUT in `src/`.
- Produces:
  - `run_cosim.py --mode {rvfi,pins} --prog <hex> --seed S --max-cycles C` exits 0 on a pass and
    prints one JSON summary line:
    `{"prog","mode","seed","pass","retired","cycles","cpi","max_cycles_per_insn","error"}`.
  - `max_cycles_per_insn`, the worst gap between retirements, is consumed by Task 5's depth
    derivation.
  - `make cosim` runs every program in both modes under seeds {1, 2, 3}.
- Modes:
  - `rvfi` builds with `-DRISCV_FORMAL` and compares every retirement field with `Iss::step`.
  - `pins` builds without the define. It compares the final RAM image, the `uo_out` change trace
    and the halt against the ISS.
- Latency cap: a watchdog fails the run if any retirement gap exceeds 1,024 cycles (or the first
  retirement comes later than 1,024 cycles after reset), per contract section 4.

- [ ] **Step 1:** Write `test_cosim_harness.py`. Each mutant must FAIL with a specific error:
  - samples MISO while SCK is low;
  - changes MOSI on the rising SCK edge;
  - CS_n toggles while SCK is high;
  - WRITE ends mid-byte;
  - a wrong RVFI rd value;
  - a behavior difference only without RVFI.
  The seed must PASS. Run it and expect failures (no harness yet).
- [ ] **Step 2:** Implement `spiram.h`:
  - evaluated after every clock edge;
  - decodes CS_n, SCK and MOSI per contract section 2;
  - drives MISO with the data bit while SCK is high and with `rand()` while it's low;
  - stops with an error on any waveform-rule violation.
- [ ] **Step 3:** Implement `harness.cpp` and `build.sh`. Build with Verilator `--x-initial unique`
  and `--x-assign unique`, seeded by `+verilator+seed`. Drive `ui_in` from a deterministic function
  of the retirement count. Match transactions against RVFI in `rvfi` mode.
- [ ] **Step 4:** Run `pytest verif/cosim/test_cosim_harness.py -v` and expect a PASS. Run
  `make cosim` on the seed and expect a PASS.
- [ ] **Step 5:** Commit.

### Task 4: Seed core

**Files:**
- Create: `src/tt_um_fesens_r32v.v`, plus any submodules in `src/`.

**Interfaces:**
- Produces: the contract-compliant top module. Plain and correct; not optimized.
- Microarchitecture (seed only):
  - an FSM that goes FETCH (SPI READ of 4 bytes into a 32-bit shift register), EXECUTE (32-bit
    ALU, barrel shifter, a 15x32 flip-flop register file), MEM (SPI READ or WRITE of 1/2/4 bytes at
    the exact address, so misaligned accesses come for free), WB, then HALT;
  - SCK toggles every cycle while active (SCK = clk/2), and MISO is captured on the edge where SCK
    falls;
  - RVFI is generated under `ifdef RISCV_FORMAL` only.

- [ ] **Step 1:** Write `src/test_seed_smoke.py`, a small cocotb test with a Python SPI RAM. It
  runs a 10-instruction program and checks `uo_out`. Expect a FAIL.
- [ ] **Step 2:** Implement the core. Lint it with `verilator --lint-only -Wall`, clean.
- [ ] **Step 3:** The smoke test passes. Then `make cosim` passes (once Task 3 lands).
- [ ] **Step 4:** Commit.

### Task 5: Formal at the pins

**Files:**
- Create: `verif/formal/`, with:
  - `isa_rv32e.txt` and `isa_rv32e.v` (generated from `isa_rv32i.txt` by `gen_isa_rv32e.py`);
  - `wrapper.sv` (TT top with RVFI, SPI monitor, GPIO checks, protocol assertions);
  - `checks.cfg`, `checks-deep.cfg`;
  - `run_formal.py`, `test_formal.py`, `mutants/`.

**Interfaces:**
- Consumes:
  - `max_cycles_per_insn` and the per-class first-retirement cycle, both from Task 3 (`run_cosim.py
    --mode rvfi --prog build/programs/selftest.hex --timing-json`);
  - riscv-formal checks.
- Produces: `run_formal.py [--deep]` exits 0 on a pass and writes `build/formal.json`:
  `{"pass", "checks": {name: "PASS|FAIL|UNREACHED"}, "depth", "runtime_s"}`. Any `UNREACHED` counts
  as a FAIL.
- Rules:
  - **Contract-owned forks in `verif/formal/checks/`, not edits to the submodule:**
    - **Testbench.** A 16-bit cycle counter. The insn, reg, pc, causal, unique and bus checks use
      `RISCV_FORMAL_UNBOUNDED`, so the solver picks the check cycle and BMC covers every retirement
      up to the depth.
    - **Liveness.** It triggers on any retirement (free `trig`) and checks at trigger + 1,024 (the
      contract's latency cap), as far as the depth allows.
    - **Bus checks.** Misaligned-aware (shadow bytes, not words), aware of the 16-bit alias, and
      blind to GPIO (`addr[31]` accesses).
    - **New models and checks.** ECALL, EBREAK and FENCE models, and a "silent after halt" check
      (no retirement and no SPI WRITE after `rvfi_halt`).
  - **Depth.** D is the deepest the Mac finishes within the time budget, and it must cover at least
    two retirements of the measured common case (`reset + 2 * max_cycles_per_insn` from
    selftest). D is recorded in `build/formal.json` and never set by an agent. Slower instances are
    left to cosim and the architecture tests: depth-limited proofs plus cosim is the
    per-candidate policy.
  - **No ALIGNED_MEM.** Misaligned accesses are architecture.
  - **E filter.** Encodings that name x16-x31 are excluded from the instruction checks
    (`spec_valid` gated) and left to reg, pc and causal consistency.
  - **Monitor.**
    - The SPI monitor reconstructs transactions and drives `rvfi_bus_*`, covering fetch, load and
      store bytes.
    - MISO is `$anyseq`, constrained stable while SCK is high.
    - `ui_in` is held constant.
    - It asserts the waveform rules.
  - **GPIO check.** It asserts the `uo_out` and `ui_in` semantics.
  - **Checks:** insn (all), reg, pc_fwd, pc_bwd, causal, unique, liveness (`rvfi_halt` excludes
    the trapping retirement), cover, ill (zero), and the bus imem and dmem checks.
  - **Reachability.** Each check gets a cover task proving its trigger is reachable within the
    depth.

- [ ] **Step 1:** Write `test_formal.py`. Each mutant (the same classes as Task 3, plus a decoder
  bug in SUB) must FAIL. The seed must PASS. Run it and expect failures.
- [ ] **Step 2:** Write `gen_isa_rv32e.py`, the wrapper and the configs, plus a runner that stages
  `src/*.v` into a per-PID work dir (pattern: `~/bonetto/auto-arch-tournament/formal/run_all.sh`).
- [ ] **Step 3:** Run it on the seed. Record each check's runtime in `build/formal.json`. If the
  full suite takes more than 60 minutes on the Mac, split it: `checks.cfg` (per candidate) keeps
  insn, reg, pc and bus at reduced depth, and `checks-deep.cfg` (champions) runs everything. Write
  the measured numbers in `docs/formal.md`.
- [ ] **Step 4:** `pytest verif/formal/test_formal.py -v` passes. Commit.

### Task 6: riscv-arch-test

**Files:**
- Create: `verif/arch-test/`, with:
  - `riscv-arch-test` (a pinned submodule under `third_party/`);
  - `model_test.h` (boot, halt by EBREAK, signature begin/end symbols, no CSRs);
  - `run_arch.py`.

**Interfaces:**
- Produces: `run_arch.py`. It builds every RV32E base test, runs each on spike for the reference
  signature (cached in `build/arch/ref/`) and on the DUT through `run_cosim.py --mode pins --dump
  sig`. It writes `build/arch.json` as `{"pass", "tests": {name: bool}}`.
- Skip only tests that need CSRs or trap handlers: list each skip and its reason in
  `docs/arch-test.md`. Tests of misaligned data access run, because we support it.

- [ ] Steps: build one test (`add-01`) on spike, then the DUT, then all tests. Expect all PASS on
  the seed. Commit.

### Task 7: Gate-level test (TT test/ directory)

**Files:**
- Create: `test/tb.v` (TT top plus `spiram_23lc512.v`), `test/spiram_23lc512.v`, `test/test.py`,
  `test/Makefile` (TT's template variables, `PROJECT_SOURCES` from `info.yaml`),
  `test/gpio_demo.hex` (committed, so TT's CI needs no gcc).

**Interfaces:**
- Consumes: `build/programs/gpio_demo.hex` and `.expect.json` (Task 2). A yosys netlist from
  Task 8 (`build/synth/netlist.v`), or TT's `gate_level_netlist.v` in CI.
- Produces:
  - `make -C test` (RTL) and `make -C test GATES=yes` (gate level) pass when the `uo_out`
    sequence equals `gpio_demo.expect.json`.
  - `make gl` runs the gate-level version locally on the yosys netlist with the sky130 cell models.
- The Verilog SPI RAM follows contract section 2: X on MISO while SCK is low, and `$fatal` on rule
  violations.

- [ ] Steps:
  1. Write the test. It fails on the template DUT.
  2. Implement the RAM model and `tb.v`.
  3. Pass on RTL.
  4. Pass at gate level.
  5. Commit.

### Task 8: Area, hardening and tile-fit tooling

**Files:**
- Create:
  - `tools/area.py`, `tools/synth_area.ys`;
  - `tools/harden_tt.sh` (tt-support-tools `--create-user-config` then `--harden`, LibreLane in
    Docker, PDK via ciel at the version LibreLane 3.0.14 pins);
  - `physical/tilefit.py`;
  - `docs/baselines.md`.

**Interfaces:**
- Produces:
  - `area.py --top tt_um_fesens_r32v --src src/` writes `build/area.json`:
    `{"area_um2", "cells", "by_type": {...}, "by_module": {...}, "flops", "latches", "netlist"}`.
    It uses LibreLane's `Yosys.Synthesis` step with TT's config if one run takes under 60 s,
    otherwise `synth_area.ys` mirroring LibreLane's script (`synth -flatten`, the `AREA 0` ABC
    script, `dfflibmap`, sky130_fd_sc_hd `tt_025C_1v80` lib). `build/synth/netlist.v` is
    consumed by Task 7.
  - `harden_tt.sh` writes `build/harden/summary.json`:
    `{"pass", "drc", "lvs", "antenna", "setup_wns", "hold_wns", "utilization", "die_um2"}`.
  - `tilefit.py` runs inside the real TT 1x1 template (`tt_tool` config, same die, pins and PDN).
    It adds a hard placement blockage over the right-hand part of the core area, leaving routing
    free, and binary-searches the blockage width for the smallest placeable fraction at which
    every signoff check in `harden_tt.sh` stays clean. Output: `build/tilefit.json` as
    `{"min_fraction", "placeable_um2", "density", "runs": [...]}`.
- Baselines: NanoV-1tile (github.com/WaiMingLee888/nanov-1tile-tinytapeout-sky) and SERV
  (github.com/olofk/serv, with the RF in its own flip-flops), each cloned into the scratchpad and
  run through `area.py`. Numbers go in `docs/baselines.md`.

- [ ] Steps:
  1. `area.py` on NanoV gives a number.
  2. Then on the seed.
  3. `harden_tt.sh` on NanoV passes (it proves the local flow).
  4. Then on the seed (at the tile size that fits it).
  5. Run `tilefit.py` on whichever of NanoV and the seed fits in 1x1.
  6. Commit.

### Task 9: Eval, contract check, ledger

**Files:**
- Create: `Makefile`, `tools/eval.py`, `tools/contract_check.py`, `tools/ledger.py`,
  `loop/ledger.jsonl`, `loop/LEADERBOARD.md`, `tools/test_contract_check.py`.

**Interfaces:**
- Produces:
  - `make eval` runs lint, cosim, equiv, formal (fast), arch, area and gl in that order and stops
    at the first failure.
  - `make equiv` (`tools/equiv.py`):
    1. Synthesize the RISCV_FORMAL build with yosys, delete its `rvfi_*` ports and run
       `opt_clean -purge`.
    2. Prove it equivalent to the plain build: `equiv_make`, `equiv_simple -seq 5`,
       `equiv_induct`, `equiv_status -assert`. Fall back to eqy if the yosys equiv flow can't
       match the state.
    3. Test it with a mutant where RVFI logic drives `uo_out`, which must FAIL. It writes `build/eval.json`: `{"sha", "pass", "gates": {name: {"pass",
    "detail"}}, "area_um2", "cells", "cpi", "timestamp"}`.
  - `contract_check.py BASE HEAD` exits 1 when any changed path is outside `src/**` (minus
    `src/config.json`), `physical/**` (when `--physical`) or `src/test_*`. It also fingerprints the
    don't-touch tree.
  - `ledger.py add --id --branch --sha --idea --eval build/eval.json --verdict
    {merged,rejected,failed}` appends one JSON line. `ledger.py board` regenerates
    `LEADERBOARD.md`.

- [ ] Steps:
  1. Tests for `contract_check` (an allowed diff passes; `verif/` and `src/config.json` fail).
  2. Implement.
  3. `make eval` on the seed gives `pass: true`.
  4. Ledger entry 000 is the seed.
  5. Commit.

### Task 10: The loop skill

**Files:**
- Create: `.claude/skills/area-loop/SKILL.md`, `.claude/skills/area-loop/agent-brief.md`.

**Interfaces:**
- The skill is the lead's round procedure from the spec ("The loop"), with exact commands:
  1. Read the evidence: `loop/ledger.jsonl`, `build/area.json` by_type and by_module.
  2. Rank hypotheses.
  3. Spawn agents: Agent tool, Opus, `isolation: worktree`, the brief filled in.
  4. Verify: a clean worktree at the reported sha, `make eval`, `contract_check.py`.
  5. Merge sequentially with a re-eval after each.
  6. Ledger, push, worktree cleanup.
  The skill also covers champion rounds (harden, mindie, deep formal, TT CI) and physical rounds.
- `agent-brief.md` is the template: the hypothesis, the contract pointers, allowed paths, the
  `make eval` loop, the report format (sha, area before and after, gates, what it tried) and the
  rules (no edits outside allowed paths; report evaluation doubts instead of working around them).

- [ ] Steps: write both files, then a dry-run read for consistency with Task 9's commands. Commit.

### Task 11: Champion zero and CI

- [ ] **Step 1:** `make eval` passes on main. Then run `make harden` for the tile config the seed
  needs, and `make tilefit` if it fits in 1x1.
- [ ] **Step 2:** Set the `info.yaml` tiles and source_files. Re-enable the gds, test and docs
  workflows (`gh workflow enable`), push, and confirm the TT gds, precheck and gl_test runs are
  green.
- [ ] **Step 3:** Ledger entry 000 holds the seed's numbers, and the leaderboard is committed.
  Report area and the NanoV and SERV baselines to the user.
