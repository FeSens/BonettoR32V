# Formal verification at the pins

`verif/formal` proves the contract (`docs/contract.md` v3: sections 1-6 and 8) on the real Tiny
Tapeout top module, `tt_um_fesens_r32v`, at its pins, on the TT QSPI Pmod. It is a fixed gate:
hypothesis agents never edit it. It runs at the final signoff and at every 10th merged
improvement of the performance loop (contract section 7), not per candidate.

```
BONETTO_FORMAL_HOST=hwe-bench python3 verif/formal/run_formal.py [--deep]    # on hwe-bench (use this)
BONETTO_FORMAL_HOST=hwe-bench python3 verif/formal/run_formal.py --fetch-orphans   # after a crash
python3 verif/formal/run_formal.py --tasks reach ...     # triggers only (fast iteration)
python3 verif/formal/run_formal.py --tasks proof ...     # proofs only (mutants)
pytest verif/formal/test_formal.py -v                    # the gate's own tests
```

The runner writes `build/formal.json`:
`{"pass", "checks": {name: "PASS|FAIL|UNREACHED|SKIPPED"}, "depth", "runtime_s", "pmod", ...}`.
It exits 0 only when every check is `PASS`. `UNREACHED` (a check whose trigger the solver cannot
reach within the depth) counts as a failure. By default the run stops at the first `FAIL` or
`UNREACHED` and marks the checks it did not finish `SKIPPED` (`first_failure` names the check);
`--keep-going` runs everything. `--tasks proof` or `--tasks reach` runs one half of every check
(the other half is `SKIPPED`, `complete` is false and the run never passes the gate).

## How it works

1. **Staging.** Every top-level `src/*.v` and `src/*.sv`, `test_*.v` included (contract section
   1: every top-level file is design; subdirectories are not), is copied into
   `build/formal/work-<pid>/`, followed by the behavioral models of the whitelisted sky130 cells
   (`verif/cells/*.v`, contract section 1; the champion instantiates `sky130_fd_sc_hd__sdfxtp_1`).
   Stale work dirs of dead processes are removed.
2. **Pre-pass.** yosys elaborates the `RISCV_FORMAL` build. It fails on latches, on flip-flops
   that are not on the rising edge of `clk`, and on formal-only cells (`$anyconst`,
   `$initstate`, ...) in the core. Formal multiclock is off, so anything but posedge-`clk`
   flip-flops would be modelled wrongly. It removes the core's own assert/assume/cover
   statements and every initial value, so no check can rely on power-up values (invariant 6).
   The core is written to `core.il` and read separately from the checkers.
3. **Pins equivalence** (`equiv`). The `RISCV_FORMAL` build with its `rvfi_*` ports removed must
   be equivalent to the plain build (yosys `equiv_make`, `equiv_simple -seq 5`, `equiv_induct`).
   The checks run on the RVFI build; this is what makes them speak for the design that ships.
