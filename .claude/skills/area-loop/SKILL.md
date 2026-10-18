---
name: area-loop
description: Run the BonettoR32V performance loop (CoreMark iterations per second, contract v3) as the lead; the skill keeps its old name. One round = read the champion's CoreMark and area evidence, rank hypotheses, spawn one Opus agent per hypothesis in its own worktree, re-run the fixed eval yourself on each reported sha with MIN_IPS = the champion's score, merge winners one at a time, run formal on every tenth merged improvement, log everything, report the score. Use when asked to optimize, speed up, run the loop, run a round, or continue the performance goal; also for champion rounds (1x2 hardening, tile-fit, signoff formal, TT CI) and physical rounds.
---

# Performance loop (lead procedure)

The directory is still called `area-loop`, because other files point at it. The loop it describes
is the performance loop of contract v3.

**Objective.** The most performant RV32E microcontroller on TT sky130 (`docs/contract.md`): the
highest CoreMark iterations per second in a 1x2 tile, with every peripheral kept.

**Scores:**
- **Score:** `coremark_ips` = f_clk / CoreMark cycles per iteration, from `make eval`
  (`build/eval.json`). f_clk is the signoff clock, `CLOCK_PERIOD` in `src/config.json` (25 ns,
  40 MHz). Higher wins.
- **Tiebreak:** the synthesized area, `area_um2` from the same eval. At an equal score, smaller
  wins; an equal area is not smaller.
- **Constraints:** every gate passes, the candidate hardens clean in 1x2, and every peripheral
  stays (contract section 7). The eval's area gate has a provisional 1x2 `fits` pre-filter; the
  champion round's harden is the real check.

You are the lead:
- You decide, verify, merge and report.
- Agents implement.
- You never take an agent's numbers on faith, and you never edit the contract to make a candidate
  pass.

## State to read at the start of every round
1. `python3 tools/ledger.py show` gives the champion: sha, `coremark_ips` (full precision; `show
   --json` gives the raw value), clock, area, formal status, physical results.
   `loop/LEADERBOARD.md` has the history.
2. The champion's evidence, from an eval on its sha (run on hwe-bench):
   - `build/gates/cosim.json`: every run with its cycles, cpi and worst retirement gap, and the
     CoreMark `coremark_cycles_per_iter`;
   - `run_cosim.py --mode rvfi --prog build/programs/coremark.hex --timing-json PATH`: the worst gap
     per instruction class (where the cycles go);
   - `build/area.json` (`make area`): `by_type`, `by_module`, `flops`, and the headroom under the
     1x2 budget.
3. `loop/ledger.jsonl` holds every hypothesis tried, with its verdict and notes. Never re-run a
   rejected idea unless you can name what's different this time.
