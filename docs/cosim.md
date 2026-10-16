# Cosim: reference ISS, test programs, pin-level harness

These are contract gates (`verif/` is on the don't-touch list). They check a design in `src/`
against `docs/contract.md` sections 2, 3, 4 and 6.

## How to run

| Command | What it does |
|---|---|
| `make -C verif/programs` | builds every program: `build/programs/<name>.{elf,hex,expect.json}` |
| `python3 verif/cosim/run_cosim.py --all` | the `make cosim` gate: every program, both modes, seeds 1, 2, 3; writes `build/cosim.json`; exit 0 only if all pass |
| `run_cosim.py --mode rvfi --prog build/programs/selftest.hex --seed 2` | one run; prints `{"prog","mode","seed","pass","retired","cycles","cpi","max_cycles_per_insn","error"}` |
| `run_cosim.py ... --timing-json PATH` | rvfi mode: writes the timing report (below) |
| `run_cosim.py ... --dump-ram PATH` | writes the final 64 KB RAM image, raw bytes, as the design left it |
| `run_cosim.py ... --trace out.fst` | separate traced build, FST waveform |
| `run_cosim.py ... --src DIR` | runs another design directory (mutants) |
| `pytest verif/cosim/test_iss.py` | ISS vs spike, ISS unit cases, program coverage |
| `pytest verif/cosim/test_cosim_harness.py` | harness self-tests: stand-in driver, design passes, mutants fail |

`build.sh` compiles every `src/*.v` file, in sorted (byte) order, with nothing in front of them
(no prelude, no include path); the rvfi build's only define is `RISCV_FORMAL` (contract sections
1 and 6). It records the exact Verilator command in `build/cosim/<mode>/verilator.args`.
`run_cosim.py` rebuilds the Verilator model when anything that can reach it changes: it keys a
stamp on every file under the design directory except `__pycache__` and non-Verilog `test_*`
files, the harness sources and `verilator --version`. Builds hold
`build/cosim/<build dir>.lock` (lockf), so concurrent processes don't build the same model at
once. `--max-cycles` defaults to the latency cap's bound, 1,024 x (reference retirements + 16).

## Programs (`verif/programs/`)

| Program | Content |
|---|---|
| `selftest` | every RV32E instruction, every branch taken and not taken, x0 writes, JALR with bit 0 set, a not-taken branch to a misaligned target, misaligned LW/LH/LHU/SW/SH including the wraps at 0xFFFD..0xFFFF, LW at 0x7FFFFFFE (RAM for the whole access), loads of the instruction's own bytes (aligned, and misaligned into the next instruction), data and fetch aliasing (pc 0x1xxxx and 0xABCDxxxx), every shift amount in all six shift forms, FENCE variants (TSO, PAUSE, nonzero rd/rs1, fm = 1111), GPIO reads and writes on every byte lane. Results go to a RAM signature area. Ends with 0xA5 (pass) or 0xEE (fail) on GPIO, then EBREAK. |
| `gpio_demo` | counts on uo_out, echoes ui_in XOR 0x5A, 194 instructions. For the gate-level test. |
| `coremark` | CoreMark, ITERATIONS=1, TOTAL_DATA_SIZE=2000, -O2, rv32e, MEM_STATIC. Prints to the RAM log ring at 0xF000-0xF7FF; must print "Correct operation validated". 791,682 instructions. |
| `trap_ecall`, `trap_jal`, `trap_jalr`, `trap_branch`, `trap_zero`, `trap_ones` | one fatal trap each, after visible GPIO and RAM state; the code after the trap writes GPIO and RAM, so a core that doesn't halt is caught in both modes |
| `random_1` .. `random_6` | `gen_random.py SEED 3000`: valid-only random streams on all of x1..x15 (ALU, shifts, LUI/AUIPC; loads and stores with misaligned accesses, alias bits addr[30:16] and the 64 KB wrap, RAM stores into 0x8000-0x8FFF and 0xFFF0-0xFFFF; GPIO anywhere in the addr[31] = 1 space, byte accesses on every lane, wider ones aligned; forward branches, bounded loops closed by backward branches, JAL, JALR with bit 0 set, FENCE); registers dumped to 0x0000..0x003B (over the prologue) at the end |

Every program writes all registers before reading them (the ISS checks it: `iss_main --check-init`),
never executes a reserved or unspecified instruction, and ends with a guard after its EBREAK.
`<name>.expect.json` holds the ISS's retired count, halt and uo_out trace for the pins ui_in
schedule with seed 1 (what the gate-level test compares), the same for the RVFI schedule, and
`log_contains` for coremark.

## The reference ISS (`verif/cosim/iss.h`)

RV32E per contract sections 3 and 4: fetch at pc[15:0]; data RAM at (addr[15:0] + k) mod 2^16
when the effective address has bit 31 clear, for the whole access; GPIO when it is set (byte
lanes with X[1:0] = 0 read ui_in and write uo_out, others read 0 and ignore writes); the five
fatal traps retire with trap = halt = 1 and no register or memory write. `Retire::reserved` flags
everything the contract leaves unspecified: reserved encodings (including register fields
x16-x31, FENCE.I, CSR instructions, compressed encodings, M) and misaligned IO accesses wider
than a byte. FENCE is any MISC-MEM encoding with funct3 = 0.

