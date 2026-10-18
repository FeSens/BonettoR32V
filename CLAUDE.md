# BonettoR32V: rules for every session and agent

Goal: the most performant RV32E microcontroller (CoreMark iterations per second) in a 1x2 Tiny
Tapeout sky130 tile. Code runs from the TT QSPI Pmod's flash, data lives in its PSRAM, and every
peripheral stays (GPIO, timer, PWM, UART, SPI, interrupts). The score is `coremark_ips` = f_clk /
CoreMark cycles per iteration, at the signoff clock (`CLOCK_PERIOD`, 20 ns = 50 MHz since hyp 102). A candidate must pass every gate and
harden clean in 1x2; synthesized area breaks ties.

- `docs/contract.md`: the exact interface (pins, the QSPI Pmod rules, memory map, EEI and CSRs,
  peripherals, the score, RVFI). Read it first. `docs/contract-v2.md` is the superseded v2 text,
  kept as history.
- `docs/superpowers/specs/2026-10-17-mcu-v3-draft.md`: the MCU design and its rationale;
  `docs/superpowers/specs/2026-10-16-bonettor32v-loop-setup-design.md`: the loop's setup.
- `.claude/skills/area-loop/SKILL.md`: how the lead runs the optimization loop (the performance
  loop; the directory keeps its old name).

## Hard invariants (each enforced by a gate that fails loudly)

| # | Invariant | Gate |
|---|---|---|
| 1 | RV32E + Zicsr per docs/contract.md section 4: every instruction correct; traps and the interrupt go to `mtvec` (nothing halts); the CSRs behave per section 4 | cosim against the ISS (rvfi mode, lockstep, every RVFI field), riscv-arch-test (53 tests, trap tests included), riscv-formal (`make eval FORMAL=1`) |
| 2 | The Pmod pins follow contract section 2 (waveform rules, each chip's command legality, power-up modes, tCEM); every PSRAM write outside the core region writes bytes of retired stores at their addresses (section 8); loads and fetches are coherent (section 3) | cosim Pmod chip models on every cycle in both modes and all four power-up modes, PSRAM attribution and final compare, the `coherence` program; gate-level test on the same Pmod model; formal bus monitor (`FORMAL=1`) |
| 3 | GPIO and every peripheral behave per contract sections 5 and 6, and every peripheral stays (section 7) | cosim peripheral pin checkers (UART, SPI, PWM, timer, GPIO timing and edges, interrupt line) and the programs' self-checks; gate-level test; formal peripheral register check (`FORMAL=1`) |
| 4 | Logic under `ifdef RISCV_FORMAL` only observes; the design without it behaves identically | equivalence gate (RVFI-stripped build equals plain build), pins-mode cosim, gate-level test |
| 5 | Architectural state (x1..x15, pc, CSRs) lives on the die or in the PSRAM core region 0x7F_FF80-0x7F_FFFF (contract section 3); no hard-wired GPRs; no other core state in memory | review, cosim (random core region at reset, PSRAM write attribution), formal (`FORMAL=1`) |
| 6 | The design doesn't rely on power-up values, of its flip-flops or of the memories' modes | cosim with random flip-flop init and the four memory power-up modes (seeds 1-4), gate-level X propagation |
| 7 | Plain RTL, one posedge clock, no latches (contract section 1) | lint (src_check and Verilator), the area gate's latch count, review |
| 8 | Every instruction retires within 2,048 cycles of the previous one, the first within 2,048 of reset (memory reset sequence included); exempt: a WFI stall while `mip.MEIP & mie.MEIE` is 0 | cosim watchdog on every program, the liveness fuzz (seeds 1-5), formal liveness (`FORMAL=1`) |
| 9 | A candidate's diff touches only allowed paths | `tools/contract_check.py` |

## Don't-touch list (hypothesis agents never modify these)

`verif/`, `tools/`, `test/`, `third_party/`, `info.yaml`, `src/config.json`, `docs/contract.md`,
`CLAUDE.md`, `Makefile`, `.github/`, `loop/`, `.claude/`.

The hypothesis is "what design change makes CoreMark faster (or the chip smaller at the same
speed)", never "what evaluation change lets this design pass". If a gate looks wrong, report it to
the lead; don't route around it.

