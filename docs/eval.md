# Evaluation: gates, eval.json, contract check, ledger

Everything here is contract: hypothesis agents run it and never edit it (CLAUDE.md).

## Commands

| Command | Runs |
|---|---|
| `make lint` ... `make gl` | one gate: `python3 tools/eval.py --gate <name>`, result in `build/gates/<name>.json` |
| `make eval` | every gate in order, stops at the first FAIL, writes `build/eval.json` (`V=1` streams runner output) |
| `make formal-deep` | the deep formal config (champions); not part of `make eval` |
| `make harden`, `make tilefit` | `tools/harden_tt.sh $(HARDEN_ARGS)`, `physical/tilefit.py $(TILEFIT_ARGS)` |
| `make test-tools` | `pytest tools/` (tests for eval, equiv, contract check, ledger) |

The Makefile prepends `~/bonetto/riscv-autoarch/.toolchain/oss-cad-suite/bin` to `PATH` when it
exists (the tools do the same when run directly). Formal, harden and tilefit run under
`lockf -k /tmp/bonetto-heavy.lock`. Don't wrap `make` itself in that lock: the inner `lockf`
would wait forever.

## Gates, in `make eval` order

Each gate is one adapter function in `tools/eval.py`. An adapter deletes the runner's output json,
runs the runner from the repo root, and passes only if the exit code is 0 AND the fresh json says
so. A missing runner is a FAIL with `gate <name>: runner missing (<path>)`. Runner output goes to
`build/logs/<gate>*.log`.

| # | Gate | Runner (as called) | PASS requires |
|---|---|---|---|
| 1 | lint | `verilator --lint-only -Wall --top-module tt_um_fesens_r32v src/*.v`, then again with the RVFI defines | exit 0 for both builds; no `*.sv`/`*.svh` in `src/` |
| 2 | cosim | `make -C verif/programs`, then for every `build/programs/*.hex`, mode in {rvfi, pins}, seed in {1, 2, 3}: `run_cosim.py --mode M --prog P --seed S --max-cycles C` | every run exits 0 with a last-line JSON summary `pass: true`, `error` empty, `max_cycles_per_insn <= 1024` |
| 3 | equiv | `tools/equiv.py --src src --top tt_um_fesens_r32v --out build/equiv.json --work build/equiv` | exit 0 and `pass: true` |
| 4 | formal | `lockf -k /tmp/bonetto-heavy.lock python3 verif/formal/run_formal.py` | exit 0, `pass: true`, at least one check, every check `PASS` (`UNREACHED` fails) |
| 5 | arch | `verif/arch-test/run_arch.py` | exit 0, `pass: true`, at least one test, every test `true` |
| 6 | area | `tools/area.py --top tt_um_fesens_r32v --src src/` | exit 0, `area_um2 > 0`, `cells > 0`, `latches == 0` (contract v1), the `netlist` file exists |
| 7 | gl | copy the area netlist to `test/gate_level_netlist.v`, `make -C test GATES=yes GL_NETLIST=<abs netlist>` (PDK_ROOT defaults to `~/.ciel`) | a fresh `test/results.xml` with at least one passed testcase and no failure or error; exit 0 |

Details:
- **RVFI build flags** (lint and equiv): `-DRISCV_FORMAL -DRISCV_FORMAL_NRET=1
  -DRISCV_FORMAL_XLEN=32 -DRISCV_FORMAL_ILEN=32`, include path
  `third_party/riscv-formal/checks`, and a generated prelude file `` `include "rvfi_macros.vh" ``
  read before `src/*.v`, so the top module can use `` `RVFI_OUTPUTS `` (contract section 1).
- **Cosim cycle budget:** `C = 1024 * (retired + 1)`, the contract latency cap applied to the
  ISS's retired count from `build/programs/<name>.expect.json` (key `retired`; the adapter also
  accepts `retired_count`, `instret`, `insns`, `instructions` and fails if none is there).
  Selftest runs first, then gpio_demo, then the rest alphabetically; the gate stops at the first
  failing run.
- **cpi** in eval.json: total cycles over total retired, over the `pins` runs with seed 1.
- **gl** runs the area gate first when it hasn't run in the same invocation (`make gl` alone).

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
- `dirty` is true when `git status --porcelain` is non-empty at the start, so the sha doesn't
  describe what was evaluated. `ledger.py add --verdict merged` refuses a dirty eval.
- `area_um2`, `cells` come from the area gate and `cpi` from cosim; each is null when its gate
  didn't run.
- The file is deleted when `make eval` starts, so a crashed run never leaves a stale pass behind.
- Exit status: 0 on pass, 1 on fail, 130 when interrupted.

`build/gates/<name>.json` (one gate): `{"gate", "sha", "dirty", "pass", "detail", "runtime_s",
"data", "timestamp"}`, where `data` is gate-specific (cosim: every run and the cpi).

## Equivalence gate (tools/equiv.py)

Contract section 6: logic under `ifdef RISCV_FORMAL` may only compute RVFI outputs.
- **A (gold):** `read_verilog -mem2reg` with the RVFI build flags, `hierarchy -check -top`,
  `proc`, `flatten`, `delete -port` on every `rvfi_*` wire, `opt_clean`.
- **B (gate):** the same without the define.
- **Ports:** both builds must expose exactly the eight TT ports with the contract's directions
  and widths.
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

```
python3 tools/contract_check.py <champion_sha> <hypothesis_sha>        # src/** only
python3 tools/contract_check.py --physical <champion_sha> <hyp_sha>    # also physical/**
```

- It compares the two trees (`git diff --raw --no-renames BASE HEAD`).
- **Allowed:** `src/**` except `src/config.json` (which covers `src/test_*`), and `physical/**`
  with `--physical`.
- **Everything else fails, including:**
  - deletions;
  - renames out of `src/` (a rename is judged as a delete plus an add, so a rename into `src/`
    also fails on its deleted source);
  - symlinks and submodules anywhere.
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
    --eval build/eval.json --verdict merged|rejected|failed [--notes "..."] [--parent-sha <sha>]
python3 tools/ledger.py board        # regenerates loop/LEADERBOARD.md
python3 tools/ledger.py show [--json]
```

`loop/ledger.jsonl` holds one JSON object per line with these fields, in order: `id, ts, branch,
sha, parent_sha, idea, verdict, area_um2, cells, delta_um2, gates, notes`.
- `gates` is `{name: true|false|null}` from eval.json.
- `parent_sha` defaults to the current champion's sha.
- `delta_um2` is `area_um2` minus the champion's area at the time of the entry (negative means
  smaller).

Champion: the latest `merged` entry.

`add` refuses:
- a duplicate or non-numeric id;
- an eval.json whose sha differs from `--sha`;
- for `merged`: a missing eval, `pass` not true, `dirty` not false, or no `area_um2`.

`board` writes the current champion, the champion history (merged entries in order, delta in µm²
and percent) and the last 20 entries with their verdicts and failed gates. Blank lines and lines
starting with `#` in the ledger are skipped.
