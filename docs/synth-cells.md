# Synthesis cell set and synthesis knobs (hypothesis 004)

Branch `hyp/004-synth-cells`, a lead-assigned physical/flow hypothesis. It changes only
`src/config.json`'s user section (above "DO NOT CHANGE ANYTHING BELOW THIS POINT") and adds this
file. No RTL change.

## Result in one paragraph

LibreLane removes the PDK's `no_synth.cells` from the synthesis liberty, so ABC never sees the
smallest drive strength of most logic cells, the `_1` flip-flops, or `edfxtp_1`, `maj3` and
`mux2i`. Two config keys give them back to synthesis, and `SYNTH_STRATEGY` "AREA 2" adds a little:

- seed: 47,724.5 to 38,939.8 um2 (-18.4 %);
- H001: 17,503.0 to 15,472.3 um2 (-11.6 %).

Both numbers are measured with `tools/area.py` (LibreLane 3.0.14 `Yosys.Synthesis`, the real
`src/config.json`). The change works in TT's flow as is: tt-support-tools copies the keys
verbatim and the precheck has no cell rule. Both designs harden clean with it, the precheck
included: H001 in 1x2, the seed in 3x2. The final placed std-cell area falls by 8.4 % (H001)
and 19.5 % (seed) against the stock config at the same tile, and timing repair shrinks too.

## The change

```json
"EXTRA_EXCLUDED_CELLS": [
  "!sky130_fd_sc_hd__a*_1", "!sky130_fd_sc_hd__o*_1", "!sky130_fd_sc_hd__n*_1",
  "!sky130_fd_sc_hd__inv_1", "!sky130_fd_sc_hd__xor2_1", "!sky130_fd_sc_hd__xnor2_1",
  "!sky130_fd_sc_hd__df*_1", "!sky130_fd_sc_hd__edf*_1", "!sky130_fd_sc_hd__maj3_*",
  "!sky130_fd_sc_hd__mux2i_*", "!sky130_fd_sc_hd__ha_*", "!sky130_fd_sc_hd__fah*_1"
],
"SYNTH_STRATEGY": "AREA 2"
```

A `!` entry is a deny wildcard of LibreLane's cell filter (Q1). It takes the matching cells back
out of the synthesis exclusion set. With these 12 entries, synthesis sees 291 of the liberty's 428
cells instead of 192. The 99 cells it gains are listed under Q4.

## Q1. Which variable controls the synthesis exclusion list in LibreLane 3.0.14?

Paths are relative to the LibreLane 3.0.14 package (`~/.cache/uv/archive-v0/bP0agsj6dNSM_MJH4FeQs/librelane`,
the same version as the `ghcr.io/librelane/librelane:3.0.14` image).

- **Three inputs, one union.** `steps/pyosys.py:343-352` (the `Yosys.Synthesis` step TT runs)
  builds the synthesis exclusion set from `EXTRA_EXCLUDED_CELLS`, then adds
  `SYNTH_EXCLUDED_CELL_FILE` and `PNR_EXCLUDED_CELL_FILE`. It hands the set to
  `toolbox.remove_cells_from_lib`. `steps/yosys.py:107-114` does the same for the Tcl-based Yosys
  steps.
  - `SYNTH_EXCLUDED_CELL_FILE` (`config/flow.py:174-180`): "excluded from the lib file in
    synthesis alone", `pdk=True`. The PDK sets it to
    `libs.tech/openlane/sky130_fd_sc_hd/no_synth.cells`. Deprecated names: `NO_SYNTH_CELL_LIST`,
    `SYNTH_EXCLUSION_CELL_LIST`.
  - `PNR_EXCLUDED_CELL_FILE` (`config/flow.py:181-187`): excluded from synthesis and PnR,
    `pdk=True`; the PDK's `drc_exclude.cells`.
  - `EXTRA_EXCLUDED_CELLS` (`config/flow.py:375-380`): a design-level list of wildcards, "to
    exclude from both synthesis and PnR".
- **PnR sees only two of them.** `steps/openroad.py:333-335` joins `EXTRA_EXCLUDED_CELLS` and
  `PNR_EXCLUDED_CELL_FILE` into `_PNR_EXCLUDED_CELLS`. `scripts/openroad/common/io.tcl:408-409`
  passes it to `set_dont_use`, and `read_current_odb` calls that in every OpenROAD step
  (`io.tcl:428`). `SYNTH_EXCLUDED_CELL_FILE` never reaches PnR.
