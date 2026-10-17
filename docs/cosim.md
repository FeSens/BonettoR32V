# Cosim: reference ISS, test programs, pin-level harness

These are contract gates (`verif/` is on the don't-touch list). They check a design in `src/`
against `docs/contract.md` v2 sections 2, 3, 4 and 6 (v2 at main 35b92b5, with the amendments
8df46d8 (core-region transactions of a trapping instruction), e321614 (clarifications: trapping
branch/JALR register reads are checked) and 78472fe (null READs; a partly clocked last byte counts
as addressed)). The liveness fuzz (below) answers the formal review's C1: liveness on reserved
encodings, per candidate.

## Contract v3 harness (`verif/cosim/harness.cpp`)

The harness has two builds until the integration swap (plan Task 9):
- **Contract v3.** `build.sh` builds `harness.cpp` by default. It uses `pmod.h`, `iss3.h` and
  `periph_models.h`. `run_cosim.py --v3` selects it.
- **Contract v2.** `HARNESS=v2` builds `harness_v2.cpp` (with `spiram.h` and `iss.h`).
  `run_cosim.py` still defaults to this build, and the eval and the arch-test run it. Every section
  after this one describes v2. Its self-tests are in `test_cosim_harness_v2.py`.

```
harness <prog.hex> [--seed S] [--expect PATH] [--clk-ns 25] [--max-cycles C] [--trace PATH]
        [--dump-psram PATH] [-v]
harness --fuzz-liveness K [--fuzz-cap N] [--seed S]
run_cosim.py --v3 --mode rvfi|pins --prog P [--seed S] [--src DIR] [--clk-ns 25]
run_cosim.py --v3 --all [--src DIR] [--no-make]    # the plan's 26 programs x 2 modes x seeds 1-4
                                                   # + the fuzz at seeds 1-5 -> build/cosim.json
```

**Flash image and seeds.**
- `prog.hex` is a sparse `$readmemh` flash image. Unset bytes are 0xFF. It is loaded once, and
  `PmodBus` and `Iss3` share the same 16 MiB buffer.
- The seed picks the Pmod power-up mode, as `(seed - 1) % 4`: standard or continuous-read flash,
  times PSRAM in SPI or QPI mode.
- The seed also picks the reset length: `1 + ((seed + 8) * 5) % 9` edges, so seed 1 uses one.

**Summary JSON (the last stdout line).**
- `pass`, `retired`, `cycles`, `cpi`, `max_cycles_per_insn`, `first_retire_cycle`.
- `end`: `selfloop`, `result_gpio` or `fail`.
- `uart_tx`, `error`, `psram_writes`.
- `coremark_cycles_per_iter`: an integer, or null without marks. Never 0.
- Extra keys: `mode`, `deterministic`, `per_class`, `interrupts`, `uo_trace`, `reset_edges`.

`run_cosim.py` adds `coremark_cycles_per_iter` to its KEYS in both builds; v2 always reports null.

**Judging a program.** Before simulating, the harness runs the program alone on `Iss3`. The
program counts as *deterministic* if that run reaches its self-loop (`jal x0, 0` retired twice)
without reading a volatile IO bit and without enabling interrupts (`mstatus.MIE` and `mie.MEIE`
both 1).
- **Test program bugs.** These fail before simulation: a reserved or unspecified step, a register
  or arbitrary-reset CSR read before any write, and a deterministic run whose final `GPIO_OUT`
  differs from `result_gpio`.
- **rvfi mode** runs `Iss3` in lockstep with the design.
  - Every RVFI field is compared (contract 8).
  - IO loads take the design's volatile bits (R13), and `mip.MEIP` comes from `rvfi_rd_wdata`
    (R14).
  - A retirement with `rvfi_intr = 1` that doesn't follow a trap is an interrupt entry, handled by
    `take_interrupt()`. It fails with `interrupt while masked` unless MIE and MEIE are both 1.
  - The run ends 2,048 cycles after the self-loop.
- **pins mode, deterministic program.**
  - uo_out must step through the reference run's GPIO states.
  - PSRAM writes must match its stores.
  - The final PSRAM must equal its written bytes.
  - The end is (tail + 2) x 2,048 cycles after the pins reach the final state, where tail is the
    reference's retirements after its last GPIO change.
- **pins mode, any other program.** It is judged by:
  - its self-check: uo_out = `result_gpio` (written with ALTFN = 0) held for 3 x 2,048 cycles, and
    0xEE fails at once;
  - the pin checkers;
  - the UART text.

  Without `result_gpio` the harness reports `pins mode can't judge this program`.

**`expect.json`** (optional; `run_cosim.py` passes `<name>.expect.json` when it exists):

| Field | Meaning |
|---|---|
| `result_gpio` | the final GPIO_OUT meaning pass, on all 8 uo pins (write it with ALTFN = 0) |
| `uart_div`, `uart_rx`, `uart_rate_ppm`, `uart_tx_expect` | `uart_rx` entries are `[cycle, byte]` or `[cycle, byte, ppm]`. The cycle counts from the anchor: in rvfi mode the first retirement; in pins mode the first edge with 4 flash bytes read, or reset release + 2,048. Bit edge k sits at round(k (DIV+1) (1 + ppm/10^6)), so positive ppm is slower. `uart_tx_expect` is the exact TX byte string. |
| `spi_div_d` | the SPI echo device on ui[3]. It replies with the previous byte (0xFF first) and holds each MISO bit from its rising SCK edge through 3 cycles after it (R20). |
| `pwm` | `[{ch, per, q, duty}]`, checked after GPIO_OUT changes to 0xA5. Period (PER+1) 2^Q, high min(DUTY, PER+1) 2^Q, measured exactly after one settling period, within 4 periods. `ch` 7 is the timer output on uo[7] (`per` 65535 without auto-reload). |
| `loop_uo7_to_ui0`, `gpio_script` | ui[0] follows uo[7]; ui_in from `[[cycle, value], ...]` relative to the anchor |
| `coremark_iters` | N: `coremark_cycles_per_iter` = cycles from uo_out = 0xC0 to uo_out = 0xC1, divided by N (R21) |

**Checks with their error texts.**

Pins and bus rules:
- Pmod chip models and bus rules every cycle (`pmod.h`, tCEM included): the model's message.
- Reset values: `during reset`.
- Outputs change only at rising edges: `outputs changed between rising clk edges`.
- PSRAM writes outside the core region carry bytes of stores (retired ones in rvfi mode, the
  reference's in pins mode): `matches no retired|reference store ... (contract 8)`.
- Final PSRAM: `final PSRAM byte`.

Latency cap (rvfi mode):
- 2,048 cycles, at the retirement and by a watchdog: `latency cap: retirement #N ... came G
  cycles after` and `latency cap: no retirement within 2048 cycles`.
- A gap next to a WFI is exempt.

GPIO (contract 6):
- In rvfi mode a pin may change while the store executes, and must change no later than 1 cycle
  after it retires: `one cycle after the store`.
- A change with no store behind it: `uo_out changed to`.

UART (5.5):
- Exact bit times: `not on a bit boundary`, `framing error`.
- A start bit within DIV+2 of the UART_DATA write (rvfi mode): `no UART start bit within DIV+2`.
- The sent byte is the written byte.
- A TX_BUSY read equals the frame in progress at the sampling time, with DIV+2 start latency and
  2 cycles of slack (R20): `TX_BUSY (UART_CTRL[13]) read`.

SPI (5.6):
- MOSI changes only while SCK is low: `MOSI (uo[6]) changed`.
- Phases are exactly 2^D: `high phase of`, `low phase of`.
- The MOSI byte is the SPI_DATA byte (rvfi mode): `SPI MOSI sent`.
- BUSY reads follow the pins.

PWM: the meter's message.

Self-check: `the program's self-check failed`.

**Self-tests.** `pytest verif/cosim/test_cosim_harness.py` runs the following.
- **Pin-model unit tests.** These use `build/cosim/test_periph`, built by `build.sh` with no
  arguments. The UART RX edges are recomputed exactly in Python.
- **Harness tests on `test_fixtures/playback`.** This stub replays a pin script (`+PINS=`) and an
  RVFI script (`+RVFI=`). The RVFI scripts come from hand-encoded programs run on
  `iss_main --v3 --log`. One build per mode covers:
  - UART bit time 17 against DIV 16 and DIV 15;
  - busy reads;
  - interrupt follow and masked interrupts;
  - PSRAM write attribution;
  - the latency cap at 2,048 and 2,049;
  - the self-loop drain window;
  - the GPIO 1-cycle rule;
  - CoreMark marks;
  - volatile programs in pins mode;
  - the SPI master pins.
- **The `mutants/tcem_stall` stub** (R12). PSRAM CS_n stays low past 320 cycles, and both modes
  fail with `tCEM`.
- **`run_cosim.py --v3`** and the v3 fuzz.

**Measured on the MCU seed.** The seed is setup/v3-seed 45a78ef, run with its own unit-test
programs (`src/test_v3/prog`) on hwe-bench, seeds 1-4, both modes. These programs are not the
plan's set:
- Passing: boot, uart_echo (+1 %), pwm (as a GPIO program), irq_timer (11 interrupts followed)
  and irq_latency.
- The trap programs pass in pins mode and fail in rvfi mode with `rvfi_intr = 0x0, expected
  0x1`. The seed doesn't set `rvfi_intr` on a trap handler's first instruction, which contract 8
  requires.
- csr is a `test program bug`: it reads mstatus (MPIE arbitrary) before any write.
- The rvfi-mode fuzz passes seeds 1-5. Each takes 28 s (2,056 sub-runs, worst gap 1,319).

**Contract v2, the core region.** RAM bytes 0xFF80-0xFFFF, in every 64 KB alias, may hold the
core's x1..x15, pc and scratch in a private layout. The checkers follow from that:
- Test programs never touch the region: the stack top is 0xFF80, and the reference ISS treats a
  fetch or RAM access touching it as unspecified, so `make -C verif/programs` fails on such a
  program.
- The cosim RAM model starts the region with random bytes (per seed), serves region transactions
  as plain memory (region WRITEs persist: they are the register file) and leaves the region out
  of the final-RAM comparison in both modes.
- Every SPI transaction must be a region transaction (all its bytes in the region, no wrap out)
  or a program transaction (no region byte). Region transactions are free; program transactions
  follow the per-instruction rules below.

## How to run

| Command | What it does |
|---|---|
| `make -C verif/programs` | builds every program: `build/programs/<name>.{elf,hex,expect.json}` |
| `python3 verif/cosim/run_cosim.py --all` | the `make cosim` gate: every program, both modes, seeds 1, 2, 3; writes `build/cosim.json`; exit 0 only if all pass |
| `run_cosim.py --mode rvfi --prog build/programs/selftest.hex --seed 2` | one run; prints `{"prog","mode","seed","pass","retired","cycles","cpi","max_cycles_per_insn","error"}` |
| `run_cosim.py ... --timing-json PATH` | rvfi mode: writes the timing report (below) |
| `run_cosim.py ... --dump-ram PATH` | writes the final 64 KB RAM image, raw bytes, as the design left it (the core region 0xFF80-0xFFFF included: the core's own bytes, random at reset) |
| `run_cosim.py ... --trace out.fst` | separate traced build, FST waveform |
| `run_cosim.py ... --src DIR` | runs another design directory (mutants) |
| `run_cosim.py --fuzz-liveness --seed S [--src DIR]` | the liveness fuzz (below), rvfi mode; prints `{"prog","mode","seed","pass","liveness_checked","runs","walks","probes_retired","halted","capped","retired","cycles","max_cycles_per_insn","error"}`; exit 0 on a pass |
| `pytest verif/cosim/test_iss.py` | ISS vs spike, ISS unit cases, program coverage |
| `pytest verif/cosim/test_cosim_harness_v2.py` | v2 harness self-tests: stand-in driver, design passes, mutants fail (the v3 ones: `test_cosim_harness.py`, above) |

`build.sh` compiles every `src/*.v` file, in sorted (byte) order, with nothing in front of them
(no prelude, no include path); the rvfi build's only define is `RISCV_FORMAL` (contract sections
1 and 6). It records the exact Verilator command in `build/cosim/<mode>/verilator.args`.
`run_cosim.py` rebuilds the Verilator model when anything that can reach it changes: it keys a
stamp on every file under the design directory except `__pycache__` and non-Verilog `test_*`
files, the harness sources and `verilator --version`. Builds hold
`build/cosim/<build dir>.lock` (lockf), so concurrent processes don't build the same model at
once. `--max-cycles` defaults to the latency cap's bound, 1,024 x (reference retirements + 16),
where the reference count is the harness's own ISS run under this mode's ui_in schedule (it
differs between modes for ui_in-dependent programs such as random_3 and random_6); tools/eval.py
passes no
`--max-cycles` (main 09cf61f). The budget bounds the program: up to the halt in rvfi
mode, up to the last store or uo_out change in pins mode. The observation windows after that
(4,096 cycles after the halt; (tail + 8) x 1,024 quiet cycles) don't count against it.

## Programs (`verif/programs/`)

| Program | Content |
|---|---|
| `selftest` | every RV32E instruction, every branch taken and not taken, x0 writes, JALR with bit 0 set, a not-taken branch to a misaligned target, misaligned LW/LH/LHU/SW/SH, accesses that end at 0xFF7F (the last byte below the core region; contract v1's 64 KB data wrap and LW at 0x7FFFFFFE touch the region, so they are unspecified now and left out), loads of the instruction's own bytes (aligned, and misaligned into the next instruction), data and fetch aliasing (pc 0x1xxxx and 0xABCDxxxx), every shift amount in all six shift forms, FENCE variants (TSO, PAUSE, nonzero rd/rs1, fm = 1111), GPIO reads and writes on every byte lane. Results go to a RAM signature area. Ends with 0xA5 (pass) or 0xEE (fail) on GPIO, then EBREAK. |
| `gpio_demo` | counts on uo_out, echoes ui_in XOR 0x5A, then stores a word, a halfword, two bytes and a misaligned word to RAM, loads them back (LW, misaligned LW, LB, LH, LHU) and shows them on uo_out, 297 instructions. For the TT test (`test/`, RTL and gate level), which sees only the pins: the stores put WRITE transactions under its RAM model. |
| `coremark` | CoreMark, ITERATIONS=1, TOTAL_DATA_SIZE=2000, -O2, rv32e, MEM_STATIC. Prints to the RAM log ring at 0xF000-0xF7FF; must print "Correct operation validated". 791,682 instructions. |
| `trap_ecall`, `trap_jal`, `trap_jalr`, `trap_branch`, `trap_zero`, `trap_ones` | one fatal trap each, after visible GPIO and RAM state; the code after the trap writes GPIO and RAM, so a core that doesn't halt is caught in both modes |
| `random_1` .. `random_6` | `gen_random.py SEED 3000`: valid-only random streams on all of x1..x15 (ALU, shifts, LUI/AUIPC; loads and stores with misaligned accesses and alias bits addr[30:16], all ending at or below 0xFF7F, RAM stores into 0x8000-0x8FFF and 0xFF70-0xFF7F; GPIO anywhere in the addr[31] = 1 space, byte accesses on every lane, wider ones aligned; forward branches, bounded loops closed by backward branches, JAL, JALR with bit 0 set, FENCE); registers dumped to 0x0000..0x003B (over the prologue) at the end |

Every program writes all registers before reading them (the ISS checks it: `iss_main --check-init`),
never executes a reserved or unspecified instruction, never touches the core region 0xFF80-0xFFFF
(the ISS flags that as unspecified too), and ends with a guard after its EBREAK. Memory layout
(`link.ld`): code and data from 0, the RAM log ring at 0xF000-0xF7FF, the stack from 0xFF80
down to 0xF800, the core region above it.
`<name>.expect.json` holds the ISS's retired count, halt and uo_out trace for the pins ui_in
schedule with seed 1 (what the gate-level test compares), the same for the RVFI schedule, and
`log_contains` for coremark.

## The reference ISS (`verif/cosim/iss.h`)

RV32E per contract sections 3 and 4: fetch at pc[15:0]; data RAM at (addr[15:0] + k) mod 2^16
when the effective address has bit 31 clear, for the whole access; GPIO when it is set (byte
lanes with X[1:0] = 0 read ui_in and write uo_out, others read 0 and ignore writes); the five
fatal traps retire with trap = halt = 1 and no register or memory write. `Retire::reserved` flags
everything the contract leaves unspecified: reserved encodings (including register fields
x16-x31, FENCE.I, CSR instructions, compressed encodings, M), misaligned IO accesses wider
than a byte, and (contract v2) any fetch or RAM data access touching the core region
0xFF80-0xFFFF, which also sets `Retire::region` (`iss_main` exits 2 with `"region": true`).
FENCE is any MISC-MEM encoding with funct3 = 0.

Register-field convention (contract section 6, "the register index the core actually read"):
LUI, AUIPC, JAL report no sources; JALR reports rs1; branches rs1 and rs2; loads rs1; stores rs1
and rs2; OP-IMM rs1; OP rs1 and rs2; FENCE, ECALL, EBREAK and the illegal words report nothing.
On a trapping retirement the harness compares only trap, halt, insn and pc_rdata, plus
rd_addr = 0 and mem_wmask = 0 (contract section 6), and on a trapping branch or JALR also the
rs1/rs2 addr and rdata (e321614: the trap depends on those operands). The other register-read
fields, rd_wdata, pc_wdata and the other memory fields are free there.

Cross-check: `test_iss.py` runs selftest, gpio_demo and coremark (2,039 + 300 + 791,686 =
794,025 retirements in their spike builds) on spike and on the ISS and compares pc, instruction,
register write, store address and data, and load address at every retirement. spike can't model
the 64 KB alias or GPIO, so those builds link at 0x10000, model IO as a RAM page in both
simulators, and skip selftest's top-of-RAM, alias and GPIO sections; unit cases cover those,
and the core region's unspecified accesses.

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
`--x-assign unique`, `randReset(2)`), the core region's 128 bytes randomized from the seed too
(contract section 3: its reset contents are arbitrary; `harness --region-zero` is a self-test hook
that leaves it zero), noise on MISO whenever SCK is low or no response bit is due, noise on every
unused `uio_in` bit, and a reset of 1 to 9 edges depending on the seed (seed 1 uses one edge).

A transaction's bytes, for classification: a READ's bytes with at least one bit clocked (a
partly clocked last byte counts), a WRITE's complete bytes. A READ with no data bit (CS_n rises
right after its 24-bit header) is a null READ: it addresses no byte and is free anywhere (contract
section 6, 78472fe). A shorter header still fails rule 4.

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
| WRITE carries a positive multiple of 8 data bits (region WRITEs too) | 2.5 | `WRITE ended mid-byte` |
| every transaction is a region transaction (every byte in 0xFF80-0xFFFF, no wrap out) or a program transaction (no region byte) | 6 | `wraps out of the core region`, `runs from program RAM into the core region` |
| final RAM image outside the region (0x0000-0xFF7F) equals the reference | 3 | `final RAM differs` |

rvfi mode (design built with `RISCV_FORMAL`):

| Check | Contract | Error text contains |
|---|---|---|
| every retirement: order, intr = 0, mode = 3, ixl = 1, insn, pc_rdata, trap, halt, rd_addr, mem_wmask; on a normal retirement also pc_wdata, rs1/rs2 addr and data, rd_wdata, mem_rmask, mem addr (when a mask is set), rdata/wdata on the masked lanes; on a trapping branch or JALR also rs1/rs2 addr and data (e321614) | 4, 6 | `RVFI mismatch at retirement #N (pc, insn): field = got, expected` |
| every SPI transaction belongs to the instruction that retires next and ends before its rvfi_valid; region transactions are free (any number, READ or WRITE, any order), also for a trapping instruction before it retires (contract section 4 as amended in 8df46d8: a trap makes no program-memory write, and nothing after the halt); null READs (CS_n rises right after the 24-bit header, no data bit) are free anywhere (section 6, 78472fe) | 4, 6 | |
| program transactions of an instruction: exactly one fetch READ of exactly its 4 bytes (A = pc[15:0], 32 data bits; the first such READ is the fetch); for a RAM load, READs that read only its bytes; for a RAM store, WRITEs that write only its bytes, each byte once and with the store's value (a store may be split); nothing else (IO accesses never appear on SPI; a null READ is neither a region nor a program transaction) | 3, 6 | `SPI READ/WRITE of byte ... is not part of retirement #N`, `the fetch READ of ... read beyond the instruction's 4 bytes`, `the fetch READ of ... read N data bits`, `a second fetch READ`, `a second time`, `the store writes 0x.. there` |
| CS_n is high in every rvfi_valid cycle | 6 | `SPI transaction still open at` |
| completeness at retirement (checked before the RVFI fields, so a bus bug is named as one): the fetch was seen, the load READs read every load byte in full (a load of the instruction's own bytes needs a READ of its own), every store byte was written | 6 | `retired without its fetch`, `retired without reading load byte`, `without writing store byte` |
| uo_out makes exactly the reference's changes, in order, each no later than one cycle after its IO store retires; any other change fails, including a glitch that returns to the old value before the store retires | 3 | `uo_out changed to`, `one cycle after the IO store retired` |
| latency cap: first retirement at most 1,024 cycles after reset release, each later one at most 1,024 after the previous; checked at the retirement edge (a retirement 1,025 cycles late fails) and by a watchdog (nothing retired by cycle 1,025 fails there) | 4 | `latency cap` |
| no retirement and no SPI WRITE (region included) for 4,096 cycles after the halt | 4 | `retirement after the halt`, `SPI WRITE started after the halt` |
| no rvfi_valid during reset | 6 | `rvfi_valid during reset` |

pins mode (the plain design):

| Check | Contract | Error text contains |
|---|---|---|
| program SPI WRITE bytes follow the reference's RAM stores in program order with the right values, each store byte exactly once (a store may be split over several WRITEs); region transactions are skipped | 3, 6 | `SPI WRITE of ... doesn't match the reference store`, `a second time for reference store` |
| uo_out changes follow the reference's change sequence | 3 | `uo_out changed to ...; the reference's change` |
| an SPI transaction completes at least every 2,048 cycles while stores or GPIO writes remain (every instruction is fetched, and retires within 1,024 cycles) | 4, 6 | `no SPI transaction for` |
| after the last store and GPIO change: (tail + 8) x 1,024 quiet cycles with no program WRITE (one whose address is outside the region; region WRITEs go on) and no uo_out change, where tail is the number of instructions after the reference's last observable effect | 4 | `SPI WRITE to 0x.... started after the reference program's last store` |
| CoreMark (and any program whose expect file lists `log_contains`): the strings are in the RAM log ring at 0xF000 | | `RAM log ring lacks` |

## Liveness fuzz (`--fuzz-liveness`)

Contract section 4: a core must retire every instruction within 1,024 cycles of the previous one,
or trap and halt, also on a reserved encoding (whatever effect it gives it). The programs contain
no reserved encoding, so the program runs can't see a core that hangs on one (mutant
`hang_reserved`, the formal agent's: custom-0 stays in EXEC forever). The fuzz runs random images
with no reference and checks only liveness and the pin rules, since a random program wanders,
writes RAM and touches the core region, all of it unspecified.

```
python3 verif/cosim/run_cosim.py --fuzz-liveness --seed S [--src DIR] [--mode rvfi|pins]
                                 [--fuzz-runs K] [--fuzz-cap N]
```

Defaults: rvfi mode, K = 2,048 probe sub-runs plus K/256 = 8 walk sub-runs, N = 2,000
retirements. Each sub-run starts from reset (1 to 9 edges, random power-up state, random core
region) on its own image of RAM 0x0000-0xFF7F:

- **probe sub-run k:** 0 to 8 valid, non-trapping, non-control instructions (random x0..x15
  registers, so loads and stores hit random addresses, IO and region included; misaligned ones
  don't trap), then one probe word, then random words to 0xFF7F (half arbitrary, half with
  bits [1:0] = 11). The probe's bits [6:0] are entry k mod 128 of a per-seed shuffle of all 128
  values (low bits other than 11 included). On visit v = k / 128 of that value, funct3 is
  (v + a per-seed offset) mod 8, the register fields name x0..x15 when v mod 16 < 8 and are
  random (x16-x31 included) otherwise, and funct7 is class (2 (v / 8) + S) mod 4 of {random,
  0000000, 0100000, 0000001}; the other bits are random. So one seed probes every (bits [6:0],
  funct3) pair twice, once with each register class, and seeds 1 to 4 together add every funct7
  class. The probe is never 0x00000000, 0xFFFFFFFF, ECALL or EBREAK. A strict-decode core traps at
  the probe; a spec-minimum decoder runs on into the random words.
- **walk sub-run:** LUI x15, 0xC at 0, then valid, non-trapping words everywhere: the probe
  prefix's mix plus branches and JALs with multiple-of-4 offsets, rd never x15, every store based
  on x15 (bytes 0xB800-0xC7FF). The core wanders, loops included, until the retirement cap, or
  until a jump lands on a word a store overwrote or in the region, where it may trap. These cover
  the cap path and long runs.

Checks, rvfi mode: the latency cap with contract section 4's count, at each retirement and by a
watchdog (the error names the last retirement and the word at the next pc); no retirement and no
SPI WRITE start (region included) in the 4,096 cycles after the halt; no rvfi_valid during reset.
Both modes: the waveform and framing rules (spiram.h, null READs included), constant `uio_oe` and
unused outputs, reset values, outputs that change only at rising clk edges. A sub-run ends at the
halt (after the 4,096-cycle window) or at the cap; the first failing sub-run stops the run.

**pins mode can't check liveness.** It sees no retirement and no halt, and a halted core and a
hung one both go quiet, so it checks only the pin rules, for 32 x 1,024 cycles per sub-run, and
reports `"liveness_checked": false` (`hang_reserved` passes it). The eval must run rvfi mode; the
equivalence gate ties the plain build to the RVFI build.

JSON keys: `prog` ("fuzz_liveness"), `mode`, `seed`, `pass`, `liveness_checked` (true in rvfi
mode), `runs` (sub-runs started, probes and walks; fewer than K + K/256 after a failure),
`walks`, `probes_retired` (probe sub-runs whose probe word retired, at its address; null in pins
mode), `halted`, `capped` (sub-runs that reached the cap), `retired`, `cycles` (summed per
sub-run up to its halt or cap), `max_cycles_per_insn` (null in pins mode), `error` (null on a
pass; else `fuzz sub-run k (probe word 0x... at 0x..., opcode 0x.., funct3 f): cycle c: ...` or
`fuzz sub-run k (walk w): ...`).

Measured with seeds 1 to 5 (the CLI as above, one process per seed; wall clock for all five from
`test_fuzz_liveness_passes`, cached builds, other jobs on the machine):

| Design | probes retired per seed | capped walks per seed | worst gap | 5 seeds |
|---|---|---|---|---|
| seed (strict decode) | 2,047-2,048 of 2,048 | 1-3 of 8 | 230 | 4.0 s |
| `legal_h002_rf` | 2,048 | 1-5 of 8 | 823 | 8.7 s |
| `legal_h002_pcram` | 2,048 | 1-5 of 8 | 991-993 | 9.2 s |

`hang_reserved` fails every seed (sub-runs 92, 105, 7, 30 and 107 for seeds 1 to 5, each at an
opcode 0x0b probe) and `hang_funct3` (LOAD with funct3 011 and x0..x15 registers stays in EXEC)
fails every seed too. One pins-mode seed on the seed core takes about 7 s (67 M cycles).

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
Cycles count rising clk edges, exactly as contract section 4 defines the latency cap's count (main
09cf61f): edge 0 is the last edge at which rst_n is sampled low, a retirement happens at the edge
after which rvfi_valid is high, the first must come at an edge numbered 1,024 or lower and each
later one at most 1,024 edges after the previous. The harness starts its count at that last reset
edge (`last_ret = reset_edges - 1`); the first retirement's latency is its edge number and each
later latency the number of edges since the previous retirement. Task 5's formal liveness check
counts the same way (gap = 1 in the step after the last reset step). `max_cycles_per_insn` is the
largest latency, `per_class` the largest per instruction class
(a trapping instruction counts as `trap`; `branch_taken` and `branch_not_taken` by outcome;
`io_load`/`io_store` are accesses with address bit 31 set). A class absent from `per_class` was not
executed. `cycles` in the summary is the halt's latency from reset release; `cpi` is
cycles / retired. In pins mode `cpi`, `max_cycles_per_insn` and the timing report are null, since
retirements are invisible, and `cycles` is the cycle of the last observable event.