## What hypotheses may change

- `src/**`, except `src/config.json`. That covers any restructuring: merge or split modules,
  rename files, serial or parallel datapaths, caches and prefetch (coherent, contract section 3),
  sky130 cell instances.
- Cell instances: only the cells in `CELL_WHITELIST` (`tools/src_check.py`), with named ports and
  no power pins or parameters; their behavioral models live in `verif/cells/`. For an sdfxtp
  enable flop, feed Q back through SCD (a Q-to-D loop costs hold buffers after place and route).
- Their own unit tests in `src/test_*` (optional).
- `physical/**` only when the lead assigns a physical hypothesis.

## Commands

| Command | What it does |
|---|---|
| `make lint` | src_check, then Verilator `-Wall` lint, plain and RISCV_FORMAL builds |
| `make formal` | riscv-formal at the pins (fast config); its contract v3 port is plan Task 10, due by the 10th merged improvement |
| `make formal-deep` | the deep config (signoff) |
| `make cosim` | Verilator cosim (contract v3): every program of `make -C verif/programs list` in rvfi and pins modes under seeds 1-4, plus the liveness fuzz (seeds 1-5); the 2,048-cycle cap; reports CoreMark cycles per iteration |
| `make arch` | riscv-arch-test, all 53 RV32E and privilege tests under seeds 1-4, on one host with GCC, spike and Verilator (hwe-bench, `tools/hwe_run.sh --third-party`) |
| `make gl` | the TT test (test/, the QSPI Pmod model) on the synthesized netlist (`GATES=yes`) |
| `make equiv` | the RVFI-stripped build equals the plain build |
| `make area` | sky130 cell area, broken down (the tiebreak), and the `fits` pre-filter (1x2, 22,000 µm²: the seed, 20,442, signs off at utilization 0.728) |
| `make eval` | every gate plus the score, cheapest first, writing `build/eval.json`. `MIN_IPS=<champion coremark_ips>` (full precision, from `tools/ledger.py show --json`) stops a slower candidate right after cosim; a tie runs on, since area breaks it. Formal is skipped unless `FORMAL=1` |
| `make eval FORMAL=1` | the same plus formal last: at every 10th merged improvement (run in parallel while development continues) and at signoff |
| `make harden` | TT 1x2 hardening (LibreLane), signoff at `CLOCK_PERIOD` (20 ns, 50 MHz) |
| `make tilefit` | the smallest fraction of the real 1x2 tile the design signs off in (champion rounds) |

## Working rules

- **Agents.** Opus only. One hypothesis per agent, in its own worktree on `hyp/NNN-slug`. An agent
  commits on its branch and never merges.
- **Verification.** The lead re-runs `make eval` on the exact sha before believing any number.
- **Honest numbers.** Label measured versus projected, and re-measure before quoting.
- **Cleanup.** Remove a worktree and its build caches once its result is logged. Keep the branch.
- **Git.**
  - No force pushes.
  - Commits: `git -c commit.gpgsign=false commit`. 1Password signing isn't reliable here.
  - Push over HTTPS:
    `git -c credential.helper= -c credential.helper='!gh auth git-credential' push`.
- **Mac load.**
  - Heavy builds and simulations run on hwe-bench through `tools/hwe_run.sh`, never on the Mac
    (it ran out of memory with several agents compiling): Verilator (lint, cosim, arch, the seed's
    unit tests), cocotb and Icarus (gl), yosys (equiv). `make arch` needs `--third-party`. At most
    2 implementers are active on the Mac, and local `make -j` stays at 4 or below (ruling R11).
  - Programs (`make -C verif/programs`) build on either host; the images are byte-identical.
  - One heavy job at a time on the Mac: formal, hardening or a tile-fit search. Wrap them in
    `lockf -k /tmp/bonetto-heavy.lock`. LibreLane (area, harden, tilefit) runs where the lead
    says.
  - Kill only the PIDs or process groups your own PID files or job records name, never by a name
    pattern. hwe-bench is shared, with other workloads under the same uid: touch nothing outside
    your own directories and process trees there (ruling R36).