- **The filter has deny entries.** `remove_cells_from_lib` (`common/toolbox.py:369-435`) matches
  cell names with `Filter` (`common/misc.py:267-316`). An entry starting with `!` goes to a deny
  list, and a cell is removed only if it matches an allow wildcard and no deny wildcard
  (`misc.py:301-316`). So a `!` entry in `EXTRA_EXCLUDED_CELLS` un-excludes cells that either
  file lists. The deny entries win over `drc_exclude.cells` too, so none of ours may match one of
  its cells; a check over the liberty confirms that none does (Q4).
- **What OpenROAD does with a `!` entry.** It treats it as a cell name, finds none and warns
  `[WARNING STA-0122] cell '!sky130_fd_sc_hd__...' not found.`, then goes on (tested with the
  image's OpenROAD dcf36133). The dont_use list stays `drc_exclude.cells`.
- **A design config may override PDK-class variables.** `config/config.py:702-722` loads the PDK
  and SCL `config.tcl` values first, then `mutable.update(...)` (line 714) applies the design's
  own keys over them. TT itself does this for gf180 (`LIB_SYNTH` and others with `pdk_dir::`,
  tt-support-tools `tech.py:192-199`). `dir::` resolves against the design directory and
  `pdk_dir::` against the PDK (`config/preprocessor.py:227-235`). A `Path` variable must point to
  an existing file (`common/types.py:63-68`).
- **What it logs.** Nothing at INFO or WARNING level; `warning.log` stays empty in all three
  variants. The evidence is:
  - `resolved.json` in the run directory, with the final `SYNTH_EXCLUDED_CELL_FILE` and
    `EXTRA_EXCLUDED_CELLS`;
  - the trimmed liberty in the run's `tmp/`, with one `/* removed <cell> */` line per excluded
    cell (`toolbox.py:414`): 236 with the stock config, 137 with ours;
  - the netlist's cell types.

### Two ways to write the change, and why the committed one uses `!` entries

| Variant | Config | Netlist (seed, H001) | Works with |
|---|---|---|---|
| file | `"SYNTH_EXCLUDED_CELL_FILE": "dir::synth_exclude.cells"` plus a list file in `src/` | byte-identical to the deny variant | TT CI and `harden_tt.sh` (both copy all of `src/`), **not** `tools/area.py` |
| deny (committed) | `EXTRA_EXCLUDED_CELLS` with 12 `!` entries | 38,939.8 / 15,472.3 um2 with AREA 2 | everything, no file |
| drc only | `"SYNTH_EXCLUDED_CELL_FILE": "pdk_dir::libs.tech/openlane/sky130_fd_sc_hd/drc_exclude.cells"` | same area, but 61 inverters become `clkinv_1` | rejected: it gives synthesis the clock and delay cells |

- **Why the file variant breaks area.py.** `tools/area.py` `stage()` (lines 190-212) copies only
  the collected `*.v` sources and `.vh`/`.svh`/`.vinc` include files into its stage. A
  `dir::synth_exclude.cells` path then doesn't exist inside the container, LibreLane rejects the
  config, and `make area`/`make eval` fail. I measured the file variant through area.py by
  copying the same list into a scratch source directory as `synth_exclude.vh`. Its netlists are
  byte-identical to the deny variant's (seed `65cb4c31...`, H001 `86ef6cd3...` under AREA 0).
- **To switch to the file variant**, `tools/area.py` would have to stage the files `config.json`
  references with `dir::` (or every regular non-`test_*` file in `src/`). That's a tools change
  for the lead. The list itself is under Q4.

## Q2. Does TT's tooling accept it?

tt-support-tools `01d5d2814fa9`, the pin `harden_tt.sh` uses (the action checks out `main`).

- **Config merge.** `project.py:480-484` (`create_merged_config`) reads `src/config.json` and
  `src/user_config.json` and merges them with `dict.update`. `config_utils.py:12-15`
  (`read_json_config`) only drops the `"//"` key. `create_user_config` writes `DESIGN_NAME`,
  `VERILOG_FILES`, `DIE_AREA`, `FP_DEF_TEMPLATE`, `VDD_PIN`, `GND_PIN`, `RT_MAX_LAYER` and the
  tech's `librelane_config`, which is `{}` for sky130A (`tech.py:78`). Neither key we add is
  touched. There's no allowed-variable list in code.
- **Hardening.** `project.py:535` runs `librelane ... src/config_merged.json`. The action
  (`TinyTapeout/tt-gds-action@ttsky26d`, `action.yml`) installs `librelane==3.0.14` and runs
  `tt_tool.py --create-user-config` then `--harden`. Nothing filters config keys.
- **Precheck.** `precheck/precheck.py:481-560` lists every check: magic DRC, KLayout FEOL, BEOL,
  offgrid, pin label, zero area, KLayout checks, pin, boundary, power pin, layer, cell name,
  urpm/nwell, antenna, analog pin and Verilog syntax. The only cell rule is the cell name check
  (`precheck.py:298-308`): no `#` or `/` in a name. There's no rule on cell types or drive
  strengths.
- **Cell usage summary.** The action's "Cell usage summary" step runs
  `tt_tool.py --print-cell-category`. `project.py:822-824` indexes `tech/sky130A/categories.json`
  by cell base name and would raise on an unknown one. All the new base names are in that map:
  `edfxtp`, `maj3` and `ha` are categories 5, 14 and 14, `mux2i` 6, `nand2` 2, and so on.
- **Template convention.** TT's `src/config.json` comments mark the keys above "DO NOT CHANGE" as
  user-tunable. The two new keys sit there, with comments.

## Q3. Why does open_pdks exclude the _1 cells?

- **History.** `no_synth.cells` dates from open_pdks' 2020 OpenLane configuration. Commit `2ca4602681`
  (2021-02-11, "Revised the exclude lists used in openlane") split the old list into
  `no_synth.cells` (synthesis only) and `drc_exclude.cells` (synthesis and optimization) and
  "aggressively reduced the don't-use list". `716d1cd89d` (2021-02-22) then allowed `mux2_1` and
  `mux4_1` again. No commit message gives a reason for the `_1` cells.
  <https://github.com/RTimothyEdwards/open_pdks/commits/master/sky130/openlane/sky130_fd_sc_hd/no_synth.cells>
- **The maintainers.**
  - OpenLane 2/LibreLane's maintainer opened "Document `no_synth.cells`" (fossi-foundation/open-pdks
    #430, 2024-02-04): the cells "do not mention a reason for exclusion, creating confusion as to
    whether the cells themselves are bad or just cause issues with Yosys". It has no answer.
    <https://github.com/fossi-foundation/open-pdks/issues/430>
  - OpenROAD's maintainer answered the same question in OpenLane discussion #2179 (2024-12-24):
    "The _1 sized cells are the smallest and can sometimes give difficulty to the router due to
    their small pin shapes. You can enable them and see if they work ok or not (the router does
    improve over time)." <https://github.com/The-OpenROAD-Project/OpenLane/discussions/2179>
- **The cells with real problems are in the other list.** OpenLane's docs describe
  `DRC_EXCLUDE_CELL_LIST` (now `PNR_EXCLUDED_CELL_FILE`) as the cells "with drc errors", and the
  resizer may still use every `no_synth.cells` cell. The known DRC case is google/skywater-pdk
  #261, where `clkdlybuf4s15_1` violates SkyWater's own poly rule; it's in `drc_exclude.cells`.
  <https://openlane.readthedocs.io/en/latest/reference/configuration.html>,
  <https://github.com/google/skywater-pdk/issues/261>
- **OpenROAD's own flow uses them.** ORFS's `flow/platforms/sky130hd/config.mk` excludes only the
  probe and `lpflow` cells, so its synthesis maps to `_1` cells freely.
  <https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/blob/master/flow/platforms/sky130hd/config.mk>
- **Verdict.** It's a routability precaution (small pin shapes), not a silicon,
  characterization, DRC or LVS defect. Our signoff checks exactly that: TritonRoute DRC, magic
  DRC, KLayout DRC and LVS, and TT's precheck repeats magic and KLayout DRC. A routing problem
  would show up there. Drive strength isn't a concern at 50 ns: the resizer upsizes and buffers
  wherever slew, capacitance or fanout need it.
- **`maj3`, `mux2i`, `ha`, `fah` and `edfxtp_1`.** No source names a problem with them. They are
  in no list OpenROAD uses, so PnR may already place them. `ha` and the `fah` cells have two
  outputs, which ABC's mapper skips, and none appears in any netlist here. Allowing them changes
  nothing today.

## Q4. The minimal exclusion list

The rule: keep out of synthesis every clock-tree, delay, physical-only, low-power, probe, latch,
clock-gate, scan and tri-state cell, plus everything `drc_exclude.cells` lists (LibreLane always
applies it). Allow the rest. As a list file (the file variant above), that's:

```
# clock-tree cells (CTS and timing repair only)
sky130_fd_sc_hd__clkbuf_*
sky130_fd_sc_hd__clkinv_*
sky130_fd_sc_hd__clkinvlp_*
sky130_fd_sc_hd__clkdlybuf4s*
# delay cells (hold repair only)
sky130_fd_sc_hd__dlygate4sd*
sky130_fd_sc_hd__dlymetal6s*
# physical-only, low-power and probe cells
sky130_fd_sc_hd__decap_*
sky130_fd_sc_hd__diode_*
sky130_fd_sc_hd__lpflow_*
sky130_fd_sc_hd__probe*
sky130_fd_sc_hd__macro_sparecell
# clock gates and latches (contract v1 has no latches; the latch map names dlxtp_1/dlxtn_1 itself)
sky130_fd_sc_hd__dlclkp_*
sky130_fd_sc_hd__sdlclkp_*
sky130_fd_sc_hd__dlrbn_*
sky130_fd_sc_hd__dlrbp_*
sky130_fd_sc_hd__dlrtn_*
sky130_fd_sc_hd__dlrtp_*
sky130_fd_sc_hd__dlxbn_*
sky130_fd_sc_hd__dlxbp_*
# scan flip-flops
sky130_fd_sc_hd__sdf*
sky130_fd_sc_hd__sedf*
# tri-state drivers
sky130_fd_sc_hd__einvn_*
sky130_fd_sc_hd__einvp_*
sky130_fd_sc_hd__ebufn_1
```

- **Cells it allows that the PDK's list excludes** (99): every `_1` logic cell (`a*_1`, `and*_1`,
  `nand*_1`, `nor*_1`, `o*_1`, `or*_1`, `inv_1`, `xor2_1`, `xnor2_1`); the `_1` flip-flops
  (`dfxtp_1`, `dfrtp_1`, `dfrtn_1`, `dfstp_1`, `dfsbp_1`, `dfrbp_1`, `dfxbp_1`, `dfbbn_1`,
  `dfbbp_1`); `edfxtp_1` and `edfxbp_1`; `maj3_1/2/4`; `mux2i_1/2/4`; `ha_1/2/4`; `fah_1`,
  `fahcin_1`, `fahcon_1`.
