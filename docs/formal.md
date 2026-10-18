# Formal verification at the pins

`verif/formal` proves the contract (`docs/contract.md`, sections 2-4 and 6) on the real Tiny
Tapeout top module, `tt_um_fesens_r32v`, at its pins. It is a fixed gate: hypothesis agents never
edit it. It runs at the final sign-off and at every 10th merged improvement of the area loop
(the user's policy), not per candidate.

```
BONETTO_FORMAL_HOST=hwe-bench python3 verif/formal/run_formal.py [--deep]    # on hwe-bench (use this)
BONETTO_FORMAL_HOST=hwe-bench python3 verif/formal/run_formal.py --fetch-orphans   # after a crash
lockf -k /tmp/bonetto-heavy.lock python3 verif/formal/run_formal.py [--deep] # on the Mac (seed-sized only)
pytest verif/formal/test_formal.py -v                                        # the gate's own tests
```

The runner writes `build/formal.json`:
`{"pass", "checks": {name: "PASS|FAIL|UNREACHED|SKIPPED"}, "depth", "runtime_s", ...}`. It exits
0 only when every check is `PASS`. `UNREACHED` (a check whose trigger the solver cannot reach
within the depth) counts as a failure. By default the run stops at the first `FAIL` or
`UNREACHED` and marks the checks it did not finish `SKIPPED` (`first_failure` names the check);
`--keep-going` runs everything.

## How it works

1. **Staging.** Every top-level `src/*.v` and `src/*.sv`, `test_*.v` included (contract section
   1: every top-level file is design; subdirectories are not), is copied into
   `build/formal/work-<pid>/`. Stale work dirs of dead processes are removed.
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
- **The core region (0xFF80-0xFFFF) is a real 128-byte memory** in every task (`rmem` in
  `wrapper.sv`), with free initial contents (contract section 3: arbitrary at reset, like on-die
  registers). A READ data bit at a region byte is assumed equal to that byte's current bit, and a
  complete WRITE byte to a region byte updates it. So a core that keeps x1..x15, the pc or
  scratch in the region sees its own writes, and register consistency (`reg`, `causal`) covers
  the core plus that memory. An uninitialized memory in SBY starts free (checked on a small
  test design before relying on it). Program bytes (outside the region) are free noise, which
  the bus checks constrain one symbolic byte at a time.

### The SPI monitor

`bonetto_spi_monitor` (in `wrapper.sv`) decodes the pins into transactions: the 8-bit command,
the 16-bit address, and data bytes at `(A + k) mod 2^16`. It emits an event for every READ data
bit (byte address, bit index, MISO value) and every complete data byte (READ or WRITE, address,
value), plus command and end-of-transaction events.

When CS_n rises it classifies the transaction (contract section 6), from the address `A`, the
number of data bits clocked (`ev_end_nbits`, saturating at 1,023) and which bytes got at least
one data bit. A partly clocked last byte counts as addressed.

- **region**: every addressed byte is in 0xFF80-0xFFFF. A READ with no data bit (a null READ)
  is a region READ if `A` is in the region. Region transactions are exempt from the bus checks.
- **mixed**: it addresses both region and program bytes (a region READ running past 0xFFFF into
  0x0000, a program READ running into 0xFF80, or one bit past the region). Always a failure
  (`bus_region_mixed`).
- **program**: everything else, null READs at a program address included. A null READ addresses
  no byte, so the bus checks never count it against an instruction: it is legal anywhere. A
  header shorter than 24 bits still fails `spi_rule4_header`.

`test_formal.py::test_monitor_decodes_stub` checks the decoding and the classes against a
hand-written waveform: stretched phases, a READ wrapping at 0xFFFF, a READ and a WRITE ending
mid-byte, an incomplete header, MISO noise while SCK is low, a region READ, null READs in the
region and in program memory, a READ clocking one bit past 0xFFFF (mixed), and a region WRITE.

### The region stop

Contract section 3: a fetch or data access that touches a region byte is UNSPECIFIED, and the
checkers stop comparing at the first retirement that makes one. Only liveness and the SPI
waveform rules apply after it. `wrapper.sv` computes, at each retirement:

- `bon_fetch_touch`: the fetch touches the region, `pc_rdata[15:0] >= 0xFF7D` (one of
  `pc[15:0] + k`, k < 4, is a region byte);
- the data access touches the region:
  - for an RV32E instruction (`spec_insn`: one of the 40 instruction models' `spec_valid`, E
    filter included, decoded by `rvfi_isa_rv32e` in the wrapper, or a fatal-trap word), from
    the specification's address only, never from what the core claims (`rvfi_mem_*`), so a
    core can neither dodge the stop nor trigger it by misreporting. For a load or store,
    `addr = rs1_rdata + imm` (`rs1 = x0` reads 0), with `n` bytes; it touches the region iff
    `addr[31] = 0` and `addr[15:0] >= 0xFF80 - (n - 1)`. That covers an access inside the
    region, one that straddles 0xFF7F/0xFF80, one that wraps at 64 KB (touching
    0xFFFD-0xFFFF), and one that crosses 0x7FFFFFFF into `addr[31] = 1` (its first bytes are
    0xFFFD-0xFFFF of the RAM);
  - for a reserved encoding (a used register field x16-x31, an undefined funct3, ...), which
    has no spec access, from the access it claims (non-trapping `rvfi_mem_*`, same formula).
    The hyp/002 cores execute such encodings as loads and stores and can touch the region
    with them: at 5d23efc, without this rule, a reserved SH (rs1 = x17) on fc07cc3 wrote
    0xFFC4, its x1 slot, and a later read of x1 failed `reg`; similar traces failed bus_dmem
    and insn_group5 on fc07cc3 and reg and bus_dmem on b9a5211, all on legal cores. A core
    cannot use it to end the checks, because formal, not the core, picks the instruction
    stream;
- `bon_region_ret`: either, at this retirement; `bon_stopped`: a region retirement has happened
  before this step. `BONETTO_FULL` = neither.

Everything that decides the stop is itself checked at the region retirement (unless the fetch
touched the region): the instruction word (`bus_imem_pc`, `bus_imem_fetch`, `bus_imem_value`:
the fetched bytes), `rs1_addr` (`insn_region_rs1_addr`), and the `rs1` value (`reg`). Also
compared there: `insn_region_mem_addr`, and `insn_region_rmask` / `insn_region_wmask` unless the
core trapped (contract section 3 lets a region-touching access trap and halt, and a trapping
retirement reports `wmask = 0`), plus rvfi's first-pc and order rules, pc_fwd, pc_bwd and
unique. Every other instruction, bus, gpio, causal, rvfi, ill and silent assertion is gated by
`BONETTO_FULL`, and reg, pc_fwd, pc_bwd and unique by `!bon_stopped`. `spi` (minus
`spi_cs_at_valid`, which is a retirement rule) and liveness never stop. The `insn_region_*`
assertions apply to the group's own instructions (`spec_valid`), like the full checks.

The stop only removes traces from the comparison after a region-touching instruction. Formal
picks the instruction stream, so every program that never touches the region (as every test
program, contract section 3) is still checked in full: the touch is the trace's choice, and the
core cannot fake one. (282f6d8 stopped on any claimed region access; the mutant
`claim_region_stop`, an ADD with a wrong result that claims a read at 0xFF80, passed it.)

### The bus checks (transaction identity)

`bus_imem` and `bus_dmem` follow one symbolic program byte `X` (a free constant outside the
region, with a free initial value, updated by WRITE bytes at `X`; every READ data bit at `X` is
assumed equal to its current bit). The window of an instruction is the SPI traffic between the
previous retirement and its own. Transaction ends are classified by the monitor (above); region
transactions are exempt. With `pc16` the expected fetch address (0 after reset, then the
previous retirement's `pc_wdata[15:0]`) and the claimed bytes of the retiring instruction:

- An **F READ** is a program READ with a full header, `A = pc16` and exactly 32 data bits. There
  must be at least one (`bus_imem_fetch`), and `pc16 = pc_rdata[15:0]` (`bus_imem_pc`). A second
  F READ is accepted only as a load READ of the same bytes (`bus_imem_refetch`: if there are two,
  each fetch byte must be a load byte). So a re-fetch fails, and so does a fetch with one extra
  bit or byte (it is not an F READ, and its bytes are not load bytes).
- Every **other program READ** (a fetch split in two, a fetch running on, a load READ) may read
  only the load's bytes: `ma + k` with `rmask[k]`, RAM, non-trapping (`bus_dmem_read_claimed`).
  Each load byte must be read in full by a READ that is not the fetch, or by a second F READ
  (`bus_dmem_load_read`). The fetch never counts toward a load, so a load of its own
  instruction bytes needs its own READ (mutant `load_own_fetch`).
- Every **program WRITE byte** must be a store byte (`bus_dmem_write_claimed`), each store byte
  is written exactly once (`bus_dmem_store_once`; a store may be split over WRITEs), and with the
  claimed value (`bus_dmem_store_value`).
- Fetch and load bytes equal `X` at the start of the window (`bus_imem_value`,
  `bus_dmem_load_value`).
- No mixed transaction (`bus_region_mixed`).

A trapping instruction claims no load and no store, whatever its fields say, so any program READ
beyond its fetch and any program WRITE fail; its region transactions are allowed (contract
section 4). Every assertion is checked at each `BONETTO_FULL` retirement for the window that
retirement closes. Null READs address no byte, so they never trip a claim.

### Forks of riscv-formal (in `verif/formal/checks/`, the submodule is untouched)

| Fork | Why |
|---|---|
| `rvfi_testbench.sv` | 16-bit cycle counter (stock: 8 bits, saturating at 255); free `check` (unbounded style); pins and monitor events for the checkers; free reset length for the `reset` check. |
| `bonetto_insn_check` | Checks a group of instructions (model `bonetto_insn_group`). Stock semantics plus: halt equals the spec's trap; a trapping retirement is compared only on trap, halt, `rd_addr = 0` and `wmask = 0`, plus the rs1/rs2 fields on a trapping branch or JALR (contract section 6); register fields of other retirements exact (unused fields report 0); exact memory masks; no PMA or ALIGNED_MEM; the region stop. |
| `bonetto_reg_check` | Stock (reads return the last write) plus: a register not yet written reads the same value every time; the reads of a trapping branch or JALR are checked too; the region stop. |
| `bonetto_pc_bwd_check` | Stock pc_bwd only fires when order N+1 retires before N, never true in order (unreachable). The fork checks the time-adjacent pair: order + 1 and pc chaining. |
| `bonetto_unique_check` | Stock unique fails on any core with a free trig. The fork counts retirements per order. |
| `bonetto_liveness_check` | Edge counter since edge 0 or the last retirement (contract section 4 count, see "Liveness"): a retirement by edge 1,024 unless halted. Stock fires check right after trig in unbounded mode. |
| `bonetto_ill_check` | 0x00000000 and 0xFFFFFFFF. Stock only covers 0x00000000. |
| `bonetto_bus_checks.sv` | Byte shadow at a 16-bit pin address outside the region, misaligned and wrap aware; GPIO is not RAM; transaction identity per instruction (one fetch READ, load READs, store WRITEs, see "The bus checks"). Stock shadows aligned words at 32-bit addresses and only on `mem_addr == addr`. |
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
(a counterexample, or a task that timed out or errored). `UNREACHED` fails the gate (and stops
a fail-fast run).

Because the triggers are assertions, SBY reports a reach task as FAIL exactly when it reached
them. The runner's progress line translates that: `insn_group2_reach REACHED 5/5 triggers`, or
`UNREACHED 3/5 ... missing: <labels>`. (Before this line existed it printed the raw
`insn_group2_reach FAIL ... failed: reach_insn_addi, ...`, which means the same as REACHED 5/5
and was misread as a failure.) `formal.json` lists the reached triggers per check
(`reached`).

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
| | `insn_trap_rs1_zero`, `insn_trap_rs2_zero`, `insn_trap_rs1_addr`, `insn_trap_rs2_addr` (trapping branch or JALR) | 6: there the trap depends on the operands, so the rs fields are truthful |
| | `insn_region_rs1_addr`, `insn_region_mem_addr`, `insn_region_rmask`, `insn_region_wmask` (at the region retirement, unless the fetch touched the region; the masks only if it did not trap) | 3: the stop uses the spec's address, so what decides it and what reports the access must be right |
| | `insn_rs1_zero`, `insn_rs2_zero`, `insn_rs1_addr`, `insn_rs2_addr` (non-trapping) | 6: register fields report the index read, unused fields 0 with rdata 0; 4: x0 reads 0 |
| | `insn_rd_addr`, `insn_rd_wdata`, `insn_pc_wdata` (non-trapping) | 4: RV32E semantics (riscv-formal's rv32i model, E filter), FENCE a no-op; 6: a write to x0 reports rd 0, wdata 0 |
| | `insn_rmask`, `insn_wmask`, `insn_mem_addr`, store data per lane | 6: exact first-byte address, LSB-aligned masks, lane k = byte at mem_addr + k; 3/4: misaligned accesses never trap |
| | reach: `reach_insn_<name>`; also `reach_insn_<name>_trap` for JAL, JALR, branches, ECALL, EBREAK | non-vacuity |
| reg | `reg_rs1`, `reg_rs2`, `reg_rs1_rs2` | 4: reads return the last value written (every encoding, trapping branches and JALR included); 1: registers start unknown but do not change by themselves; 3/6: on the die or in the region, one real memory |
| pc_fwd | `pc_fwd` | 4: PCs chain (order N's pc_rdata = order N-1's pc_wdata) |
| pc_bwd | `pc_bwd_order`, `pc_bwd_pc` | 6: one instruction per pulse, order counts retirements; 4: PCs chain |
| causal | `causal` | 6: in-order retirement; 4: register reads return the last write |
| unique | `unique_order` | 6: `rvfi_order` counts retirements (no repeats) |
| rvfi | `conv_intr`, `conv_mode`, `conv_ixl` | 6: intr 0, mode 3, ixl 1 |
| | `conv_rs1_x0`, `conv_rs2_x0`, `conv_rd_x0`, `conv_rmask` (non-trapping); `conv_wmask` | 6: register and memory field conventions |
| | `conv_trap_halt`, `conv_trap_rd_addr`, `conv_trap_wmask` | 4: a trap halts, no register or memory write; stricter reading: nothing halts without trapping |
| | `conv_first_order`, `conv_first_pc`, `conv_order_next` | 6: order from 0; 1: execution starts at pc = 0 |
| ill | `ill_trap`, `ill_halt`, `ill_rd_addr`, `ill_wmask` for `0x00000000` and `0xFFFFFFFF` | 4: fatal traps |
| silent | `silent_retire`, `silent_write`, `silent_uo` (after a halting retirement) | 4: never retires again, never starts another SPI WRITE (a region WRITE included); 3: uo_out changes only on an IO store |
| liveness (deep only) | `liveness` (first retirement by edge 1,024, then at most 1,024 edges apart, unless halted; see "Liveness") | 4: latency cap |
| bus_imem | `bus_imem_pc`, `bus_imem_fetch`, `bus_imem_value` (also at the region retirement unless its fetch touched the region), `bus_imem_refetch` | 6: exactly one fetch, a READ of exactly the 4 instruction bytes, before retirement, no caching; 3: fetch reads RAM at pc[15:0] (64 KB alias); 2: memory semantics |
| bus_dmem | `bus_dmem_load_read`, `bus_dmem_load_value` (non-trapping) | 6: load completeness, the fetch never counts; 3: RAM bytes (addr[15:0] + k) mod 2^16, misaligned and wrapping |
| | `bus_dmem_store_once`, `bus_dmem_store_value` | 3: store bytes; 2 rule 5: every byte sent is written; 6: each store byte exactly once, traffic finishes before retirement |
| | `bus_dmem_write_claimed`, `bus_dmem_read_claimed` | 6: program transactions are the fetch, the load's READs and the store's WRITEs, nothing else (no program write on a trap); 3: an IO access never appears on the pins |
| | `bus_region_mixed` | 6: a transaction is region, program or a null READ; a partial last byte counts as addressed |
| gpio | IO load lanes (unnamed, one per byte lane) | 3: byte X with X[1:0] = 0 reads `ui_in`, other IO bytes read 0 (misaligned multi-byte IO unspecified) |
| | `gpio_uo_hold`, `gpio_uo_once`, `gpio_uo_cause`, `gpio_uo_value` | 3: uo_out changes only from an IO store writing byte 0, at most once per instruction (it may move early, within the store's own window), holds the new value no later than one cycle after it retires; 1: 0 after reset. Not compared from a misaligned IO load or store on (unspecified, its own retirement included) |
| spi | `spi_uio_oe`, `spi_uio_out_hi`, `spi_uio_out_2` | 1: uio_oe constant, unused outputs 0 |
| | `spi_reset_cs`, `spi_reset_sck`, `spi_reset_uo`, `spi_reset_valid` | 1: reset state of the pins; nothing retires in reset |
| | `spi_rule1_prev`, `spi_rule1_now`, `spi_rule2` | 2: rules 1 and 2 |
| | `spi_rule4_cmd`, `spi_rule4_header`, `spi_rule5_write` | 2: rules 4 and 5 |
| | `spi_cs_at_valid` (compared retirements) | 6: CS_n high at the retirement cycle |
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

The timing comes from the cosim when it runs: `run_cosim.py --mode rvfi --prog <p>.hex --src
<dir> --timing-json <file>` for every built program in `build/programs/` except CoreMark (too
long for a timing run), the selftest first, then the rest four at a time. The timing is the
maximum over the programs, per class too (`merge_timings`); `depth_policy` lists the programs.
On the v2 cores this matters: the slowest class differs between programs. Otherwise the runner
measures it itself: `timing_tb.v` runs the candidate in Icarus against a behavioural 23LC512
(noise on MISO while SCK is low) on the selftest, or on `timing_prog.S` when the selftest isn't
built (per class `{class: {max_cycles, ...}}`). The cosim counts cycle c as the outputs right
after edge c. BMC step s holds the state after edge s - 1, with step 0 the reset cycle. So cosim
cycle c is BMC step c + 1, and margin covers it.

A short-depth trace therefore covers the first retirement, plus one more retirement of the
class under check after the fastest first retirement. The full depth D also covers a retirement
of the slowest class after the slowest first retirement (on the seed: a load after a load).
Slower instances than the timing programs measure are left to cosim and the architecture tests.
The seed has none (its shifter is a barrel shifter). hyp/002-rf-in-ram-pcram (b9a5211) has one:
its commit message measures 993 cycles for a SW or JALR as the very first instruction after
reset (its own directed tests), and no cosim program starts with one, so the measured first
retirement is 729. A trace still reaches such a first retirement within the depth (994 < 1,623),
but not a slow second retirement after it. Each check's reach task still demands that its
triggers occur within the depth.

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

| | fast (`checks.cfg`) | deep (`checks-deep.cfg`) |
|---|---|---|
| checks | every check except liveness | every check, liveness included |
| depth | short depth, per class group | full depth D; liveness D + 1,024 |
| instruction tasks | 9 class groups (`CLASS_GROUPS`) | 8 round-robin groups |
| task timeout | 40,000 s: 2 x the longest V2 task, reg at 19,669 s (see "Measured runtimes") | 40,000 s, never below fast (V2 at its full depth, 1,785, not measured) |
| depth cap | 2,048 | 4,096 |
| jobs | at most 8 on the Mac, `BONETTO_FORMAL_REMOTE_JOBS` (18) on a remote host, and never more than memory holds (below) | same |

Project policy (the user's): formal runs at the final sign-off and at every 10th merged
improvement, not per candidate.

**Memory and jobs.** Each SBY task (bitwuzla plus yosys-smtbmc) needs about
0.4 GB + 3.7 MB per step of its depth: measured about 1.4 GB of bitwuzla at depth 348 (the
seed) and 6.1 GB at depth 1,623 (V2). The runner caps the job count at the memory budget
divided by that need at the deepest task's depth (`mem_jobs`; `formal.json` records `jobs`).
The budget is `$BONETTO_FORMAL_MEM_GB` if set (forwarded to a remote run; set it to split a
host between concurrent runs), else 85 % of `MemAvailable` on Linux (so other users' jobs on a
shared host count), else half the RAM on macOS (the Mac is shared with other agents). On hwe-bench with about 55 GB available that is 18 jobs for the seed but 7 for V2; on
the Mac 8 for the seed and 2 for V2. Before this cap, 10 V2 jobs pushed hwe-bench 11 GB into
swap. Some tasks need several times the estimate: `reset` (free reset length) reached 22 GB at
depth 1,222 on fc07cc3, against 4.9 GB estimated, so it counts five times (`TASK_MEM_WEIGHT`).
On top of the job cap, each task waits until its estimate fits in the budget next to the
running ones (`MemGate`; a task alone always runs).

Stopping a run (fail-fast, a signal, a timeout, an exception) signals every process below each
SBY run it started (`kill_tree`, by pid from the process tree, never by name). SBY puts each
engine in its own process group and forwards SIGTERM to them; `kill_tree` also reaches them if
SBY itself cannot. (The bitwuzla processes first blamed on a process-group kill on hwe-bench
came from the launch bug under "Fail fast, scheduling, remote runs": no signal had been sent.)

Fast keeps every instruction check and every bus, SPI, GPIO and RVFI-consistency check. It does
not cover:

1. **Liveness** (the 1,024-edge retirement cap, invariant 8). The lead ruled that fast needs no
   liveness; deep checks it. Outside formal, cosim covers it two ways: its watchdog on the test
   programs (which contain no reserved encodings and no region accesses), and its random-image
   liveness fuzz (`run_cosim.py --fuzz-liveness`, in the eval cosim gate), which is what catches
   a hang on a reserved encoding or an unspecified access per candidate. The fuzz is
   probabilistic (random images), not a proof.
2. **Retirements after step d_short.** That is the second retirement when the first one was
   slow (on the seed, a load or store first, then a load or store), and every third or later
   retirement. Deep adds the slow-then-slow pair. Neither config covers a third retirement of
   the slowest class.

**Ruling R9 (the lead's).** The fast depth policy is accepted as it stands. It is below the
original brief's floor of 2 x max_cpi: an instruction group runs at reset + first + the slowest
member class + margin, which on the seed is 234 for the ALU groups against 2 x 230 = 460, and
every other check at reset + first + max_cpi + margin (348 on the seed). Deep keeps the full
depth D, which is at least 2 x max_cpi + 3.

Neither config is an unbounded proof (see "What is and is not proven").

### Measured runtimes

Mac: Apple M5, 10 cores, 32 GB, shared with other agents' jobs. hwe-bench: Intel Core Ultra 7
265 (8 P + 12 E cores, 62 GB), shared with another project's jobs at times. Times are wall
clock for the whole runner, with `--keep-going` (a passing run is the same either way).

**Contract v2 checks (this version).** Every run below passed every check it ran (56 in fast),
except fc07cc3's `reg`, which ran into the task timeout of the time (21,600 s).

| run | Mac, 8 jobs | hwe-bench |
|---|---|---|
| seed, fast | 486.5 s at 4c03d59, cosim timing included (reg 419 s, bus_dmem 358 s; load ~11 from other agents) | 208 s at 38551e4 with 18 jobs (the memory cap allowed 18: 1.7 GB per task, 53.7 GB budget), 251 s with staging and transfer; reg 206 s, bus_dmem 201 s |
| V2 = hyp/002-rf-in-ram-pcram b9a5211, fast (depths 1,461 to 1,623) | not run (memory) | 32,538 s (9 h 2 min) at 7050096 with 4 jobs (the memory cap: 6.4 GB per task, 30 GB budget, the host shared), 56/56 PASS and every reach task REACHED; reg 19,669 s, reset 9,076 s, bus_dmem 7,796 s, bus_imem 6,773 s, the instruction groups 943 to 3,581 s, every other check 184 to 3,118 s. A first 7-job attempt was hung up after 2 h 20 min when the Mac crashed (see "Orphans") |
| fc07cc3 (hyp/002-rf-in-ram), fast (depths up to 1,222) | not run (memory) | 36,823 s (10 h 14 min) at 7050096 with 3 jobs (4.9 GB per task, 15 GB budget, the host shared): 55/56 PASS, every reach task REACHED, and `reg` TIMEOUT at 21,602 s (a timeout is a FAIL); reset 11,487 s, bus_imem 7,636 s, bus_dmem 6,882 s, the instruction groups 378 to 1,517 s, every other check 240 to 1,731 s |

The first V2 attempt (7 jobs, the host partly shared) finished these proofs before it was hung
up, every one PASS: bus_dmem 6,495 s, the instruction groups 919 to 3,196 s, pc_bwd 2,859 s,
rvfi 1,860 s, gpio 1,668 s, causal 713 s, ill 374 s; every instruction group's reach task
reached all its triggers (267 to 1,407 s). reg was still running at about 8,260 s. Memory: at
depth 1,623 each bitwuzla holds about 6 GB, so 10 jobs pushed the host into swap (11 GB after
38 minutes, that run was stopped); use at most about one job per 9 GB on V2-class cores.
The task timeout (fast and deep) is 40,000 s: 2 x the longest V2 task, reg, 19,669 s in the
7050096 run below (39,338 s, rounded up).

**Contract v1 checks (commit 738a135, before the v2 port).** Every completed run below passed
every check (56 in fast, 57 in deep: 40 instructions, 14 or 15 SBY checks, structure and
equiv). Timing given, so no cosim. The fast runs used a trial config with the same checks and
depths as `checks.cfg` (only the cap and timeout differed).

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
    1,026, so a liveness counterexample can take longer than a proof. At depth 1,487 on
    hwe-bench, the counterexamples took 296 to 1,832 s and the proofs 87 to 309 s (658 s for
    the seed's own) with the v1 checks, and 221 to 2,530 s against 113 and 353 s at 5d23efc
    (see "Mutants").
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

Each mutant is a design with one bug, plus `expect.json`. `mutants/make_mutants.py` generates
the seed-based ones (the seed with one change). The ones marked "cosim" in the table are shared
with the cosim gate: their `expect.json` points at `verif/cosim/mutants/<name>` (`"rtl"`), so both
gates test the same designs. That includes main's frozen copies of the two hyp/002 cores
(`legal_h002_rf` = fc07cc3, `legal_h002_pcram` = b9a5211, each unchanged below a 3-line header)
and three mutants of them.

`test_mutant_fails` runs each one with `--only` its checks (`--deep` when a check is deep-only),
on the seed's timing (cosim, max over 14 programs: first retirement at cycle 115, so fast depths
234 and 348, deep D = 463), or on the timing of the design its `"timing"` names. A FAIL must be a
counterexample: a failed assertion (or the pins equivalence), never a timeout or an error. The
legal variants (`"expect": "pass"`, `"checks": "all"`) must pass the whole fast suite, and the
`_1024` stall mutants must PASS their check. Mutants marked `"slow"` (the hyp/002 cores, hours
each) run only with `BONETTO_FORMAL_SLOW=1`.

Results, all on hwe-bench (shared with other runs at the time). Every FAIL mutant on the seed's
timing was re-run at 7b2aba3, the final checks (3 jobs), and so were the three hyp/002 region
mutants (2 jobs, times are proof seconds); the six seed-based legal variants passed at 7b2aba3
too (3 jobs); the liveness mutants ran at 5d23efc (2 jobs); the hyp/002 legal rows say where
they ran. Which assertions fail first can differ between runs of the same mutant; the table gives
the 7b2aba3 run and notes an earlier run's extra ones.

| mutant | RTL | bug | checks | result: failed assertions |
|---|---|---|---|---|
| cs_sck_high | seed | CS_n rises with SCK still high | spi | FAIL: `spi_rule1_prev` |
| mosi_rise | seed | MOSI changes while SCK is high | spi | FAIL: `spi_rule2`, `spi_rule4_cmd` |
| cs_open_at_retire | cosim | CS_n still low in the retirement cycle | spi | FAIL: `spi_cs_at_valid` |
| write_midbyte | seed | store WRITE one bit short | spi, bus_dmem | FAIL: `spi_rule5_write`; `bus_dmem_store_once`, `bus_dmem_store_value` |
| miso_low | seed | samples MISO while SCK is low | bus_imem | FAIL: `bus_imem_value` |
| refetch | seed | every instruction fetched twice | bus_imem | FAIL: `bus_imem_refetch` |
| fetch_extra_byte | cosim | the fetch READ clocks a fifth byte | bus_imem, bus_dmem | FAIL: `bus_imem_fetch`; `bus_dmem_read_claimed` |
| zero_init | cosim | pc not reset (relies on power-up 0) | rvfi, bus_imem | FAIL: `conv_first_pc`; `bus_imem_fetch`, `bus_imem_pc` |
| load_half_swap | seed | LH/LHU bytes swapped | bus_dmem | FAIL: `bus_dmem_load_value` |
| load_own_fetch | cosim | an LW at its own pc reuses the fetch, no READ | bus_dmem | FAIL: `bus_dmem_load_read` |
| load_skip_byte | cosim | LW reads 3 bytes, takes the 4th from the shift register | bus_dmem | FAIL: `bus_dmem_load_read`, `bus_dmem_load_value` |
| store_twice | cosim | every store WRITE sent twice | bus_dmem | FAIL: `bus_dmem_store_once` |
| store_extra_byte | cosim | SB writes a second byte | bus_dmem | FAIL: `bus_dmem_write_claimed` |
| trap_program_write | seed | a program WRITE (0x0100) before a trap retires | bus_dmem | FAIL: `bus_dmem_write_claimed` |
| trap_region_wrap | seed | a READ at 0xFFFE before a trap: 0xFFFE-0x0001 | bus_dmem | FAIL: `bus_region_mixed` |
| trap_region_straddle | seed | a READ at 0xFF7E before a trap: 0xFF7E-0xFF81 | bus_dmem | FAIL: `bus_region_mixed` |
| region_wrap | cosim, hyp/002-rf | x15's slot at 0xFFFE: its transactions wrap to 0x0001 | bus_dmem | FAIL: `bus_region_mixed`, 598 s |
| region_straddle | cosim, hyp/002-rf | the scratch slot at 0xFF7E straddles 0xFF7F/0xFF80 | bus_dmem | FAIL: `bus_region_mixed`, 943 s |
| region_zero_init | cosim, hyp/002-pcram | no boot write: the pc comes from the region's reset contents | rvfi, bus_imem | FAIL: `conv_first_pc`, 462 s; `bus_imem_fetch`, `bus_imem_pc`, 866 s |
| reg_wrong_index | seed | even rd writes one register up | reg | FAIL: `reg_rs1`, `reg_rs2` |
| rvfi_rd | seed | RVFI XOR result bit 0 flipped | insn_xor | FAIL: `insn_rd_wdata` |
| sub_decode | seed | SUB decoded as ADD (a two-member group) | insn_sub, insn_add | FAIL: `insn_rd_wdata` (insn_sub; insn_add not proven) |
| trap_fields_brj | seed | junk rs fields on every trap | insn_jalr | FAIL: `insn_trap_rs1_addr`, `insn_trap_rs2_addr` |
| claim_region_stop | seed | an OP with rs1 = rs2 flips result bit 0 and claims a 1-byte read at 0xFF80 | insn_add | FAIL: `insn_rmask`, 46 s (it PASSed the 282f6d8 checks, which also stopped on a claimed region access) |
| trap_writes_rd | cosim | a trapping JAL writes rd | insn_jal, rvfi | FAIL: `insn_trap_rd_addr`; `conv_trap_rd_addr` |
| trap_continues | seed | fetches again after a misaligned-target trap | silent | FAIL: `silent_retire` (and `silent_write` in another run) |
| no_halt | cosim | EBREAK traps but the core keeps running | silent | FAIL: `silent_retire` |
| region_write_after_halt | seed | a region WRITE (0xFF80) after the halt | silent | FAIL: `silent_write` |
| gpio_any_byte | seed | any IO store sets uo_out | gpio | FAIL: `gpio_uo_hold` (`gpio_uo_cause`, `gpio_uo_value` too at 5d23efc) |
| uo_glitch | cosim | uo_out shows the value, restores the old one, then sets it again | gpio | FAIL: `gpio_uo_once` |
| uo_late | cosim | uo_out set two cycles after the store retires | gpio | FAIL: `gpio_uo_hold` |
| norvfi_diff | seed | uo_out inverted in the plain build only | equiv | FAIL (pre-pass) |
| hang_reserved | seed | custom-0 opcode hangs | liveness (deep) | FAIL: `liveness`, 221 s (the first instruction hangs, no retirement) |
| stall_first_1024 | seed | first retirement at edge 1,024 | liveness (deep) | PASS, 113 s |
| stall_first_1025 | seed | first retirement at edge 1,025 | liveness (deep) | FAIL: `liveness`, 519 s |
| stall_later_1024 | seed | a gap of 1,024 edges | liveness (deep) | PASS, 353 s |
| stall_later_1025 | seed | a gap of 1,025 edges | liveness (deep) | FAIL: `liveness`, 2,530 s |
| legal_mosi_echo | cosim | MOSI carries noise after the last bit (CS_n still low) | all (fast) | PASS, 56 checks |
| legal_split_store | cosim | SW as two 2-byte WRITEs | all (fast) | PASS, 56 checks |
| legal_trap_fields_nb | seed | junk rs fields, rd_wdata and pc_wdata on non-branch traps | all (fast) | PASS, 56 checks |
| legal_uo_early | seed | uo_out set on the edge into the retirement cycle | all (fast) | PASS, 56 checks |
| legal_trap_region | seed | a region READ (0xFF80) before a trap | all (fast) | PASS, 56 checks |
| legal_null_read | seed | a null READ (0x1234, header only) before a trap | all (fast) | PASS, 56 checks |
| legal_h002_rf | cosim, fc07cc3 | registers in the region, pc on the die | all (fast) | running at the time of writing (detached, hwe-bench) |
| legal_h002_pcram | cosim, b9a5211 | registers and pc in the region | all (fast) | running at the time of writing (detached, hwe-bench); the first attempt PASSed every proof that finished (see "Measured runtimes") |

On the seed's timing, every fast-config FAIL above came within 227 s of proof time at 7b2aba3
(the slowest: `load_half_swap`'s `bus_dmem`, 226.5 s, with the V2 and fc07cc3 runs sharing the
host). The liveness counterexamples (deep, depth 1,487) are slower: 221 to 2,530 s on hwe-bench
at 5d23efc, against 208 s for `stall_first_1025` on the Mac in an earlier run (see "Engines
tried" on why a single-call liveness counterexample can take longer than a proof). A legal
variant's whole fast run took 1,329 to 1,529 s at 7b2aba3 with 3 jobs (the V2 and fc07cc3 runs
sharing the host; longest task 221 to 390 s), and 215 to 253 s with 10 jobs in an earlier run.
The hyp/002 region mutants FAILed within 943 s of proof time at 7b2aba3 (2 jobs, host shared),
against 317 s on the Mac (2 jobs) in an earlier run.

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
2. Exactly the inputs are copied (rsync) into a fresh
   `~/bonetto-formal/run-<machine>-<checkout tag>-<pid>-<time>` (the tag is a hash of the
   checkout's path: worktrees on one machine share its name and pid space):
   the candidate's `src`, `verif/formal` (without the mutants), riscv-formal's `rvfi_macros.vh`
   and instruction models, and the timing JSON.
3. The remote run holds the host's own lock (`flock ~/bonetto-formal/formal.lock`), not the Mac's
   heavy lock, with `BONETTO_FORMAL_REMOTE_JOBS` jobs (default 18). It uses the oss-cad-suite at
   `BONETTO_FORMAL_REMOTE_OSS` (default `/opt/hwe-toolchain/oss-cad-suite/bin`).
4. **The remote run is detached** from the ssh session (`setsid nohup`, its session leader's pid
   in `remote.pid`, its exit code in `done`, its log in `run.log`). The local runner follows the
   log over ssh; a dropped connection only makes it follow again 30 s later. The launch
   (`remote_launch_cmd`) backgrounds only the simple command (`cd <dir> || exit 1; setsid ... &`).
   Up to 7b2aba3 it was `cd <dir> && setsid ... &`, which backgrounds a subshell that keeps the
   ssh channel open and waits for the run: the launch ssh returned only when the run ended. The
   run itself was detached all the same (it survived the Mac, and its result was fetched at the
   end), but the log only showed at the end, and an interrupt during the run removed the run dir
   without stopping the run (the runner had not yet marked it launched). On 2026-10-17 that left
   a V2 and an fc07cc3 run computing in deleted dirs on hwe-bench, stopped by hand.
5. `formal.json`, the task logs and any counterexample traces come back to
   `build/formal/remote-<run id>/`, and the remote run dir is deleted. During a run, each task's
   model files and each reach task's traces are deleted as soon as the task finishes.

SIGTERM, SIGINT or SIGHUP on the local runner stops the remote run and deletes its dir
(`remote_stop_cmd`): SIGTERM to the run's own process group (its runner then stops every SBY
task), waiting up to 60 s for it to end. The host is shared, so nothing else is ever signalled:
not by name, not after the run wrote `done`, and only if the `remote.pid` process still works in
the run dir (`/proc/<pid>/cwd`; a reused pid belongs to someone else). Any other exception in a
runner stops every SBY task before it exits. `test_formal.py::test_remote_launch_and_stop` runs
the launch and the stop on a Linux host (setsid and /proc; skipped on macOS): the launch returns
at once, a `remote.pid` naming a process in another dir is left alone, and the run's own process
group is stopped. It passed on hwe-bench, and a real run there streamed its log live, fetched its
result, and on SIGTERM left no process in its dir.

**Orphans.** A local runner that dies without cleaning up (the Mac crashes or runs out of
memory, SIGKILL) leaves its remote run alive: it finishes on its own, holding the host's lock,
and leaves `out/formal.json` and `done` in its dir. (Before the detached mode, such a run was
hung up whenever sshd noticed the dead connection, possibly hours later, and its finished work
was lost: that is what happened to the first 7-job V2 run below.) Recovery:

- `run_formal.py --host <host> --fetch-orphans` fetches the result of every finished orphan of
  this checkout (its run dirs whose local pid is gone) into
  `build/formal/orphan-<run id>/formal.json` with its logs, deletes a finished orphan's dir only
  once that fetch worked, deletes the dirs of dead orphans, and lists the orphans still running
  (left alone);
- every remote run does the same first (`reap_remote`), so orphans never pile up on the host's
  shared disk;
- each run also deletes the local work dirs and `build/formal/remote-*` logs of dead runs
  (`reap_stale`).

`test_formal.py::test_reap_remote_orphans` checks the classification (finished, dead, running,
live runner, another checkout, another machine, a failed fetch); the SIGKILL, `--fetch-orphans` and SIGTERM paths were exercised on
hwe-bench. `BONETTO_FORMAL_REMOTE_DIR` changes the remote base dir (and so the lock). With the
host set, `test_formal.py` sends every formal run there and skips the local heavy lock.

## What is and is not proven

Proven, for the candidate as built with `-DRISCV_FORMAL`, under the environment above:

- **Structure and RVFI isolation, for all time.** No latches, posedge `clk` only, no formal-only
  cells. The pins equivalence (`equiv_simple` plus `equiv_induct`) is inductive.
- **Every assertion of the property table, on every trace from reset up to the check's depth.**
  That means:
  - every free MISO bit, `uio_in` value and `ui_in` constant;
  - free power-up values of every register, the register file included;
  - free initial contents of the core region (a real memory);
  - a free program memory image, through the bus checks' byte shadow.
- **Non-vacuity of each check.** Its triggers occur within that same depth (the reach tasks).
- **Liveness, in deep only.** Every 1,024-edge window that closes within D + 1,024.

Not proven:

- Anything beyond the depth. There is no induction, no k-induction and no unbounded proof of the
  ISA, bus or liveness properties. A bug that needs three or more retirements of a slow class, or
  internal state built up over many instructions, is cosim's and the architecture tests' to
  find.
- Instruction instances slower than the timing programs measured (for example, a
  shift-amount-dependent shifter shifting further than the programs do, or b9a5211's
  993-cycle first-instruction SW or JALR followed by a slow second retirement).
- A reset after the start: `rst_n` goes low only at the beginning (the `reset` check varies its
  length from 1 to 8 edges).
- Encodings with register fields x16-x31 and other UNSPECIFIED behaviour: the E filter drops them
  from the instruction checks; the consistency checks still apply.
- What a trapping retirement claims on top of trap, halt, `rd_addr = 0` and `wmask = 0` (its
  `rmask` and read data are not checked), and its rs fields unless it is a branch or JALR.
- Anything at or after the first retirement whose fetch or data access touches the core region
  (contract section 3), except `insn_region_mem_addr` and the reads before the touch at that
  retirement, the SPI waveform rules and (deep) liveness. Region transactions themselves are
  only checked for staying inside the region (no wrap, no mixing), for not starting after a
  halt (a WRITE) and, through the region memory, for register consistency. Their number, order
  and what they write are not compared.
- The rs2 read of a load or store that touches the region in its data access (it may come after
  the touch).
- The plain build, other than through the pins equivalence. Gate-level behaviour, X propagation
  and timing belong to `make gl` and hardening.