## Harness self-tests

`verif/cosim/mutants/standin/` is a fixed-sequence SPI driver (not a core) with one `BUG_*` define
per broken rule; `harness --spi-only N` runs it without an ISS. `verif/cosim/mutants/<name>/` are
copies of the seed (or of the hyp/002 contract v2 designs) with one deliberate bug each, listed
with their mode and expected error in `mutants/MANIFEST.json`. The legal variants must pass:
`legal_trap_fields` (arbitrary rd_wdata and pc_wdata on trapping retirements, and arbitrary
register-read fields on the traps that read no register), `legal_null_read` (a null READ at the
IO address before every IO load), `legal_mosi_echo`
(no header gate on the shift-in, so after a 56-bit transaction's last bit, while CS_n is still
low, MOSI carries what MISO held during the header: noise in the cosim, X in the TT test; the
contract constrains MOSI only at rising SCK edges), and for contract v2 `legal_split_store` (the
seed with SW split into two 2-byte WRITEs), `legal_h002_rf` (hyp/002-rf-in-ram fc07cc3: x1..x15
at 0xFFC0 + 4r) and `legal_h002_pcram` (hyp/002-rf-in-ram-pcram b9a5211: the pc in the region
too, read before the fetch, scratch at 0xFF80, SW split in two), the two real v2 designs, copied
unchanged below a header.

