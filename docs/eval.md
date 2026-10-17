# Evaluation: gates, eval.json, src hygiene, contract check, ledger

Everything here is contract: hypothesis agents run it and never edit it (CLAUDE.md).

## Commands

| Command | Runs |
|---|---|
| `make lint` ... `make gl` | one gate: `python3 tools/eval.py --gate <name>`, result in `build/gates/<name>.json` |
| `make eval` | every gate in order, cheapest first, stops at the first FAIL, writes `build/eval.json` (`V=1` streams runner output). `MAX_AREA=<champion um2>` retires a candidate right after the area gate unless it is smaller (`no_gain: true`, exit 3) |
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

Cheapest first, so a bad candidate retires early: lint (~1 s), area (~10 s), cosim and arch
(minutes), equiv, gl (minutes), formal (the longest). With `--max-area`, a candidate that is not
smaller than the champion stops after the area gate, because it cannot win.

Each gate is one adapter function in `tools/eval.py`. An adapter deletes the runner's output json,
runs the runner from the repo root, and passes only if the exit code is 0 AND the fresh json says
so. A missing runner is a FAIL with `gate <name>: runner missing (<path>)`. Runner output goes to
`build/logs/<gate>*.log`.

| # | Gate | Runner (as called) | PASS requires |
|---|---|---|---|
| 1 | lint | `tools/src_check.py` rules (below), then `verilator --lint-only -Wall --top-module tt_um_fesens_r32v src/*.v`, then again with the RVFI defines | src hygiene clean; Verilator exit 0 for both builds |
| 2 | area | `tools/area.py --top tt_um_fesens_r32v --src src/` | exit 0, `area_um2 > 0`, `cells > 0`, `engine == "librelane"`, `latches == 0` (contract v1), the `netlist` file exists |
| 3 | cosim | `make -C verif/programs`, then for every `build/programs/*.hex`, mode in {rvfi, pins}, seed in {1, 2, 3}: `run_cosim.py --mode M --prog P --seed S` (no `--max-cycles`: the harness bounds each run by 1,024 x its own ISS retirement count + 16, and its watchdog bounds every gap) | every run exits 0 with a last-line JSON summary `pass: true`, `error` empty, `max_cycles_per_insn <= 1024` |
| 4 | arch | `verif/arch-test/run_arch.py` | exit 0, `pass: true`, at least one test, every test `true` |
| 5 | equiv | `tools/equiv.py --src src --top tt_um_fesens_r32v --out build/equiv.json --work build/equiv` | exit 0 and `pass: true` |
| 6 | gl | copy the area netlist to `test/gate_level_netlist.v`, `make -C test GATES=yes GL_NETLIST=<abs netlist>` (PDK_ROOT defaults to `~/.ciel`) | the netlist is no older than this run's area step; a fresh `test/results.xml` with at least one passed testcase and no failure or error; exit 0 |
| 7 | formal | `lockf -k /tmp/bonetto-heavy.lock python3 verif/formal/run_formal.py` (with `BONETTO_FORMAL_HOST` set, no Mac lock: the solvers run on that host under its own lock) | exit 0, `pass: true`, at least one check, every check `PASS` (`UNREACHED` fails) |

Details:
- **RVFI build flags** (lint, equiv and the differential check): just `-DRISCV_FORMAL`.
  - The restricted preprocessor (contract section 1) forbids every macro use, so the RVFI ports
    must be declared explicitly inside `` `ifdef RISCV_FORMAL ``, as the seed does.
  - `` `RVFI_OUTPUTS `` is a macro use and fails src_check, even though the contract's section 1
    example still shows it. That example should change.
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

This enforces contract section 1, "Restricted preprocessor".

Each tool predefines different macros: Verilator `VERILATOR` (and lowercase `verilator`,
`verilator3`, `coverage_block_off`), yosys `YOSYS` and `SYNTHESIS`, SBY `FORMAL`, TT's
gate-level flow `GL_TEST`/`FUNCTIONAL`/`SIM`. Each also handles preprocessor corners (include
search order, macro pasting) differently. A design could use either to show each tool a
different circuit while equiv (yosys on both sides) still passes. File names also reach tool
scripts.

So src_check is an allowlist, enforced on a real tokenizer, plus a differential backstop. It
runs first in the lint gate and again inside `tools/equiv.py`.

