# Evaluation: gates, eval.json, src hygiene, contract check, ledger

Everything here is contract: hypothesis agents run it and never edit it (CLAUDE.md).

## Commands

| Command | Runs |
|---|---|
| `make lint` ... `make gl` | one gate: `python3 tools/eval.py --gate <name>`, result in `build/gates/<name>.json` |
| `make eval` | every gate in order, stops at the first FAIL, writes `build/eval.json` (`V=1` streams runner output) |
| `make gates` | exactly the same command as `make eval`, writing the same `build/eval.json` |
| `make formal-deep` | the deep formal config (champions); not part of `make eval` |
| `make harden`, `make tilefit` | `tools/harden_tt.sh $(HARDEN_ARGS)`, `physical/tilefit.py $(TILEFIT_ARGS)` |
| `make test-tools` | `pytest tools/` (tests for eval, equiv, src_check, contract check, ledger) |

- **`make gates`** exists because some agent sandboxes block any command containing the word
  "eval". Use it there.
- **PATH:** the Makefile prepends `~/bonetto/riscv-autoarch/.toolchain/oss-cad-suite/bin` to
  `PATH` when it exists (the tools do the same when run directly). It stops with a clear error
  when `python3` is missing.
- **Heavy lock:** only formal, harden and tilefit take `lockf -k /tmp/bonetto-heavy.lock`, one
  heavy job at a time on this Mac. Never wrap `make eval` or `make gates` in that lock: formal
  would then wait on it forever.

## Gates, in `make eval` order

Each gate is one adapter function in `tools/eval.py`. An adapter deletes the runner's output json,
runs the runner from the repo root, and passes only if the exit code is 0 AND the fresh json says
so. A missing runner is a FAIL with `gate <name>: runner missing (<path>)`. Runner output goes to
`build/logs/<gate>*.log`.

| # | Gate | Runner (as called) | PASS requires |
|---|---|---|---|
| 1 | lint | `tools/src_check.py` rules (below), then `verilator --lint-only -Wall --top-module tt_um_fesens_r32v src/*.v`, then again with the RVFI defines | src hygiene clean; Verilator exit 0 for both builds |
| 2 | cosim | `make -C verif/programs`, then for every `build/programs/*.hex`, mode in {rvfi, pins}, seed in {1, 2, 3}: `run_cosim.py --mode M --prog P --seed S --max-cycles C` | every run exits 0 with a last-line JSON summary `pass: true`, `error` empty, `max_cycles_per_insn <= 1024` |
| 3 | equiv | `tools/equiv.py --src src --top tt_um_fesens_r32v --out build/equiv.json --work build/equiv` | exit 0 and `pass: true` |
| 4 | formal | `lockf -k /tmp/bonetto-heavy.lock python3 verif/formal/run_formal.py` | exit 0, `pass: true`, at least one check, every check `PASS` (`UNREACHED` fails) |
| 5 | arch | `verif/arch-test/run_arch.py` | exit 0, `pass: true`, at least one test, every test `true` |
| 6 | area | `tools/area.py --top tt_um_fesens_r32v --src src/` | exit 0, `area_um2 > 0`, `cells > 0`, `latches == 0` (contract v1), the `netlist` file exists |
| 7 | gl | copy the area netlist to `test/gate_level_netlist.v`, `make -C test GATES=yes GL_NETLIST=<abs netlist>` (PDK_ROOT defaults to `~/.ciel`) | the netlist is no older than this run's area step; a fresh `test/results.xml` with at least one passed testcase and no failure or error; exit 0 |

