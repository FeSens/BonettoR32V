![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg)

# BonettoR32V

A 32-bit RISC-V microcontroller (RV32E + Zicsr) in a 1x2 [Tiny Tapeout](https://tinytapeout.com)
tile on sky130. Code runs straight from the flash of TT's QSPI Pmod, data and most registers live
in its PSRAM. Peripherals: GPIO with edge interrupts, a 16-bit timer (compare, capture, output),
three 10-bit PWM channels with an IR carrier gate, a UART, an SPI master and one interrupt line.
It comes with an Arduino-style SDK.

Status: pre-silicon. Verified in simulation (RTL and gate level), and hardened clean in 1x2.

## Headline numbers (sign-off design, measured)

| Item | Value |
|---------|---------------------------------------------------|
| Clock | 50 MHz signoff (setup slack +3.51 ns, hold slack +0.022 ns at every corner) |
| CoreMark | 0.6791 iterations/s, 73,622,855 cycles per iteration (0.01358 CoreMark/MHz) |
| Cycles per instruction | 99.0 over the 32 test programs (total cycles / total instructions) |
| Area | 23,084.6 µm² synthesized, 2,339 cells, 496 flip-flops; utilization 0.798 in the 1x2 tile |
| Memory | 16 MiB flash (code, execute in place), 8 MiB PSRAM (data) |
| Gates | lint, area, cosim (256 runs + liveness fuzz), riscv-arch-test 53/53, equivalence, TT's test on the RTL and on the netlist 5/5; riscv-formal fast config 65/65 (bounded from reset; the deep config does not finish on this core); 2,850-run soak |

The design is hypothesis 113 (RTL `f0a62bf`) with the layout settings of `afdb678`. The clock,
slack and utilization figures come from hardening that commit with TT's flow: DRC, LVS, antenna
and TT precheck clean, and TT's test passes on the routed netlist (manual section 9).
[docs/signoff.md](docs/signoff.md) is the tape-out sign-off record.

## Documentation

| Document | What it covers |
|---------------------|---------------------------------------|
| [docs/signoff.md](docs/signoff.md) | the tape-out sign-off record: every check, the fixes it led to, the remaining risks |
| [docs/manual.md](docs/manual.md) | the reference manual: pins, memory map, boot, the core and its timing, every peripheral register, programming, the IR remote walkthrough, performance, verification, limitations |
| [docs/info.md](docs/info.md) | the Tiny Tapeout datasheet page |
| [docs/programming-guide.md](docs/programming-guide.md) | a two-page programming summary |
| [docs/contract.md](docs/contract.md) | the authoritative interface every gate checks |
| [docs/cosim.md](docs/cosim.md), [docs/eval.md](docs/eval.md), [docs/physical.md](docs/physical.md) | the simulation harness, the gates, the hardening flow |
| [loop/LEADERBOARD.md](loop/LEADERBOARD.md) | the optimization history |

## Repository

| Path | Contents |
|---------|---------------------------------------------------|
| `src/` | the design: `tt_um_fesens_r32v.v` (core), `r32v_mem.v` (QSPI Pmod controller), `r32v_csr.v`, `r32v_periph.v` (bus, pin mux, interrupt line) with one file per peripheral: `r32v_gpio.v`, `r32v_timer.v`, `r32v_pwm.v`, `r32v_uart.v`, `r32v_spi.v` |
| `sdk/` | the Arduino-style SDK (`core/`), examples (`examples/`: blink, fade, serial_echo, button, tone, ir_remote, c_blink), tests |
| `verif/` | reference ISS, cosim harness with QSPI Pmod chip models, test programs (CoreMark included), riscv-arch-test flow |
| `test/` | the Tiny Tapeout cocotb test (RTL and gate level) |
| `tools/`, `physical/` | gates, area, hardening and tile-fit tools |
| `loop/` | the optimization ledger |

## Build and test

Programs (xPack `riscv32-unknown-elf-gcc` 15.2.0, macOS or Linux):

```
make -C sdk                  # every example: sdk/build/<name>/<name>.bin is the flash image (offset 0)
make -C sdk SKETCH=/path/to/my_sketch
make -C sdk test             # image checks; the RTL runs need Verilator (Linux)
```

Design gates (Verilator, Icarus, yosys and LibreLane; see docs/eval.md):

```
make lint                    # src_check and Verilator lint
make cosim                   # every test program on the RTL, two modes, four memory power-up modes
make arch                    # riscv-arch-test
make gl                      # the TT test on the synthesized netlist
make eval                    # every gate plus the CoreMark score, writing build/eval.json
make harden                  # TT's 1x2 hardening flow, locally
```

Flashing the Pmod with TT's tools is described in docs/manual.md section 6.9; it has not yet been
tried on hardware.

## What is Tiny Tapeout?

Tiny Tapeout is an educational project that makes it easier and cheaper than ever to get digital
and analog designs manufactured on a real chip. Learn more at https://tinytapeout.com.

## License

Apache-2.0 (see `LICENSE`).
