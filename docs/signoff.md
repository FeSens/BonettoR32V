# Tape-out sign-off: BonettoR32V on TTSKY26d

What was checked before submitting the design to Tiny Tapeout, on which exact commit, with what
result, and what is still a risk. Every number here was measured by the lead on 2026-10-19 unless
it is labelled otherwise.

- **Design:** hypothesis 113 on top of champion 112 (pair fetch). Source commit d944aaf (the RTL is
  hyp/113-pmod-timing f0a62bf); 50 MHz signoff clock; 1x2 tile.
- **Score:** CoreMark 0.6791 iterations/s at 50 MHz (73,622,855 cycles per iteration), synthesized
  area 23,084.6 µm², 2,339 cells, 496 flip-flops. Hypothesis 113 costs 0.78 % of CoreMark against
  112 (0.6845) to meet the PSRAM's CS_n high time and to enter flash continuous read the documented
  way (below).
- **Shuttle:** TTSKY26d closes 2026-11-30; chips about 2027-05, boards about 2027-06.

## Evidence

| Area | Check | Result |
|---|---|---|
| Gates (`make eval FORMAL=1`, d944aaf, hwe-bench) | lint (Verilator, plain and RVFI builds) | pass |
| | area and the 1x2 pre-filter | 23,084.64 µm² ≤ 23,100 |
| | cosim: 32 programs x rvfi and pins modes x 4 power-up seeds, liveness fuzz x 5 | 256/256, fuzz 5/5; worst retirement gap 305 cycles (fuzz 488) |
| | riscv-arch-test, RV32E and privilege, seeds 1-4 | 53/53 |
| | equivalence, RVFI-stripped build vs plain build | pass (2,656 cells proven) |
| | TT's test on the RTL with Icarus (TT's test workflow) and on the synthesized netlist | 5/5 and 5/5 |
| | riscv-formal at the pins, fast config | 65/65 PASS in 3,615 s |
| Formal strength | 46 planted bugs (docs/formal.md, "Mutants") against the fast config on 112 | 39 caught by formal; the other 7 caught by cosim (one only after the byte-lane checks of a5b20fb); every mutant fails at least one gate |
| Soak (signoff/soak.sh, d944aaf) | 300 random programs of about 4,200 instructions and 50 of about 13,000, both modes, 4 seeds; liveness fuzz seeds 6-55 (102,400 probes) | 2,850/2,850 runs pass, 2.1 billion simulated cycles |
| Clock range (cosim, d944aaf) | the whole cosim suite at 12 MHz and 6.25 MHz | pass; 5 MHz fails the PSRAM's 8 µs tCEM (expected: a 41-cycle transaction is 8.2 µs) |
| Memory chips vs datasheets | docs/pmod-audit.md: pin map, every command, timing, power-up, the read-timing budget | 3 mismatches and 4 unverified points; each fixed, documented or kept with a reason (below) |
| Layout (LibreLane 3.0.14, TT's flow and precheck) | DRC, LVS, antenna, setup and hold at 9 corners, TT precheck | pending (see "Layout") |
| Post-layout netlist | TT's test on the placed-and-routed netlist with power pins | pending (112's layout: 5/5) |

## Fixes made during sign-off

1. **PSRAM CS_n high time (audit M3).** The APS6404L needs 18 ns between bursts; the core gave one
   20 ns clock, and TT's pads shorten a high pulse by up to about 4 ns. The core now holds CS_n
   high for 2 clocks between PSRAM transactions (hyp 113), and contract 2.1.2, the cosim Pmod model
   and the formal monitor check ceil((18 + 4) ns / T) cycles.
2. **Flash continuous-read entry (audit U1).** The boot entered continuous read with a read aborted
   before its dummy clocks, which the datasheet never documents; if the flash did not latch it, the
   chip would never boot. The boot read now completes (4 dummy and 4 data edges, bus released), and
   the models reject the aborted form.
3. **TT's RTL test could not compile the design.** Icarus refused a net used before its
   declaration (Verilator and yosys accept it), so TT's GitHub test workflow would have failed. The
   declaration moved (no logic change), and the gl gate now runs TT's RTL test under Icarus too.
4. **TT's test checked the Pmod at 40 MHz.** test/test.py still had the pre-102 clock; it now reads
   `CLOCK_PERIOD`.
5. **UART and SPI byte lanes.** No program wrote byte 1 of UART_DATA, so a design starting a
   transmission on it passed every gate; uart_echo and spi_loop now check contract 5's byte-lane
   rules (a5b20fb).
6. **Documentation.** The flash part is the W25Q128JVSIM (factory QE = 0; TT's store Pmods ship
   with QE = 1, and TT's flasher does not set it); power-cycle the board after a design that may
   leave the flash in QPI, deep power-down or wrap mode, and before re-flashing; clock range 6.25 to
   50 MHz; never stop the clock while the design runs.

## Layout

Pending.

## Remaining risks

| Risk | Why it remains | Mitigation |
|---|---|---|
| Read timing through TT's pads at 50 MHz | TT publishes only TT 3.5 room-temperature round-trip data (20.4 ns); paper setup margin +11 ns, about +4 ns with a 35 % slower pad | lower the clock on the demo board (6.25 to 50 MHz verified in simulation) |
| Real memory chips never exercised | no hardware; every check uses our models, now audited against the datasheets | the audit's unverified points (docs/pmod-audit.md) are low risk or field-proven |
| Formal is bounded | the fast config proves 308-466 cycles from reset; the deep config does not finish on this core (proofs at 613 cycles timed out at 5 h) | cosim, the soak, arch-test and the mutant results cover later behavior |
| Slew in the slow corner | max_ss_100C_1v60 has nets above 1.5 ns (112's layout: 9 pins, worst 2.41 ns) on weak drivers; setup still closes there | timing includes them; lower clock if needed |
| Flash left in an unsupported mode by another design | the boot only undoes standard SPI and EBh continuous read | power-cycle the board first (manual, programming guide) |
| Flashing flow | taken from TT's sources, not tried | documented step by step (manual 6.9) |

## What the user does to submit

1. Enable the repository's GitHub workflows (gds, test, docs) and run gds on main: TT's own
   hardening, precheck and gate-level test must pass there.
2. Buy 2 tiles (1x2) for TTSKY26d at app.tinytapeout.com and submit the repository before
   2026-11-30.