Register-field convention (contract section 6, "the register index the core actually read"):
LUI, AUIPC, JAL report no sources; JALR reports rs1; branches rs1 and rs2; loads rs1; stores rs1
and rs2; OP-IMM rs1; OP rs1 and rs2; FENCE, ECALL, EBREAK and the illegal words report nothing.
On a trapping retirement the harness compares only trap, halt, insn and pc_rdata, plus
rd_addr = 0 and mem_wmask = 0 (contract section 6). The register-read fields, rd_wdata, pc_wdata
and the other memory fields are free there. (The ISS still reports the sources a trapping JALR or
taken branch read.)

Cross-check: `test_iss.py` runs selftest, gpio_demo and coremark (791,686 retirements) on spike
and on the ISS and compares pc, instruction, register write, store address and data, and load
address at every retirement. spike can't model the 64 KB alias or GPIO, so those builds link at
0x10000, model IO as a RAM page in both simulators, and skip selftest's wrap, alias and GPIO
sections; unit cases cover those.

## GPIO input schedules

Contract section 3: an IO load samples ui_in after its own fetch transaction has completed
(CS_n has risen after the fetch) and before its own retirement; the cosim changes ui_in only
shortly after retirements (within 2 cycles). The value is `ui_value(seed, k)`, defined in
`iss.h` (an integer hash, reproducible in any language):

- **rvfi mode:** the instruction with retirement index i sees `ui_value(seed, i)`. After
  retirement i - 1 (rvfi_valid at edge t) the harness puts the new value on ui_in at the falling
  edge after edge t + d, with d = 0 or 1 chosen per retirement from the seed, so the core's flops
  first see it at edge t + 1 or t + 2. An IO load that samples ui_in before its own fetch has
  completed (for instance in the cycle after the previous retirement) then reads the old value on
  some retirements and fails (mutant `ui_at_retire`).
- **pins mode and the gate-level test:** retirements are invisible, so ui_in changes when uo_out
  changes: after the c-th uo_out change, ui_in = `ui_value(seed, c)`, applied at the falling edge
  after the edge where uo_out changed. uo_out changes no later than one cycle after its IO store
  retires (contract section 3), so ui_in changes within 2 cycles of that retirement. A fetch is a
  READ of at least 56 bits with SCK at most clk/2, so the next instruction's fetch can't complete
  sooner than 112 cycles after the store retires, and its IO load (which samples after that
  fetch) always sees the new value. If a core changes uo_out before the store retires, ui_in
  changes before the retirement too; no instruction can observe that, since the store doesn't
  sample ui_in and the next instruction's fetch only starts after the store retires.

## What the harness checks

The DUT runs with every power-up value randomized from the seed (`--x-initial unique`,
`--x-assign unique`, `randReset(2)`), noise on MISO whenever SCK is low or no response bit is
due, noise on every unused `uio_in` bit, and a reset of 1 to 9 edges depending on the seed
(seed 1 uses one edge).

Both modes:

| Check | Contract | Error text contains |
|---|---|---|
| `uio_oe` = 0x0B and `uio_out[7:4]`, `uio_out[2]` = 0 at every edge | 1 | `uio_oe =`, `bits [7:4] and [2]` |
| CS_n = 1, SCK = 0, uo_out = 0 after every edge with rst_n low, the first one included (reset is applied once, at the start) | 1 | `during reset` |
| outputs change only at rising clk edges (not with clk falling or an input changing) | 1 | `outputs changed between rising clk edges` |
| CS_n changes only while SCK is low | 2.1 | `CS_n changed while SCK was high` |
| MOSI stable on rising SCK edges | 2.2 | `MOSI changed on a rising SCK edge` |
| MISO sampled while SCK is low reads noise | 2.3 | shows up as a data mismatch |
| command is 0x03 or 0x02; command and address complete | 2.4 | `invalid command`, `before its command and address` |
| WRITE carries a positive multiple of 8 data bits | 2.5 | `WRITE ended mid-byte` |
| no SPI WRITE after the halt | 4 | `SPI WRITE started after` |
| final RAM image equals the reference | 3 | `final RAM differs` |

rvfi mode (design built with `RISCV_FORMAL`):