- **Cells it excludes that the PDK's list allows:** none.
- **Still excluded via `drc_exclude.cells`**, among others: `o21ai_0`, `a2111oi_0`, `a21boi_0`,
  `and2_0`, `or2_0`, `o311ai_0`, `xor3_*`, `xnor3_*`, `fa_4`, `mux4_4`, `buf_16`. So
  `o21ai_2 -> o21ai_0` from the lead's resize estimate isn't available; ABC uses `o21ai_1`.
- **The 12 deny entries give the same set.** A script applies LibreLane's `Filter` rule to the
  liberty for both forms and finds the same 137 excluded cells. No deny entry matches a
  `drc_exclude.cells` cell. The netlists agree byte for byte.

## Measurements

`tools/area.py` (LibreLane 3.0.14 image, flat `Yosys.Synthesis`, `--no-hier` for the sweep).
Seed = main `032a65f`'s `src/tt_um_fesens_r32v.v`. H001 = `9a39f1d:src/tt_um_fesens_r32v.v`.
"stock" = main's `src/config.json`. "new cells" = the cell set above. Every number below is
measured.

### Stock versus the committed config

| Design | Config | area_um2 | cells | flops |
|---|---|---|---|---|
| seed | stock | 47,724.5216 | 4,065 | 631 |
| seed | new cells, AREA 0 | 39,449.0848 | 3,673 | 631 |
| seed | **new cells, AREA 2 (committed)** | **38,939.8464** | 3,748 | 631 |
| H001 | stock | 17,503.0368 | 1,150 | 582 |
| H001 | new cells, AREA 0 | 15,494.8608 | 1,164 | 582 |
| H001 | **new cells, AREA 2 (committed)** | **15,472.3392** | 1,164 | 582 |

