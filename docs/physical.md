# Physical tooling: area score, local TT hardening, tile fit

Three tools score a candidate, from cheap to expensive:

| Tool | Score | Runtime on this Mac | Load |
|---|---|---|---|
| `tools/area.py` | flattened sky130 cell area after TT's own synthesis step | 4 to 8 s | light |
| `tools/harden_tt.sh` | TT's full GDS flow plus TT's precheck, pass or fail | 4 to 5 min (NanoV 1x2, seed 3x2) | heavy, takes the lock |
| `physical/tilefit.py` | smallest share of the real TT tile that still signs off clean | 7 hardening runs, about 25 min plus lock waits (NanoV 1x2) | heavy, takes the lock per run |

## Pins and versions

| Item | Pin | Where it comes from |
|---|---|---|
| TT action | `TinyTapeout/tt-gds-action@ttsky26d` (commit 395eedb) | `.github/workflows/gds.yaml` |
| tt-support-tools | `01d5d2814fa9dd61e9d211e0b235a4a592a9316a` (main, 2026-08-20) | the action checks out `main`; pinned here |
| LibreLane | 3.0.14, image `ghcr.io/librelane/librelane:3.0.14` (native arm64) | the action's `librelane-version` default |
| Tools in that image | yosys 0.62, OpenROAD dcf36133, magic 8.3.623, klayout 0.30.7, netgen 1.5.316, Verilator 5.044 | `docker run ... --version` |
| sky130A for the flow | open_pdks `8afc8346a57fe1ab7934ba5a6056ea8b43078e71` | LibreLane 3.0.14 `pdk_hashes.yaml`, fetched with ciel 2.4.0 into `~/.ciel` |
| sky130A for the precheck | open_pdks `0536d02d875c8f67dd7cca3902ac457e62f20005` | `tt-gds-action/precheck@ttsky26d`; fetched (not enabled) into `~/.ciel/ciel/sky130/versions/` |
| Python | 3.11 venvs for tt-support-tools and the precheck (`build/tt-env/`) | the action uses 3.11 |
| Precheck python deps | klayout 0.30.8, gdstk 0.9.52, yowasp-yosys 0.63 | `tt/precheck/requirements.txt` |

`tools/harden_tt.sh --setup-only` fetches all of it (tt-support-tools clone, both venvs, both
PDKs, the magic/klayout shims). Nothing from the PDK or tt-support-tools is committed.

## `tools/area.py`: the fast score

```
tools/area.py --top tt_um_fesens_r32v --src src/
```