| Check | Contract | Error text contains |
|---|---|---|
| every retirement: order, intr = 0, mode = 3, ixl = 1, insn, pc_rdata, trap, halt, rd_addr, mem_wmask; on a normal retirement also pc_wdata, rs1/rs2 addr and data, rd_wdata, mem_rmask, mem addr (when a mask is set), rdata/wdata on the masked lanes | 4, 6 | `RVFI mismatch at retirement #N (pc, insn): field = got, expected` |
| every SPI transaction ends before the next rvfi_valid and touches only that instruction's bytes: its 4 fetch bytes, its RAM load bytes, its RAM store bytes with their values; IO accesses never appear on SPI | 3, 6 | `SPI READ/WRITE of byte ... is not part of retirement #N` |
| CS_n is high in every rvfi_valid cycle | 6 | `SPI transaction still open at` |
| fetch completeness, per transaction: one READ carries all 4 instruction bytes in full (the first such READ is the fetch) | 6 | `retired without an SPI READ carrying all 4 instruction bytes` |
| load completeness, per transaction: every RAM load byte is carried in full by a READ other than the fetch, so a load of the instruction's own bytes still needs its own READ; every store byte written | 6 | `retired without reading load byte` / `without writing` |
| uo_out makes exactly the reference's changes, in order, each no later than one cycle after its IO store retires; any other change fails, including a glitch that returns to the old value before the store retires | 3 | `uo_out changed to`, `one cycle after the IO store retired` |
| latency cap: first retirement at most 1,024 cycles after reset release, each later one at most 1,024 after the previous; checked at the retirement edge (a retirement 1,025 cycles late fails) and by a watchdog (nothing retired by cycle 1,025 fails there) | 4 | `latency cap` |
| no retirement and no SPI WRITE for 4,096 cycles after the halt | 4 | `retirement after the halt` |
| no rvfi_valid during reset | 6 | `rvfi_valid during reset` |

pins mode (the plain design):

| Check | Contract | Error text contains |
|---|---|---|
| SPI WRITE bytes follow the reference's RAM stores in program order with the right values | 3 | `SPI WRITE of ... doesn't match the reference store` |
| uo_out changes follow the reference's change sequence | 3 | `uo_out changed to ...; the reference's change` |
| an SPI transaction completes at least every 2,048 cycles while stores or GPIO writes remain (every instruction is fetched, and retires within 1,024 cycles) | 4, 6 | `no SPI transaction for` |
| after the last store and GPIO change: (tail + 8) x 1,024 quiet cycles with no WRITE and no uo_out change, where tail is the number of instructions after the reference's last observable effect | 4 | `SPI WRITE started after the reference program's last store` |
| CoreMark (and any program whose expect file lists `log_contains`): the strings are in the RAM log ring at 0xF000 | | `RAM log ring lacks` |

## Timing report (`--timing-json`, rvfi mode)

```json
{"pass": true, "first_retire_cycle": 115, "max_cycles_per_insn": 230,
 "per_class": {"alu": 116, "alu_imm": 116, "branch_not_taken": 116, "branch_taken": 116,
               "fence": 116, "io_load": 116, "io_store": 116, "jal": 116, "jalr": 116,
               "load": 230, "lui_auipc": 116, "shift": 116, "store": 230, "trap": 116}}
```

(The seed core on selftest: `run_cosim.py --mode rvfi --prog build/programs/selftest.hex
--timing-json build/seed_timing.json`.) The file is written on every run; with `"pass": false`
the numbers only cover the run up to the failure (or are null), so a consumer must check `pass`.
Cycles count rising clk edges. Reset release
is the last edge with rst_n low; the first retirement's latency is the number of edges from there
to its rvfi_valid edge, and each later latency is the number of edges since the previous rvfi_valid
edge. `max_cycles_per_insn` is the largest latency, `per_class` the largest per instruction class
(a trapping instruction counts as `trap`; `branch_taken` and `branch_not_taken` by outcome;
`io_load`/`io_store` are accesses with address bit 31 set). A class absent from `per_class` was not
executed. `cycles` in the summary is the halt's latency from reset release; `cpi` is
cycles / retired. In pins mode `cpi`, `max_cycles_per_insn` and the timing report are null, since
retirements are invisible, and `cycles` is the cycle of the last observable event.

## Harness self-tests

`verif/cosim/mutants/standin/` is a fixed-sequence SPI driver (not a core) with one `BUG_*` define
per broken rule; `harness --spi-only N` runs it without an ISS. `verif/cosim/mutants/<name>/` are
copies of the seed with one deliberate bug each, listed with their mode and expected error in
`mutants/MANIFEST.json`. `legal_trap_fields` is a legal variant that must pass (arbitrary
register-read fields on trapping retirements). `stall_gap` holds every retirement until exactly
`STALL_GAP` cycles after the previous one: `test_latency_cap_boundary` builds it with 1,024
(passes, timing report 1,024 everywhere), 1,025 (fails at retirement #0, and at #1 with
`STALL_SKIP_FIRST`) and 1,026 (the watchdog fails at cycle 1,025).

## Speed

Measured on this Mac with the seed core (CoreMark: 791,682 instructions, 103.6 M cycles, CPI
130.8): 22.0 M cycles/s in rvfi mode and 25.8 M cycles/s in pins mode (4.7 s and 4.0 s per CoreMark
run, single run on a quiet machine; an earlier run under load measured 11.0 and 14.6). The full
`--all` matrix (15 programs x 2 modes x 3 seeds, 90 runs) took 13.1 s wall clock with 8 parallel
jobs, starting from no builds (programs and both Verilator models included).
