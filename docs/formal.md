# Formal verification at the pins

`verif/formal` proves the contract (`docs/contract.md`, sections 2-4 and 6) on the real Tiny
Tapeout top module, `tt_um_fesens_r32v`, at its pins. It is a fixed gate: hypothesis agents never
edit it, and every candidate core in the area loop must pass it.

```
lockf -k /tmp/bonetto-heavy.lock python3 verif/formal/run_formal.py          # fast: per candidate
lockf -k /tmp/bonetto-heavy.lock python3 verif/formal/run_formal.py --deep   # deep: champions
BONETTO_FORMAL_HOST=hwe-bench python3 verif/formal/run_formal.py [--deep]    # same, on hwe-bench
pytest verif/formal/test_formal.py -v                                        # the gate's own tests
```

The runner writes `build/formal.json`:
`{"pass", "checks": {name: "PASS|FAIL|UNREACHED|SKIPPED"}, "depth", "runtime_s", ...}`. It exits
0 only when every check is `PASS`. `UNREACHED` (a check whose trigger the solver cannot reach
within the depth) counts as a failure. By default the run stops at the first `FAIL` or
`UNREACHED` and marks the checks it did not finish `SKIPPED` (`first_failure` names the check);
`--keep-going` runs everything.

## How it works

1. **Staging.** `src/*.v` and `src/*.sv` are copied into `build/formal/work-<pid>/`. Stale work
   dirs of dead processes are removed.
2. **Pre-pass.** yosys elaborates the `RISCV_FORMAL` build, reading `src/` with that one define
   and no other (contract section 6). It fails on latches, on flip-flops that are not on the
   rising edge of `clk`, and on formal-only cells (`$anyconst`, `$initstate`, ...) in the core.
   Formal multiclock is off, so anything but posedge-`clk` flip-flops would be modelled wrongly.
   It removes the core's own assert/assume/cover statements and every initial value, so no check
   can rely on power-up values (contract section 1). The core is written to `core.il` and read
   separately from the checkers, so its macros cannot reach them.
3. **Pins equivalence** (`equiv`). The `RISCV_FORMAL` build with its `rvfi_*` ports removed must
   be equivalent to the plain build (yosys `equiv_make`, `equiv_simple -seq 5`, `equiv_induct`).
   The checks run on the RVFI build; this is what makes them speak for the design that ships. It
   overlaps with the equivalence gate (`make equiv`) on purpose, so the formal result stands on
   its own.