The committed `src/config.json` reproduces the AREA 2 rows exactly (`tools/area.py --src src/`
for the seed, and H001's source with `--config src/config.json`; same netlist hashes).

Where the area went (stock to committed):

- **Seed.** Flip-flops plus muxes: 27,576 to 23,422 um2. 574 of the 631 flops became `edfxtp_1`
  (30.03 um2), an enable flop that absorbs the `mux2_1` in front of a `dfxtp_2` (21.27 + 11.26
  um2). The other logic went from 20,148 to 15,517 um2.
- **H001.** The flops went from 12,379 to 12,012 um2 (546 `dfxtp_1`, 36 `edfxtp_1`). The muxes
  went from 1,216 to 571 um2 and the other logic from 3,908 to 2,890 um2.

### Knob sweep

Each row is one variable changed from the row's base: "new" is the new cell set with AREA 0, and
"stock" is main's config. All values are area_um2.

| Knob | seed new | H001 new | seed stock | H001 stock |
|---|---|---|---|---|
| base (AREA 0) | 39,449.1 | 15,494.9 | 47,724.5 | 17,503.0 |
| SYNTH_STRATEGY AREA 1 | 39,087.5 | 15,503.6 | 48,201.2 | 17,479.3 |
| SYNTH_STRATEGY AREA 2 | **38,939.8** | 15,472.3 | 47,773.3 | 17,589.4 |
| SYNTH_STRATEGY AREA 3 | 44,462.6 | 16,502.1 | 63,239.4 | 19,379.8 |
| SYNTH_STRATEGY DELAY 0 | 42,733.5 | 16,074.2 | 59,065.4 | 18,780.5 |
| SYNTH_STRATEGY DELAY 1 | 41,835.1 | 16,143.0 | 57,405.1 | 18,809.3 |
| SYNTH_STRATEGY DELAY 2 | 42,400.7 | 16,021.6 | 58,762.6 | 18,999.5 |
| SYNTH_STRATEGY DELAY 3 | 41,826.4 | 16,059.2 | 57,375.0 | 18,745.5 |
| SYNTH_STRATEGY DELAY 4 | 42,980.0 | 16,524.6 | 56,397.8 | 18,911.9 |
| SYNTH_ABC_AREA_USE_NF | 39,666.8 | 15,503.6 | 49,087.1 | 17,508.0 |
| SYNTH_ABC_USE_MFS3 | ABC crash | ABC crash | ABC crash | ABC crash |
| SYNTH_ABC_LEGACY_REFACTOR | 39,306.4 | 15,474.8 | 48,251.3 | 17,443.0 |
| SYNTH_ABC_LEGACY_REWRITE | 39,748.1 | 15,516.1 | 47,551.9 | 17,539.3 |
| SYNTH_ABC_DFF | 39,078.7 (630 flops) | 15,482.3 | 48,228.8 (630 flops) | 17,549.3 |
| SYNTH_SHARE_RESOURCES false | 39,449.1 | 15,494.9 | 47,724.5 | 17,503.0 |
| SYNTH_ADDER_TYPE FA | 39,449.1 | 15,494.9 | 47,724.5 | 17,503.0 |
| SYNTH_ADDER_TYPE RCA | 39,644.3 (632 flops) | 15,837.7 | 46,131.7 (632 flops) | 17,918.4 |
| SYNTH_ADDER_TYPE CSA | 42,140.4 (632 flops) | 15,526.1 | 48,715.5 (632 flops) | 17,550.6 |
| SYNTH_SIZING | 39,711.8 | 15,643.8 | 48,148.7 | 17,636.9 |
| SYNTH_ABC_BUFFERING | 40,507.6 | 15,828.9 | 51,358.0 | 18,007.3 |
| SYNTH_DIRECT_WIRE_BUFFERING false | 39,434.1 | 15,479.8 | 47,709.5 | 17,488.0 |
| SYNTH_BUFFER_CELL buf_1 | 39,445.3 | 15,491.1 | 47,720.8 | 17,499.3 |

Combinations on the new cell set (area_um2, seed / H001):

| Combination | seed | H001 |
|---|---|---|
| AREA 2 + LEGACY_REFACTOR | 39,148.8 | **15,406.0** |
| AREA 2 + LEGACY_REFACTOR + buf_1 | 39,145.0 | 15,402.3 |
| AREA 2 + buf_1 | 38,936.1 | 15,468.6 |
| AREA 2 + LEGACY_REWRITE | 38,992.4 | 15,516.1 |
| AREA 2 + LEGACY_REFACTOR + LEGACY_REWRITE | 38,976.1 | 15,472.3 |
| AREA 1 + LEGACY_REFACTOR | 39,150.0 | 15,469.8 |
| AREA 2 + AREA_USE_NF | 39,043.7 | 15,547.4 |
| AREA 2 + SIZING | 39,123.8 | 15,617.5 |
| AREA 2 + ABC_DFF | 38,717.1 (630 flops) | 15,426.0 |

What I took and what I left:

- **Taken: the cell set** (-17.3 % seed, -11.5 % H001 at AREA 0) and **AREA 2**, which is the
  best robust strategy on both designs (-509.2 um2 seed, -22.5 um2 H001 on top of the cell set).
- **Left: `SYNTH_ABC_LEGACY_REFACTOR`.** It's the best for H001 (another -66 um2), but it costs
  the seed 209 um2, and LibreLane describes it as "more unstable". ABC's choices here move by a
  few tenths of a percent with any RTL change, so a 0.4 % edge on one design isn't a reason to
  carry it.
- **Left: `SYNTH_ABC_DFF`.** It hands the flops to ABC, whose AREA scripts contain `retime`. The
  seed came out with 630 flops instead of 631. Retiming and flop merging change the state encoding, which works
  against invariant 6 (no reliance on power-up values) and the RVFI/equivalence gates.
- **Left: `SYNTH_BUFFER_CELL` buf_1 and `SYNTH_DIRECT_WIRE_BUFFERING` false.** 4 to 15 um2.
  Turning direct-wire buffering off can leave `assign` statements, and LibreLane's
  `Checker.NetlistAssignStatements` stops the flow on them (`ERROR_ON_NL_ASSIGN_STATEMENTS`
  defaults to true, `steps/checker.py`).
- **Left: `SYNTH_ABC_USE_MFS3`.** ABC asserts (`pCut[i] == pCut0[k]`) on both designs.
- **No effect:** `SYNTH_SHARE_RESOURCES` and `SYNTH_ADDER_TYPE` FA. RCA saves 1,593 um2 on the
  stock seed (it maps adders onto `fa` cells), but it costs area once the small cells are there.

## Hardening

`tools/harden_tt.sh` (TT's flow and precheck, see docs/physical.md), one run per row, each
against the stock config at the same tile. H001 used `--project` with H001's source and the
committed `src/config.json`; the seed's new run hardened the committed tree itself. Every run
exited 0 with status `pass`:

- magic DRC 0, route DRC 0, and the precheck's KLayout FEOL, BEOL and offgrid decks clean;
- LVS 0, antenna 0 nets and 0 pins;
- setup and hold clean over all nine corners;
- every precheck row passed.

| Design, tile | Config | Synth um2 | Final std-cell um2 | Utilization | repair_um2 (cells) | hold_buffers | Setup WS ns | Hold WS ns |
|---|---|---|---|---|---|---|---|---|
| H001, 1x2 | stock | 17,503.0 | 25,514.5 | 0.7448 | 6,107.11 (612) | 561 | 32.657 | 0.1257 |
| H001, 1x2 | committed | 15,472.3 | **23,363.7** | 0.6820 | 5,960.72 (598) | 540 | 34.581 | 0.1552 |
| seed, 3x2 | stock | 47,724.5 | 63,547.2 | 0.5731 | 11,531.06 (1,185) | 607 | 20.559 | 0.1152 |
| seed, 3x2 | committed | 38,939.8 | **51,131.5** | 0.4612 | 7,073.03 (767) | 75 | 18.558 | 0.1076 |

- **The net counts.** "Final std-cell um2" is LibreLane's `design__instance__area__stdcell`
  after PnR: logic, repair, CTS, taps and endcaps. It drops by 2,150.8 um2 (-8.4 %) for H001 and
  by 12,415.7 um2 (-19.5 %) for the seed. Repair didn't grow. It shrank, most of all for the
  seed, whose `edfxtp_1` flops need far fewer hold delay cells than `dfxtp_2` plus `mux2_1`.
- **Repair cells by type.**
  - H001: `dlygate4sd3_1` 561 to 540, `clkdlybuf4s25_1` 48 to 54, `buf_1` 3 to 4.
  - Seed: `dlygate4sd3_1` 607 to 75, `clkdlybuf4s25_1` 465 to 524, `buf_4` 77 to 71, `buf_2` 28
    to 54, `buf_1` 1 to 28, `buf_6` 7 to 15.
- **Reported, not gated** (TT's flow doesn't gate them either):
  - max slew violations: H001 265 to 305, seed 2,252 to 2,296;
  - max cap violations: H001 0 to 0, seed 24 to 7.
- **Wirelength.** H001 76,679 to 70,030 um, seed 225,652 to 237,296 um.
- **Runtime.** LibreLane plus precheck: H001 125 + 40 s to 160 + 26 s, seed 263 + 62 s to 337 +
  82 s, all with the formal jobs running alongside.
- **No 1x1 run for H001.** Its synthesized area (15,472 um2) is above the brief's ~13.9k
  threshold, and its final 1x2 std-cell area (23,364 um2) is far above the 1x1 core (16,493 um2).
- **The stock seed run reproduces docs/physical.md**: setup 20.56 ns, hold 0.115 ns, utilization
  0.573, repair 11,531.06 um2 in 1,185 cells, 607 hold buffers.
- **TT's other CI steps.**
  - `tt_tool.py --print-cell-category`, the action's "Cell usage summary" step, exits 0 on both
    new runs. It lists `edfxtp`, `mux2i` and `maj3`.
  - Each OpenROAD step logs the 12 STA-0122 warnings: 216 in the seed run over 18 step logs,
    none in the stock runs. `error.log` is empty.
- **PnR already places `no_synth.cells` cells on its own.** The stock runs' final netlists hold
  `dlygate4sd3_1`, `clkdlybuf4s25_1`, clock buffers and, in the seed, one `nor2_1` and one
  `nand2_1` from the resizer.

## Gate-level test and eval

- **`python3 tools/eval.py`** (what `make eval` runs) on the committed tree, sha `3eba62f`:
  - lint PASS;
  - area PASS, 38,939.8464 um2, 3,748 cells, 631 flops;
  - cosim PASS, 90 runs, CPI 130.74, worst retirement gap 230;
  - arch PASS, 43 tests;
  - equiv PASS, 1,672 `$equiv` cells;
  - gl PASS;
  - formal FAIL, "runner missing (verif/formal/run_formal.py)": formal isn't on main yet.
- **`python3 tools/eval.py --gate gl`** on its own: PASS, 1 cocotb test, on the seed's new
  netlist.
- **H001's new synthesis netlist** (36 `edfxtp_1`) under the same `make -C test GATES=yes`:
  PASS.
- **Both hardened, powered netlists** (`tt_submission/tt_um_fesens_r32v.v`, the file TT's
  gl_test action simulates, hold buffers included): PASS for H001 and for the seed.

## Risks

- **Routability of `_1` cells.** That's the reason the list exists (Q3). Signoff covers it on the
  runs above, but a denser design, a smaller tile or a tile-fit search pushes routing harder. A
  route DRC failure there would show up as a fail, not a silent defect.
- **Hold repair and slew.** Smaller cells are faster and drive less. On these two designs the
  repair area went down, not up. Max slew violations (reported, not gated) rose by 40 (H001) and
  44 (seed). Neither TT's flow nor `harden_summary.py` gates them, and every corner meets setup
  and hold.
- **Tighter tiles.** The 1x2 and 3x2 runs had slack: utilization 0.68 and 0.46. A tile-fit
  search or a 1x1 attempt with the small cells is untested.
- **Undocumented semantics.** The `!` entries rely on LibreLane's `Filter` deny rule
  (`common/misc.py:267-316`), which the `EXTRA_EXCLUDED_CELLS` description doesn't mention. TT's
  action pins LibreLane 3.0.14 for ttsky26d. A later LibreLane could change it; the area would
  then jump back to the stock numbers, visibly. The file variant uses only documented variables
  but needs the area.py staging fix.
- **STA-0122 noise.** Each OpenROAD step prints 12 "cell '!sky130_fd_sc_hd__...' not found"
  warnings, 216 per run. They're warnings only; TT's flow and `harden_summary.py` don't gate on
  them.
- **`edfxtp_1`.** The seed now has 574 enable flops. They're standard, characterized cells and
  the gate-level test simulates them, but they're a cell type the stock flow never synthesized.
