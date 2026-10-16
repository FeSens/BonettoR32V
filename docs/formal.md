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
5. **Tasks.** One SBY task per check (each one proves its own triggers reachable, see below),
   run in parallel (at most 8).

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
| `bonetto_insn_check` | Stock semantics plus: halt equals the spec's trap; a trapping retirement is compared only on trap, halt, `rd_addr = 0` and `wmask = 0` (contract section 6); register fields of other retirements exact (unused fields report 0); exact memory masks; no PMA or ALIGNED_MEM. |
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

### Non-vacuity: a reach task per check

Every check has one or more `reach_*` statements: its triggers. For an instruction check that is
a retirement of that instruction with `spec_valid`, and for JAL, JALR, the branches, ECALL and
EBREAK also a trapping one. Each check runs as two SBY tasks on the same design (same checker,
same assumptions, same depth):

- the **proof task**: the check's assertions, with the reach statements removed;
- the **reach task**: `BONETTO_REACH_AS_ASSERT` turns each `reach_*` into `assert(!trigger)` and
  every other assertion is removed. smtbmc runs with `--keep-going` in small windows, so a failed
  `reach_*` assertion is a trigger reached within the depth, it is dropped (never assumed), and
  the run stops as soon as every trigger has been reached.

A check is `PASS` only if its proof task passed and every required trigger was reached,
`UNREACHED` if the proof passed but a required trigger was not reached, and `FAIL` otherwise
(a counterexample, or a task that timed out or errored).

The two queries are kept apart on purpose. One solver call that mixes easy satisfiable goals (the
triggers) with a hard unsatisfiable one (the proof) can stall: on the seed, `bus_imem` at D = 232
took 19.6 s as a proof alone and 61.1 s as a reach alone (one window each), but more than 10
minutes with both in one run (stopped). SBY's cover mode would solve step by step.

## Properties and the contract clauses they encode

Every assertion, by check (one SBY task each; `insn_<name>` is 40 tasks). Each check also has
the triggers listed under "reach", which must be reached (see "Non-vacuity").

