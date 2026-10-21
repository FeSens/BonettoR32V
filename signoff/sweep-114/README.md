# Layout sweep for hypothesis 114 (2026-10-21)

The record of the sweep that found sign-off layout settings for 114 (ba04be4, branch
hyp/114-periph-files: the peripheral block split into one module per peripheral, 23,109.7 µm²
synth). With the committed `src/config.json` (113's c1), 114 reached global-routing overflow 0 but
detailed routing plateaued near 400 violations (the lead's harden in `~/bonetto-work/h114`, as
reported to this sweep; this sweep's own screen of c1, point `base`, reproduced overflow 0). The
scripts ran on hwe-bench in
`~/bonetto-work/sw114`, a `git archive` of ba04be4 (without `third_party/` and `.claude/`), from
`signoff/sweep-114/`; TT's flow through `tools/harden_tt.sh --overlay`, unchanged.

- `NAME.json`: one overlay each, merged over ba04be4's `src/config.json` (which already holds c1:
  threshold 0.13, no initial placement, layer adjustments 0.99/0.1/0.1/0/0/0).
- `results.txt`: one line per point (`kind` s = screened to global routing, d = full flow). Fields:
  `ov` global-routing overflow, `wl` global-routing wirelength (µm), `lay` per-layer global-routing
  demand/resource, `hold` hold buffers, `drt` the detailed-routing iteration and violations at the
  end, `why` why the run stopped; full runs add `curve` (violations per DRT iteration, the
  antenna re-route iterations appended) and the summary (`status`, setup and hold worst slack,
  utilization, hold buffers, max slew and cap violation counts, failing checks).
- `mgr.sh`: the queue manager (113's, plus the per-layer record, added after the first 30 records,
  which therefore have no `lay`); `drtprog.sh`, `stoppt.sh`, `st.sh` as in sweep-113; `mgr.log` its
  log (manual stops through `stoppt.sh` are not in it, their reason is in `results.txt`); `maxd`,
  `maxs` its last concurrency limits. Points first screened and later run in full have two lines.
- `gl.txt`: TT's gate-level test (`make -C test GATES=yes GL_NETLIST=<run>/proj/tt_submission/
  tt_um_fesens_r32v.v`, hwe-bench, PDK_ROOT=~/.ciel) on each clean run's routed netlist.
- Families: `a*` threshold, initial placement, layer adjustments, PL hold margin and wirelength
  coefficient around c1; `b*` met3/met4 at their real capacity (see below) and GRT_ADJUSTMENT;
  `c*` a15 with a larger PL hold margin.

## Outcome

Seven points sign off clean (every check of `summary.json` true, TT precheck pass, all 15 rows),
and TT's gate-level test passes on each routed netlist (TESTS=5 PASS=5):

| Point | Overlay over ba04be4's config.json | Setup WS | Hold WS | Util | Hold buf | Slew viol (ss 100C 1.60V: >0.75 / >1.5 ns, worst) | Cap viol | DRT 0 at | LibreLane s |
|---|---|---|---|---|---|---|---|---|---|
| a15 | `PL_ROUTABILITY_OVERFLOW_THRESHOLD` 0.14, `PL_SKIP_INITIAL_PLACEMENT` false | +2.012 | +0.0193 | 0.7982 | 134 | 789 / 12, 1.634 | 2 | it 16 (+2 antenna re-route its) | 1,670 |
| c01 | a15 + PL hold margin 0.02 | +2.746 | +0.0248 | 0.7983 | 135 | 794 / 21, 1.721 | 5 | it 16 (+6) | 2,235 |
| c02 | a15 + PL hold margin 0.025 | +2.621 | +0.0361 | 0.7996 | 139 | 850 / 28, 1.845 | 6 | it 13 (+4) | 2,423 |
| c03 | a15 + PL hold margin 0.03 | +3.156 | +0.0462 | 0.8012 | 145 | 807 / 15, 1.852 | 5 | it 16 | 2,777 |
| c04 | a15 + PL hold margin 0.04 | +3.124 | +0.0551 | 0.8027 | 150 | 786 / 29, 1.988 | 5 | it 14 (+6) | 2,181 |
| c05 | a15 + PL hold margin 0.05 | +2.961 | +0.0639 | 0.8126 | 183 | 819 / 24, 1.876 | 5 | it 21 (+5) | 1,739 |
| c06 | a15 + PL hold margin 0.06 | +3.111 | +0.0697 | 0.8221 | 216 | 829 / 34, 2.132 | 8 | it 21 (+9) | 2,464 |

Setup and hold are the worst slack over the 9 corners (ns); the worst hold corner is
min_ff_n40C_1v95 throughout. "DRT 0 at" is the main detailed-routing iteration that reached 0,
then the iterations of the re-route after the antenna repair. Runtimes are on a heavily shared
hwe-bench (load 25 to 60), not comparable with an idle-box harden. The runs are kept on hwe-bench
in `~/bonetto-work/sw114/build/harden-NAME/` (`summary.json`, `proj/tt_submission/`), the
gate-level logs in `~/bonetto-work/sw114/build/gl-NAME.log`.

c07 (hold margin 0.08: 263 hold buffers, global-routing overflow 31) was stopped with detailed
routing iteration 0 still unfinished after 18 minutes. Past 0.05 the hold gain flattens (+0.0058
ns for 33 more hold buffers at 0.06) while slow-corner slew gets worse.

Choice by the lead's order (hold slack, then slew above 1.5 ns, then setup): c06 has the most hold
slack, c05 is nearly as good with 33 fewer hold buffers, 10 fewer slow-corner pins above 1.5 ns
and a 1.88 ns worst slew instead of 2.13; c03 has the best slew of the higher-margin points. Any
of them is a fallback for the others; they share one placement, so a placement change (say, in
TT's CI) would affect them together, and a08 (a different placement, 3 violations left) is not a
fallback.

## What we learned

1. **GRT_LAYER_ADJUSTMENTS: a per-layer 0 means "use GRT_ADJUSTMENT" (0.3).** The global
   router's resources show it: the default `[0.99, 0, 0, 0, 0, 0]` gives met1/met2/met3/met4
   5,309/6,510/4,718/3,007; c1's `[0.99, 0.1, 0.1, 0, 0, 0]` gives 6,968/8,463/4,718/3,007; a small
   nonzero met3/met4 (0.01) gives met3 6,260 and met4 4,185. So c1 *relaxes* met1 and met2 (0.3 to
   0.1 derating) in global routing and in routability-driven placement, which is why its overflow is
   0 so often; it does not reserve more room for detailed routing.
2. **Overflow 0 does not predict detailed routing for 114.** 28 of the 52 screened points reached
   overflow 0. Among full runs with overflow 0, detailed routing ranged from the 400-violation
   plateau (c1 itself) to 0 at iteration 16.
3. **Global-routing wirelength (under c1's adjustments) ranked the full runs in order**: a15
   145,376 µm clean; a08 151,144 plateau at 3 violations (iterations 27-41); a30 151,958 314 at
   iteration 12; a10 153,048 1,242 at iteration 7; a27 160,949 1,183 at 7; a07 162,081 3,898 at 3;
   c1 168,429 (point `base`; its detailed routing is the lead's run, about 400 at 16). For a future
   netlist: screen, then run the full flow on the lowest-wirelength overflow-0 points first.
4. **The hold margin can be raised on a routable placement.** Hold repair runs after CTS
   (ResizerTimingPostCTS; RUN_POST_GRT_RESIZER_TIMING is off, so GRT_RESIZER_HOLD_SLACK_MARGIN is
   unused), so a15 and the c* points share the same detailed placement (identical DEF after
   34-openroad-detailedplacement, md5 checked for every c* point that finished) and differ only in
   the post-CTS hold buffers. Up to 0.06 all of them route to 0, and the worst hold slack grows with
   the margin.
5. The "realistic capacity" family (`b*`: met1/met2 at 0.2-0.4, met3/met4 at 0.01-0.1, or a lower
   GRT_ADJUSTMENT) screened to overflow 0 at several points, but none went through detailed routing
   to the end: b05 and b09 were stopped early to relieve the shared box once the a15 family had
   signed off.
6. The early DRT curve separates runs within a few iterations: the clean runs had 1,200-1,900
   violations at iteration 3 and fewer than 150 at iteration 8; every run that did not converge had
   more than 2,000 at iteration 3 and more than 400 at iteration 8 (or 790 at iteration 7 where it
   was stopped there).
