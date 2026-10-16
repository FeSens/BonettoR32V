# Formal verification at the pins

`verif/formal` proves the contract (`docs/contract.md`, sections 2-4 and 6) on the real Tiny
Tapeout top module, `tt_um_fesens_r32v`, at its pins. It is a fixed gate: hypothesis agents never
edit it, and every candidate core in the area loop must pass it.

```
lockf -k /tmp/bonetto-heavy.lock python3 verif/formal/run_formal.py          # per candidate
lockf -k /tmp/bonetto-heavy.lock python3 verif/formal/run_formal.py --deep   # champions
pytest verif/formal/test_formal.py -v                                        # the gate's own tests
```

The runner writes `build/formal.json`:
`{"pass", "checks": {name: "PASS|FAIL|UNREACHED"}, "depth", "runtime_s", ...}`. It exits 0 only
when every check is `PASS`. `UNREACHED` (a check whose trigger the solver cannot reach within the
depth) counts as a failure.

## How it works

1. **Staging.** `src/*.v` and `src/*.sv` are copied into `build/formal/work-<pid>/`. Stale work
   dirs of dead processes are removed.
2. **Pre-pass.** yosys elaborates the `RISCV_FORMAL` build. It fails on latches, on flip-flops
   that are not on the rising edge of `clk`, and on formal-only cells (`$anyconst`, `$initstate`,
   ...) in the core. Formal multiclock is off, so anything but posedge-`clk` flip-flops would be
   modelled wrongly. It removes the core's own assert/assume/cover statements and every initial
   value, so no check can rely on power-up values (contract section 1). The core is written to
   `core.il` and read separately from the checkers, so its macros cannot reach them.
3. **Pins equivalence** (`equiv`). The `RISCV_FORMAL` build with its `rvfi_*` ports removed must
   be equivalent to the plain build (yosys `equiv_make`, `equiv_simple -seq 5`, `equiv_induct`).
   The checks run on the RVFI build; this is what makes them speak for the design that ships. It
   overlaps with the equivalence gate (`make equiv`) on purpose, so the formal result stands on
   its own.
4. **Depth.** See below.
5. **Tasks.** One SBY task per check, plus the reach tasks, run in parallel (at most 8).

### Environment (assumptions)

- `rst_n` is low for exactly the first rising edge (the contract's minimum), then high. The
  `reset` check instead holds it low for 1 to 8 edges.
- `ui_in` is a free constant. Formal holds it constant (contract section 3).
- `uio_in[7:3]` and `uio_in[1:0]` are free every cycle.
- MISO (`uio_in[2]`) is free every cycle, except during a high SCK phase that began with a rising
  edge clocking a READ data bit. There it holds the value it had on that rising edge (contract
  rule 3). The SPI monitor records that value as the memory's bit. The bus checks then constrain
  it to the memory contents. A core that samples MISO while SCK is low reads noise and fails the
  bus checks (mutant `miso_low`).
- `ena = 1`. All non-reset registers, the register file included, start at free values.

### The SPI monitor

`bonetto_spi_monitor` (in `wrapper.sv`) decodes the pins into transactions: the 8-bit command,
the 16-bit address, and data bytes at `(A + k) mod 2^16`. It emits an event for every READ data
bit (byte address, bit index, MISO value) and every complete data byte (READ or WRITE, address,
value), plus command and end-of-transaction events. `test_formal.py::test_monitor_decodes_stub`
checks it against a hand-written waveform (stretched phases, a READ wrapping at 0xFFFF, a READ
and a WRITE ending mid-byte, an incomplete header, MISO noise while SCK is low).

### Forks of riscv-formal (in `verif/formal/checks/`, the submodule is untouched)

| Fork | Why |
|---|---|
| `rvfi_testbench.sv` | 16-bit cycle counter (stock: 8 bits, saturating at 255); free `check` (unbounded style); pins and monitor events for the checkers; free reset length for the `reset` check. |
| `bonetto_insn_check` | Stock semantics plus: halt equals the spec's trap; a trap has no effect (rd, rmask and wmask all 0); register fields exact (unused fields report 0); exact memory masks; no PMA or ALIGNED_MEM. |
| `bonetto_reg_check` | Stock (reads return the last write) plus: a register not yet written reads the same value every time. |
| `bonetto_pc_bwd_check` | Stock pc_bwd only fires when order N+1 retires before N, never true in order (unreachable). The fork checks the time-adjacent pair: order + 1 and pc chaining. |
| `bonetto_unique_check` | Stock unique fails on any core with a free trig. The fork counts retirements per order. |
| `bonetto_liveness_check` | Counter since the last retirement: the 1,024-cycle window must close with a retirement unless halted. Stock fires check right after trig in unbounded mode. |
| `bonetto_ill_check` | 0x00000000 and 0xFFFFFFFF. Stock only covers 0x00000000. |
| `bonetto_bus_checks.sv` | Byte shadow at a 16-bit pin address, misaligned and wrap aware; GPIO is not RAM. Stock shadows aligned words at 32-bit addresses and only on `mem_addr == addr`. |
| new: `fence`, `ecall`, `ebreak` models, `silent`, `rvfi`, `spi`, `gpio`, `reset`, reach | See the property table. |

`isa_rv32e.txt` and `isa_rv32e.v` are generated by `gen_isa_rv32e.py` from the submodule's
`isa_rv32i.txt`. Each `rvfi_insn_e_<name>` wraps the stock RV32I model and drops `spec_valid`
when a register field the instruction uses names x16-x31 (the E filter). Those encodings are
reserved (UNSPECIFIED), so the instruction checks demand nothing of them. The consistency checks
(reg, pc, causal, unique, rvfi, bus, gpio, silent, liveness) still apply to every retirement.

### Non-vacuity: the reach tasks

Every check has one or more `reach_*` statements: its trigger (for an instruction check, a
retirement of that instruction with `spec_valid`). The reach task instantiates all checkers in
one design, keeps every assumption, turns each `reach_*` into `assert(!trigger)` and removes all
other assertions. A BMC with `--keep-going` over the whole depth then falsifies every reachable
trigger. A trace of the combined design is also a trace of each single check's design (fewer
assumptions), so a trigger reached there is reachable in that check's own task. A check whose
proof passes but whose trigger is not reached is `UNREACHED`.

## Depth policy

D is computed by the runner, never set by an agent, and recorded in `build/formal.json`:

```
D_min = reset_steps + max(first_retire_cycle, max_cycles_per_insn) + max_cycles_per_insn + margin
D     = max(depth_floor, D_min)        (config)
FAIL if D > depth_cap                  (config)
```

`first_retire_cycle` and `max_cycles_per_insn` come from the cosim's `run_cosim.py --mode rvfi
--prog build/programs/selftest.hex --timing-json <file>` when it exists. Until then the runner
measures them itself: `timing_tb.v` runs the candidate in Icarus against a behavioural 23LC512
(noise on MISO while SCK is low) on `build/programs/selftest.hex`, or on `timing_prog.S` when the
selftest isn't built. D therefore covers at least two retirements of the slowest instruction
class measured. Slower instances (none in the seed: its shifter is a barrel shifter) are left to
cosim and the architecture tests.

(Measured numbers, chosen D and the fast/deep split: filled in below.)
