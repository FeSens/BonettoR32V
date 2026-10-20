# Layout sweep for hypothesis 113 (2026-10-20)

The record of the sweep that found the sign-off layout settings in `src/config.json`
(docs/signoff.md, "Layout"). The scripts ran on hwe-bench from `signoff/overlays/` in a mirror of
d944aaf; they are kept here as they ran, so their paths still say `signoff/overlays`.

- `NAME.json`: one overlay each, the LibreLane settings that point adds to `src/config.json`.
- `results.txt`: one line per point the manager ran (`s*`, `t*` screened to global routing; `b3`,
  `c1`, `c2` through the full flow). The other overlays (`v*`, `w*`, `z*`, `a1`, `b1`, `b2`, `c3`)
  are from the earlier hand-started runs of the same sweep (`start.sh`, `watch.sh`, `st.sh`,
  `stoppt.sh`); their numbers are not recorded here, and the best of them plateaued at 9
  detailed-routing violations.
- Line format: `kind` s (screen) or d (full run), `ov` the global-routing overflow, `wl` the global-routing wirelength, `hold` the hold buffers, `drt` the detailed-routing
  iteration and violations at the end (`21:0` = 0 violations at iteration 21), then the overlay.
- `mgr.sh`: the queue manager (screens, full runs with an early-abort curve, `mgr.stop` to stop);
  `drtprog.sh` prints a run's detailed-routing curve; `mgr.log` its log; `maxd`, `maxs` its
  concurrency limits.

Outcome: c1 (overflow threshold 0.13, no initial placement, layer adjustments 0.99/0.1/0.1) and c2
(0.13, no initial placement, PL hold margin 0.005) reached 0 violations and signed off clean. c1
is in `src/config.json`; a fresh harden of afdb678 reproduced it exactly.
