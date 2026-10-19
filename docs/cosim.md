# Cosim: reference ISS, test programs, pin-level harness

These are contract gates (`verif/` is on the don't-touch list). They check a design in `src/`
against `docs/contract.md` (contract v3): the QSPI Pmod rules (section 2), the memory map and
coherence (3), the EEI with traps and the interrupt (4), the peripherals (5, 6) and RVFI (8). The
contract v2 harness (`harness_v2.cpp`, the 23LC512 RAM model `spiram.h`, the v2 ISS `iss.h`), its
mutants and its self-tests were deleted at the integration swap (plan Task 9, ruling R26); git
history has them.

## The harness (`verif/cosim/harness.cpp`)

`build.sh MODE` builds `harness.cpp` with `pmod.h` (the two Pmod chips), `iss3.h` (the reference
ISS) and `periph_models.h` (the peripheral pin models), in two modes: rvfi (the design built with
`RISCV_FORMAL`, the ISS in lockstep) and pins (the plain design).

```
harness <prog.hex> [--seed S] [--expect PATH] [--clk-ns 25] [--max-cycles C] [--trace PATH]
        [--dump-psram PATH] [-v]
harness --fuzz-liveness K [--fuzz-cap N] [--seed S]
run_cosim.py --mode rvfi|pins --prog P [--seed S] [--src DIR] [--clk-ns 25]
run_cosim.py --all [--src DIR] [--jobs N] [--no-make]
```

`--all` covers every program `make -C verif/programs list` reports (32 today: the plan's 26
plus gpio_demo, uart_echo_fast and the full-range pwm10, pwm10q, timer16 and ir_gate; the plan's 26 must be among them, else a setup error). Each
runs in both modes at seeds 1-4, and the rvfi-mode fuzz runs at seeds 1-5. The result goes to
`build/cosim.json` with `"contract": "v3"`. That is the `make cosim` gate: `tools/eval.py` runs
it once and re-checks the json (docs/eval.md). `--v3`, the flag that selected this harness before
the swap, is still accepted and does nothing.

**Flash image and seeds.**
- `prog.hex` is a sparse `$readmemh` flash image. Unset bytes are 0xFF. It is loaded once into a
  16 MiB buffer: `PmodBus` reads it, and `Iss3` takes its own copy.
- The seed picks the Pmod power-up mode, as `(seed - 1) % 4`: standard or continuous-read flash,
  times PSRAM in SPI or QPI mode.
- The seed also picks the reset length: `1 + ((seed + 8) * 5) % 9` edges, so seed 1 uses one.

**Summary JSON (the last stdout line).**
- `pass`, `retired`, `cycles`, `cpi`, `max_cycles_per_insn`, `first_retire_cycle`.
  - In rvfi mode, `cycles` runs to the self-loop's retirement.
  - In pins mode, `cycles` and `cpi` are the time until the final pin state: the pins reach the
    reference's final GPIO state (deterministic programs), or `result_gpio` first shows (other
    programs). pins mode can't see the self-loop.
- `end`: `selfloop`, `result_gpio` or `fail`.
- `uart_tx`, `error`, `psram_writes`.
- `coremark_cycles_per_iter`: an integer (`%.6f` when N doesn't divide), or null without marks.
  Never 0.
- Extra keys:
  - `mode`, `deterministic`, `per_class`, `interrupts`, `uo_trace`, `reset_edges`;
  - `sim_cycles`: every edge simulated, the drain included;
  - `sim_seconds`: the wall-clock time.

`run_cosim.py` adds `coremark_cycles_per_iter` to its KEYS.

**Judging a program.** Before simulating, the harness runs the program alone on `Iss3`. The
program counts as *deterministic* if that run reaches its self-loop (`jal x0, 0` retired twice)
without reading a volatile IO bit and without enabling interrupts (`mstatus.MIE` and `mie.MEIE`
both 1).
- **Test program bugs.** These fail before simulation:
  - a reserved or unspecified step;
  - a register or arbitrary-reset CSR read before any write;
  - a deterministic run whose final `GPIO_OUT & ~final_altfn` differs from
    `result_gpio & ~final_altfn`, or whose final ALTFN isn't `final_altfn`.
- **rvfi mode** runs `Iss3` in lockstep with the design.
  - Every RVFI field is compared (contract 8). On a trap that includes `rd_addr = 0`,
    `rd_wdata = 0` and both masks 0.
  - IO loads take the design's volatile bits (R13), and `mip.MEIP` comes from `rvfi_rd_wdata`
    (R14).
  - A retirement with `rvfi_intr = 1` that doesn't follow a trap is an interrupt entry, handled by
    `take_interrupt()`. It fails with `interrupt while masked` unless MIE and MEIE are both 1.
  - The contract 4 clause "the core takes the interrupt before retiring more than one further
    instruction" is not checked. MEIP isn't observable outside reads of mip and IRQ_PEND.
  - The run ends 2,048 cycles after the self-loop.
  - Every PSRAM write outside the core region must carry a byte of a retired store (contract 8).
  - Every byte the program stored is compared at the end.
- **pins mode, deterministic program.**
  - uo_out must step through the reference run's GPIO states.
  - PSRAM writes must match its stores (contract 8), and the final PSRAM must equal its written
    bytes.
  - UART frames and SPI transfers carry its UART_DATA / SPI_DATA bytes, in order and in number
    (R33). Only writes made while ALTFN shows the port count.
  - The end is (tail + 2) x 2,048 cycles after the pins reach the final state, where tail is the
    reference's retirements after its last GPIO change.
- **pins mode, any other program.** It is judged by:
  - its self-check (R24): the pins outside `final_altfn` show `result_gpio` for 3 x 2,048 cycles,
    and 0xEE on those pins fails at once;
  - the pin checkers;
  - `uart_tx_expect` and `spi_tx_expect`, which need `uart_div` / `spi_div_d` to decode.

  There is no reference run, so neither PSRAM attribution (contract 8) nor a memory compare
  applies. Without `result_gpio` the harness reports `pins mode can't judge this program`.

**Inputs.**
- ui[1] (UART RX) idles high wherever no UART RX model drives it: before the anchor, as GpioStim's
  value before its first entry, and in the noise driven for deterministic programs.
- A `gpio_script` value is driven as written.
- The anchor:
  - rvfi mode: the first retirement;
  - pins mode: the first edge with 4 flash bytes read, or reset release + 2,048.

**`expect.json`** (optional; `run_cosim.py` passes `<name>.expect.json` when it exists).
Malformed fields are setup errors (exit 2, no JSON):

| Field | Meaning |
|---|---|
| `result_gpio`, `final_altfn` | The final GPIO_OUT meaning pass, compared on the pins in GPIO mode at the end: `(uo ^ result_gpio) & ~final_altfn` (R24). `final_altfn` defaults to 0. Setup errors: `result_gpio & ~final_altfn == 0`, and `final_altfn = 0xFF`. |
| `uart_div`, `uart_rx`, `uart_rate_ppm`, `uart_tx_expect` | `uart_rx` entries are `[cycle, byte]` or `[cycle, byte, ppm]`, with the cycle counted from the anchor. Bit edge k sits at round(k (DIV+1) (1 + ppm/10^6)), so positive ppm is slower. `uart_tx_expect` is the exact TX byte string. |
| `spi_div_d`, `spi_tx_expect` | `spi_div_d` (1..7; R29: D ≥ 1) puts the SPI echo device on ui[3]. It replies with the previous byte (0xFF first) and holds each MISO bit from its rising SCK edge through 3 cycles after it (contract 5.6, c7d9910). `spi_tx_expect` gives the MOSI bytes in order, as a string or a list of bytes. |
| `pwm` | `[{ch, per, q, duty}]`, checked after the GPIO-mode pins show 0xA5. Period (PER+1) 2^Q, high min(DUTY, PER+1) 2^Q, measured exactly after one settling period, within 4 periods. `ch` 7 is the timer output on uo[7] (`per` 65535 without auto-reload, `q` = P, `duty` = CMP). Extra keys (`pin`) are ignored. |
| `loop_uo7_to_ui0`, `gpio_script` | ui[0] follows uo[7]; ui_in from `[[cycle, value], ...]` relative to the anchor |
| `ir_gate` | `{per, q, duty, marks, within}` (opt-in, contract 5.2): after the GPIO-mode pins show 0xA6, uo[1] must carry the PWM0 carrier (period (PER+1) 2^Q, high DUTY 2^Q) exactly while the timer output uo[7] is high and stay low while it is low, for `marks` marks within `within` cycles (periph_models.h IrGateMeter; program ir_gate) |
| `coremark_iters` | N: `coremark_cycles_per_iter` = cycles from the GPIO-mode pins showing 0xC0 to their showing 0xC1, divided by N (R21, R24) |
| `max_cycles` | N (positive): the run budget (R35; below) |

**Run budget** (R35). The run fails with `timeout after N+1 cycles (<source>)` past its budget,
taken in this order:
1. `--max-cycles` on the harness command line (`run_cosim.py --max-cycles` passes it through, and
   only when given: `--all` never passes it);
2. expect.json `max_cycles`;
3. the default: 2,048 x (reference retirements + 16) for deterministic programs (the latency cap
   bounds a correct run), else 50,000,000 cycles.

The 0xA5 and 0xC0/0xC1 marks are looked for only when `pwm` / `coremark_iters` is present. They
are masked with ALTFN:
- rvfi mode: the ISS's ALTFN;
- pins mode, deterministic programs: the tracked state's ALTFN;
- pins mode, other programs: `final_altfn` plus the `pwm` pins.

Task 4's `psram_window` isn't read. Every byte the program stored, outside the core region, is
compared instead (a superset in rvfi mode and for deterministic programs).

**Checks with their error texts.**

Pins and bus rules:
- Pmod chip models and bus rules every cycle (`pmod.h`): the model's message, `Pmod protocol
  violation at cycle N: ...`. Among them, at the run's `--clk-ns` T:
  - tCEM (2.1.7) and tRST (2.3);
  - the CS_n high time between two transactions of a chip (2.1.2): `CS_n high time: PSRAM CS_n
    high for 1 cycle(s) between transactions, needs ceil((18 ns tCPH + 4 ns pad allowance) /
    20 ns) = 2 cycles` (the flash: 10 ns tSHSL1). Each chip on its own CS_n; the first fall of
    a run is not measured;
  - the continuous-read entry (2.2): `flash continuous-read entry: CS_n rose after M[5:4] = 10
    with 2 of 2 M edges, 0 of 4 dummy edges and 0 of 2 data edges; ...`, once the first M
    nibble has M[5:4] = 10 and CS_n rises before the 4 dummy edges and one data byte.
  - Measured on champion 112 (fa98a50, `make cosim` at 20 ns on hwe-bench, 2026-10-19): all
    256 runs and the 5 fuzz seeds fail with the continuous-read entry message (the boot's entry
    transaction, harness cycle 162). With that check disabled in a scratch copy, all
    261 fail with the PSRAM CS_n high time (1 cycle, harness cycle 1,290 in selftest).
- Reset values: `during reset`.
- Outputs change only at rising edges: `outputs changed between rising clk edges`.
- PSRAM writes outside the core region carry bytes of stores (retired ones in rvfi mode, the
  reference's for deterministic programs in pins mode): `matches no retired|reference store ...
  (contract 8)`.
- Final PSRAM: `final PSRAM byte`.

Latency cap (rvfi mode, R31):
- 2,048 cycles, at the retirement and by a watchdog: `latency cap: retirement #N ... came G
  cycles after` and `latency cap: no retirement within 2048 cycles`.
- Only the gap that starts at a WFI's retirement is exempt (WFI retires, then may stall).
- Neither the gap before a WFI nor an interrupt entry before one is exempt.
- In the fuzz, a sub-run that ends in a WFI stall passes.

GPIO (contract 6):
- In rvfi mode a pin may change while the store executes, and must change no later than 1 cycle
  after it retires: `one cycle after the store`.
- A change with no store behind it: `uo_out changed to`.
- In rvfi mode, a GPIO_IN load returns the ui_in the design saw at an edge in [p+1-3, t] (R30;
  p = the previous retirement, t = this one): `GPIO_IN read`.

UART (5.5):
- Exact bit times: `not on a bit boundary`, `framing error`.
- In rvfi mode (ALTFN known) a stop bit of 0 fails at once: `stop bit of 0`.
- rvfi mode, soft window: while a store that clears ALTFN[0] executes (from the previous
  retirement to its own), the pin may take its GPIO value early (contract 6). A frame cut there
  (a stop bit of 0 or a change off the bit grid) is abandoned, not failed. From the store's
  retirement the gate is 0, and a frame in progress is abandoned too.
- pins mode, deterministic programs: a stop bit of 0, followed by the line staying low to the end,
  is no byte (the pin may have gone back to GPIO 0). A rise after it is a framing error.
- pins mode, other programs (lenient; the program's ALTFN writes are unknown): a stop bit of 0
  followed by a rise is abandoned too (the pin may have gone to GPIO 0 and then 1). Such a
  program's bytes are judged by `uart_tx_expect`. Without it, an abandoned frame fails at once:
  `no uart_tx_expect to judge the bytes`.
- A start bit at most DIV+2 cycles after the UART_DATA store's retirement edge (rvfi mode, R28,
  7c5740e; no slack): `no UART start bit within DIV+2`.
- The sent byte is the written byte: rvfi mode via the retired store, pins mode via the reference
  run (R33).
- A frame no write started (rvfi mode): `is not a UART_DATA write`.
- TX_BUSY and IRQ_PEND.TXIDLE (= !TX_BUSY) reads (R20, R30): `TX_BUSY (UART_CTRL[13]) read`,
  `TXIDLE (IRQ_PEND[3], = !TX_BUSY) read`. A read must equal the frame in progress at the sampling
  time, allowing the DIV+2 start latency and 2 cycles of slack.

SPI (5.6):
- MOSI changes only while SCK is low: `MOSI (uo[6]) changed`.
- Phases are exactly 2^D: `high phase of`, `low phase of`.
- The MOSI byte is the SPI_DATA byte: `SPI MOSI sent`. rvfi mode checks the retired store, pins
  mode the reference run.
- BUSY reads follow the pins.
- An SPI_DATA read after a transfer equals the byte the device drove (rvfi mode): `SPI_DATA read`.

End of the run (rvfi mode): no UART frame, SPI transfer or started write may still be in flight:
`still in progress at the end of the run`.

PWM: the meter's message. In rvfi mode, clearing ALTFN before the measurement ends fails:
`ALTFN bit N was cleared`.

Self-check: `the program's self-check failed`.

**Self-tests.** `pytest verif/cosim/test_cosim_harness.py` (44 tests) runs the following.
- **Pin-model unit tests.** These use `build/cosim/test_periph`, built by `build.sh` with no
  arguments. The UART RX edges are recomputed exactly in Python.
- **Harness tests on `test_fixtures/playback`.** This stub replays a pin script (`+PINS=`) and an
  RVFI script (`+RVFI=`). The RVFI scripts come from hand-encoded programs run on
  `iss_main --log`. One build per mode covers:
  - UART bit time 17 against DIV 16 and DIV 15;
  - the start bit at DIV+2 (passes) and DIV+3 (fails);
  - the stop bit of 0 (strict, rvfi mode), the lenient cut frame (pins mode) and the soft window
    while an ALTFN[0] clear executes;
  - a wrong UART byte against the reference run (pins mode);
  - orphan frames;
  - TX_BUSY and TXIDLE reads;
  - interrupt follow and masked interrupts;
  - mip.MEIP from `rvfi_rd_wdata`;
  - PSRAM write attribution;
  - the latency cap at 2,048 and 2,049;
  - the WFI exemption;
  - the self-loop drain window;
  - the GPIO 1-cycle rule;
  - the GPIO_IN window;
  - `result_gpio` / `final_altfn` and the degenerate setup errors;
  - 0xEE fast fail;
  - CoreMark marks;
  - volatile programs in pins mode;
  - the run budget: expect.json `max_cycles`, `--max-cycles` over it, and `run_cosim` passing
    neither by default (R35);
  - the SPI master pins, `spi_tx_expect` and the reference bytes;
  - SPI_DATA against MISO;
  - an SPI transfer in flight at the end;
  - `spi_div_d` 0;
  - the PWM path (mark, (PER+1)·2^Q, settle and deadline, ALTFN cleared early).
- **The `mutants/tcem_stall` stub** (R12). PSRAM CS_n stays low past 320 cycles, and both modes
  fail with `tCEM`.
- **Cell mutants** (contract section 1, cell instances). The playback stub with its `uo_out`
  register built from `sky130_fd_sc_hd__sdfxtp_1` scan flops, on the GPIO store scenario:
  - SCE = load, SCD = new value, D = Q: passes in both modes under seeds 1-4, and the build reads
    `verif/cells`;
  - SCD and D exchanged (the select polarity wrong): fails in both modes;
  - no reset folded in: fails under all four seeds (uo_out not 0 during reset), and passes when
    built with `--x-initial 0`, so the randomized cell state is what catches it.
- **Design submodule power-up** (invariant 6, `noinline.vlt`). The playback stub with its `uo_out`
  register built from 8 unreset `r32v_ureg` submodules (`output reg q`, each tied to a bit-select)
  fails under all four seeds (uo_out not 0 during reset). It passes when built with
  `--x-initial 0`, and when a test-only configuration file forces the inlining back, so
  `noinline.vlt` is what catches it. The test also checks that the build reads `noinline.vlt`.
- **`run_cosim.py`** (with and without the old `--v3` flag, now a no-op), the program-list rule,
  and the fuzz.

**Measured on the MCU seed** (fix round 2).
- **Setup.**
  - Design: setup/v3-seed 133d76b.
  - Programs: Task 4's setup/v3-programs 2bab5a2, built on the Mac (`make list`: 28 programs).
  - That run used 2bab5a2's `verif/cosim/iss3.h` (R32: mstatus.MPIE resets to 0), which the
    merged tree will carry. With this branch's older iss3.h, every C program stops at once as a
    setup error, because crt0 reads mstatus before writing MPIE.
  - Command: `run_cosim.py --all --no-make --src <seed src> --jobs 6` on hwe-bench (then spelled
    `--all --v3`).
  - Wall clock: 271 s for 224 runs and 5 fuzz seeds.
- **Results.**
  - 224 of 224 runs pass: all 28 programs, 8 of 8 each (2 modes x 4 seeds).
  - The rvfi-mode fuzz passes seeds 1-5: 2,056 sub-runs each, 2,048 probes retired, worst gap
    1,319.
- **Timing.**
  - Worst non-exempt retirement gap: 1,203, and 1,716 in wfi.
  - CPI in rvfi mode: 709-830.
  - Run lengths: irq_stress 15.8M cycles (inside the 50M default; R35 lets it declare its own);
    uart_echo 2.1M; CoreMark 605M.
  - CoreMark: `coremark_cycles_per_iter` = 578,875,473 (ITERATIONS = 1).

## How to run

Verilator runs on hwe-bench, never on the Mac (ruling R11): wrap these in
`tools/hwe_run.sh [--pull PATH] -- ...`.

| Command | What it does |
|---|---|
| `make -C verif/programs` | builds every program: `build/programs/<name>.{elf,hex,expect.json}` (either host; the images are byte-identical) |
| `make cosim` | the gate: `tools/eval.py --gate cosim`, which runs `run_cosim.py --all --no-make --jobs N` after `make -C verif/programs` and judges `build/cosim.json` |
| `python3 verif/cosim/run_cosim.py --all [--jobs N]` | the same runs without eval's re-check; exit 0 only if all pass |
| `run_cosim.py --mode rvfi --prog build/programs/selftest.hex --seed 2` | one run; prints `{"prog","mode","seed","pass","retired","cycles","cpi","max_cycles_per_insn","coremark_cycles_per_iter","error"}` |
| `run_cosim.py ... --timing-json PATH` | rvfi mode: writes the timing report (below) |
| `run_cosim.py ... --dump-ram PATH` | writes the final 8 MiB PSRAM image (raw bytes from PSRAM byte 0) as the design left it, the core region included |
| `run_cosim.py ... --max-cycles C` | overrides the run budget (R35) |
| `run_cosim.py ... --trace out.fst` | separate traced build, FST waveform |
| `run_cosim.py ... --dump-pins PATH` | writes every uo_out change as `cycle hex` lines, then `cycle end` (observation only; sdk/tests decodes the IR NEC frames from it) |
| `run_cosim.py ... --src DIR` | runs another design directory (mutants, candidates) |
| `run_cosim.py --fuzz-liveness --seed S [--src DIR]` | the liveness fuzz (below), rvfi mode; prints `{"prog","mode","seed","pass","liveness_checked","runs","walks","probes_retired","ended","capped","wfi_stalls","retired","cycles","max_cycles_per_insn","error"}`; exit 0 on a pass |
| `pytest verif/cosim/test_iss.py` | the reference ISS's unit cases (needs riscv32-unknown-elf-gcc) |
| `pytest verif/cosim/test_cosim_harness.py` | the harness self-tests (above) |
| `pytest verif/cosim/test_pmod.py` | the Pmod chip models' unit tests and C API |

`build.sh` compiles every `src/*.v` file, in sorted (byte) order, with nothing in front of them
(no prelude, no include path); the rvfi build's only define is `RISCV_FORMAL` (contract sections
1 and 8). After the design it reads the behavioral models of the sky130 cells `src/` may
instantiate (`verif/cells/*.v`, one per `CELL_WHITELIST` entry in `tools/src_check.py`, sorted);
`--top-module` drops the unused ones. Then comes `verif/cosim/noinline.vlt`, a Verilator
configuration file (below). It records the exact Verilator command in
`build/cosim/<mode>/verilator.args`.

**Every flop powers up random (invariant 6).** Verilator runs with `--x-initial unique`, and the
harness seeds the random values from `--seed`. One case escaped that until `noinline.vlt`:
- **The gap.** When Verilator inlines a module and the parent connects one of its `output reg`
  ports to a bit-select, the port becomes a `__Vcellout` temporary that Verilator initializes to
  0, not at random. A design relying on that flop's power-up value passed the cosim.
  - Measured: the playback stub with its `uo_out` register made of 8 unreset `r32v_ureg`
    submodules (`output reg q`, each tied to `uo_r[k]`) passed under seeds 1-4 with 5c6e3be's
    `build.sh`.
  - The same register as a plain `reg` in the top failed under all four seeds.
- **The fix.** `noinline.vlt` holds `no_inline -module "r32v_*"`. Every design submodule (contract
  section 1 names them all `r32v_*`; the top is never inlined) stays its own class, so the port
  stays a member and is randomized like any flop. The same stub now fails under all four seeds
  (uo_out not 0 during reset).
  - It applies to every harness build: both modes, the liveness fuzz and arch.
  - `-fno-inline` closes the gap too, but it also un-inlines every cell model instance. No
    narrower `-f` flag does: `-fno-gate`, `-fno-subst`, `-fno-dfg`, `-fno-life`, `-fno-localize`,
    `-fno-expand`, `-fno-split`, `-fno-reorder`, `-fno-const`, `-fno-table`, `-fno-combine`,
    `-fno-merge-cond` and `--inline-mult 1` or `10` all left the stub passing.
- **Cost**, measured on hwe-bench with champion 107: CoreMark (seed 1), 4 interleaved repetitions
  per mode, took 172.9 s with `noinline.vlt` against 165.5 s with the inlining forced back (+4.5%):
  rvfi 91.2 against 84.9 s (+7.4%), pins 81.7 against 80.7 s (+1.2%), summed from the per-run
  times as printed (0.1 s). Cycle counts are identical.
- **Cell flops.** Each cell model keeps its state in an internal `reg q` and drives `Q` with
  `assign Q = q`, so an inlined cell is randomized as well; the cell models stay inlined.
- **Self-tests.** `test_cell_stub_power_up_reliance_fails` covers the cells.
  `test_submodule_output_reg_power_up_reliance_fails` covers design submodules: the `r32v_ureg`
  stub fails under seeds 1-4, and passes when built with `--x-initial 0` or with the inlining
  forced back by a test-only configuration file.
- The gate-level test starts every flop at X, so it never had this gap.

`run_cosim.py` rebuilds the Verilator model when anything that can reach it changes: it keys a
stamp on every file under the design directory except `__pycache__` and non-Verilog `test_*`
files, the harness sources (`harness.cpp`, `pmod.h`, `iss3.h`, `periph_models.h`, `build.sh`,
`noinline.vlt`), the cell models (`verif/cells/*.v`) and `verilator --version`. Builds hold
`build/cosim/<build dir>.lock` (lockf), so concurrent processes don't build the same model at
once. `build.sh` with no arguments builds the standalone pieces
instead: `test_pmod`, `libpmod.so` (the Pmod models behind a C API, for the TT test's ctypes),
`iss_main` and `test_periph`.

## Programs (`verif/programs/`)

`make -C verif/programs list` names them in harness order: selftest, csr, coherence,
mem_init_check, gpio_demo, coremark, trap_ecall .. trap_ones (one synchronous trap each),
random_1 .. random_6 (`gen_random.py` streams over the v3 map), and the peripheral programs
irq_timer, irq_stress, wfi, uart_echo, uart_echo_fast, spi_loop, pwm, ir_loop and gpio_edge. The
Makefile's header and `mkexpect.py` describe each `expect.json`:
- **Deterministic programs** (`kind: "iss"`): the standalone ISS run (`iss_main --check-init`),
  which must end in the self-loop with GPIO_OUT = 0x5A, never read a register or an
  arbitrary-reset CSR before writing it, never execute an unspecified instruction and never touch
  the core region.
- **Peripheral programs** (`kind: "declared"`, ruling R13): their outcome depends on peripheral
  timing or interrupts, so expect.json carries the declared stimulus and expectations (`stim.h`):
  UART frames and rates, SPI bytes, PWM settings, GPIO scripts, the `ir_loop` loopback, and for
  irq_stress its own run budget (`max_cycles` 800,000,000, R35).
- **CoreMark** (ITERATIONS = 1, TOTAL_DATA_SIZE = 2000, -O2): timing marks GPIO_OUT 0xC0 / 0xC1
  around the timed region (R21) and `coremark_iters`, and "Correct operation validated" in the
  PSRAM log ring (CPU 0x017F_0000).

Every program starts in `crt0.S` (it copies `.data` from flash, clears `.bss`, and checks that
mstatus.MIE/MPIE and mie.MEIE read 0 at reset, R32), keeps its stack below the core region at
CPU 0x017F_FF80, and ends with `jal x0, 0` retired twice at the same pc (contract 4).
`make -C verif/programs check` runs `test_programs.py`: the expect.json contract, the random
streams' coverage and `progsim` (the ISS with a timed peripheral model) on the peripheral programs
at 3 to 2,048 cycles per instruction.

## The reference ISS (`verif/cosim/iss3.h`, `iss_main`)

RV32E plus the contract-4 Zicsr subset, traps to `mtvec`, the external interrupt entry
(`take_interrupt`), the flash / PSRAM / IO map of contract 3 and a register-level model of the
peripherals whose volatile bits (counters, received bytes, busy and pending flags) come from the
DUT through `IoHooks` in lockstep. Unspecified steps (reserved encodings, unlisted CSRs, IO
fetches, misaligned IO, core-region accesses) set `Retire::reserved` and stop the comparison.
The header comment of `iss3.h` is the full description; `iss_main`'s header lists its options
(`--json`, `--log`, `--vread`, `--irq`, `--check-init`, `--dump-psram`). `test_iss.py` holds its
unit cases; the arch gate cross-checks it against spike on all 53 tests (each signature, ISS
against spike, before the DUT runs).

## Liveness fuzz (`--fuzz-liveness`)

Contract section 4: every instruction, unspecified ones included, retires within 2,048 cycles of
the previous one (a WFI stall excepted). The programs never execute reserved encodings, so the
fuzz runs random flash images with no reference and checks only liveness and the pin rules:

```
python3 verif/cosim/run_cosim.py --fuzz-liveness --seed S [--src DIR] [--mode rvfi|pins]
                                 [--fuzz-runs K] [--fuzz-cap N]
```

Defaults: rvfi mode, K = 2,048 probe sub-runs plus K/256 = 8 walk sub-runs, N = 2,000
retirements. Each sub-run starts from reset with its own memory power-up modes (all four cycle
through), flip-flop state and noise, on a random image of flash bytes 0-64 KB (the rest erased):
- **probe sub-run k:** 0 to 8 valid, non-trapping, non-control instructions (random x0..x15, so
  loads and stores reach flash, PSRAM, IO and the core region), then one probe word, then random
  words. The probe's bits [6:0] are entry k mod 128 of a per-seed shuffle of all 128 values; on
  visit v = k / 128 of that value, funct3 is (v + a per-seed offset) mod 8, the register fields
  name x0..x15 when v mod 16 < 8 and are random otherwise, and funct7 is class (2 (v / 8) + S)
  mod 4 of {random, 0000000, 0100000, 0000001}. The probe is never 0x00000000, 0xFFFFFFFF, ECALL
  or EBREAK. The sub-run passes once the probe and 8 more instructions have retired (traps go to
  the arbitrary-reset mtvec, so the core wanders), or at the retirement cap.
- **walk sub-run:** LUI x15 at 0, then valid, non-trapping words (the probe prefix's mix plus
  branches and JALs to multiples of 4), rd never x15, every store based on x15 (PSRAM
  0x0100_7800-0x0100_87FF), run to the retirement cap.
- A sub-run whose design stalls after a WFI ends there (legal while `mip.MEIP & mie.MEIE` is 0;
  counted in `wfi_stalls`).

Checks, rvfi mode: the latency cap at each retirement and by a watchdog (only the gap after a
WFI retirement is exempt, R31), no `rvfi_valid` during reset, the Pmod rules (`pmod.h`), the reset
state and output stability. **pins mode can't check liveness**: it sees no retirement, so it
checks only the pin rules, for 16 x 2,048 cycles per sub-run, and reports
`"liveness_checked": false`. The eval runs rvfi mode; the equivalence gate ties the plain build to
the RVFI build. Measured on the seed (133d76b): seeds 1-5 pass, 2,056 sub-runs each, 2,048 probes
retired, worst gap 1,319.

## Timing report (`--timing-json`, rvfi mode)

`run_cosim.py --mode rvfi --prog P --timing-json PATH` writes `{"pass", "first_retire_cycle",
"max_cycles_per_insn", "per_class"}` on every run. With `"pass": false` the numbers only cover the
run up to the failure (or are null), so a consumer must check `pass`. Cycles count rising clk
edges as contract section 4 defines the latency cap: edge 0 is the last edge with `rst_n`
sampled low, and a retirement happens at the edge after which `rvfi_valid` is high.
`max_cycles_per_insn` is the largest non-exempt gap (a gap that starts at a WFI's retirement is
exempt, R31), and `per_class` the largest gap per instruction class (`iss_class` in `iss3.h`). In
pins mode the summary's `max_cycles_per_insn` and `per_class` are null.

## Speed

Measured on hwe-bench with the MCU seed (133d76b): the `--all` matrix (28 programs x 2 modes x
4 seeds = 224 runs, plus 5 fuzz seeds) took 271 s wall clock with `--jobs 6`. CoreMark is the long
pole: about 605 M simulated cycles per run (578,875,473 cycles per iteration).
