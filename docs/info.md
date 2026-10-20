## How it works

BonettoR32V is a 32-bit RISC-V microcontroller (RV32E with the Zicsr subset, MRET and WFI,
machine mode) that runs its program straight from the flash of Tiny Tapeout's QSPI Pmod and keeps
its data, its stack and most of its registers in the Pmod's PSRAM.

- **Core.** The registers x1-x10, x12-x15 and the CSRs `mtvec`, `mscratch` and `mepc` live in
  the top 128 bytes of the PSRAM; the pc and x11 live on the die. The datapath is 4 bits wide,
  matched to the quad SPI bus. Every instruction is a short series of quad transactions: a
  42-cycle read per register operand, a 34-cycle write per result (one cycle more right after
  another PSRAM transaction, for the chip's CS_n high time), a 10-cycle internal step for
  x0 or x11. SCK runs at half the core clock. A flash fetch of a non-branch instruction also
  reads the next word, so the next instruction skips its fetch.
- **Memory map.** Flash (16 MiB, read-only) at `0x0000_0000`, where execution starts after
  reset. PSRAM (8 MiB) at `0x0100_0000`; its last 128 bytes, `0x017F_FF80` and up, belong to the
  CPU. Peripheral registers at `0x8000_0000 + 4 x slot`.
- **Reset.** The core resets both memory chips from whatever mode they are in, puts the flash in
  quad continuous-read mode and the PSRAM in QPI mode, and starts at address 0. The first
  instruction retires 305 cycles after reset.
- **Traps.** ECALL, EBREAK, misaligned jumps, the illegal words 0x00000000 and 0xFFFFFFFF, and
  one external interrupt all trap to `mtvec`.
- **Peripherals.** GPIO (8 in, 8 out) with per-pin edge interrupts. A 16-bit timer with
  prescaler, compare, auto-reload, capture on `ui[0]` and an output on `uo[7]` (toggle or PWM).
  Three 10-bit PWM channels on `uo[1]`-`uo[3]`, where channel 0 can be gated by the timer to send
  IR remote codes with no CPU work. A UART (8N1, up to 3.125 Mbaud at 50 MHz) on `uo[0]`/`ui[1]`.
  An SPI master (mode 0, up to 12.5 MHz) on `uo[4]`-`uo[6]`/`ui[3]`. Each `uo` pin is GPIO or its
  peripheral function, chosen by the ALTFN register.
- **Speed** (simulated on the RTL with models of the Pmod chips): CoreMark 0.6791
  iterations/s at 50 MHz (73,622,855 cycles per iteration), about 100 cycles per instruction.
  Everything time-critical (UART, SPI, PWM, tones, IR timing) is done in hardware.

The full reference manual, with every register bit, instruction timing and the Arduino-style
SDK, is `docs/manual.md` in the project repository:
https://github.com/FeSens/BonettoR32V/blob/main/docs/manual.md

## How to test

You need the QSPI Pmod (below) and a RISC-V GCC: xPack `riscv32-unknown-elf-gcc` 15.2.0, with
`-march=rv32e_zicsr -mabi=ilp32e`.

1. Build a program with the SDK in the repository: `make -C sdk blink` gives
   `sdk/build/blink/blink.bin`, the raw flash image (`make -C sdk SKETCH=<dir>` builds your own).
2. Write the `.bin` to the Pmod's flash at offset 0, for example with Tiny Tapeout's web flasher
   (https://tinytapeout.github.io/tinytapeout-flasher/). This step is taken from TT's flasher
   source and has not yet been tried with this design.
3. Plug the Pmod into the bidirectional (`uio`) header, select the design, set the clock to
   50 MHz (or rebuild with `make -C sdk F_CPU=<Hz>` for another clock) and pulse `rst_n` low.
4. `blink` blinks `uo[7]`, the decimal point of the demo board's 7-segment display, at 1 Hz.
   `serial_echo` greets you at 115,200 baud 8N1 on `uo[0]` and echoes what it receives on
   `ui[1]`.

Without hardware, `make -C sdk test` runs the examples on the RTL (Verilator, Linux), and
`verif/programs/progsim.cpp` runs any image on the reference instruction-set simulator.

## External hardware

- **Required: Tiny Tapeout QSPI Pmod** on the `uio` header: W25Q128JV flash (16 MiB, quad enable
  bit set, as first-party boards ship) and APS6404L PSRAM (8 MiB; the second PSRAM is unused).
- Optional, by project:
  - a USB-serial adapter on `uo[0]` (TX) and `ui[1]` (RX);
  - push buttons on `ui` pins with external pull-down resistors (the chip has no pull-ups);
  - an IR LED driven through an NPN transistor from `uo[1]`, for the IR remote example;
  - a piezo or speaker through a transistor on `uo[7]` (tones);
  - SPI devices on `uo[4]`-`uo[6]` and `ui[3]`.
