---
name: area-loop
description: Run the BonettoR32V area-minimization loop as the lead. One round = read the champion's area evidence, rank hypotheses, spawn one Opus agent per hypothesis in its own worktree, re-run the fixed eval yourself on each reported sha, merge winners one at a time, log everything, report the area. Use when asked to optimize, shrink, run the loop, run a round, or continue the area goal; also for champion rounds (hardening, tile-fit, deep formal, TT CI) and physical rounds.
---

# Area loop (lead procedure)

**Objective.** The smallest correct RV32E core on TT sky130 (`docs/contract.md`). Speed isn't
scored. Space matters because the end goal is a full microcontroller (core, GPIO and peripherals)
in ONE 1x1 tile.

**Scores:**
- **Fast score:** `area_um2`, the sky130_fd_sc_hd cell area from `make area`. Lower wins. Ties go
  to fewer cells, then fewer cycles.
- **Physical score:** the smallest fraction of the real 1x1 tile the champion signs off in, from
  `make tilefit`. Champions only.

You are the lead:
- You decide, verify, merge and report.
- Agents implement.
- You never take an agent's numbers on faith, and you never edit the contract to make a candidate
  pass.

## State to read at the start of every round
1. `python3 tools/ledger.py show` gives the champion: sha, area, physical score.
   `loop/LEADERBOARD.md` has the history.
2. Go to the champion's sha, run `make area`, then read `build/area.json`:
   - `by_type` is where the µm² go, by cell type;
   - `by_module` is where they go in the hierarchy;
   - `flops` and `latches`.
3. `loop/ledger.jsonl` holds every hypothesis tried, with its verdict and notes. Never re-run a
   rejected idea unless you can name what's different this time.
4. Check the user's standing decisions in `docs/contract.md` and `CLAUDE.md`.

## A round

### 1. Hypotheses
Rank by expected µm² saved times confidence, divided by risk. Look for:
- **The biggest by_type rows.** Flip-flops are 20 µm² each, `mux2` 11.3, `mux4` 26.3. Count bits
  of state: which state is redundant or could be shared?
- **Datapath width.** Bit-serial or nibble-serial versus parallel. Serial trades cycles, which are
  free, for area.
- **Muxes that exist only to select.** A register file read through muxes can become a shift
  register that rotates; an addressed array can become a sequential one.
- **State held twice.** The PC copied into the SPI shift register, the instruction register next to
  the SPI shifter, the address register next to the ALU result.
- **Decoder and control.** Minimal decode under the contract's reserved-encoding freedom; one-hot
  versus binary FSM encoding (measure both).
- **Reset.** Reset only what the contract requires. Every reset flip-flop costs area (dfrtp is
  25 µm², dfxtp 20).
- **Fused SPI and datapath.** The address can stream straight from the adder or the PC; load data
  can stream straight into the register file.

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
3. Run `python3 tools/contract_check.py <champion_sha> <sha>`. It must exit 0.
4. Run `make eval` in the verify worktree and read `build/eval.json`. Every gate must pass, and
   `area_um2` must beat the champion's.
5. Read the diff (`git diff <champion_sha> <sha> -- src/`). Look for contract violations no gate
   catches:
   - hard-wired registers;
   - state kept off-chip;
   - logic under `ifdef RISCV_FORMAL` that does more than observe;
   - power-up reliance hidden by reset tricks.
6. Remove the verify worktree.

### 4. Merge
- **Order.** Merge winners one at a time, best first.
- **Re-evaluate after every merge.** After each merge into main, run `make eval` again. Two wins
  can conflict or stop adding up.
- **On a conflict:** resume the agent that owns the second branch (SendMessage) to rebase onto
  the new champion. Don't resolve RTL conflicts yourself.
- **Commit and push:**
  - commit with `git -c commit.gpgsign=false`;
  - push with `git -c credential.helper= -c credential.helper='!gh auth git-credential' push`;
  - plain pushes only, never force.

### 5. Log and clean up
- **Log every hypothesis**, failures included, with the reason:
  `python3 tools/ledger.py add --id NNN --branch hyp/NNN-slug --sha <sha> --idea "..."
  --eval build/eval.json --verdict merged|rejected|failed --notes "..."`.
  Then run `python3 tools/ledger.py board` and commit `loop/`.
- **Clean up.** Remove every finished worktree and its `build/`, but keep the branches. No
  `git branch -d`, no remote deletes.

### 6. Report to the user
- Area before and after: µm², cells, flip-flops, and the delta in percent.
- What merged, and what failed and why, one line each.
- The next round's plan.
- Measured numbers only; label anything projected as projected.

## Champion rounds (when the champion changed materially, or every ~3 merges)
1. Run `make formal-deep`. A failure means the champion goes back to its parent in the ledger,
   with the counterexample logged.
2. Run `make harden` (TT 1x1 via LibreLane), then `make tilefit` (the physical score). Record both
   in the ledger notes.
3. Update `info.yaml` (`tiles` = the smallest config that fits, and `source_files` = `src/*.v`),
   then push main. The TT gds, precheck and gl_test workflows run on main pushes only. Check them
   with `gh run list -R FeSens/BonettoR32V`.
4. Re-run the baselines (`docs/baselines.md`) only if the tools changed.

## Physical rounds
- Hypotheses on floorplan and density settings (within what TT's `config.json` allows),
  standard-cell macros and hand-placed arrays.
- Scored by `make tilefit`. The agent's allowed path is `physical/**`, and `contract_check.py` runs
  with `--physical`.
- Same rules: clean signoff or it doesn't count.

## Contract changes (lead only, never in a hypothesis branch)
- **Within the user's direction.** Bump the contract when a promising hypothesis needs it, for
  example v1.1 clock-gated latches with contract-owned cell models. That requires every checker
  updated in the same commit, plus mutants proving the new rule is enforced.
- **One-way decisions go to the user, with the numbers.** These are changes to the ISA, the
  memory protocol or map, and the reserved-encoding policy.

## Rules
- Agents are Opus only. Workflows only if the user asks.
- Heavy jobs run one at a time on the Mac, under `lockf -k /tmp/bonetto-heavy.lock`. They are
  formal, LibreLane and tile-fit.
- Honest numbers: measured versus projected is always labeled. Re-measure before quoting.
- Ambitious structural hypotheses come first, with a checkpoint. Small tweaks fill in around them.
- Never weaken a gate. If a gate is wrong, fix it in a lead commit with a mutant that proves the
  fix, never inside a hypothesis branch.