| Check | Assertions | Contract clause |
|---|---|---|
| structure (pre-pass) | no latches; every flip-flop `$dff`-family on the rising edge of `clk`; no formal-only cells in the core; initial values removed | 1: single clock domain, no latches, no negedge flip-flops, no reliance on power-up values |
| equiv (pre-pass) | RVFI build with `rvfi_*` stripped is equivalent to the plain build | 6: RVFI logic drives no pin |
| `insn_<name>` | `insn_trap`, `insn_halt` (trap and halt equal the spec's trap) | 4: fatal traps (ECALL, EBREAK, misaligned jump/branch target), trap = halt; nothing else traps |
| | `insn_trap_rd_addr`, `insn_trap_wmask` (on a spec trap) | 4: no register write and no memory write; 6: only these are compared on a trapping retirement |
| | `insn_rs1_zero`, `insn_rs2_zero`, `insn_rs1_addr`, `insn_rs2_addr` (non-trapping) | 6: register fields report the index read, unused fields 0 with rdata 0; 4: x0 reads 0 |
| | `insn_rd_addr`, `insn_rd_wdata`, `insn_pc_wdata` (non-trapping) | 4: RV32E semantics (riscv-formal's rv32i model, E filter), FENCE a no-op; 6: a write to x0 reports rd 0, wdata 0 |
| | `insn_rmask`, `insn_wmask`, `insn_mem_addr`, store data per lane | 6: exact first-byte address, LSB-aligned masks, lane k = byte at mem_addr + k; 3/4: misaligned accesses never trap |
| | reach: `reach_insn`; also `reach_insn_trap` for JAL, JALR, branches, ECALL, EBREAK | non-vacuity |
| reg | `reg_rs1`, `reg_rs2`, `reg_rs1_rs2` | 4: reads return the last value written (every encoding); 1: registers start unknown but do not change by themselves |
| pc_fwd | `pc_fwd` | 4: PCs chain (order N's pc_rdata = order N-1's pc_wdata) |
| pc_bwd | `pc_bwd_order`, `pc_bwd_pc` | 6: one instruction per pulse, order counts retirements; 4: PCs chain |
| causal | `causal` | 6: in-order retirement; 4: register reads return the last write |
| unique | `unique_order` | 6: `rvfi_order` counts retirements (no repeats) |
| rvfi | `conv_intr`, `conv_mode`, `conv_ixl` | 6: intr 0, mode 3, ixl 1 |
| | `conv_rs1_x0`, `conv_rs2_x0`, `conv_rd_x0`, `conv_rmask` (non-trapping); `conv_wmask` | 6: register and memory field conventions |
| | `conv_trap_halt`, `conv_trap_rd_addr`, `conv_trap_wmask` | 4: a trap halts, no register or memory write; stricter reading: nothing halts without trapping |
| | `conv_first_order`, `conv_first_pc`, `conv_order_next` | 6: order from 0; 1: execution starts at pc = 0 |
| ill | `ill_trap`, `ill_halt`, `ill_rd_addr`, `ill_wmask` for `0x00000000` and `0xFFFFFFFF` | 4: fatal traps |
| silent | `silent_retire`, `silent_write`, `silent_uo` (after a halting retirement) | 4: never retires again, never starts another SPI WRITE; 3: uo_out changes only on an IO store |
| liveness | `liveness` (no 1,024-cycle window without a retirement, unless halted) | 4: latency cap |
| bus_imem | `bus_imem_read`, `bus_imem_value` | 6: fetch completeness (own READ of all 4 bytes, before retirement); 3: fetch reads RAM at pc[15:0] (64 KB alias); 2: memory semantics |
| bus_dmem | `bus_dmem_load_read`, `bus_dmem_load_value` (non-trapping) | 6: load completeness; 3: RAM bytes (addr[15:0] + k) mod 2^16, misaligned and wrapping |
| | `bus_dmem_store_once`, `bus_dmem_store_value` | 3: store bytes; 2 rule 5: every byte sent is written; 6: traffic finishes before retirement |
| | `bus_dmem_write_claimed`, `bus_dmem_read_claimed` | 6: transaction order (a fetch reads only its bytes, a load or store moves only its data); 3: an IO access never appears on the pins |
| gpio | IO load lanes (unnamed, one per byte lane) | 3: byte X with X[1:0] = 0 reads `ui_in`, other IO bytes read 0 (misaligned multi-byte IO unspecified) |
| | `gpio_uo_hold`, `gpio_uo_once`, `gpio_uo_cause`, `gpio_uo_value` | 3: uo_out changes only from an IO store writing byte 0, holds the new value no later than one cycle after it retires; 1: 0 after reset |
| spi | `spi_uio_oe`, `spi_uio_out_hi`, `spi_uio_out_2` | 1: uio_oe constant, unused outputs 0 |
| | `spi_reset_cs`, `spi_reset_sck`, `spi_reset_uo`, `spi_reset_valid` | 1: reset state of the pins; nothing retires in reset |
| | `spi_rule1_prev`, `spi_rule1_now`, `spi_rule2` | 2: rules 1 and 2 |
| | `spi_rule4_cmd`, `spi_rule4_header`, `spi_rule5_write` | 2: rules 4 and 5 |
| | `spi_cs_at_valid` | 6: CS_n high at the retirement cycle |
| reset | spi and rvfi checks with rst_n low for 1 to 8 edges | 1: rst_n low for at least one edge |
| cover | reach only: a non-trapping retirement, a trapping one, a second retirement | riscv-formal's cover check |

Rule 3 (MISO valid only while SCK is high) and rule 6 (no maximum phase length) are environment
assumptions, not assertions: MISO is noise outside a held high phase, and phase lengths are free.

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