Contract v2 mutants, each failing with its own message:

| Mutant | Bug | rvfi | pins |
|---|---|---|---|
| `region_wrap` | hyp/002-rf-in-ram with register slots at 0xFFC2 + 4r: x15's transactions run 0xFFFE..0x0001 | `wraps out of the core region` | same |
| `region_straddle` | hyp/002-rf-in-ram with its scratch slot at 0xFF7E: 0xFF7E..0xFF81 | `runs from program RAM into the core region` | same |
| `store_twice` | every RAM store's WRITE is sent twice | `a second time` | same |
| `store_extra_byte` | SB writes a second byte | `is not part of` | `doesn't match the reference store` |
| `fetch_extra_byte` | the fetch READ reads a 5th byte | `read beyond the instruction's 4 bytes` | `no SPI transaction for` |
| `load_skip_byte` | LW reads only 3 of its bytes | `retired without reading load byte` | `doesn't match the reference store` |
| `region_zero_init` | hyp/002-rf-in-ram-pcram without its boot write: the pc comes from the region's reset contents | `retirement #0` | `no SPI transaction for` |
| `null_read_short` | `legal_null_read` with a 23-bit header | `before its command and address were complete` | same |
| `trap_rs_garbage` | arbitrary rs fields on every trap, a trapping branch or JALR included (e321614) | `RVFI mismatch` (`rvfi_rs1_addr`) | passes |
| `hang_reserved` | custom-0 (0001011) stays in EXEC forever (setup/formal's mutant) | passes the programs; the fuzz fails it (`latency cap`, `opcode 0x0b`) | passes |
| `hang_funct3` | LOAD with funct3 011 and x0..x15 registers stays in EXEC forever | passes the programs; the fuzz fails it (`latency cap`) | passes |

MANIFEST entries with `"fuzz_fail"` must fail the rvfi-mode fuzz in seeds 1 to 5 with
`latency cap` and that text. `test_fuzz_liveness_passes` runs the fuzz (seeds 1 to 5) on the seed
and both hyp/002 designs, and `test_fuzz_pins_mode_cannot_check_liveness` shows pins mode passing
`hang_reserved` with `"liveness_checked": false`.

`test_region_starts_random` shows `region_zero_init` passes with `--region-zero` and fails with
the random fill (seeds 1 to 3). `test_v2_designs_pass_the_full_matrix` runs `run_cosim.py --all`
(every program, both modes, seeds 1 to 3: the eval cosim gate's 90 runs) on both hyp/002 designs.

`stall_gap` holds every retirement until exactly
`STALL_GAP` cycles after the previous one: `test_latency_cap_boundary` builds it with 1,024
(passes, timing report 1,024 everywhere), 1,025 (fails at retirement #0, and at #1 with
`STALL_SKIP_FIRST`) and 1,026 (the watchdog fails at cycle 1,025).
`test_core_at_the_cap_passes_as_eval_runs_it` runs it at 1,024 with `STALL_SKIP_FIRST` on every
random program whose retired count depends on the ui_in schedule (random_3 and random_6 since the
v2 generator) in both modes, seeds 1 to 3, invoked as `tools/eval.py` invokes the cosim (no
`--max-cycles`); it must pass.

`test_tt_test_on_variants` runs the TT test (`make -C test`, RTL) on the seed and the legal
variants (must pass) and on pin-visible mutants (must fail with their message: the RAM
model's rule, or the uo_out sequence). The TT test checks gpio_demo's uo_out sequence,
`uio_oe`, CS_n and SCK known, `uio_out[7:4]` and `[2]` zero, and the RAM model's waveform
rules; MOSI is left to the model's rising-edge checks. Bugs that only show against RVFI or
the retirement timing (`uo_late`, `cs_open_at_retire`, `load_own_fetch`) pass it by design, and so
does `rvfi_hidden` (gpio_demo runs no SLTU/SLTIU); the cosim catches them.

The RAM model (`test/spiram_23lc512.v`) also checks the contract section 6 transaction classes
that the pins alone show, so a wrap or a double write that synthesis introduces fails the
gate-level test: every byte a transaction addresses (a READ's partly clocked last byte included;
a null READ addresses none) is in the region or none is (`wraps out of the core region`,
`runs from program RAM into the core region`); a program READ addresses at most 4 bytes
(`addresses a fifth byte`); and between two program READs (each instruction starts with its
fetch) program WRITEs write at most 4 bytes, none twice (`written a second time since the last
program READ`, `a fifth program byte`). `region_wrap`, `region_straddle`, `store_twice`,
`fetch_extra_byte` and `null_read_short` fail it with those messages; `legal_null_read` passes.

## Speed

Measured on this Mac with the seed core (CoreMark: 791,682 instructions, 103.6 M cycles, CPI
130.8): 22.0 M cycles/s in rvfi mode and 25.8 M cycles/s in pins mode (4.7 s and 4.0 s per CoreMark
run, single run on a quiet machine; an earlier run under load measured 11.0 and 14.6). The full
`--all` matrix (15 programs x 2 modes x 3 seeds, 90 runs) took 13.1 s wall clock with 8 parallel
jobs, starting from no builds (programs and both Verilator models included). On the contract v2
designs, with cached builds: hyp/002-rf-in-ram 43.4 s (CoreMark CPI 508.5, worst retirement gap
in the 90 runs 805), hyp/002-rf-in-ram-pcram 58.2 s (CoreMark CPI 741.0, worst gap 893).