**Names and layout**
- any entry under `src/` (recursively) not matching `^[A-Za-z0-9_][A-Za-z0-9_.-]*$` fails;
- any symlink under `src/` fails;
- any subdirectory fails, except `test_*` (a hypothesis's own unit tests) and `__pycache__`
  (Python creates it when `src/test_*.py` runs). No gate reads either.
- any `.v` file not matching `^[A-Za-z0-9_]+\.v$` fails;
- any `.sv` or `.svh` file fails (contract section 1: plain Verilog).

**Tokens of every `src/*.v`** (comments and strings handled as Verilator, yosys and Icarus
handle them; escaped identifiers read whole and then rejected by rule 11):
1. **Directives.** The only directives allowed are `` `ifdef RISCV_FORMAL ``,
   `` `ifndef RISCV_FORMAL ``, `` `else ``, `` `endif ``, `` `default_nettype none|wire `` and
   `` `timescale <1|10|100><unit>/<1|10|100><unit> ``.
   - Each must be alone on its line, and the `ifdef`/`endif` nesting must balance.
   - `` `default_nettype `` and `` `timescale `` sit outside `module ... endmodule`, and a
     `` `timescale `` precision may not be coarser than its unit.
   - Every other backtick token fails: `` `define ``, any macro use (`` `RVFI_OUTPUTS ``,
     `` `__LINE__ ``), `` `include ``, `` `undef ``, `` `elsif ``, `` `celldefine ``,
     `` `resetall ``, `` `line ``, `` `pragma ``, and stray sequences such as `` `` `` or
     `` `" ``.
   - Use `parameter`/`localparam` instead of macros.
2. **No power-up values.**
   - The token `initial` fails anywhere outside comments and strings.
   - Initializers on variable declarations fail: `reg`, `integer`, `time`, `real`, `realtime`,
     and the SystemVerilog `logic`, `bit`, `int`, `byte`, `shortint`, `longint`, ANSI output
     ports included.
   - `parameter` and `localparam` (typed or not) are fine.
   - `wire w = e;` is a continuous assignment, not a power-up value, and stays allowed: the
     contract names `reg` and `integer` only, and the seed uses the form throughout.
3. **No attributes.** `(*` fails unless it is the `@(*)` sensitivity list (whitespace allowed
   inside, `@ (*)` and `@*` too).
4. **No tool metacomments.** A comment fails when its first word is `verilator`, `synopsys`,
   `synthesis`, `pragma` or `yosys` (any case), or when it contains `translate_`, `lint_off`,
   `full_case` or `parallel_case`. Lint must be clean without suppressions.
5. **Plain ASCII.** Carriage returns (CRLF), other control characters (tab and newline aside) and
   non-ASCII bytes fail anywhere in the file. So do unterminated comments and strings.
6. **Module names.** Every `module` is named `tt_um_fesens_r32v` or `r32v_[A-Za-z0-9_]*`, as a
   plain identifier, so no design module can shadow a checker's (`rvfi_wrapper`, `tb`,
   `spiram_23lc512`, `iss`, ...).
   - The same rule covers `macromodule`, `primitive` and the SystemVerilog units Verilator
     accepts (`interface`, `program`, `checker`, `package`, `class`, `config`).
   - Escaped names (`\r32v_x`), `$` in a name, and a lifetime keyword after `module` all fail.

**Constructs that simulate differently from the synthesized circuit.** Rules 7 to 12 run on the
token stream each build compiles: `ifdef RISCV_FORMAL` resolved for the plain and the
RISCV_FORMAL build, directive lines dropped. Lint and equiv run Verilator and yosys, which
ignore or reject many of these constructs, while Icarus RTL simulation honors them. Review round
3 showed `assign #1000 uo_out = q;` passing src_check, lint and equiv, while Icarus RTL showed
`uo_out=xx` and the synthesized netlist showed `a5`.

7. **No timing controls.**
   - A `#` is allowed only as `<name> #(`, where `<name>` is a design module name: the
     parameter port list `module r32v_x #(` or a parameterized instantiation `r32v_x #(...)`.
     So `assign #5`, `assign #(5)`, `wire #1 w`, `q <= #1 d`, `#5 stmt`, `##1` and `mod #5 u`
     all fail.
   - `always` must be followed by `@`, and `@` may only follow `always`, so there are no waits
     and no intra-assignment events.
   - The event control is `@(*)`, `@*` or a list of `posedge`/`negedge` signals joined by `or`
     or `,`. A level-sensitive list (`@(a)`, `@(a or b)`) fails: Icarus honors an incomplete
     list, synthesis does not.
8. **System tasks and functions.** Only `$signed` and `$unsigned` are allowed (the seed uses
   both). Every other `$name` fails: `$display`, `$random`, `$time`, `$finish`, `$clog2`,
   `$readmemh`, and so on.
9. **Keywords.** These fail:
   - procedural and simulation-only: `specify`, `specparam`, `defparam`, `force`, `release`,
     `deassign`, `wait`, `fork`/`join`, `disable`, `event` and its `->` trigger, `task`,
     `final`;
   - SystemVerilog assertions;
   - the types `real`, `realtime`, `time`;
   - UDPs (`primitive`, `table`);
   - resolved and pulled nets: `tri`, `tri0`, `tri1`, `triand`, `trior`, `trireg`, `wand`,
     `wor`, `uwire`, `supply0`/`supply1`, `pullup`, `pulldown`;
   - drive and charge strengths;
   - `always_comb`, `always_ff`, `always_latch` (contract section 1 says plain Verilog);
   - the gate primitives (`and`, `or`, `nand`, `nor`, `xor`, `xnor`, `buf`, `not`,
     `bufif0/1`, `notif0/1`, `nmos`, `pmos`, `cmos`, `tran*`, `rtran*`, ...). `or` is allowed
     only as the separator inside an event control.
