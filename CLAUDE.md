# BonettoR32V: rules for every session and agent

Goal: the smallest correct RV32E core on Tiny Tapeout sky130 (programs from SPI RAM, GPIO). It's
step one toward a full microcontroller in ONE 1x1 tile. The score is silicon area: RTL cell area
first, then the smallest share of the real 1x1 tile that hardens clean. Speed isn't scored.

- `docs/contract.md`: the exact interface (pins, SPI protocol, memory map, EEI, RVFI). Read it
  first.
- `docs/superpowers/specs/2026-10-16-bonettor32v-loop-setup-design.md`: the design and its
  rationale.
- `.claude/skills/area-loop/SKILL.md`: how the lead runs the optimization loop.

## Hard invariants (each enforced by a gate that fails loudly)

| # | Invariant | Gate |
|---|---|---|
| 1 | RV32E per docs/contract.md section 4: every instruction correct, all fatal traps halt with `rvfi_trap = rvfi_halt = 1` | riscv-formal (rv32e config), cosim against the ISS, riscv-arch-test |
| 2 | The pins follow the SPI waveform rules (contract section 2), and every SPI transaction matches the RVFI-reported fetch, load or store | formal bus checks via the SPI monitor, cosim memory model |
| 3 | GPIO behaves per contract section 3 | formal GPIO check, cosim |
| 4 | Logic under `ifdef RISCV_FORMAL` only observes; the design without it behaves identically | equivalence gate (RVFI-stripped build equals plain build), plain-build cosim, gate-level test |
| 5 | Registers live on the die; no hard-wired GPRs; no state in the SPI RAM other than program data | review and formal (reg check) |
| 6 | The design doesn't rely on power-up values | cosim with random init (several seeds), gate-level X propagation |
| 7 | Plain RTL, one posedge clock, no latches in v1 (contract section 1) | lint and review |
| 8 | Every instruction retires within 1,024 cycles of the previous one | cosim watchdog, formal liveness |
| 9 | A candidate's diff touches only allowed paths | `tools/contract_check.py` |

## Don't-touch list (hypothesis agents never modify these)

`verif/`, `tools/`, `test/`, `third_party/`, `info.yaml`, `src/config.json`, `docs/contract.md`,
`CLAUDE.md`, `Makefile`, `.github/`, `loop/`, `.claude/`.

The hypothesis is "what design change makes the chip smaller", never "what evaluation change lets
this design pass". If a gate looks wrong, report it to the lead; don't route around it.

## What hypotheses may change

- `src/**`, except `src/config.json`. That covers any restructuring: merge or split modules,
  rename files, bit-serial, latches, sky130 cell instances.
- Their own unit tests in `src/test_*` (optional).
- `physical/**` only when the lead assigns a physical hypothesis.

## Commands

| Command | What it does |
|---|---|
| `make lint` | Verilator lint |
| `make formal` | riscv-formal at the pins (fast config) |
| `make formal-deep` | the deep config (champions) |
| `make cosim` | Verilator cosim: selftest, GPIO, CoreMark, random streams, RVFI and non-RVFI builds |
| `make arch` | riscv-arch-test RV32E subset |
| `make gl` | gate-level test of the synthesized netlist |
| `make equiv` | the RVFI-stripped build equals the plain build |
| `make area` | the fast score: sky130 cell area, broken down |
| `make eval` | every gate plus the score, cheapest first, writing `build/eval.json`; `MAX_AREA=<champion um2>` stops a candidate that isn't smaller right after the area gate |
| `make harden` | TT 1x1 hardening (LibreLane) |
| `make tilefit` | the physical score: the smallest fraction of the real 1x1 tile the design signs off in |

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
  - One heavy job at a time: formal, hardening or a tile-fit search. Wrap them in
    `lockf -k /tmp/bonetto-heavy.lock`.
  - Cosim and area runs are light.
