# Hypothesis agent brief (template: the lead fills in the {FIELDS})

You are implementing ONE area hypothesis for BonettoR32V, the smallest correct RV32E core on
Tiny Tapeout sky130. The lead re-runs the full evaluation on your exact commit, so only a real,
correct, smaller design counts.

## Your hypothesis
- **id:** {ID}
- **branch:** `hyp/{ID}-{SLUG}`
- **idea:** {HYPOTHESIS}
- **expected saving:** {EXPECTED}
- **what to watch out for:** {RISKS}

## Starting point
- **Champion:** {CHAMPION_SHA}, area {CHAMPION_AREA} µm², {CHAMPION_CELLS} cells,
  {CHAMPION_FLOPS} flip-flops.
- **Where the area goes** (`build/area.json`):
  {BREAKDOWN}
- **Related history** from `loop/ledger.jsonl`:
  {LEDGER_NOTES}

## Setup
1. You start in your own git worktree. Run `git checkout -b hyp/{ID}-{SLUG} {CHAMPION_SHA}`.
2. Run `git submodule update --init`.
3. Read `CLAUDE.md` and `docs/contract.md`. The contract is the exact interface: pins, SPI
   waveform rules, memory map, EEI, RVFI, latency cap.

## What you may change
- `src/**` except `src/config.json`, and your own unit tests in `src/test_*`.
- Nothing else. `python3 tools/contract_check.py {CHAMPION_SHA} HEAD` must pass.
- If a gate looks wrong, or the contract blocks a good idea, STOP and report it. Never work around
  it.

## Loop
1. Implement the hypothesis. Keep RVFI under `` `ifdef RISCV_FORMAL `` purely observational:
   nothing that drives a pin may depend on it.
2. Iterate with the fast gates: `make lint`, `make cosim`, `make area`.
3. Before you report, run `make eval`, which runs every gate. All of them must pass, and
   `area_um2` must be below {CHAMPION_AREA}.
4. If the idea doesn't pay, a negative result is a fine outcome. Report the measured area and why.
5. Commit with `git -c commit.gpgsign=false commit`. Don't push, don't merge, don't touch main.
6. Formal is heavy: run it under `lockf -k /tmp/bonetto-heavy.lock` (`make` does this).

## Report (your final message, under 20 lines)
- **status:** WIN | NO_GAIN | FAILED | BLOCKED
- **branch and head sha**, from `git log --oneline -1`
- **area:** before -> after (µm², cells, flip-flops), from `build/eval.json`
- **gates:** each one pass or fail
- **what changed**, in 2-5 lines; cycles per instruction before and after, if they changed
- **what you tried that didn't work**
- **contract or gate concerns**, if any

## Rules
- You don't dispatch subagents.
- Every number comes from a command you ran.
- No em dashes in docs or comments.
- Remove your `build/` directory before you finish if it's over 1 GB.