- **Synthesis.** It runs LibreLane's own `Yosys.Synthesis` step (`librelane --only
  Yosys.Synthesis`) in the LibreLane image. The config is the repo's `src/config.json` merged
  with the keys `tt_tool --create-user-config` adds (`DESIGN_NAME`, `VERILOG_FILES`, `VDD_PIN`,
  `GND_PIN`, `RT_MAX_LAYER`; the floorplan-only `DIE_AREA` and `FP_DEF_TEMPLATE` are left out).
  So the netlist is the one TT's flow places and routes:
  - `SYNTH_STRATEGY` "AREA 0" (LibreLane's default; TT's config doesn't set it);
  - `SYNTH_HIERARCHY_MODE` flatten, the `tt_025C_1v80` liberty minus the PDK's
    `no_synth.cells` and `drc_exclude.cells`;
  - ABC with `-D 50000` (the 50 ns clock).
- **Excluded cells.** `no_synth.cells` removes 141 of the `_1` drive cells, `dfxtp_1`,
  `nand2_1`, `nor2_1` and `inv_1` among them. Synthesis therefore maps to `dfxtp_2` (21.27 um2)
  and other `_2` cells, but keeps `mux2_1`.
- **Score.** `area_um2` is the flattened netlist's `stat -liberty` area. `cells`, `flops` and
  `latches` count that netlist. Flop and latch kinds come from the liberty's `ff` and `latch`
  groups; clock-gating cells count as latches.
- **Breakdowns.**
  - `by_type`: count and area per cell type, sorted by area.
  - `by_module`: a second, hierarchical run (`SYNTH_HIERARCHY_MODE=keep`, otherwise identical,
    run in parallel). It gives each module's own area (`self_area_um2`) and yosys' inclusive
    figure. Its total (`by_module_total_um2`) is larger than the score, because flattening
    optimizes across module boundaries. NanoV: 17,737 vs 14,662 um2.
- **Outputs.**
  - `build/area.json`.
  - `build/synth/netlist.v`: the flattened, unpowered LibreLane netlist, for the gate-level
    test.
- **Sources.** By default, every `*.v` and `*.sv` under `--src`, minus any path component
  starting with `test_`. TT's `info.yaml` list isn't used, since hypotheses can't edit it.
  `--files` or `--info` override this (baselines).
- **Name check.** Source names reach yosys command strings, so a file named
  `z.v; exec -- cmd; y.v` would inject commands. Every collected `src/` file must therefore
  match `^[A-Za-z0-9_]+\.v$`: no `.sv`, no subdirectories, nothing else. Otherwise the tool exits
  with status 2. Explicit baseline lists may add plain `[A-Za-z0-9_]+` directory components.
  `--top` and `--param` are checked the same way, and the yosys fallback also quotes paths.
  `harden_tt.sh` applies the same rule before `tt_tool` sees the list.
- **Speed.** 3.6 to 7.7 s per call (flat and hierarchical runs in parallel), measured on NanoV
  and SERV.
- **Determinism.** Two NanoV runs gave identical JSON, `runtime_s` aside, and a byte-identical
  netlist.
- **Fallback.** `--engine yosys` renders `tools/synth_area.ys`, a pass-by-pass mirror of
  LibreLane's `synthesize.py` for these settings, and runs the yosys on PATH.
  - Run with the image's yosys 0.62, it gives NanoV's LibreLane area exactly (14,661.5616 um2,
    1,310 cells).
  - The oss-cad-suite yosys 0.64 gives 14,591.4944 um2, 0.48 % less, because the yosys versions
    differ.
  - Use the fallback only when Docker is down, and never compare its numbers with the default
    engine's.
- **Docker mounts.** It mounts only its stage directory and the PDK, never `$HOME` (see
  "Docker on this Mac").

## `tools/harden_tt.sh`: TT's flow, locally

```
tools/harden_tt.sh                      # this repo, 1x1, writes build/harden/summary.json
tools/harden_tt.sh --tiles 1x2          # same, staged with a bigger tile
```

It stages a copy of the project in `build/harden/proj` (the real `src/` and `info.yaml` are
never touched). Then it does what the action does:

1. `tt/tt_tool.py --create-user-config`, with the pinned tt-support-tools in a Python 3.11
   venv.
2. LibreLane 3.0.14's Classic flow on `src/config_merged.json`:
   `librelane --pdk-root ~/.ciel --run-tag wokwi --force-run-dir runs/wokwi`. That's the
   command `tt_tool --harden` hands to `librelane --dockerized`.
3. `tt_tool.py --create-tt-submission`, then TT's `precheck.py` on the submission GDS, with the
   precheck's own PDK pin.

**Deviations from CI.** All of them are mechanical; none touches the flow config:

- LibreLane runs in the image directly. It mounts the stage directory and `~/.ciel`, not all of
  `$HOME`, as `--dockerized` would.
- The precheck's `magic` and `klayout` binaries are thin shims into the LibreLane image: magic
  8.3.623 and klayout 0.30.7, against TT's nix pins of 8.3.568 and 0.30.8. Its Python side
  (klayout 0.30.8, gdstk) runs on the host, because gdstk has no linux-aarch64 wheel for the
  image's Python.
- The info.yaml source list is regenerated from `src/` (same rule as `area.py`), unless
  `--keep-info-sources` is given.
- `commit_id.json` and `pdk.json` are written locally, as `tt_tool --harden` would, so that
  `--create-tt-submission` works.

**`build/harden/summary.json`.** `pass` is true only when all of these hold:

- the flow completed;
- magic DRC = 0 and TritonRoute DRC = 0;
- KLayout DRC is clean. TT's config sets `RUN_KLAYOUT_DRC=0`, so this comes from the
  precheck's KLayout FEOL, BEOL and offgrid decks;
- LVS = 0;
- antenna: 0 violating nets and 0 violating pins;
- setup and hold: 0 violations and worst slack >= 0, over all nine STA corners (LibreLane's
  aggregate metrics);
- 0 power-grid violations, 0 critical disconnected pins, no illegal overlap;
- the whole precheck passes.

Definitions:

- `setup_wns` and `hold_wns` are min(0, worst slack). `setup_ws` and `hold_ws` are the signed
  worst slacks.
- `utilization` is LibreLane's `design__instance__utilization`: placed standard-cell area
  (taps included, fill excluded) over core area.
- `die_um2` is the die area.
- Max slew and max cap violations are reported, not gated: TT's flow doesn't gate them either.
- With `--no-precheck`, `klayout_drc` and `precheck` become null and `pass` covers the rest.

Exit status: 0 = pass, 1 = it doesn't sign off, 2 = tool error.

**Locking.** The script takes `/tmp/bonetto-heavy.lock` itself (`lockf -k`). Don't wrap it in
`lockf` yourself: the second lock waits on the first forever. If you already hold the lock, set
`BONETTO_HEAVY_LOCKED=1`.

**Proof.** Two runs passed every check, the precheck included:

- NanoV with its own config, `--tiles 1x2`: LibreLane 186 s plus precheck 43 s; setup worst
  slack 23.94 ns, hold worst slack 0.085 ns; utilization 0.452.
- The seed, `--tiles 3x2`: LibreLane 253 s plus precheck 59 s; setup worst slack 20.56 ns,
  hold worst slack 0.115 ns; utilization 0.573.

The 1x1 and 2x2 failures are under "Findings".

## `physical/tilefit.py`: the physical score

```
physical/tilefit.py                     # this repo, 1x1, writes build/tilefit.json
```

- **Blockage.** Each trial is a full `harden_tt.sh` run, in the same staged TT template (tt_tool
  config, the tile's die, top-edge pins and PDN), plus one LibreLane key:
  `FP_OBSTRUCTIONS = [[x_cut, core_y0, core_x1, core_y1]]`. That's LibreLane 3.0.14's supported
  hard placement blockage: `floorplan.tcl` calls `odb::dbBlockage_create` before
  `initialize_floorplan`, and the docs say no placement site is ever generated there. Routing
  and the PDN aren't obstructed.
- **Fraction.** The placeable fraction is `(x_cut - core_x0) / core_width`, with `x_cut` on the
  0.46 um site grid. The 1x1 core is (2.76, 2.72) to (158.24, 108.80), which is 16,493.3 um2.
- **Pass.** A trial passes when its `summary.json` passes: every check above, the precheck
  included.
- **Density.** Trials keep the config's `PL_TARGET_DENSITY_PCT` (60 in TT's template).
  LibreLane 3.0.14's OpenROAD raises the target itself when the free area is too small for it.
  It logs warning GPL-0302, "Automatically adjusting to uniform density". The seed in 2x2, for
  example, ran at 0.76 instead of 0.60.
  - So density goes up only when the blockage forces it, and every run records the target
    placement actually used (`gpl_target_density`).
  - GPL-0301 means the cells don't fit at all: utilization above 100 %, counting OpenROAD's
    pin-density area adjustment.
  - If a future OpenROAD turns GPL-0302 back into an error, the trial reruns once at the
    suggested density plus 1.
- **Search.**
  1. The full tile first. If it fails, `min_fraction` is null.
  2. The lower bound is the synthesized cell area over the core area, a fraction that can't pass.
  3. Then `--steps` (default 6) bisection steps, so 7 runs, or a few more when density retries
     happen.
  The search assumes pass and fail are monotonic in the fraction. PnR noise can break that near
  the edge.
- **Output.** `build/tilefit.json`: `min_fraction`, `placeable_um2`, `density` (the passing
  run's standard-cell area over `placeable_um2`), `target_density_pct`,
  `highest_failing_fraction`, and every run with its checks and failure reason.
- **Physical hypotheses** pass `--overlay FILE.json` (extra LibreLane keys for every trial),
  kept under `physical/`.

## Findings

- **NanoV-1tile doesn't harden in 1x1 with TT's stock flow.** Global placement stops with
  GPL-0301, utilization 104.491 %:
  - 14,661.6 um2 of cells plus a 1,972.3 um2 "pin density area adjust", against 16,493.3 -
    574.3 um2 of free core.
  - Its signed-off 1x1 run used its own workflow and a patched LibreLane 3.0.5 image: `gpl.tcl`
    gets `-disable_pin_density_adjust` and `cts.tcl` gets `-dont_use_dummy_load
    -no_insertion_delay`. TT's action can't do that.
  - In the stock flow, a 1x1 design must keep its cells plus the pin-density adjustment (about
    13 % of the cell area here) under roughly 15,900 um2. In practice routing needs much more
    margin.
- **Our 1x1 has no room for a flip-flop register file.**
  - 16 x 32 RF bits as `dfxtp_2` alone are 10,890 um2 (512 x 21.27).
  - SERV with its RF cut to 16 registers synthesizes to 30,820 um2 (docs/baselines.md).
  - The seed (47,725 um2 synthesized) passes in 3x2, not in 2x2 (docs/baselines.md). Neither
    NanoV nor the seed fits 1x1 in the stock flow, so the 1x1 tilefit has no candidate yet.
    `--tiles` runs it in a bigger tile meanwhile; NanoV in 1x2 is the worked example.
- **Hold fixing is a large physical cost for the seed.** 1,285 timing-repair buffers
  (12,238 um2) on 47,725 um2 of logic, 607 of them hold buffers. `area.py` can't see this;
  tilefit and harden do.

## Which flip-flop and latch cells the flow uses

- **`dfxtp_1` is excluded from synthesis.** It's on the PDK's `no_synth.cells`
  (`libs.tech/openlane/sky130_fd_sc_hd/no_synth.cells`), which LibreLane strips from the
  liberty before synthesis (`SYNTH_EXCLUDED_CELL_FILE`). So `dfflibmap` maps every flop to
  `dfxtp_2` (21.27 um2; `dfxtp_1` is 20.02 um2).
- **The resizer doesn't bring `dfxtp_1` back.** PnR's dont_use list is only `drc_exclude.cells`
  (`PNR_EXCLUDED_CELL_FILE`), so OpenROAD would be allowed to use `dfxtp_1`, but it never
  downsizes flops for area. NanoV's final 1x2 netlist has 196 `dfxtp_2` and no `dfxtp_1`.
- **`dlxtp_1` is on neither list.** LibreLane's `SYNTH_LATCH_MAP` (`latch_map.v`) maps yosys
  `$_DLATCH_P_` to `sky130_fd_sc_hd__dlxtp_1` explicitly, and `$_DLATCH_N_` to `dlxtn_1`. The
  other latch cells are on `no_synth.cells`: `dlrtp`, `dlrtn`, `dlrbp`, `dlrbn`, `dlxbn`,
  `dlxbp` and the clock gates `dlclkp` and `sdlclkp`.
- **Other `_1` cells.** `no_synth.cells` also removes `inv_1`, `nand2_1`, `nor2_1`, `xnor2_1`,
  the `clkbuf` and `clkinv` cells and 141 `_1` cells in all. `mux2_1` stays.

## Docker on this Mac

- **The crash.** During the first SERV area run, Docker Desktop's VM died with "service fs
  failed: injecting event blocked for 60s". At that point both containers were mounting all of
  `$HOME`. It had to be force-quit and relaunched.
- **The fix.** Since then, every container here mounts only what it needs: the stage
  directory, the PDK and `build/`. That's also why `harden_tt.sh` doesn't use
  `librelane --dockerized`. There's been no crash since.
- **If Docker is down**, `area.py --engine yosys` still works, with the caveat above.