10. **No hierarchical references.** A `.` is allowed only in a named connection, right after
    `(` or `,` and followed by `name(` (ports and `#(.W(8))`). So `u.sig`, `g[0].sig`,
    `rvfi_wrapper.uut.x`, `.*` and implicit `.name` all fail.
11. **No escaped identifiers, string literals or real literals.**
    - Icarus expands macros inside escaped identifiers (`\r`__ICARUS__` becomes `\r1`).
    - A lone `\` (a backslash line continuation outside a macro) is an escaped identifier too.
    - With system tasks gone, nothing in a design needs a string.
12. **Functions read nothing stale.** A function's local variable, or its own result, must not
    be read before it is assigned on every path.
    - The walker understands `begin/end`, `if/else` (both branches), `case` (only with
      `default`), `for` (the init assigns its variable), `while`, `repeat` and assignments. A
      partial assignment (`t[0] = ...`) never counts as a full one.
    - Anything else in a function body fails, as "cannot follow its body".
    - Why: Icarus keeps a function's locals between calls, but synthesis makes them undefined.
      With `begin f = t; t = a; end`, Verilator lint is clean and yosys synthesizes it, yet
      Icarus RTL shows `a5` where the netlist shows `xx`.
    - A result left partly unassigned is not caught. Verilator `-Wall` flags locals that are
      never written.

**Differential backstop.** For the plain and the RISCV_FORMAL builds, src_check preprocesses
`src/*.v` (sorted) three times: with `verilator -E -P`, with yosys `read_verilog -ppdump` and
with `iverilog -E`.
- It tokenizes all three outputs without comments or whitespace.
- It drops `` `timescale `` (yosys consumes it) and yosys's `` `file_push ``/`` `file_pop ``
  markers.
- It requires identical token streams, and fails on any difference, any preprocessor error, or
  any Icarus preprocessor warning.

This catches tool-specific behavior the static rules might miss. With static rules bypassed, it
alone catches the reviewer's `` `ifdef verilator `` split and Icarus's macro expansion inside an
escaped identifier.

Run it alone with `python3 tools/src_check.py [--src DIR] [--no-differential]`.

Fixtures in `tools/test_fixtures/src_check/`:
- **`fail_*`:** the review repros, which must fail: Verilator lowercase predefine plus dead-region
  define, macro-pasted initial on RVFI-only state, cross-file define, include search order,
  attribute or `=` built from macros, pasted initial, `SIMD_WIDTH` define, `(* full_case *)`
  after `@(* )`.
  - Round 3 adds: assign, nonblocking and net delays, defparam, hierarchical references (own
    port, upward into `rvfi_wrapper`), a bare `always`, `$random`/`$time`/`$finish`/`$fopen`,
    `specify`, `wand`, `tri0`, `tri1`, `event`, `force`, `real`, an escaped identifier with a
    backtick, a stale function local, and a level-sensitive list.
  - The delay, stale-local and level-sensitive fixtures also fail the real lint and equiv
    gates.
- **`pass_*`:** must pass.
  - `pass_bitserial` is a realistic bit-serial sample (generate loop, function, typed
    localparams and parameters, `` `default_nettype none ``, `` `timescale ``, `always @(*)`,
    an `ifdef RISCV_FORMAL` port block). It also passes lint and equiv.
  - The others are a generate loop, `parameter integer`/`localparam integer`, `@(* )`, and the
    reviewer's minimal lint-clean top (`pass_minimal`).

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
  "order": ["lint", "area", "cosim", "arch", "equiv", "gl", "formal"],
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
- **First:** src_check must pass (see above), the static rules and the differential backstop.
- **A (gold):** `read_verilog -mem2reg -DRISCV_FORMAL`, `hierarchy -check -top`, `proc`,
  `flatten`, `delete -port` on every `rvfi_*` wire, `opt_clean`.
- **B (gate):** the same without the define.
- **Ports:** both builds must expose exactly the eight TT ports with the contract's directions
  and widths.
- **No initial values:** no `init` attribute may appear in any of three places:
  - B;
  - A after the strip;
  - A before the strip (`gold_prestrip.il`). That is where RVFI-only state still exists, so a
    power-up value on RVFI-only state shows up only there.

  The proof ignores init values, so otherwise formal could assume a power-up value the chip
  lacks.
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
- **Portable paths:** every changed path present in HEAD must use only `[A-Za-z0-9._-]` and
  `/` (the characters src_check allows), with no component starting with `-`. That rules out
  `+`, whitespace, control characters, `;`, quotes, backslash and non-ASCII.
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
with `annotate`. It rewrites that one line; comment lines, blank lines and every other entry stay
byte for byte, and git keeps the history.

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