4. **Timing and depth.** See "Depth policy".
5. **Tasks.** Each check runs as a proof task and a reach task (see "Non-vacuity"). The 40
   instruction checks run in groups: one task checks several instructions through one muxed
   model (`bonetto_insn_group`, generated per task; the members' encodings are disjoint), with
   one trigger per instruction. A group shares one unrolling of the core, which cuts the cost per
   instruction (see "Engines tried"). A group counterexample is attributed to the instruction in the
   trace; the other members are reported `FAIL` as not proven.
6. **Scheduling.** At most 8 tasks in parallel on the Mac (`BONETTO_FORMAL_MAX_JOBS` raises the
   cap on a bigger host). See "Fail fast, scheduling, remote runs". SIGTERM, SIGINT and SIGHUP
   stop every SBY run.

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
| `bonetto_insn_check` | Checks a group of instructions (model `bonetto_insn_group`). Stock semantics plus: halt equals the spec's trap; a trapping retirement is compared only on trap, halt, `rd_addr = 0` and `wmask = 0` (contract section 6); register fields of other retirements exact (unused fields report 0); exact memory masks; no PMA or ALIGNED_MEM. |
| `bonetto_reg_check` | Stock (reads return the last write) plus: a register not yet written reads the same value every time. |
| `bonetto_pc_bwd_check` | Stock pc_bwd only fires when order N+1 retires before N, never true in order (unreachable). The fork checks the time-adjacent pair: order + 1 and pc chaining. |
| `bonetto_unique_check` | Stock unique fails on any core with a free trig. The fork counts retirements per order. |
| `bonetto_liveness_check` | Edge counter since edge 0 or the last retirement (contract section 4 count, see "Liveness"): a retirement by edge 1,024 unless halted. Stock fires check right after trig in unbounded mode. |
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

Every assertion, by check. Each check also has the triggers listed under "reach", which must be
reached (see "Non-vacuity").

| Check | Assertions | Contract clause |
|---|---|---|
| structure (pre-pass) | no latches; every flip-flop `$dff`-family on the rising edge of `clk`; no formal-only cells in the core; initial values removed | 1: single clock domain, no latches, no negedge flip-flops, no reliance on power-up values |
| equiv (pre-pass) | RVFI build with `rvfi_*` stripped is equivalent to the plain build | 6: RVFI logic drives no pin |
| `insn_<name>` | `insn_trap`, `insn_halt` (trap and halt equal the spec's trap) | 4: fatal traps (ECALL, EBREAK, misaligned jump/branch target), trap = halt; nothing else traps |
| | `insn_trap_rd_addr`, `insn_trap_wmask` (on a spec trap) | 4: no register write and no memory write; 6: only these are compared on a trapping retirement |
| | `insn_rs1_zero`, `insn_rs2_zero`, `insn_rs1_addr`, `insn_rs2_addr` (non-trapping) | 6: register fields report the index read, unused fields 0 with rdata 0; 4: x0 reads 0 |
| | `insn_rd_addr`, `insn_rd_wdata`, `insn_pc_wdata` (non-trapping) | 4: RV32E semantics (riscv-formal's rv32i model, E filter), FENCE a no-op; 6: a write to x0 reports rd 0, wdata 0 |
| | `insn_rmask`, `insn_wmask`, `insn_mem_addr`, store data per lane | 6: exact first-byte address, LSB-aligned masks, lane k = byte at mem_addr + k; 3/4: misaligned accesses never trap |
| | reach: `reach_insn_<name>`; also `reach_insn_<name>_trap` for JAL, JALR, branches, ECALL, EBREAK | non-vacuity |
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
| liveness (deep only) | `liveness` (first retirement by edge 1,024, then at most 1,024 edges apart, unless halted; see "Liveness") | 4: latency cap |
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

The runner computes every depth from the timing measured on the candidate; no agent sets one.
`build/formal.json` records them: `depth` (the deepest base depth used), `per_check_depth`, and
`depth_policy` (`d_min`, `d`, `d_short`, the timing and its source).

```
full depth   D    = max(depth_floor, reset_steps + max(first, max_cpi) + max_cpi + margin)
short depth  d(c) = reset_steps + first + max_cycles(c) + margin
liveness          = base depth + 1,024
FAIL if a base depth > depth_cap   (liveness's fixed +1,024 is not counted)
```

`first` is the first retirement's cycle and `max_cpi` the slowest instruction measured.
`max_cycles(c)` is the slowest measured instance of the classes under check. An instruction
group uses its members' classes (`INSN_CLASS`, `COSIM_CLASSES` in `run_formal.py`). Any other
check, or a class the timing does not measure, uses `max_cpi`. reset_steps = 1 and margin = 2 in
both configs.

The timing comes from the cosim (`run_cosim.py --mode rvfi --prog build/programs/selftest.hex
--src <dir> --timing-json <file>`, per class `{class: cycles}`) when it runs. Otherwise the runner
measures it itself: `timing_tb.v` runs the candidate in Icarus against a behavioural 23LC512 (noise
on MISO while SCK is low) on the selftest, or on `timing_prog.S` when the selftest isn't built
(per class `{class: {max_cycles, ...}}`). The cosim counts cycle c as the outputs right after
edge c. BMC step s holds the state after edge s - 1, with step 0 the reset cycle. So cosim
cycle c is BMC step c + 1, and margin covers it.

A short-depth trace therefore covers the first retirement, plus one more retirement of the
class under check after the fastest first retirement. The full depth D also covers a retirement
of the slowest class after the slowest first retirement (on the seed: a load after a load).
Slower instances than the timing program measures (none in the seed, whose shifter is a barrel
shifter) are left to cosim and the architecture tests. Each check's reach task still demands
that its triggers occur within the depth.

| | seed (`src/`) | bit-serial (hyp/001, 9a39f1d) |
|---|---|---|
| timing (`timing_tb.v`, `timing_prog.S`) | first 114, max 230 (loads and stores 230, all else 116) | first 319, max 736 (loads and stores 736, ALU 416, branches and JALR 320, LUI/AUIPC/JAL/FENCE 288, ECALL/EBREAK 256) |
| D (deep) | 463 | 1,475 |
| liveness (deep) | 1,487 | 2,499 |
| d_short, non-instruction checks (fast) | 347 | 1,058 |
| instruction groups (fast) | 233; loads, stores 347 | ALU 738; branches, U/J/system 642; loads, stores 1,058 |

(The cosim measures the seed's first retirement at cycle 115, with the same 230 maximum. With
the cosim the depths are one more.)

## Liveness

Contract section 4 counts edges. Edge 0 is the last rising edge at which `rst_n` is sampled low.
The first retirement must come at an edge <= 1,024, and each later one at most 1,024 edges after
the previous. A halted core is exempt. A retirement "at edge e" means `rvfi_valid` is high after
edge e, as the cosim harness counts it.

`bonetto_liveness_check` keeps `gap`. It is 0 in step 1 (right after edge 0) and counts up by one
per step without a retirement. After a retirement in step r it is 1 in step r + 1. So in step s,
gap = (s - 1) - (the edge of the last retirement, or 0). The assertion fires when gap reaches
1,024 without `rvfi_valid` and the core has not halted. Two boundaries follow:

- A first retirement at edge 1,024 (step 1,025) passes, and at edge 1,025 it fails.
- A retirement 1,024 edges after the previous one passes, and 1,025 edges after it fails.

The stall mutants pin both on the seed, one cycle apart:

- `stall_first_1024` and `stall_first_1025` delay the first fetch by 795 and 796 cycles. The
  latest first retirement (a 4-byte RAM access at edge 229) moves to edge 1,024 or 1,025.
- `stall_later_1024` and `stall_later_1025` stall the fetch after the first retirement by 794
  and 795 cycles. The longest gap (230 edges) becomes 1,024 or 1,025.

The `_1024` mutants must PASS and the `_1025` ones must FAIL (`test_mutant_fails`).

This is a safety property, checked by BMC: every window that closes within the depth (D +
1,024) is checked, and windows that close later are not. A core that stops retiring after the
first few instructions fails, because its window closes within the depth. An unbounded proof
(`abc pdr`) did not converge on the seed: frame 126 after about 16 minutes, stopped.

## Fast and deep

| | fast (`checks.cfg`, every candidate) | deep (`checks-deep.cfg`, champions) |
|---|---|---|
| checks | every check except liveness | every check, liveness included |
| depth | short depth, per class group | full depth D; liveness D + 1,024 |
| instruction tasks | 9 class groups (`CLASS_GROUPS`) | 8 round-robin groups |
| task timeout | 7,200 s | 21,600 s |
| depth cap | 2,048 | 4,096 |

Fast keeps every instruction check and every bus, SPI, GPIO and RVFI-consistency check. It does
not cover:

1. **Liveness** (the 1,024-edge retirement cap, invariant 8). The cosim watchdog covers it on
   every candidate. Formal checks it in deep.
2. **Retirements after step d_short.** That is the second retirement when the first one was
   slow (on the seed, a load or store first, then a load or store), and every third or later
   retirement. Deep adds the slow-then-slow pair. Neither config covers a third retirement of
   the slowest class.

Neither config is an unbounded proof (see "What is and is not proven").

### Measured runtimes

Every completed run below passed every check (56 in fast, 57 in deep: 40 instructions, 14 or
15 SBY checks, structure and equiv). Times are wall clock for the whole runner (timing given, so no
cosim), with `--keep-going` (a passing run is the same either way). Mac: Apple M5, 10 cores,
8 jobs, shared with other agents' jobs (load average 14.7 at the end of the fast bit-serial
run). hwe-bench: Intel Core Ultra 7 265 (8 P + 12 E cores, 62 GB). The fast runs used a trial config
with the same checks and depths as `checks.cfg` (only the cap and timeout differed).

| run | Mac, 8 jobs | hwe-bench, 18 jobs |
|---|---|---|
| seed, fast | 227 s | 190 s (191 s with 10 jobs, in the test suite) |
| seed, deep (D = 463, liveness 1,487) | 1,072 s | 941 s |
| bit-serial 9a39f1d, fast | 4,402 s (73 min) | 2,805 s (47 min) |
| bit-serial 9a39f1d, deep | not run (memory, see below) | stopped after 16 min (memory, see below) |

The slowest tasks (proof task seconds, Mac / hwe-bench):

- Seed, fast: reg 177 / 162, bus_dmem 158 / 161. Every other task is under 80 s on the Mac.
- Seed, deep: bus_dmem 981 / 829, liveness 859 / 658, reg 766 / 898.
- Bit-serial, fast: bus_dmem 3,580 / 2,257, reg 2,387 / 2,779, the two ALU groups with the
  shifts 2,630 and 2,492 / 1,590 and 1,378, reset 1,441 / 1,880, bus_imem 1,039 / 1,043.

On the Mac the bit-serial fast run is bound by total work: 28,303 task-seconds over 8 jobs is
3,538 s even with perfect packing. On hwe-bench it is bound by its longest task, reg at 2,779 s.
Deep on a bit-serial core takes hours, and memory bounds its parallelism. An earlier run of
every check at the full depth (1,475), 8 jobs on hwe-bench, still had every instruction group and
bus_dmem running after 60 minutes, so it was stopped. The committed deep config with 10 jobs on
hwe-bench was stopped after 16 minutes. Its 11 bitwuzla processes held 3.4 to 5.7 GB each and
were still growing, and the host was down to 4 GB available of 62. Run deep on such a core with
at most one job per 8 GB of RAM (`--jobs`). The Mac has 32 GB (memory per task was not measured
there).

Levers if a candidate is slower than 9a39f1d (none is in the gate today):

- More jobs on a bigger host (`BONETTO_FORMAL_REMOTE_JOBS`).
- Splitting `reg` and `bus_dmem` (the critical path on hwe-bench).
- `abc bmc3` for the instruction groups. On the seed's 8 groups at D = 463 it took 332 s against
  414 s for bitwuzla (Mac). The bit-serial core has constant x bits, which abc's AIGER flow
  rejects ("Design contains 'x' or 'z' bits"), so it would need `setundef` first.

### Engines tried

- **bitwuzla** (chosen) through smtbmc.
  - Proofs use `step=all`: one solver call checks every step up to the depth. Windows of 64
    steps for every proof took 520 s on the seed's fast config, against 302 s with one call
    (hwe-bench, 8 jobs each, the two runs side by side).
  - Liveness also uses one call. 64-step windows were tried at depth 1,487 (hwe-bench):
    - they found `stall_first_1025`'s counterexample (step 1,026) in 56 s and proved
      `stall_first_1024` in 323 s;
    - but the seed's own proof was still running after 33 minutes, at 16 GB, against 658 s with
      one call.

    A single call has to find a trace of the full depth even when the violation is at step
    1,026, so a liveness counterexample can take longer than a proof. At depth 1,487 (see
    "Mutants"), the counterexamples took 296 to 1,832 s and the proofs 87 to 309 s (658 s for
    the seed's own).
- **yices** stalls on the bus checks.
- **abc bmc3**: see above.
- **abc pdr**:
  - it did not converge on liveness;
  - with `--keep-going` SBY crashes converting its witness, so it is never used for a reach task.
- **Combined proof and reach**, one solver call: stalls (see "Non-vacuity").
- **Instruction groups**: one unrolling of the core for several instructions. On the seed at
  D = 463 (Mac), the 8 groups took 2,525 proof task-seconds in all (414 s wall clock). In an
  earlier run on a differently loaded Mac, single-instruction proofs at the same depth took 79 to
  222 s each (the first 16 to finish, mean 153 s): about 6,100 task-seconds for 40. Class groups
  at the short depth are the cheapest arrangement measured.

## Mutants

`mutants/make_mutants.py` generates them: each is the seed with one bug, plus `expect.json`.
`test_mutant_fails` runs each one on the seed's timing (cosim: first retirement at cycle 115,
so fast depths 234 and 348, deep D = 463), with `--only` its checks and `--deep` when a check is
deep-only. A FAIL must be a counterexample: a failed assertion (or the pins equivalence), never
a timeout or an error. The `_1024` stall mutants must PASS.

Measured at commit 738a135 on hwe-bench. "Proof" is the check's proof task, in seconds.

| mutant | bug | check | result: failed assertions | proof |
|---|---|---|---|---|
| cs_sck_high | CS_n rises with SCK still high | spi | FAIL: `spi_rule1_prev` | 9 s |
| gpio_any_byte | any IO store sets uo_out | gpio | FAIL: `gpio_uo_hold`, `gpio_uo_cause`, `gpio_uo_value` | 62 s |
| load_half_swap | LH/LHU bytes swapped | bus_dmem | FAIL: `bus_dmem_load_value` | 92 s |
| miso_low | samples MISO while SCK is low | bus_imem | FAIL: `bus_imem_value` | 10 s |
| mosi_rise | MOSI changes while SCK is high | spi | FAIL: `spi_rule2`, `spi_rule4_cmd` | 10 s |
| norvfi_diff | uo_out inverted in the plain build only | equiv | FAIL (pre-pass) | 5 s (whole run) |
| reg_wrong_index | even rd writes one register up | reg | FAIL: `reg_rs1` | 50 s |
| rvfi_rd | RVFI XOR result bit 0 flipped | insn_xor | FAIL: `insn_rd_wdata` | 15 s |
| sub_decode | SUB decoded as ADD | insn_sub | FAIL: `insn_rd_wdata` | 14 s |
| trap_continues | fetches again after a fatal trap | silent | FAIL: `silent_write`, `silent_retire` | 37 s |
| write_midbyte | store WRITE one bit short | bus_dmem, spi | FAIL: `bus_dmem_store_once`, `bus_dmem_store_value`; `spi_rule5_write` | 94 s, 42 s |
| hang_reserved | custom-0 opcode hangs | liveness (deep) | FAIL: `liveness` (retirements at steps 230 and 460, then none) | 1,832 s |
| stall_first_1024 | first retirement at edge 1,024 | liveness (deep) | PASS | 87 s |
| stall_first_1025 | first retirement at edge 1,025 | liveness (deep) | FAIL: `liveness` (first retirement at step 1,026) | 296 s |
| stall_later_1024 | a gap of 1,024 edges | liveness (deep) | PASS | 309 s |
| stall_later_1025 | a gap of 1,025 edges | liveness (deep) | FAIL: `liveness` (retirements at steps 230 and 1,255) | 1,348 s |

## Fail fast, scheduling, remote runs

By default the runner stops at the first proof that does not pass, or the first reach task that
finishes without every required trigger. It kills the running SBY tasks and marks the unfinished
checks `SKIPPED`. `--keep-going` runs everything. Tasks start in this order:

- the proofs longest first (by `TASK_COST`, measured: bus_dmem, reg, the instruction groups,
  reset, ...), which are also the likeliest to fail;
- interleaved with the reach tasks, which are cheap and catch a dead or stuck core early as
  `UNREACHED`.

A failing BMC proof stops at its counterexample, so a long proof that fails still fails early.

`--host <ssh host>`, or the environment variable `BONETTO_FORMAL_HOST` (unset by default), runs
the gate on another machine. The verdict is the same on any host:

1. The pre-pass and the timing run locally, where the cosim lives.
2. Exactly the inputs are copied (rsync) into a fresh `~/bonetto-formal/run-<host>-<pid>-<time>`:
   the candidate's `src`, `verif/formal` (without the mutants), riscv-formal's `rvfi_macros.vh`
   and instruction models, and the timing JSON.
3. The remote run holds the host's own lock (`flock ~/bonetto-formal/formal.lock`), not the Mac's
   heavy lock, with `BONETTO_FORMAL_REMOTE_JOBS` jobs (default 18). It uses the oss-cad-suite at
   `BONETTO_FORMAL_REMOTE_OSS` (default `/opt/hwe-toolchain/oss-cad-suite/bin`).
4. `formal.json`, the task logs and any counterexample traces come back to
   `build/formal/remote-<run id>/`.
5. The remote run dir is always deleted afterwards. During a run, each task's model files and
   each reach task's traces are deleted as soon as the task finishes.

SIGTERM, SIGINT or SIGHUP on the local runner hangs up the ssh session, which stops the remote SBY
runs, and then cleans up. `BONETTO_FORMAL_REMOTE_DIR` changes the remote base dir (and so the
lock). With the host set, `test_formal.py` sends every formal run there and skips the local heavy
lock.

## What is and is not proven

Proven, for the candidate as built with `-DRISCV_FORMAL`, under the environment above:

- **Structure and RVFI isolation, for all time.** No latches, posedge `clk` only, no formal-only
  cells. The pins equivalence (`equiv_simple` plus `equiv_induct`) is inductive.
- **Every assertion of the property table, on every trace from reset up to the check's depth.**
  That means:
  - every free MISO bit, `uio_in` value and `ui_in` constant;
  - free power-up values of every register, the register file included;
  - a free memory image, through the SPI monitor's byte shadow.
- **Non-vacuity of each check.** Its triggers occur within that same depth (the reach tasks).
- **Liveness, in deep only.** Every 1,024-edge window that closes within D + 1,024.

Not proven:

- Anything beyond the depth. There is no induction, no k-induction and no unbounded proof of the
  ISA, bus or liveness properties. A bug that needs three or more retirements of a slow class, or
  internal state built up over many instructions, is cosim's and the architecture tests' to
  find.
- Instruction instances slower than the timing program measured (for example, a
  shift-amount-dependent shifter shifting further than the program does).
- A reset after the start: `rst_n` goes low only at the beginning (the `reset` check varies its
  length from 1 to 8 edges).
- Encodings with register fields x16-x31 and other UNSPECIFIED behaviour: the E filter drops them
  from the instruction checks; the consistency checks still apply.
- What a trapping retirement claims on top of trap, halt, `rd_addr = 0` and `wmask = 0` (its
  `rmask` and read data are not checked).
- The plain build, other than through the pins equivalence. Gate-level behaviour, X propagation
  and timing belong to `make gl` and hardening.