4. **Signoff period and Pmod limits.** `T` is `CLOCK_PERIOD` in `src/config.json` (the eval
   cosim gate uses the same, contract 7), or `--clk-ns`. tCEM = floor(8000 / T) and tRST =
   ceil(50 / T) cycles (the cosim's `pmod.h` computes the same); at T = 20 ns, 400 and 3. They
   reach the checkers as `BONETTO_TCEM` and `BONETTO_TRST`, and `formal.json` records them
   (`pmod`). The monitor counts edges, not SCK phase lengths: the slowest command limit (33 MHz,
   PSRAM SPI `0x03`) holds for any waveform as long as a 2-cycle SCK period is within it, so the
   runner refuses a T below 15.15 ns.
5. **Timing and depth.** See "Depth policy".
6. **Tasks.** Each check runs as a proof task and a reach task (see "Non-vacuity"). The 48
   instruction checks run in class groups: one task checks several instructions through one muxed
   model (`bonetto_insn_group`, generated per task; the members' encodings are disjoint), with
   one trigger per instruction. A group counterexample is attributed to the instruction in the
   trace; the other members are reported `FAIL` as not proven.
7. **Scheduling.** See "Fail fast, scheduling, remote runs". SIGTERM, SIGINT and SIGHUP stop
   every SBY run.

### Environment (assumptions)

`wrapper.sv`, module `rvfi_wrapper`:

- `rst_n` is low for exactly the first rising edge (the contract's minimum), then high. The
  `reset` check instead holds it low for 1 to 8 edges.
- `ena = 1`. `ui_in` is a free constant (formal holds it constant).
- `uio_in` is free every cycle: every line the core does not drive carries noise (contract
  2.1.5).
- **Memory outputs** (2.1.5): from a data-out rising edge until SCK falls, the lines the selected
  chip drives (SD1 for a single read, SD0-SD3 for a quad read) hold the value they had at that
  edge. Outside such a high phase they are noise. A core that samples while SCK is low reads
  noise (mutant `sd_low`).
- **Power-up modes** (2.4): one free constant per chip, flash standard or continuous read, PSRAM
  SPI (not reset) or QPI, so every check covers all four combinations at once.
- **The core region** (PSRAM `0x7F_FF80-0x7F_FFFF`, A[23] ignored) is a real 128-byte memory in
  every task (`rmem`), with free initial contents (contract 3: arbitrary at reset). Each
  data-out edge at a region byte drives that byte's bits; each completed write byte at a region
  byte updates it. So a core that keeps x1..x15, the pc or CSRs there reads back what it wrote,
  and the consistency checks (`reg`, `causal`, `csr`) cover the core plus that memory. Every other
  memory byte is noise, which `coherence` constrains one symbolic byte at a time.
- All flip-flops without reset start at free values.

### The QSPI chip monitors

`bonetto_qspi_chip` (in `wrapper.sv`, one instance per chip) is a passive port of the command
state machines of `verif/cosim/pmod.h` (`QspiFlash`, `Psram`), clocked by the rising SCK edges
(2.1.4) while its CS_n is low. The chips sample `(uio_out & uio_oe) | (uio_in & ~uio_oe)`.

- **Flash** (2.2): standard mode (8-bit single command) or continuous read (no command: the
  transaction starts with `0xEB`'s quad address). At CS_n rise, a transaction that clocked both
  mode nibbles enters or stays in continuous read iff M[5:4] = `10`, else it leaves it (whether
  or not it completed).
- **PSRAM** (2.3): SPI mode or QPI mode (2-edge quad command); `0x35` enters QPI, `0xF5` leaves
  it, `0x66` then `0x99` (consecutive transactions with a complete command field) reset it to
  SPI mode, initialized. A read or write before that reset is a violation. A transaction that
  ends inside its command field is ignored by either chip.
- Fields per the command tables: address 24 single or 6 quad edges, M (flash `0xEB`), dummy or
  wait edges, data single or quad. Data bytes go to `(A + k) mod 2^24` (flash) or
  `(A + k) mod 2^23` (PSRAM, A[23] ignored).

Events and flags per cycle: `rd` (a data-out edge: address, quad, bit or nibble index), `wb` (a
completed write byte: address, value), `dout` (the chip is in its data-out phase), `fin`,
`fin_rd`, `fin_wr`, `bad_cmd` (unlisted command for the chip's state, `0x99` not right after
`0x66`, `0x66` not followed by `0x99`, an access before the PSRAM reset), `bad_extra` (an edge
after a command that takes no fields), `bad_wend` (a write ending inside its address or
mid-byte), `bad_trst` (PSRAM CS_n falls less than tRST cycles after a `0x99` ended), `bad_cem`
(PSRAM CS_n low for more than tCEM cycles).

`test_formal.py::test_qspi_monitor_decodes_stub` checks both monitors against a hand-written
waveform, from both power-up mode pairs: every command family, quad and single fields,
stretched SCK phases, A[23] and the PSRAM burst wrap, the flash wrap, M = `0x20` and `0xFF`,
reads ending mid-byte, a page program (ignored), and one of each violation (unlisted command on
each chip, an edge after `0xFF` and after `0x66`, a write ending mid-byte and one ending in its
address, tRST, tCEM). The expected events come from a Python reading of tables 2.2 and 2.3, not
from the Verilog. Dropping one expected event (the tRST violation) makes the run fail.

### The UNSPECIFIED stop

Contracts 3 and 4: a program access to the core region, a fetch from IO, an access whose bytes
span two areas (flash, PSRAM, IO) and a misaligned IO access are UNSPECIFIED, and the checkers
stop comparing at the first retirement that makes one. Only the QSPI rules and (deep) liveness
apply after it. `wrapper.sv` computes, at each retirement:

- `bon_fetch_unspec`: `pc[31] = 1`, or one of the 4 fetched bytes is a region byte, or they span
  two areas;
- the data access is UNSPECIFIED:
  - for a specified instruction (`bon_spec`: one of the 48 instruction models' `spec_valid`,
    E filter included, or a trap word), from the specification's address only (`rs1 + imm`,
    `rs1 = x0` reads 0, `n` bytes), never from what the core claims (`rvfi_mem_*`), so a core
    can neither dodge the stop nor trigger it by misreporting;
  - for a reserved encoding, which has no spec access, from the access it claims (non-trapping
    `rvfi_mem_*`). A core cannot use it to end the checks: formal, not the core, picks the
    instruction stream;
- `bon_unspec_ret`: either, at this retirement; `bon_stopped`: one has happened before.
  `BONETTO_FULL` = neither.

At the stop retirement only what decided the stop is compared: its pc and order (`pc_fwd`,
`pc_bwd`, `unique`, the first-pc and order rules), its instruction word (`coherence`, unless the
fetch is the UNSPECIFIED part), its rs1 read (`reg`) and, in the instruction checks, rs1_addr,
mem_addr and the masks unless it trapped. (282f6d8, a v2 version, stopped on any claimed region
access; the mutant `claim_region_stop` checks that a claim stops nothing.)

### The bus checks

Contract 8 removed v2's per-instruction transaction attribution; in its place:

**`qspi`** (`bonetto_qspi_check`, every cycle, never stops): `uio_oe[0,3,6,7] = 1` and
`uio_out[7] = 1` (`qspi_oe_fixed`, `qspi_csb`); in reset both CS_n high, SCK 0, `uo_out` 0,
nothing retires (`qspi_reset_*`); 2.1.1 at most one CS_n low (`qspi_rule1`); 2.1.2 a CS_n
changes only with SCK low at t-1 and t (`qspi_rule2`); 2.1.3 SCK 0 while both CS_n are high
(`qspi_rule3`); 2.1.4 every line the core drives at a rising edge was driven at t-1 with the
same value (`qspi_rule4`); 2.1.6 no contention during a data-out phase (`qspi_rule6_flash`,
`qspi_rule6_psram`); 2.1.7 tCEM (`qspi_tcem`); 2.3 tRST (`qspi_trst`); 2.1.10, 2.2, 2.3, 2.4
command legality (`qspi_cmd_flash`, `qspi_cmd_psram`, `qspi_extra`); 2.1.9 writes end on a byte
boundary (`qspi_wend`). CS_n high for at least one cycle between transactions (2.1.2) holds by
construction in a cycle model (one cycle is at least tCPH = 18 ns at T >= 18 ns).

**`psram_write`** (`bonetto_psram_write_check`): every PSRAM write outside the core region writes
a byte of a retired store, at its address, with its data. One symbolic PSRAM byte address `wa`
outside the region and one symbolic value `wv` (free constants, so every pair is covered). A
write of `wv` at `wa` must be explained by a compared, non-trapping retirement that stores `wv`
at `wa` (lane k of `rvfi_mem_wmask`, byte address `mem_addr + k`). **Design decision:** the write
may land before its store retires, as the champion's does (the store's M slot writes during the
store's execution, and `rvfi_valid` comes at its tail), but no later than that retirement: every
write since the previous retirement must be explained by the stores retired so far, this
retirement included. This is the cosim's rule (`verif/cosim/harness.cpp` `check_psram_writes`).
A write after its store retired (a write buffer) is explained too. A trapping retirement stores
nothing, so a write before a trap fails.
Not covered here: that every store byte reaches the PSRAM at all (`coherence` catches a store
whose byte a later load does not see; the cosim's final memory compare catches the rest), and
the order of two writes of different values to one byte (`coherence`).

**`coherence`** (`bonetto_coherence_check`): every load and fetch returns the value most recently
stored to that address, or the loaded image (contract 3). One symbolic byte X: a flash byte
(`cx = 0`) or a PSRAM byte outside the region (`cx = 1`).
- `pmem`: the chip's byte at X, with a free initial value. The flash never changes; a PSRAM write
  byte at X updates it. Every data-out edge at X drives `pmem`'s bits (the bit or nibble the
  edge clocks).
- `amem`: the architectural byte: `pmem`'s initial value (the loaded image, or the PSRAM's
  arbitrary content), then the bytes of compared, non-trapping retirements that store at X. A
  store to flash changes nothing.
- At each compared retirement, every fetched byte (`rvfi_insn`, `pc_rdata + k`) and every loaded
  byte (lane k of `rvfi_mem_rdata`) at X equals `amem` before this retirement
  (`coherence_fetch`, `coherence_load`). The fetch is also checked at the stop retirement, unless
  the fetch itself is UNSPECIFIED.

Caches, prefetch and write buffers are free in between: only what retires is compared. Together
with `insn_*` (rd from `mem_rdata`) this says each load's rd comes from memory, and with `reg`
that the fetched word is what executes.

### The CSR, trap and interrupt check

Contract v3 has no `rvfi_csr_*` ports (the lead ruled against adding them), so riscv-formal's
CSR checks cannot run. `bonetto_csr_check` keeps a shadow of the contract-4 CSR state built from
the retirement stream alone and compares every observation with it:

- **state**: mstatus.MIE, mstatus.MPIE, mie.MEIE, mtvec[31:2], mscratch, mepc[31:2],
  mcause {31, 3:0}, each bit with a known flag. MIE, MPIE, MEIE are known 0 after reset; mtvec,
  mscratch, mepc, mcause start unknown (arbitrary reset values).
- **learning**: an unknown bit takes the first value observed (a CSR read's rd_wdata, a trap's
  pc_wdata for mtvec, an MRET's pc_wdata for mepc, an interrupt entry's pc_rdata for mtvec);
  every later observation must agree. So CSR state may start arbitrary but never changes by
  itself.
- **updates**: CSR instructions with the contract-4 masks (CSRRS/CSRRC with rs1 = x0 or zimm = 0
  do not write); trap entry (mepc = pc, mcause = the cause, MPIE = MIE, MIE = 0); interrupt
  entry (mepc = the previous retirement's pc_wdata, mcause = 0x8000_000B, MPIE = MIE, MIE = 0);
  MRET (MIE = MPIE, MPIE = 1). An interrupt entry is a retirement with `rvfi_intr = 1` that does
  not follow a trapping retirement (contract 8).
- **havoc**: after a reserved encoding (`bon_spec = 0`, which includes CSR instructions on
  unlisted numbers and writes to the read-only id CSRs) every shadow bit becomes unknown
  (contract 4: any consistent effect).

Assertions at each compared retirement: `csr_read` (a CSR instruction's rd_wdata is the old
value: known shadow bits, MPP = 3, every other bit 0; misa, mtval, the id CSRs and mip read 0,
except mip.MEIP), `csr_trap_target` (a trap's pc_wdata = mtvec), `csr_mret_target` (MRET's
pc_wdata = mepc), `csr_irq_enabled` (an interrupt entry only with MIE = MEIE = 1),
`csr_irq_target` (its pc_rdata = mtvec). The same checker runs twice: `csr` and `irq`, each
with its own triggers. In deep, `csr` runs at cpi=2 (a CSR write or a trap, then a read) and
`irq` at cpi=4 (an IO store and two CSR writes enable the interrupt, then the entry and the
handler's first instruction). In fast, `csr` is bounded at the short depth and `irq` does not
run (see "Fast and deep").

The instruction models of the CSR instructions, MRET and WFI (`gen_isa_rv32e.py`, `NEW_INSNS`)
check everything else about them: register fields (rs1 for the register forms, 0 for the
immediate forms), rd_addr, masks 0, pc_wdata = pc + 4 (MRET's pc_wdata is the csr check's), and
that they do not trap. A CSR instruction on an unlisted number or writing an id CSR is
reserved.

### The IO checks

**`gpio`** (contract 6): a uo pin whose ALTFN bit is 0 shows GPIO_OUT's bit. It takes a stored
value no later than one cycle after the store retires (`gpio_uo_hold`: in the step after each
compared retirement every GPIO-mode pin equals the architectural GPIO_OUT), and within an
instruction's window it moves only to its new value, only if the instruction changes it, and
only once (`gpio_uo_change`, `gpio_uo_once`; it may move early, inside the store's own window).
Pins with ALTFN = 1 carry their peripheral and are not compared.

**`periph`** (contract 5.1): a model of every slot, updated by compared IO stores and compared
with every compared IO load where it knows the bit (`periph_read`): reset values, read/write
masks, byte lanes (a store writes only the bytes it covers), read-only and unimplemented bits
read 0, W1C and transfer starts on byte 0 only. GPIO_IN reads `ui_in`. Bits the hardware changes
on its own are known only while their source is provably off (TIMER_CNT while the timer has
been disabled, capture and edge flags while their select is off, TX_BUSY and SPI BUSY until the
first byte-0 write). The UART receiver's bits are not compared. Bit-exact timer, PWM, UART and
SPI waveforms are the cosim's (`verif/cosim/periph_models.h`).

### Forks of riscv-formal (in `verif/formal/checks/`, the submodule is untouched)

| Fork | Why |
|---|---|
| `rvfi_testbench.sv` | 16-bit cycle counter; free `check` (unbounded style); pins and monitor events for the checkers; free reset length for the `reset` check. |
| `bonetto_insn_check` | Checks a group of instructions (model `bonetto_insn_group`). Stock semantics plus: trap equals the spec's trap; a trapping retirement is compared only on trap, `rd_addr = 0` and `wmask = 0`, plus the rs fields of a trapping branch or JALR (contract 8); register fields exact (unused fields report 0); exact memory masks; no PMA or ALIGNED_MEM; CSR rd_wdata and MRET pc_wdata left to the csr check; the UNSPECIFIED stop. |
| `bonetto_reg_check` | Stock plus: a register not yet written reads the same value every time; the reads of a trapping branch or JALR are checked too; the stop. |
| `bonetto_pc_fwd_check`, `bonetto_pc_bwd_check` | pc chaining between time-adjacent retirements, except into an interrupt entry (contract 8: the previous pc_wdata is the interrupted pc). Stock pc_bwd only fires when order N+1 retires before N. |
| `bonetto_unique_check` | Counts retirements per order (stock fails on any core with a free trig). |
| `bonetto_liveness_check` | Edge counter since edge 0 or the last retirement (see "Liveness"); 2,048 edges; the gap after a WFI is exempt. |
| `bonetto_ill_check` | 0x00000000 and 0xFFFFFFFF trap with no write. Stock only covers 0x00000000. |
| `bonetto_bus_checks.sv` | `qspi`, `psram_write`, `coherence` (above). Stock shadows aligned words at 32-bit addresses and knows nothing about the pins. |
| new: `bonetto_csr_check.sv`, `bonetto_io_checks.sv`, the `fence`, `ecall`, `ebreak`, CSR, `mret`, `wfi` models, `rvfi`, `reset`, `cover` | See the property table. |

`isa_rv32e.txt` and `isa_rv32e.v` are generated by `gen_isa_rv32e.py` from the submodule's
`isa_rv32i.txt` plus the models above. Each `rvfi_insn_e_<name>` drops `spec_valid` when a
register field the instruction uses names x16-x31 (the E filter). Those encodings are reserved,
so the instruction checks demand nothing of them; the consistency checks still apply.

### Non-vacuity: a reach task per check

Every check has one or more `reach_*` statements: its triggers. For an instruction check that is
a retirement of that instruction with `spec_valid`, and for JAL, JALR, the branches, ECALL and
EBREAK also a trapping one. Each check runs as two SBY tasks on the same design (same checker,
same assumptions, same depth):

- the **proof task**: the check's assertions, with the reach statements removed;
- the **reach task**: `BONETTO_REACH_AS_ASSERT` turns each `reach_*` into `assert(!trigger)` and
  every other assertion is removed. smtbmc runs with `--keep-going` in small windows, so a failed
  `reach_*` assertion is a trigger reached within the depth, and the run stops as soon as every
  trigger has been reached.

A check is `PASS` only if its proof task passed and every required trigger was reached,
`UNREACHED` if the proof passed but a required trigger was not reached, and `FAIL` otherwise
(a counterexample, or a task that timed out or errored). The progress line reads
`insn_group2_reach REACHED 5/5 triggers` or `UNREACHED 3/5 ... missing: <labels>`. The two
queries are kept apart on purpose: one solver call mixing easy satisfiable goals with a hard
unsatisfiable one can stall (v1 seed: `bus_imem` 19.6 s as a proof, 61.1 s as a reach, more than
10 minutes combined).

The v3 triggers beyond the instruction ones: `qspi` a flash read, a PSRAM read and a PSRAM
write, each with data; `psram_write` an explained write outside the region; `coherence` a fetch
and a load of X, and (deep) a load of X after a store to X; `csr` a CSR read, a trap, an MRET,
and (deep) a read after a CSR write and a read after a trap; `irq` (deep) an interrupt entry; `rvfi` a second retirement and a
handler's first instruction; `gpio` a GPIO_OUT change on a pin and an ALTFN change; `periph` a
read of a written register, of IRQ_PEND, of GPIO_IN; `reset` a retirement after a reset of 3
edges or more; `cover` a non-trapping and a trapping retirement and a second one.

## Properties and the contract clauses they encode

| Check | Assertions | Contract clause |
|---|---|---|
| structure (pre-pass) | no latches; posedge `clk` flip-flops only; no formal-only cells; initial values removed | 1, invariants 6 and 7 |
| equiv (pre-pass) | RVFI build with `rvfi_*` stripped equals the plain build | 8, invariant 4 |
| `insn_<name>` (48) | `insn_trap`; on a trap `insn_trap_rd_addr`, `insn_trap_wmask` and, for a branch or JALR, the rs fields; otherwise `insn_rs*_addr`, `insn_rs*_zero`, `insn_rd_addr`, `insn_rd_wdata`, `insn_pc_wdata`, the masks, `insn_mem_addr`, store data per lane; `insn_unspec_*` at the stop | 4 (RV32E + Zicsr semantics, misaligned accesses never trap, FENCE a no-op, synchronous traps and nothing else), 8 (field conventions) |
| reg | `reg_rs1`, `reg_rs2`, `reg_rs1_rs2` | 4: reads return the last value written; 3: registers on the die or in the region, one real memory |
| pc_fwd, pc_bwd | `pc_fwd`, `pc_bwd_order`, `pc_bwd_pc` | 4, 8: pcs chain (trap target = next pc_rdata; interrupt entries exempt) |
| causal, unique | `causal`, `unique_order` | 8: in-order retirement, order counts retirements |
| rvfi | `conv_halt` (always 0), `conv_mode`, `conv_ixl`, `conv_intr_trap` (the handler's first instruction after a trap carries `rvfi_intr`), `conv_rs*_x0`, `conv_rd_x0`, `conv_rmask`, `conv_wmask`, `conv_trap_rd_addr`, `conv_trap_wmask`, `conv_first_order`, `conv_first_pc`, `conv_order_next` | 8; 1: execution starts at pc 0 |
| ill | `ill_trap`, `ill_rd_addr`, `ill_wmask` for the two trap words | 4 |
| qspi | see "The bus checks" | 1, 2.1-2.4 |
| psram_write | `psram_write_attr` | 8 |
| coherence | `coherence_fetch`, `coherence_load` | 3 |
| csr, irq | `csr_read`, `csr_trap_target`, `csr_mret_target`, `csr_irq_enabled`, `csr_irq_target` | 4, 8 |
| gpio | `gpio_uo_hold`, `gpio_uo_change`, `gpio_uo_once` | 6 |
| periph | `periph_read` | 5.1 (and 5.3-5.7 where a register shows it) |
| reset | the qspi and rvfi checks with rst_n low for 1 to 8 edges | 1 |
| liveness (deep) | `liveness` | 4: latency cap, invariant 8 |
| cover | reach only | riscv-formal's cover check |

## Depth policy

The runner computes every depth from the timing measured on the candidate; no agent sets one.
`build/formal.json` records them: `depth` (the deepest base depth used), `per_check_depth`, and
`depth_policy` (`d_min`, `d`, `d_short`, the timing and its source).

```
full depth   D       = max(depth_floor, reset_steps + max(first, max_cpi) + max_cpi + margin)
short depth  d(c)    = reset_steps + first + max_cycles(c) + margin
reach depth  r(c)    = min(d(c), max(reset_steps + first + margin, w(c) + 1 + margin))
cpi=N        d_N     = reset_steps + first + N x max_cpi + margin
liveness             = D + 2,048 (deep)
FAIL if a base depth > depth_cap   (liveness's fixed +2,048 is not counted)
```

`depth=reach` (the fast config): the check's reach task runs first, at d(c). w(c) is the last
step of the solver window (29 steps, the reach engine's) in which the last of the check's
required triggers was first reached; the proof then runs to r(c). A trigger not reached within
d(c) is UNREACHED and the proof stays at d(c). `formal.json` records each proof's depth, its
cap and the reach step in `reach_depths`. With `--tasks proof` (no reach task) a depth=reach
proof runs at d(c).

`first` is the first retirement's cycle (the memory reset sequence included) and `max_cpi` the
slowest instruction measured. `max_cycles(c)` is the slowest measured class among an
instruction group's members (`COSIM_CLASSES` in `run_formal.py`; csr, mret and wfi have their
own cosim classes); a class the timing does not measure uses `max_cpi`. reset_steps = 1 and
margin = 2.

The timing comes from the cosim: `run_cosim.py --mode rvfi --prog <p>.hex --src <dir>
--timing-json <file> --clk-ns T` for every built program in `build/programs/` except CoreMark.
The timing is the maximum over the programs, per class too (`merge_timings`); `depth_policy`
lists the programs. With `--host`, the programs and the cosim are copied to the host and the
timing runs there (Verilator stays off the Mac). The v2 Icarus fallback (`timing_tb.v`) is
gone: there is no v3 Pmod model in Verilog outside the formal monitor.

Champion 111 (346c4a3, cosim timing over 27 programs): first 289, max 289 (alu_imm and
lui_auipc 289 include the first instruction after reset; csr 270; store and shift 236; trap and
io_store 202; alu and load 160; branches 136; jalr and io_load 118; mret 84; jal 76; fence and
wfi 52). So d_short = 581, cpi=2 = 870 (deep `coherence`, `csr`), cpi=4 = 1,448 (deep `irq`),
D = 581, liveness 2,629. Champion 112 (6810e6f, 31 programs): first 305, max 305, so
d_short = D = 613, cpi=2 = 918, cpi=4 = 1,528, liveness 2,661.

A short-depth trace covers the first retirement plus one more retirement of the class under
check. Slower instances than the timing programs measure (a shift by more bits than the
programs shift) are left to cosim and the architecture tests. Each check's reach task demands
that its required triggers occur within the depth: for deep `irq` that is the IO store that
enables the interrupt source, the two CSR writes (mie, mstatus), the entry and the handler's
first instruction within cpi=4.

## Liveness

Contract section 4 counts edges. Edge 0 is the last rising edge at which `rst_n` is sampled low.
The first retirement must come at an edge <= 2,048 (the memory reset sequence included), and
each later one at most 2,048 edges after the previous. A retirement "at edge e" means
`rvfi_valid` is high after edge e, as the cosim harness counts it.

`bonetto_liveness_check` keeps `gap`: 0 in step 1 (right after edge 0), counting up by one per
step without a retirement, 1 in the step after a retirement. The assertion fires when gap
reaches 2,048 without `rvfi_valid`. So a first retirement at edge 2,048 (step 2,049) passes and
one at edge 2,049 fails; the same for later gaps. **WFI:** the gap that starts at a WFI's
retirement is exempt as a whole. The contract exempts the stall only while `mip.MEIP & mie.MEIE`
is 0, but MEIP is internal (no port), so formal cannot tell; the cosim checks it (rule R31, the
irq programs).

This is a safety property, checked by BMC: every window that closes within the depth (D +
2,048) is checked. The v2 boundary pairs (`stall_*`) are not re-derived: they need the
champion's worst-case first-retirement latency over all instructions, which the timing programs
do not measure (see "Mutants").

## Fast and deep

| | fast (`checks.cfg`) | deep (`checks-deep.cfg`) |
|---|---|---|
| checks | every check except irq and liveness | every check, irq and liveness included |
| depth | reach depth r(c) for every check: its reach task at the short depth first, then the proof to the step where its triggers were reached | full depth D; `coherence`, `csr` cpi=2, `irq` cpi=4; liveness D + 2,048 |
| triggers | `coherence`, `csr`: the ones reachable at the short depth (`reach=`) | all |
| instruction tasks | 10 class groups (`CLASS_GROUPS`) | 8 round-robin groups |
| task timeout | 18,000 s (5 h: a run fits in about 6 hours, the lead's target) | 44,000 s (2 x reg, 21,934 s, fc07cc3 reg-only run); 172,800 s for `coherence`, `csr`, `irq` |
| depth cap | 2,048 | 4,096 |
| jobs | at most 12 (`jobs` in the config), and never more than memory holds | 8, same rule |

**Why fast bounds coherence and csr and drops irq.** The first fast config ran `coherence` and
`csr` at cpi=2 and `irq` at cpi=4. On champion 111 (hwe-bench, 3 jobs, host shared) the three
proofs started first: `coherence` (870) and `irq` (1,448) hit TIMEOUT at 44,000 s (44,001.8 s
and 44,002.0 s, peak RSS 12.6 GB and 27.2 GB, still growing about 1 GB per hour), and `csr`
(870) was past 41,000 s at 15.3 GB when the run was stopped: 2 of 52 tasks finished in 12 hours.
So fast now runs every check at the short depth, and only the deep config keeps the full depths.
What that costs, check by check:
- `coherence` (fast): every fetch and load of the first two retirements returns the loaded image
  (the flash byte, or the PSRAM's arbitrary content). Not in fast: a store followed by a load or
  fetch of the same byte (the symbolic byte's `amem` update), so a store that never reaches the
  PSRAM is caught only by deep, by the cosim's coherence program and final memory compare, and
  by `psram_write` when the write goes to the wrong place.
- `csr` (fast): every CSR read by the first two retirements (reset values of MIE, MPIE and MEIE,
  MPP = 3, the read-only and zero bits, mip, misa, mtval and the id CSRs reading 0, mtvec and
  mepc bits 1:0) and the first trap and MRET. Not in fast: anything compared with a value learned
  or written earlier: `csr_trap_target` and `csr_mret_target` (they need mtvec or mepc read or
  written by an earlier instruction), a read after a write, mcause and mepc after a trap, MPIE
  after MRET.
- `irq` (deep only): an interrupt entry needs three instructions first (an IO store and two
  CSR writes), about 1,100 steps on 111 by the class timings (an estimate, not measured). Not in fast: `csr_irq_enabled`, `csr_irq_target`, the interrupt's mepc and mcause.

**Why fast runs each proof at its reach depth.** The short depth did not fit either. In the fast
run on champion 112 (6810e6f, d_short = 613, hwe-bench, 9 jobs, host shared) the proofs of reg,
psram_write, coherence, csr and instruction groups 1, 2 and 3 hit TIMEOUT at 18,000 s (one solver
call each, bitwuzla RSS 3.8 to 5.6 GB). The cost grows much faster than the depth (same host,
one solver call): psram_write took 192 s at depth 408 and 3,646 s at 484; csr 1,437 s at 466 and
2,870 s at 484; reg 48 s at 321, and was still running past 6,500 s at 484. Smaller solver windows
do not rescue it: with 29-step windows the steps up to 289 took 25 s, but each later window took
about 2.7 times the one before (51 s, 149 s and 402 s for steps 290 to 376 of instruction group
0; reg's window 348 to 376 was still running after 8 minutes). So each fast proof stops at its
reach depth r(c), where every behavior it requires has occurred. What that bound proves, and what
it leaves to deep:
- Proven in fast, per check: every trace from reset (any power-up mode, any flip-flop power-up
  values, any flash image and PSRAM content, free registers) through the memory reset sequence
  and the first retirement, up to r(c). In practice that is the first instruction and the
  pair-fetched second one. The floor reset + first + margin covers the first retirement in the
  slowest power-up mode the cosim measured.
- Not in fast: anything after r(c). That includes an instruction that only retires after a slow
  first one (a load or a store first), a trigger that a slower power-up mode reaches later than
  the fastest one, and the qspi rules over long windows: `qspi_tcem` can only fail in fast for a
  CS# low period that starts before r(qspi) - 400, so on 112 (r(qspi) = 321) fast never checks
  tCEM; deep (613) checks the CS# low periods that start before step 213, and the cosim's Pmod
  model checks tCEM on every cycle of every program.
- Deep keeps every check at the full depth D or above (613 on 112; coherence and csr at cpi=2,
  irq at cpi=4).

The deep config keeps coherence, csr and irq at their full depth with a 172,800 s timeout each; on 111 they
did not finish within 44,000 s, so a deep run is not known to finish on this core, and `irq`
may outgrow a shared 62 GB host before then. The cosim (every program against the ISS, in
lockstep, the irq and coherence programs) covers these properties on every candidate. A proposal
that would bring them back into fast: observation-only `rvfi_csr_*` ports for the contract-4
CSRs (the riscv-formal convention), so the CSR and interrupt checks compare each retirement's
CSR values directly and need no earlier instruction (a contract change, the lead's ruling).

**Memory and jobs.** Each SBY task needs up to about 0.4 GB + 9.5 MB per step of its depth:
bitwuzla's RSS on the champion (hwe-bench) was 13.7 GB for irq at depth 1,448, 7.6 GB for csr and
6.9 GB for coherence at 870, 3.5 GB for periph and 3.3 GB for gpio at 581. (The v2 checks needed
3.7 MB per step; with that estimate the first champion runs put hwe-bench down to 5 GB
available.) The runner caps the thread count at the memory budget divided by that need at the
shallowest task's depth (`mem_jobs`), and each task waits until its own estimate fits. The budget is `$BONETTO_FORMAL_MEM_GB` if set (forwarded to a
remote run; set it to split a shared host), else 85 % of `MemAvailable` on Linux, else half the
RAM on macOS. `reset` (free reset length) counts twice (22 GB at depth 1,222 on a v2 core, 6.2 GB
at 581 on the champion). The waiting is `MemGate`; the deep tasks start first, so they are not
starved by the short ones.

Fast does not cover:

1. **Liveness** (ruling R9, kept for v3: deep checks it; the cosim watchdog and liveness fuzz
   cover it per candidate).
2. **Retirements after the check's reach depth** r(c): in practice everything after the first
   instruction and its pair-fetched second one (above).
3. **irq, and the multi-instruction parts of coherence and csr** (above).

Neither config is an unbounded proof (see "What is and is not proven").

### Measured runtimes (contract v3)

**Champion 112, fast (the 10th-improvement check).** 6810e6f (pair fetch), hwe-bench (shared),
12 jobs, memory budget 40 GB, timing measured in the run (31 programs: first 305, max 305, so
d_short = 613): **PASS, 65 of 65 checks, 3,367.6 s** (the remote run's runtime; about 57 minutes on
the wall clock, cosim timing included). Peak bitwuzla RSS 5.3 GB (csr's reach task at 613;
sampled once a minute). Per task: the proof depth r(c) (cap d(c)), proof and reach seconds.

| task | r(c) (cap) | proof s | reach s | task | r(c) (cap) | proof s | reach s |
|---|---|---|---|---|---|---|---|
| insn_group0 | 408 (560) | 2,184.6 | 676.0 | reset | 308 (613) | 166.9 | 190.4 |
| csr | 466 (613) | 1,436.6 | 1,650.0 | gpio | 379 (613) | 152.0 | 361.6 |
| insn_group4 | 379 (469) | 1,107.1 | 743.3 | insn_group8 | 379 (560) | 140.1 | 462.2 |
| insn_group1 | 408 (560) | 873.9 | 841.8 | insn_group9 | 379 (578) | 131.5 | 554.0 |
| periph | 408 (613) | 663.0 | 556.5 | ill | 379 (613) | 107.5 | 303.3 |
| pc_fwd | 408 (613) | 612.4 | 464.6 | insn_group2 | 321 (613) | 78.1 | 303.3 |
| rvfi | 408 (613) | 305.9 | 397.0 | insn_group3 | 321 (613) | 74.1 | 238.4 |
| coherence | 350 (613) | 261.3 | 140.2 | insn_group6 | 321 (484) | 61.6 | 168.2 |
| insn_group5 | 350 (613) | 202.3 | 693.2 | insn_group7 | 321 (484) | 53.8 | 129.8 |
| psram_write | 408 (613) | 191.7 | 398.4 | reg | 321 (613) | 48.0 | 56.7 |
| pc_bwd | 321 (613) | 44.2 | 68.7 | qspi | 321 (613) | 38.7 | 123.2 |
| unique | 321 (613) | 37.4 | 68.7 | causal | 308 (613) | 36.3 | 47.9 |
| cover | 350 (613) | 33.5 | 170.5 | | | | |

**Champion 112 at the short depth (stopped).** The same champion with every proof at d_short
(613, 9 jobs, budget 48 GB): reg, psram_write, coherence and csr TIMEOUT at 18,000 s (18,002.1,
18,002.0, 18,001.6 and 18,001.3 s), instruction groups 1, 2 and 3 past 16,800 s; the run was
stopped there with 37 of its 50 tasks not started.

**Champion 111 (346c4a3), the first fast config** (coherence and csr at cpi=2, 870; irq at cpi=4,
1,448; hwe-bench, 3 jobs): coherence and irq TIMEOUT at 44,000 s (44,001.8 s and 44,002.0 s, peak
RSS 12.6 GB and 27.2 GB), csr past 41,000 s at 15.3 GB; 2 of 52 tasks finished in 12 hours, then
the run was stopped.

Deep has not been run on contract v3.

### Contract v2 measurements (history)

The timeout comes from the v2 runs (hwe-bench): V2 (hyp/002-rf-in-ram-pcram b9a5211, depths
1,461 to 1,623) took 32,538 s in fast with 4 jobs, its longest task `reg` 19,669 s; fc07cc3
(hyp/002-rf-in-ram, depths up to 1,222) 36,823 s with `reg` TIMEOUT at 21,602 s under the then
limit of 21,600 s. A reg-only re-run of fc07cc3 with a 24 h timeout PASSED in 21,933.8 s (host
shared), above the 20,000 s threshold, so the timeout is 2 x that, rounded up: 44,000 s (the
lead's L7 rule). The v2 seed's fast run took 208 s on hwe-bench with 18 jobs.

### Engines tried

- **bitwuzla** (chosen) through smtbmc. Proofs use `step=all`: one solver call checks every step
  up to the depth (64-step windows took 520 s against 302 s on the v1 seed). Liveness also uses
  one call: 64-step windows found the v1 stall counterexamples faster but the seed's own proof
  ran past 33 minutes at 16 GB, against 658 s with one call. A single call has to find a trace of
  the full depth, so a counterexample can take as long as a proof. On champion 112, 29-step
  windows at depth 613 grew about 2.7 times per window after the first retirement (see "Why
  fast runs each proof at its reach depth"), so `step=all` stays.
- **yices** stalls on the bus checks.
- **abc bmc3**: faster on the v1 seed's instruction groups (332 s against 414 s); rejects designs
  with constant x bits without `setundef`.
- **abc pdr**: did not converge on liveness; with `--keep-going` SBY crashes converting its
  witness, so it is never used for a reach task.
- **Combined proof and reach**, one solver call: stalls (see "Non-vacuity").

## Mutants

Each mutant is a design with one bug, plus `expect.json`. `mutants/make_mutants.py` generates
them from the champion: each mutant directory holds only the design files the change touches
(`test_formal.py` lays them over a copy of `src/`), and every substitution must match exactly
once, so a changed champion fails loudly in the generator. `tcem_stall` is shared with the
cosim gate (`"rtl": "verif/cosim/mutants/tcem_stall"`, a stub with the contract ports).

`test_mutant_fails` runs each one with `--tasks proof --only` its checks (`--deep` for liveness)
on the champion's timing. A FAIL must be a counterexample: a failed assertion (or the pins
equivalence), never a timeout or an error.

The set re-derives the v2 mutants where their rule still exists (`miso_low` is `sd_low`,
`mosi_rise` is `sd_rise`, `cs_sck_high`, `write_midbyte`, `store_extra_byte`, `region_wrap`,
`zero_init`, `trap_writes_rd`, `reg_wrong_index`, `rvfi_rd`, `sub_decode`, `claim_region_stop`,
`gpio_any_byte`, `uo_glitch`, `uo_late`, `norvfi_diff`, `hang_reserved`) and adds one per v3
rule. Not re-derived: the v2 transaction-attribution mutants (`refetch`, `fetch_extra_byte`,
`store_twice`, `load_own_fetch`, `cs_open_at_retire`, `load_skip_byte`, `load_half_swap`,
`trap_program_write`, the region-mixing ones: repeated or extra accesses are legal in v3, and a
wrong load value is `coherence`'s, covered by `sd_low` and `store_dropped`), the halt mutants
(`no_halt`, `trap_continues`, `region_write_after_halt`: nothing halts in v3; `trap_target` and
`rvfi_halt` replace them), the hyp/002 legal cores (v2 designs), `trap_fields_brj` (the trap
field rules are `insn_*` and `conv_*` now) and the liveness boundary pairs (see "Liveness").

### Fast config, depth=reach, 112

Champion 112 (6810e6f; main 64c8cd7) with the fast config as merged, one `run_formal.py --host
hwe-bench` run per mutant, fail-fast as in `make eval`, on the champion's timing (first 305,
max 305), with `--only` the fast tasks that hold the mutant's expected checks: an instruction
check runs as its whole class group (so at that group's reach depth), `irq` as fast `csr` (its
checker holds the interrupt assertions), `liveness` as the whole fast config, `equiv` is the
pre-pass. 8 lanes on hwe-bench (2 jobs, 7 GB budget each), then `hang_reserved` alone (12 jobs):
1 h 37 min for the 45, then 55 min for `hang_reserved`, 2 h 33 min wall in all.

**36 caught** by a counterexample in the expected check (35) or by the pins equivalence
(`norvfi_diff`); **3 caught as UNREACHED**: the mutant breaks PSRAM or flash traffic, so qspi's
reach task misses a trigger up to the cap and the gate fails without a counterexample (`sd_rise`,
`psram_cmd`, and the cosim stub `tcem_stall`, which has no core); **7 escaped**; **no timeouts**.
Of the 10 mutants tagged deep, fast catches 5 (`fetch_wrong_chip`, `trap_target`, `mret_target`,
`mret_mpie`, `trap_mepc`). Every escape but `hang_reserved` is a bug its check sees, whose first
visible effect comes after the check's reach depth; `hang_reserved` needs liveness (deep). "cex
step" is the step where the failed assertion first fails in the replayed trace (`vcd_sim`, see
"Fail fast, scheduling, remote runs"); "wall" is the mutant's whole run, reach task included.
`sub_decode` was run a second time after the step-based group attribution went in (same verdict;
the first run named no culprit instruction).

| mutant | expected check | fast verdict: failed assertions | proof depth | cex step | wall s | why it escaped |
|---|---|---|---|---|---|---|
| oe_sck_reset | qspi | FAIL in qspi: `qspi_oe_fixed` | qspi 321 | 1 | 189 | |
| reset_cs_low | reset | FAIL in reset: `qspi_reset_cs` | reset 308 | 1 | 287 | |
| cs_both | qspi | FAIL in qspi: `qspi_rule1`, `qspi_cmd_psram` | qspi 321 | 215 | 181 | |
| cs_sck_high | qspi | FAIL in qspi: `qspi_rule2` | qspi 321 | 255 | 184 | |
| sck_masked | qspi | FAIL in qspi: `qspi_rule3`, `qspi_rule4` | qspi 321 | 257 | 194 | |
| sd_rise | qspi | UNREACHED in qspi (missing reach_qspi_psram_read, reach_qspi_psram_write) | reach ran to the cap 613; proof not run (fail-fast) | - | 1166 | |
| contention | qspi | FAIL in qspi: `qspi_rule6_flash`, `qspi_rule6_psram` | qspi 321 | 240 | 195 | |
| flash_no_exit | qspi | FAIL in qspi: `qspi_rule6_flash`, `qspi_cmd_flash` | qspi 321 | 155 | 181 | |
| flash_m_byte | qspi | FAIL in qspi: `qspi_cmd_flash` | qspi 321 | 306 | 189 | |
| psram_cmd | qspi | UNREACHED in qspi (missing reach_qspi_psram_read) | reach ran to the cap 613; proof not run (fail-fast) | - | 1282 | |
| psram_no_reset | qspi | FAIL in qspi: `qspi_cmd_psram` | qspi 321 | 270 | 193 | |
| trst_short | qspi | FAIL in qspi: `qspi_trst` | qspi 321 | 116 | 191 | |
| cmd_extra_edge | qspi | FAIL in qspi: `qspi_extra` | qspi 321 | 83 | 198 | |
| write_midbyte | qspi | PASS (escaped) | qspi 321 | - | 193 | after the reach depth: qspi sees it (`qspi_wend`, at 581 on 111), but no store's write ends within 321 |
| store_extra_byte | psram_write | FAIL in psram_write: `psram_write_attr` | psram_write 408 | 405 | 584 | |
| store_data_flip | psram_write | FAIL in psram_write: `psram_write_attr` | psram_write 408 | 402 | 626 | |
| region_wrap | psram_write | FAIL in psram_write: `psram_write_attr` | psram_write 408 | 355 | 569 | |
| sd_low | coherence | FAIL in coherence: `coherence_fetch` | coherence 350 | 332 | 273 | |
| store_dropped | coherence (deep) | PASS (escaped) | coherence 350 | - | 464 | after the reach depth: coherence sees it only when a load or fetch reads a byte after its store (store then load, cpi=2, deep): two memory instructions, beyond 350; psram_write cannot see a missing write |
| fetch_wrong_chip | coherence (deep) | FAIL in coherence: `coherence_fetch` | coherence 350 | 347 | 440 | |
| trap_target | csr (deep) | FAIL in csr: `csr_trap_target` | csr 466 | 352 | 2265 | |
| mret_target | csr (deep) | FAIL in csr: `csr_mret_target` | csr 466 | 453 | 2759 | |
| irq_target | irq (deep) | PASS (escaped) | csr 466 | - | 3582 | after the reach depth: csr's interrupt assertions need an interrupt entry, which needs an IO store and two CSR writes first, beyond 466 (irq, cpi=4, is deep only) |
| irq_masked | irq (deep) | PASS (escaped) | csr 466 | - | 3225 | after the reach depth: as irq_target, no interrupt can be taken within 466 |
| csr_mtvec_lowbits | csr | FAIL in csr: `csr_mret_target` | csr 466 | 461 | 1972 | |
| csr_mpp | csr | FAIL in csr: `csr_read` | csr 466 | 417 | 2597 | |
| mret_mpie | csr (deep) | FAIL in csr: `csr_read` | csr 466 | 456 | 3431 | |
| trap_cause | csr (deep) | PASS (escaped) | csr 466 | - | 3589 | after the reach depth: the wrong mcause shows when the handler reads mcause after the EBREAK; csr's trap trigger is met only in steps 435 to 463 of 466, the read comes later |
| trap_mepc | csr (deep) | FAIL in csr: `csr_mret_target` | csr 466 | 463 | 2773 | |
| hang_reserved | liveness (deep) | PASS (escaped) | every fast task, 308 to 466 | - | 3315 | not visible to fast: a hang has no retirement, which only liveness (deep) flags; the whole fast config passes 65/65 |
| gpio_any_byte | gpio, periph | FAIL in gpio: `gpio_uo_hold` | gpio 379, periph 408 | 369 | 1624 | |
| uo_glitch | gpio | FAIL in gpio: `gpio_uo_change`, `gpio_uo_once` | gpio 379 | 368 | 531 | |
| uo_late | gpio | FAIL in gpio: `gpio_uo_hold` | gpio 379 | 369 | 484 | |
| periph_wmask | periph | FAIL in periph: `periph_read` | periph 408 | 389 | 1173 | |
| uart_any_byte | periph | PASS (escaped) | periph 408 | - | 1252 | after the reach depth: periph sees it on a read of UART_CTRL after a byte-1 store (TX_BUSY 1); none fails within 408 |
| rvfi_halt | rvfi | FAIL in rvfi: `conv_halt` | rvfi 408 | 352 | 472 | |
| intr_after_trap | rvfi | FAIL in rvfi: `conv_intr_trap` | rvfi 408 | 394 | 593 | |
| trap_writes_rd | rvfi | FAIL in rvfi: `conv_trap_rd_addr` | rvfi 408 | 371 | 494 | |
| zero_init | rvfi | FAIL in rvfi: `conv_first_pc` | rvfi 408 | 283 | 456 | |
| ill_zero | ill | FAIL in ill: `ill_trap` | ill 350 | 345 | 292 | |
| reg_wrong_index | reg | FAIL in reg: `reg_rs1` | reg 321 | 303 | 132 | |
| sub_decode | insn_sub | FAIL in insn_sub: `insn_rd_wdata` | insn_sub group 408 | 329 | 919 | |
| rvfi_rd | insn_xor | FAIL in insn_xor: `insn_rd_wdata` | insn_xor group 408 | 407 | 1055 | |
| claim_region_stop | insn_add | FAIL in insn_add: `insn_rd_wdata` | insn_add group 408 | 404 | 657 | |
| norvfi_diff | equiv | FAIL (pins equivalence pre-pass) | - | - | 37 | |
| tcem_stall | qspi | UNREACHED in qspi (missing reach_qspi_flash_read, reach_qspi_psram_write) | reach ran to the cap 613; proof not run (fail-fast) | - | 72 | |

**The 7 escapes against cosim** (lead, 2026-10-19: main bea2777 plus each mutant's file,
`make cosim` on hwe-bench with the programs built fresh; the unmodified design passes all 262
runs). Six fail cosim: `write_midbyte` (the Pmod model, rule 2.1.9, in every program),
`store_dropped` (lockstep loads, `coherence`, the final PSRAM compare), `trap_cause` (`trap_ebreak`:
mcause reads 0xb, expected 3), `irq_target` (`irq_timer`, `irq_stress`, `wfi`), `irq_masked`
(`csr`, `irq_timer`, `irq_stress`, `wfi`: an interrupt entry while MIE = 0) and `hang_reserved`
(the liveness fuzz's 2,048-cycle cap). `uart_any_byte` passed: no program wrote byte 1 of
UART_DATA. Since a5b20fb, `uart_echo` and `spi_loop` check contract 5's byte-lane rules (writing
bytes 1-3 of UART_DATA or SPI_DATA starts nothing; reading byte 1 of UART_DATA keeps RX_FULL and
RX_OVR). `uart_any_byte` and two one-off mutants of `r32v_periph.v` (an SPI transfer started by
byte 1, RX_FULL cleared by a byte-1 read) fail cosim in those programs, and the champion passes
`make eval`. So every one of the 46 mutants fails at least one gate.

### Champion 111, proofs at the short depth (history)

Results (hwe-bench, `--tasks proof`, so each proof ran at its cap: the short depth 581 of
champion 111, 346c4a3, one solver call; generated on 111, then regenerated on 112, where the
generator still matches every substitution, but not re-run there). 31 of the 46 mutants FAIL as
expected, each with a counterexample in its own check. The 15 marked "not run" were not run in
this port: the deep ones (a deep proof did not finish within 44,000 s on 111), and `sd_low`,
`csr_mtvec_lowbits`, `csr_mpp`, `ill_zero` and `uart_any_byte` (stopped when the shared host ran
low on memory, or not reached in the time available). The fast proofs now stop at their reach
depth r(c), below 581: a mutant whose first wrong retirement comes after r(c) is caught by deep
only.

| mutant | files | bug | checks | result: failed assertions, proof time |
|---|---|---|---|---|
| oe_sck_reset | r32v_mem | SCK's output enable is off during the reset sequence | qspi | qspi FAIL: `qspi_oe_fixed`, 113.1 s |
| reset_cs_low | r32v_mem | flash CS_n low while rst_n is low | reset | reset FAIL: `qspi_reset_cs`, 206.0 s |
| cs_both | r32v_mem | both CS_n low | qspi | qspi FAIL: `qspi_rule1`, `qspi_cmd_psram`, 116.3 s |
| cs_sck_high | tt_um_fesens_r32v | CS_n rises while SCK is high | qspi | qspi FAIL: `qspi_rule2`, 115.1 s |
| sck_masked | r32v_mem | SCK toggles with both CS_n high | qspi | qspi FAIL: `qspi_rule3`, `qspi_rule4`, 651.9 s |
| sd_rise | r32v_mem | SD0 changes at a rising SCK edge | qspi | qspi FAIL: `qspi_rule4`, `qspi_cmd_psram`, 111.8 s |
| contention | r32v_mem | drives SD0-SD3 during a read's data phase | qspi | qspi FAIL: `qspi_rule6_flash`, `qspi_rule6_psram`, 113.1 s |
| flash_no_exit | r32v_mem | the reset sequence skips the flash's continuous-read exit | qspi | qspi FAIL: `qspi_rule6_flash`, `qspi_cmd_flash`, 328.5 s |
| flash_m_byte | r32v_mem | M = 0x30 in the flash header | qspi | qspi FAIL: `qspi_wend`, `qspi_cmd_flash`, 243.0 s |
| psram_cmd | r32v_mem | PSRAM read command 0x0C | qspi | qspi FAIL: `qspi_cmd_psram`, 242.4 s |
| psram_no_reset | r32v_mem | no PSRAM reset | qspi | qspi FAIL: `qspi_cmd_psram`, 277.1 s |
| trst_short | r32v_mem | CS_n high for one cycle after 0x99 | qspi | qspi FAIL: `qspi_trst`, 115.7 s |
| cmd_extra_edge | r32v_mem | a ninth edge after 0x66 | qspi | qspi FAIL: `qspi_extra`, 116.0 s |
| write_midbyte | tt_um_fesens_r32v | a store ends mid-byte | qspi | qspi FAIL: `qspi_wend`, 1776.4 s |
| store_extra_byte | tt_um_fesens_r32v | SB writes two bytes | psram_write | psram_write FAIL: `psram_write_attr`, 1540.6 s |
| store_data_flip | tt_um_fesens_r32v | a store writes a wrong bit | psram_write | psram_write FAIL: `psram_write_attr`, 1579.0 s |
| region_wrap | r32v_mem | core-region words at a 2-byte offset | psram_write | psram_write FAIL: `psram_write_attr`, 735.5 s |
| sd_low | tt_um_fesens_r32v | samples SD0-SD3 while SCK is low | coherence | not run |
| store_dropped | tt_um_fesens_r32v | stores issue nothing | coherence (deep) | not run |
| fetch_wrong_chip | tt_um_fesens_r32v | fetches always read the flash | coherence (deep) | not run |
| trap_target | r32v_csr | a trap jumps through the wrong core-region word | csr (deep) | not run |
| mret_target | r32v_csr | MRET jumps to mtvec | csr (deep) | not run |
| irq_target | r32v_csr | an interrupt jumps to mepc | irq (deep) | not run |
| irq_masked | r32v_csr | the interrupt ignores mstatus.MIE | irq (deep) | not run |
| csr_mtvec_lowbits | r32v_csr | mtvec and mepc bits 1:0 read back | csr | not run |
| csr_mpp | r32v_csr | mstatus.MPP bit 12 reads 0 | csr | not run |
| mret_mpie | r32v_csr | MRET leaves MPIE | csr (deep) | not run |
| trap_cause | tt_um_fesens_r32v | EBREAK's mcause is 11 | csr (deep) | not run |
| trap_mepc | tt_um_fesens_r32v | a trap writes the ring to mepc | csr (deep) | not run |
| hang_reserved | tt_um_fesens_r32v | custom-0 encodings hang | liveness (deep) | not run |
| gpio_any_byte | r32v_periph | a store to any byte of GPIO_OUT writes it | gpio, periph | gpio FAIL: `gpio_uo_hold`, 1772.0 s; periph SKIPPED |
| uo_glitch | r32v_periph | uo_out[0] glitches at a GPIO_OUT store | gpio | gpio FAIL: `gpio_uo_change`, `gpio_uo_once`, 1100.0 s |
| uo_late | r32v_periph | uo_out takes GPIO_OUT two clocks late | gpio | gpio FAIL: `gpio_uo_hold`, 2610.2 s |
| periph_wmask | r32v_periph | TIMER_CMP's high byte is not writable | periph | periph FAIL: `periph_read`, 3311.6 s |
| uart_any_byte | r32v_periph | a store to UART_DATA byte 1 starts a transmission | periph | not run |
| rvfi_halt | tt_um_fesens_r32v | rvfi_halt at a trap | rvfi | rvfi FAIL: `conv_halt`, 1411.6 s |
| intr_after_trap | tt_um_fesens_r32v | rvfi_intr only after an interrupt | rvfi | rvfi FAIL: `conv_intr_trap`, 1316.5 s |
| trap_writes_rd | tt_um_fesens_r32v | a trap reports rd | rvfi | rvfi FAIL: `conv_trap_rd_addr`, 873.2 s |
| zero_init | tt_um_fesens_r32v | the pc is not cleared | rvfi | rvfi FAIL: `conv_first_pc`, 476.3 s |
| ill_zero | tt_um_fesens_r32v | the all-zeros word executes | ill | not run |
| reg_wrong_index | tt_um_fesens_r32v | writes go to an odd register | reg | reg FAIL: `reg_rs1`, 452.0 s |
| sub_decode | tt_um_fesens_r32v | SUB adds | insn_sub | insn_sub FAIL: `insn_rd_wdata`, 217.6 s |
| rvfi_rd | tt_um_fesens_r32v | a wrong RVFI rd value | insn_xor | insn_xor FAIL: `insn_rd_wdata`, 204.3 s |
| claim_region_stop | tt_um_fesens_r32v | claims a core-region address to stop the checks | insn_add | insn_add FAIL: `insn_rd_wdata`, 202.7 s |
| norvfi_diff | r32v_periph | a behavior difference only without RVFI | equiv | FAIL (pins equivalence pre-pass) |
| tcem_stall | cosim stub | tcem_stall | qspi | qspi FAIL: `qspi_tcem`, 36.5 s |

## Fail fast, scheduling, remote runs

By default the runner stops at the first proof that does not pass, or the first reach task that
finishes without every required trigger. It kills the running SBY tasks and marks the unfinished
checks `SKIPPED`. `--keep-going` runs everything. Tasks start in this order: the proofs longest
first (by `TASK_COST`: liveness, irq, coherence, csr, reg, psram_write, the instruction groups,
reset, ...), interleaved with the reach tasks, which are cheap and catch a dead or stuck core
early as `UNREACHED`. A depth=reach check's reach task goes first (slowest checks first), and its
proof is queued as soon as that reach task finishes.

**Counterexamples.** A proof's trace is replayed by yosys sim from the solver's witness (SBY's
`vcd_sim on`; reach tasks keep smtbmc's own traces), because a `step=all` call does not say at
which step an assertion failed. SBY's summary then names it, and `formal.json` records it as
`cex_step` with the retirements up to it (`cex_retirements`, the last 8); in an instruction
group, the retirement at that step names the culprit instruction.

`--host <ssh host>`, or `BONETTO_FORMAL_HOST` (unset by default), runs the gate on another
machine. The verdict is the same on any host:

1. The pre-pass runs locally. Exactly the inputs are copied (rsync) into a fresh
   `~/bonetto-formal/run-<machine>-<checkout tag>-<pid>-<time>`: the candidate's `src`,
   `verif/formal` (without the mutants), `verif/cells`, riscv-formal's `rvfi_macros.vh` and
   instruction models, and the timing JSON if given, else `verif/cosim` and the built programs
   (`build/programs/*.hex`, `*.expect.json`, `*.stim`) so the timing runs there.
2. The remote run holds the host's own lock (`flock <base>/formal.lock`) with
   `BONETTO_FORMAL_REMOTE_JOBS` jobs (default 18), the oss-cad-suite at
   `BONETTO_FORMAL_REMOTE_OSS`, and the signoff period passed as `--clk-ns`.
   `BONETTO_FORMAL_REMOTE_DIR` changes the base dir (and so the lock): separate bases let
   several runs share the host, each with its own `BONETTO_FORMAL_MEM_GB`.
3. **The remote run is detached** from the ssh session (`setsid nohup`, its session leader's pid
   in `remote.pid`, its exit code in `done`, its log in `run.log`). The local runner follows the
   log; a dropped connection only makes it follow again 30 s later.
4. `formal.json`, the task logs and any counterexample traces come back to
   `build/formal/remote-<run id>/`, and the remote run dir is deleted.

SIGTERM, SIGINT or SIGHUP on the local runner stops the remote run and deletes its dir
(`remote_stop_cmd`): SIGTERM to the run's own process group, only if the `remote.pid` process
still works in the run dir (`/proc/<pid>/cwd`). The host is shared, so nothing else is ever
signalled, never by name. `test_formal.py::test_remote_launch_and_stop` runs the launch and the
stop on a Linux host.

**Orphans.** A local runner that dies without cleaning up leaves its remote run alive: it
finishes on its own and leaves `out/formal.json` and `done`. `run_formal.py --host <host>
--fetch-orphans` fetches the result of every finished orphan of this checkout, deletes a
finished orphan's dir once that fetch worked, deletes the dirs of dead orphans, and lists the
running ones. Every remote run does the same first (`reap_remote`); each run also deletes the
local work dirs and logs of dead runs (`reap_stale`). `test_reap_remote_orphans` checks the
classification.

## What is and is not proven

Proven, for the candidate as built with `-DRISCV_FORMAL`, under the environment above:

- **Structure and RVFI isolation, for all time.** No latches, posedge `clk` only, no formal-only
  cells. The pins equivalence is inductive.
- **Every assertion of the property table, on every trace from reset up to the check's depth**:
  every free `uio_in` value and `ui_in` constant, every memory power-up mode, free power-up
  values of every flip-flop, free initial contents of the core region (a real memory), a free
  flash image and PSRAM content (through the coherence check's symbolic byte).
- **Non-vacuity of each check**: its triggers occur within that same depth (the reach tasks).
- **Liveness, in deep only**: every 2,048-edge window that closes within D + 2,048.

Not proven:

- Anything beyond the depth: no induction, no unbounded proof of the ISA, bus, CSR or liveness
  properties. A bug that needs more retirements than the check's depth holds is cosim's and the
  architecture tests' to find.
- Instruction instances slower than the timing programs measured.
- A reset after the start (the `reset` check varies its length from 1 to 8 edges).
- Encodings with register fields x16-x31 and other reserved encodings: the instruction checks
  drop them; the consistency checks still apply, and the csr shadow forgets everything after
  one.
- The interrupt line: mip.MEIP is internal, so formal checks neither its value in `csr_read`,
  nor that an enabled, pending interrupt is taken within one more instruction (contract 4), nor
  the WFI stall condition. The cosim's irq programs and rule R31 do.
- That every store byte reaches the PSRAM (the cosim's final memory compare), the UART
  receiver's register bits, and bit-exact peripheral waveforms (the cosim).
- Anything at or after the first UNSPECIFIED retirement, except what decided the stop, the QSPI
  rules and (deep) liveness. Region transactions themselves are checked only through the QSPI
  rules and, through the region memory, register consistency.
- The plain build, other than through the pins equivalence. Gate-level behaviour, X propagation
  and timing belong to `make gl` and hardening.