Details:
- **RVFI build flags** (lint and equiv): `-DRISCV_FORMAL -DRISCV_FORMAL_NRET=1
  -DRISCV_FORMAL_XLEN=32 -DRISCV_FORMAL_ILEN=32`, include path
  `third_party/riscv-formal/checks`, and a generated prelude file `` `include "rvfi_macros.vh" ``
  read before `src/*.v`. So the top module can use `` `RVFI_OUTPUTS `` without including
  anything; src_check forbids including it from `src/`. Declaring the RVFI ports explicitly (as
  the seed does) works in every gate.
- **Paths on command lines:** Verilator gets each path as its own argv entry, absolute, and
  checked against `^/[A-Za-z0-9_./+-]+$`. Yosys scripts get the same check plus double quotes
  where yosys strips them.
- **Cosim cycle budget:** `C = 1024 * (retired + 1)`, the contract latency cap applied to the
  ISS's retired count from `build/programs/<name>.expect.json`.
  - The key is `retired`; the adapter also accepts `retired_count`, `instret`, `insns` and
    `instructions`, and fails if none is there.
  - Selftest runs first, then gpio_demo, then the rest alphabetically.
  - The gate stops at the first failing run.
- **cpi** in eval.json: total cycles over total retired, over the `pins` runs with seed 1.
- **gl** runs the area gate first when it hasn't run in the same invocation (`make gl` alone).

## Src hygiene (tools/src_check.py)

Each tool predefines different macros: Verilator `VERILATOR`, yosys `YOSYS` and `SYNTHESIS`, SBY
`FORMAL`, TT's gate-level flow `GL_TEST`/`FUNCTIONAL`/`SIM`, cocotb `COCOTB_SIM`. A design could
use them to show each tool a different circuit while equiv (yosys on both sides) still passes.
File names also reach tool scripts. So src_check runs first in the lint gate and again inside
`tools/equiv.py`, and either FAILs on:

**Names and layout**
- any entry under `src/` (recursively) not matching `^[A-Za-z0-9_][A-Za-z0-9_.-]*$`;
- any symlink under `src/`;
- any subdirectory except `test_*` (a hypothesis's own unit tests) and `__pycache__` (Python
  creates it when `src/test_*.py` runs). No gate reads either.
- any `.v` file not matching `^[A-Za-z0-9_]+\.v$`;
- any `.sv` or `.svh` file (contract section 1: plain Verilog).

**HDL rules** (in `src/*.v` and every file they include; comments are skipped, escaped
identifiers handled the way the tools read them)
- `` `ifdef ``, `` `ifndef `` and `` `elsif `` may only test `RISCV_FORMAL` or a macro
  `` `define ``'d in `src/` itself.
- `` `define `` and `` `undef `` may not name a tool macro: `SYNTHESIS, YOSYS, VERILATOR, FORMAL,
  SIM, SIMULATION, FUNCTIONAL, USE_POWER_PINS, UNIT_DELAY, GL_TEST, IVERILOG, ICARUS, COCOTB_SIM,
  VCS, MODEL_TECH, QUESTA, XCELIUM, SYSTEMVERILOG`, or any name starting with `RISCV_FORMAL`,
  `RVFI_`, `rvformal_`, `COCOTB`, `VERILATOR`, `YOSYS`, `SYNTHESIS`, `FORMAL`, `SIM`, `__` or
  `SV_`.
- `` `undefineall `` and `` `` `` token pasting are not allowed.
- `` `include `` only `"name"`, where `name` is a bare file that exists in `src/`.
- No `initial` and no declaration initializers (`reg r = 0;`). The chip has no power-up values,
  and formal must not assume one.
- No attributes or pragma comments that make synthesis differ from simulation:
  - attributes `(* full_case *)`, `(* parallel_case *)`, `(* init *)`, `(* nosync *)`,
    `(* nolatches *)`, `(* blackbox *)`, `(* whitebox *)`, `(* lib_whitebox *)`,
    `(* nomem2reg *)`;
  - comments containing `translate_off`, `translate_on`, `full_case` or `parallel_case`.
- No non-ASCII characters in code.

Run it alone with `python3 tools/src_check.py [--src DIR]`.

## build/eval.json

```json
{
  "sha": "<git rev-parse HEAD, or \"unknown\">",
  "dirty": false,
  "pass": true,
  "gates": {
    "lint":   {"pass": true, "detail": "<one line>", "runtime_s": 1.2},
    "cosim":  {"pass": true, "detail": "...", "runtime_s": 300.0},
    "equiv":  {"pass": true, "detail": "...", "runtime_s": 2.0},
    "formal": {"pass": true, "detail": "...", "runtime_s": 1800.0},
    "arch":   {"pass": true, "detail": "...", "runtime_s": 60.0},
    "area":   {"pass": true, "detail": "...", "runtime_s": 20.0},
    "gl":     {"pass": true, "detail": "...", "runtime_s": 30.0}
  },
  "area_um2": 15432.1,
  "cells": 987,
  "cpi": 92.4,
  "timestamp": "2026-10-16T19:43:49Z",
  "order": ["lint", "cosim", "equiv", "formal", "arch", "area", "gl"],
  "stopped_at": null,
  "tools": {"yosys": "...", "verilator": "...", "iverilog": "..."},
  "runtime_s": 2213.4
}
```

- `pass` is true only if all seven gates ran and passed.
- A gate after the first failure has `{"pass": null, "detail": "not run (stopped after X
  failed)"}`, and `stopped_at` names the failing gate.
- `dirty` is true when `git status --porcelain --ignore-submodules=untracked` is non-empty at the
  start, so the sha doesn't describe what was evaluated. `ledger.py add --verdict merged` refuses
  a dirty eval.
  - `git status` can't see gitignored files. A stale `test/gate_level_netlist.v` or a leftover
    `build/` file in an agent's worktree is therefore invisible to `dirty`.
  - The flag is only trustworthy in the lead's fresh verify worktree (`git worktree add` at the
    exact sha, then `git submodule update --init`), which is where merge decisions are made.
- `area_um2`, `cells` come from the area gate and `cpi` from cosim; each is null when its gate
  didn't run.
- The file is deleted when `make eval` starts, so a crashed run never leaves a stale pass behind.
- Exit status: 0 on pass, 1 on fail, 130 when interrupted.

`build/gates/<name>.json` (one gate): `{"gate", "sha", "dirty", "pass", "detail", "runtime_s",
"data", "timestamp"}`, where `data` is gate-specific (cosim: every run and the cpi).

## Equivalence gate (tools/equiv.py)

Contract section 6: logic under `ifdef RISCV_FORMAL` may only compute RVFI outputs.
- **First:** src_check must pass (see above).
- **A (gold):** `read_verilog -mem2reg` with the RVFI build flags, `hierarchy -check -top`,
  `proc`, `flatten`, `delete -port` on every `rvfi_*` wire, `opt_clean`.
- **B (gate):** the same without the define.
- **Ports:** both builds must expose exactly the eight TT ports with the contract's directions
  and widths.
- **No initial values:** neither build may carry an `init` attribute. The proof ignores init
  values, so otherwise formal could assume a power-up value the chip lacks.
- **Proof:** `async2sync`, `equiv_make gold gate equiv`, `equiv_simple -seq 5`,
  `equiv_induct -seq 5`, `equiv_status -assert`.
- **Coverage:** every one of the 24 TT output bits must be the output of a `$equiv` cell.

Signals are matched by name. A register that exists only in A has no partner, so if it reaches a
pin or a shared register the proof fails. Extra matches can only add proof obligations.

`opt_clean` runs without `-purge`. With `-purge`, yosys keeps one name per net and can keep an
RVFI-only alias in A (`wire [7:0] a_obs = acc;`) where B keeps `acc`. The shared register then goes
unmatched and only `equiv_induct` can prove it, and yosys doesn't check `equiv_induct`'s base case.
Measured on fixture `pass_rvfi_alias`, an observation-only design:
- with `-purge`: 50 cells, 16 proven by `equiv_simple`, 33 by `equiv_induct`, and 1 unproven
  (`uio_out[1]`). That is a false FAIL.
- without `-purge`: 106 cells, all proven by `equiv_simple`.

The RVFI-only cells are removed either way: both builds end with the same cell inventory.

**Why yosys's equiv flow and not eqy.** The gate uses yosys `equiv_*` only. eqy would be needed
if shared state could not be matched by name, for example:
- a design that, under `RISCV_FORMAL`, keeps the same state under different names or with a
  different structure;
- generate-block renumbering shifted by an RVFI-only block.

That would show up as `proved_by_induct > 0` or as unproven cells on internal state, with no
behavior difference behind them. On the seed (trial of the in-progress RTL) all 1672 cells were
matched and proven by `equiv_simple`, so eqy isn't implemented. Add it if a real design hits
this, and record why.

`build/equiv.json`: `{"pass", "detail", "top", "method", "src_files", "rvfi_ports",
"equiv_cells", "proved_by_simple", "proved_by_induct", "unproven": ["uo_out[0]", ...],
"output_bits_checked", "runtime_s"}`.

Fixtures in `tools/test_fixtures/equiv/`:

| Fixture | Expected |
|---|---|
| `pass_observer` | PASS |
| `pass_rvfi_alias` | PASS |
| `fail_rvfi_drives_pin` | FAIL: an RVFI signal XORed into `uo_out` |
| `fail_rvfi_reg_feeds_pin` | FAIL: an RVFI-only counter flips `uo_out` at 0xBEEF, far past any BMC depth |
| `fail_rvfi_alters_shared_reg` | FAIL: an RVFI-only counter clears the GPIO register |
| `fail_plain_extra_port` | FAIL: a non-TT port |

## Contract check (tools/contract_check.py)

**Run it from the CHAMPION checkout (main), never from the candidate's worktree.** A candidate's
own `tools/` could otherwise certify itself:

```
python3 <main>/tools/contract_check.py --repo <main> <champion_sha> <hypothesis_sha>
python3 <main>/tools/contract_check.py --repo <main> --physical <champion_sha> <hyp_sha>
```

`--repo` takes any repository path (default: the current directory). The hypothesis sha only
has to be reachable from that repository; worktrees share the object store.

- It compares the two trees (`git diff --raw --no-renames BASE HEAD`).
- **Allowed:** `src/**` except `src/config.json` (which covers `src/test_*`), and `physical/**`
  with `--physical`.
- **Everything else fails, including:**
  - deletions;
  - renames out of `src/` (a rename is judged as a delete plus an add, so a rename into `src/`
    also fails on its deleted source);
  - symlinks and submodules (gitlinks) anywhere, `src/` included.
- **Portable paths:** every changed path present in HEAD must use only `[A-Za-z0-9._+-]` and
  `/`, with no component starting with `-`. That rules out whitespace, control characters, `;`,
  quotes, backslash and non-ASCII.
- **Case collisions:** no changed path, or any of its parent directories, may collide under
  case folding with another path in HEAD. macOS checkouts are case-insensitive.
- **Fingerprint:** independently, every path in the don't-touch list (CLAUDE.md) must have the
  same git object id in both commits, and so must the `third_party/riscv-formal` submodule
  pointer.
- **Exit status:** 0 on pass, 1 on fail (offending paths listed with the reason), 2 on a bad
  revision.

How the lead uses it (area-loop step 3): run it on the reported sha before `make eval`. A non-zero
exit rejects the hypothesis without evaluating it.

Because the trees are compared directly, a hypothesis based on an older champion fails if the
contract (for example `tools/`) moved since: rebase it onto the current champion first. An edit
made and reverted inside the branch leaves no tree difference and passes.

## Ledger (tools/ledger.py)

```
python3 tools/ledger.py next-id
python3 tools/ledger.py add --id NNN --branch hyp/NNN-slug --sha <sha> --idea "..." \
    --eval build/eval.json --verdict merged|rejected|failed|rolled_back [--notes "..."] \
    [--parent-sha <sha>] [--tile-fraction F] [--formal-deep pass|fail]
python3 tools/ledger.py annotate --id NNN [--tile-fraction F] [--formal-deep pass|fail] \
    [--notes-append "..."]
python3 tools/ledger.py board        # regenerates loop/LEADERBOARD.md
python3 tools/ledger.py show [--json]
```

`loop/ledger.jsonl` holds one JSON object per line with these fields, in order: `id, ts, branch,
sha, parent_sha, idea, verdict, area_um2, cells, delta_um2, gates, notes, tile_fraction,
formal_deep`.
- `gates` is `{name: true|false|null}` from eval.json.
- `parent_sha` defaults to the current champion's sha.
- `delta_um2` is `area_um2` minus the champion's area at the time of the entry (negative means
  smaller).
- `tile_fraction` is the `make tilefit` score, in (0, 1]. `formal_deep` is `pass`, `fail` or
  null. Both are optional and default to null.

**Champion:** the latest `merged` entry that no later `rolled_back` entry names.

**Rollback.** When a champion fails formal-deep (or anything else that disqualifies it):
1. Record the failure on the champion with
   `annotate --id <champion id> --formal-deep fail --notes-append "<why>"`.
2. Add `add --verdict rolled_back --sha <champion sha> ...`.

From then on `show` returns the previous merged entry. A `rolled_back` entry must name the
current champion's sha.

**Champion-round results** (tile fraction, deep formal pass) go onto the champion's own entry
with `annotate`. It rewrites that one line; git keeps the history.

`add` refuses:
- a duplicate or non-numeric id;
- an eval.json whose sha differs from `--sha`;
- a `tile_fraction` outside (0, 1];
- for `merged`: a missing eval, `pass` not true, `dirty` not false, or no `area_um2`;
- for `rolled_back`: a sha that isn't the current champion's.

`board` writes:
- the current champion, with its tile fraction and deep-formal result;
- the champion history: merged entries in order, with delta in µm² and percent, tile fraction,
  deep formal, and status (champion, or rolled back by NNN);
- the last 20 entries with their verdicts and failed gates.

Blank lines and lines starting with `#` in the ledger are skipped.
