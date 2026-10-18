# Hypothesis agent brief (template: the lead fills in the {FIELDS})

You are implementing ONE performance hypothesis for BonettoR32V, an RV32E microcontroller on Tiny
Tapeout sky130 (contract v3: code from the QSPI Pmod's flash, data in its PSRAM, every peripheral
kept, a 1x2 tile). The score is CoreMark iterations per second at the 40 MHz signoff clock;
synthesized area breaks ties. The lead re-runs the full evaluation on your exact commit, so only a
real, correct, faster design counts.

## Your hypothesis
- **id:** {ID}
- **branch:** `hyp/{ID}-{SLUG}`
- **idea:** {HYPOTHESIS}
- **expected gain:** {EXPECTED}
- **what to watch out for:** {RISKS}

## Starting point
- **Champion:** {CHAMPION_SHA}, coremark_ips {CHAMPION_IPS} ({CHAMPION_CYCLES} CoreMark cycles per
  iteration at {CLOCK_NS} ns), area {CHAMPION_AREA} µm², {CHAMPION_CELLS} cells,
  {CHAMPION_FLOPS} flip-flops.
- **Where the cycles and the area go:**
  {BREAKDOWN}
- **Related history** from `loop/ledger.jsonl`:
  {LEDGER_NOTES}

## Setup
1. You start in your own git worktree. Run `git checkout -b hyp/{ID}-{SLUG} {CHAMPION_SHA}`.
2. Run `git submodule update --init`.
3. Read `CLAUDE.md` and `docs/contract.md`. The contract is the exact interface: pins, the QSPI
   Pmod rules, memory map and coherence, EEI and CSRs, peripherals, RVFI, latency cap.

## What you may change
- `src/**` except `src/config.json`, and your own unit tests in `src/test_*`.
- Nothing else. `python3 tools/contract_check.py {CHAMPION_SHA} HEAD` must pass.
- Every peripheral stays, with its contract behavior.
- If a gate looks wrong, or the contract blocks a good idea, STOP and report it. Never work around
  it.

## Loop
1. Implement the hypothesis. Keep RVFI under `` `ifdef RISCV_FORMAL `` purely observational:
   nothing that drives a pin may depend on it.
2. Heavy work runs on hwe-bench through `tools/hwe_run.sh`, never on the Mac (Verilator, cocotb,
   yosys): `tools/hwe_run.sh --pull build/gates/cosim.json -- make cosim`, and the same for
   `make lint`, `make equiv`. Programs build on either host.
3. Before you report, run `make eval MIN_IPS={CHAMPION_IPS}` on hwe-bench
   (`tools/hwe_run.sh --third-party --pull build/eval.json -- make gates MIN_IPS=...`), which runs
   every gate but formal, cheapest first (lint, area, cosim, arch, equiv, gl). All of them must
   pass, and `coremark_ips` must beat {CHAMPION_IPS} (or tie it with a smaller area). If it is
   slower, eval stops after the cosim gate with `no_gain` (exit 3). Then report NO_GAIN, or keep
   going.
4. If the idea doesn't pay, a negative result is a fine outcome. Report the measured score and why.
5. Commit with `git -c commit.gpgsign=false commit`. Don't push, don't merge, don't touch main.

## Report (your final message, under 20 lines)
- **status:** WIN | NO_GAIN | FAILED | BLOCKED
- **branch and head sha**, from `git log --oneline -1`
- **score:** coremark_ips and CoreMark cycles per iteration, before -> after, from
  `build/eval.json`; area before -> after (µm², cells, flip-flops)
- **gates:** each one pass or fail
- **what changed**, in 2-5 lines; the worst retirement gap before and after
- **what you tried that didn't work**
- **contract or gate concerns**, if any

## Rules
- You don't dispatch subagents.
- Every number comes from a command you ran.
- No em dashes in docs or comments.
- Kill only processes your own records name, never by a name pattern; hwe-bench is shared.
- Remove your `build/` directory (and your hwe-bench mirror) before you finish if it's over 1 GB.
