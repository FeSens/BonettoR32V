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
`csr_irq_target` (its pc_rdata = mtvec). The same checker runs twice: `csr` at cpi=2 (a CSR
write or a trap, then a read) and `irq` at cpi=4 (an IO store and two CSR writes enable the
interrupt, then the entry and the handler's first instruction), each with its own triggers.

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
and a load of X, and a load of X after a store to X; `csr` a CSR read, a read after a CSR write,
a read after a trap, a trap, an MRET; `irq` an interrupt entry; `rvfi` a second retirement and a
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
cpi=N        d_N     = reset_steps + first + N x max_cpi + margin
liveness             = D + 2,048 (deep)
FAIL if a base depth > depth_cap   (liveness's fixed +2,048 is not counted)
```

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

The champion (346c4a3, cosim timing over 27 programs): first 289, max 289 (alu_imm and
lui_auipc 289 include the first instruction after reset; csr 270; store and shift 236; trap and
io_store 202; alu and load 160; branches 136; jalr and io_load 118; mret 84; jal 76; fence and
wfi 52). So d_short = 581 (every fast check except below), cpi=2 = 870 (`coherence`, `csr`),
cpi=4 = 1,448 (`irq`), D = 581, liveness 2,629.

A short-depth trace covers the first retirement plus one more retirement of the class under
check. Slower instances than the timing programs measure (a shift by more bits than the
programs shift) are left to cosim and the architecture tests. Each check's reach task demands
that its triggers occur within the depth: for `irq` that is the IO store that enables the
interrupt source, the two CSR writes (mie, mstatus), the entry and the handler's first
instruction within 1,448 steps.

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
| checks | every check except liveness | every check, liveness included |
| depth | short depth (per class group for the instructions); `coherence`, `csr` cpi=2; `irq` cpi=4 | full depth D; cpi checks keep cpi; liveness D + 2,048 |
| instruction tasks | 10 class groups (`CLASS_GROUPS`) | 8 round-robin groups |
| task timeout | 44,000 s (2 x the longest task measured: reg, 21,934 s on hwe-bench, fc07cc3 reg-only run, host shared) | 44,000 s |
| depth cap | 2,048 | 4,096 |
| jobs | at most 8 on the Mac, `BONETTO_FORMAL_REMOTE_JOBS` (18) on a remote host, and never more than memory holds | same |

**Memory and jobs.** Each SBY task needs up to about 0.4 GB + 9.5 MB per step of its depth:
bitwuzla's RSS on the champion (hwe-bench) was 13.7 GB for irq at depth 1,448, 7.6 GB for csr and
6.9 GB for coherence at 870, 3.5 GB for periph and 3.3 GB for gpio at 581. (The v2 checks needed
3.7 MB per step; with that estimate the first champion runs put hwe-bench down to 5 GB
available.) The runner caps the job count at the memory budget divided by that need at the
deepest task's depth (`mem_jobs`). The budget is `$BONETTO_FORMAL_MEM_GB` if set (forwarded to a
remote run; set it to split a shared host), else 85 % of `MemAvailable` on Linux, else half the
RAM on macOS. `reset` (free reset length) counts twice (22 GB at depth 1,222 on a v2 core, 6.2 GB
at 581 on the champion). Each task also waits until its estimate fits next to the running ones
(`MemGate`).

Fast does not cover:

1. **Liveness** (ruling R9, kept for v3: deep checks it; the cosim watchdog and liveness fuzz
   cover it per candidate).
2. **Retirements after the check's depth**: at d_short, a second retirement after a slow first
   one, and every third retirement; the cpi checks reach 2 or 4 more instructions.

Neither config is an unbounded proof (see "What is and is not proven").

### Measured runtimes (contract v3, champion 346c4a3)

@@RUNTIMES@@

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
  the full depth, so a counterexample can take as long as a proof.
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

@@MUTANTS@@

## Fail fast, scheduling, remote runs

By default the runner stops at the first proof that does not pass, or the first reach task that
finishes without every required trigger. It kills the running SBY tasks and marks the unfinished
checks `SKIPPED`. `--keep-going` runs everything. Tasks start in this order: the proofs longest
first (by `TASK_COST`: liveness, irq, coherence, csr, reg, psram_write, the instruction groups,
reset, ...), interleaved with the reach tasks, which are cheap and catch a dead or stuck core
early as `UNREACHED`.

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