4. Your own notes (the lead's notes file, outside this skill) hold the ranked hypothesis ideas and
   the measurements behind them.
5. Check the user's standing decisions in `docs/contract.md` and `CLAUDE.md`.

## A round

### 1. Hypotheses
Rank by expected `coremark_ips` gain times confidence, divided by risk. Price each idea's area
against the 1x2 budget too: a faster design that doesn't harden in 1x2 doesn't count. The ideas
themselves live in your notes, not here.

Keep 2-5 hypotheses per round. Two that touch the same structure are not independent: run them
in sequence, not in parallel.

### 2. Spawn
- One agent per hypothesis: Agent tool, `model: opus`, `isolation: worktree`,
  `run_in_background: true`.
- The prompt is `agent-brief.md` from this directory, filled in.
- IDs are sequential: the next id comes from the ledger. Branch `hyp/NNN-slug`, from the
  champion's sha.

### 3. Verify each report (never skip)
1. Run `git log --oneline -3 <branch>` and confirm the sha the agent reported.
2. Run `git worktree add ../bonetto-verify-NNN <sha>` in a clean worktree. Run
   `git submodule update --init` there too.
3. Run the contract check from the CHAMPION checkout (main), never from the candidate's worktree,
   so a candidate's own tools can't certify themselves:
   `python3 /Users/bonetto/bonetto/BonettoR32V/tools/contract_check.py --repo
   /Users/bonetto/bonetto/BonettoR32V <champion_sha> <sha>`. It must exit 0. The candidate's
   `tools/`, `verif/` and `test/` are then byte-identical to the champion's, so its `make eval`
   runs the real gates.
4. Run `make eval MIN_IPS=<champion coremark_ips>` (or its alias `make gates`) for the verify
   worktree, on hwe-bench through `tools/hwe_run.sh --third-party` (the arch gate needs the suite),
   and read `build/eval.json`. Every gate must pass, and the candidate must outrank the champion:
   a higher `coremark_ips`, or the same with a smaller `area_um2`.
   - The gates run cheapest first: lint, area, cosim, arch, equiv, gl. Formal is skipped (its
     cadence is below).
   - With `MIN_IPS`, a candidate that is slower stops right after the cosim gate (`no_gain: true`,
     exit 3). Log it as `rejected`. A tie runs every gate, because the area tiebreak may still
     make it win.
   - `MIN_IPS` must be the champion's exact value from `tools/ledger.py show --json`, never the
     board's rounded one.
5. Read the diff (`git diff <champion_sha> <sha> -- src/`). Look for contract violations no gate
   catches:
   - hard-wired registers;
   - core state kept in memory outside the PSRAM core region;
   - logic under `ifdef RISCV_FORMAL` that does more than observe;
   - power-up reliance hidden by reset tricks;
   - a cache, prefetch or write buffer that is coherent only for the programs the gates run;
   - a peripheral weakened or removed.
6. Remove the verify worktree.

### 4. Merge
- **Order.** Merge winners one at a time, best first.
- **Re-evaluate after every merge.** After each merge into main, run `make eval` again, with no
  `MIN_IPS`, because the merged champion must pass every gate. Two wins
  can conflict or stop adding up. `tools/ledger.py add --verdict merged` refuses an entry that
  doesn't outrank the champion.
- **On a conflict:** resume the agent that owns the second branch (SendMessage) to rebase onto
  the new champion. Don't resolve RTL conflicts yourself.
- **Formal cadence.** At every tenth merged improvement, start `make eval FORMAL=1` on that
  champion and let it run in parallel while the next round goes on. If it fails, the champion is
  disqualified: find the first failing merge since the last formal pass, record the failure on it
  (`tools/ledger.py annotate`), roll back to its parent (`add --verdict rolled_back`), and rebase
  the later wins. Formal also runs at final signoff.
- **Commit and push:**
  - commit with `git -c commit.gpgsign=false`;
  - push with `git -c credential.helper= -c credential.helper='!gh auth git-credential' push`;
  - plain pushes only, never force.

### 5. Log and clean up
- **Log every hypothesis**, failures included, with the reason:
  `python3 tools/ledger.py add --id NNN --branch hyp/NNN-slug --sha <sha> --idea "..."
  --eval build/eval.json --verdict merged|rejected|failed --notes "..."`.
  Then run `python3 tools/ledger.py board` and commit `loop/`.
- **Clean up.** Remove every finished worktree and its `build/` (and its hwe-bench mirror under
  `~/bonetto-work/<tag>`), but keep the branches. No `git branch -d`, no remote deletes.

### 6. Report to the user
- Score before and after: `coremark_ips`, CoreMark cycles per iteration and the clock, with the
  change in percent; area and cells for the tiebreak and the 1x2 budget.
- What merged, and what failed and why, one line each.
- The next round's plan.
- Measured numbers only; label anything projected as projected.

## Champion rounds (when the champion changed materially, or every ~3 merges)
1. Run `make harden` (TT 1x2 via LibreLane, 40 MHz), then `make tilefit` (the fraction of the 1x2
   tile). Record both in the ledger notes, together with harden's `repair_um2` and
   `hold_buffers`, which `area_um2` doesn't see. A champion that doesn't harden clean in 1x2
   fails the contract: roll it back.
   - A tilefit result counts only on exit 0 with `min_fraction_tainted: false`.
   - Exit 3 means a trial timed out, and the min_fraction may be inflated. Find out why
     (Docker hang or routing), then rerun.
2. Update `info.yaml` (`source_files` = `src/*.v`; `tiles` stays 1x2), then push main. The TT
   gds, precheck and gl_test workflows run on main pushes only. Check them with
   `gh run list -R FeSens/BonettoR32V`.
3. At final signoff: `make eval FORMAL=1` and `make formal-deep`. A failure means the champion
   goes back to its parent in the ledger, with the counterexample logged.
4. Re-run the baselines (`docs/baselines.md`) only if the tools changed.

## Physical rounds
- Hypotheses on floorplan and density settings (within what TT's `config.json` allows),
  standard-cell macros and hand-placed arrays, and on the signoff clock (contract section 7: up to
  the memory limits and the 50 MHz IO ceiling, unless a measurement proves more).
- Scored by `coremark_ips` at the clock the design signs off at, with `make harden` and
  `make tilefit` as evidence. The agent's allowed path is `physical/**`, and `contract_check.py`
  runs with `--physical`.
- Same rules: clean signoff or it doesn't count.

## Contract changes (lead only, never in a hypothesis branch)
- **Within the user's direction.** Bump the contract when a promising hypothesis needs it. That
  requires every checker updated in the same commit, plus mutants proving the new rule is
  enforced.
- **One-way decisions go to the user, with the numbers.** These are changes to the ISA, the
  memory protocol or map, the peripheral set, the tile size and the reserved-encoding policy.

## Rules
- Agents are Opus only. Workflows only if the user asks.
- Heavy builds and simulations (Verilator, cocotb, yosys: lint, cosim, arch, equiv, gl, the
  seed's unit tests) run on hwe-bench through `tools/hwe_run.sh`, never on the Mac (ruling R11).
  Formal, LibreLane and tile-fit run one at a time under `lockf -k /tmp/bonetto-heavy.lock` where
  they run on the Mac. Kill only processes your own records name, never by pattern; hwe-bench is
  shared (ruling R36).
- Honest numbers: measured versus projected is always labeled. Re-measure before quoting.
- Ambitious structural hypotheses come first, with a checkpoint. Small tweaks fill in around them.
- Never weaken a gate. If a gate is wrong, fix it in a lead commit with a mutant that proves the
  fix, never inside a hypothesis branch.
