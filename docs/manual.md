# BonettoR32V reference manual

BonettoR32V is a 32-bit RISC-V microcontroller (RV32E + Zicsr) in a 1x2 Tiny Tapeout tile on
sky130. It runs its code straight from the flash of Tiny Tapeout's QSPI Pmod, keeps its data and
most of its registers in the Pmod's PSRAM, and has the peripherals a hobby project needs: GPIO
with edge interrupts, a 16-bit timer with compare, capture and an output, three 10-bit PWM
channels with an IR carrier gate, a UART, an SPI master and one interrupt line.

This manual describes the design at commit `4c82e3a` (the pair-fetch candidate, 2026-10-18). It is
written for hardware hobbyists and Tiny Tapeout users who want to wire, program and evaluate the
chip. `docs/contract.md` is the authoritative interface; where this manual and the contract
differ, the contract wins. `docs/programming-guide.md` is a two-page summary, and `sdk/` holds an
Arduino-style SDK with examples.

**Status: pre-silicon.** Everything here is verified in simulation (RTL and gate level) and in a
clean 1x2 hardening run. Nothing has run on a fabricated chip, and the flashing flow has not been
tried on hardware (section 6.9).

**Number labels.** *Measured* numbers come from the project's gates on the RTL or netlist at
the 50 MHz signoff clock. *Derived* numbers are computed from the RTL source and are labeled so.

## Contents

1. Overview
2. Pins, wiring, clock and reset
3. Memory: map, boot, flash image, PSRAM layout
4. The CPU core: ISA, CSRs, traps, how it works, instruction timing
5. Peripherals: register reference
6. Programming: toolchain, SDK, bare metal, interrupts, simulation, flashing
7. Walkthrough: an IR TV remote
8. Performance: CoreMark and what limits speed
9. Physical implementation
10. Verification
11. Known limitations

## 1. Overview

### 1.1 Headline specifications

| Item | Value |
|-----|-------------------------------------------------------|
| ISA | RV32E (16 registers, no multiply/divide) + Zicsr subset, MRET, WFI; machine mode only |
| Clock | 50 MHz signoff (20 ns period) |
| Speed (measured) | CoreMark 0.6845 iterations/s (73,047,400 cycles per iteration), 0.01369 CoreMark/MHz; 99.1 cycles per instruction over the 32 test programs (total cycles / total instructions), about 0.5 MIPS |
| Code memory | W25Q128JV flash on the QSPI Pmod, 16 MiB, execute in place (quad continuous read) |
| Data memory | APS6404L PSRAM A on the QSPI Pmod, 8 MiB (QPI mode); the top 128 bytes hold the CPU's registers |
| On-die state | pc, x11, mstatus/mie/mcause bits, the instruction register and a one-word prefetch buffer |
| GPIO | 8 inputs (`ui`), 8 outputs (`uo`), per-pin rising or falling edge flags on every input |
| Timer | 16-bit counter, prescaler 2^0 to 2^15, compare (match interrupt, auto-reload), capture on `ui[0]`, output on `uo[7]` (toggle or PWM) |
| PWM | 3 channels, 10-bit period and duty, prescaler 2^0 to 2^7; channel 0 can be gated by the timer (IR carrier) |
| UART | 8N1, bit time DIV+1 cycles, DIV 15..8191: 6,104 to 3,125,000 baud at 50 MHz |
| SPI master | mode 0, MSB first, SCK = f_clk / 2^(D+1), D 1..7: 12.5 MHz to 195 kHz at 50 MHz |
| Interrupts | one machine external interrupt from 5 sources (timer compare, capture, UART RX, UART TX idle, GPIO edges); synchronous traps to `mtvec` |
| Silicon | sky130A, `sky130_fd_sc_hd`, Tiny Tapeout 1x2 tile; 23,073.4 µm² synthesized, 2,336 cells, 495 flip-flops |
| Software | xPack `riscv32-unknown-elf-gcc` 15.2.0, `-march=rv32e_zicsr -mabi=ilp32e`; Arduino-style SDK in `sdk/` |

### 1.2 Block diagram

```
            ui[7:0]                                             uo[7:0]
               |                                                   ^
   +-----------v---------------------------------------------------+-------+
   |  r32v_periph   GPIO + edge flags | 16-bit timer | 3 PWM + IR gate      |
   |                UART (8N1)        | SPI master   | ALTFN pin select     |
   |                interrupt line (mip.MEIP) ----------------+             |
   +-------------------------^--------------------------------|-------------+
                             | IO slots at 0x8000_0000        |
   +-------------------------v--------------------------------v-------------+
   |  core: decode and slot sequencer, 4-bit ALU,      r32v_csr: mstatus,   |
   |        32-bit operand ring, pc, x11,              mie, mcause, trap    |
   |        pair-fetch buffer                          and interrupt entry  |
   +-------------------------^----------------------------------------------+
                             | 4-bit nibbles
   +-------------------------v----------------------------------------------+
   |  r32v_mem: memory reset sequence, QSPI command/address headers,        |
   |            flash continuous read (0xEB), PSRAM QPI (0x0B read, 0x02    |
   |            write), SCK = clk/2                                         |
   +-------------------------^----------------------------------------------+
                             | uio[7:0]
               +-------------v---------------+
               |  TT QSPI Pmod               |
               |  W25Q128JV flash, 16 MiB    |  code, constants, .data image
               |  APS6404L PSRAM A, 8 MiB    |  .data, .bss, stack, x1-x10,
               |  (PSRAM B: CS_n held high)  |  x12-x15, mtvec, mscratch, mepc
               +-----------------------------+
```

### 1.3 What it is good for

The chip trades speed for size: a full register file in flip-flops would not fit the tile next
to the peripherals, so the registers live in external PSRAM and every instruction is a short
series of 4-bit-wide memory transactions. An instruction takes about 100 cycles (2 µs at 50 MHz).

| Good fit | Poor fit |
|------------------------------|------------------------------|
| LEDs, buttons, 7-segment displays (the TT demo board's display shows `uo`) | bit-banged protocols with microsecond timing (NeoPixel, software serial) |
| IR remote control (hardware carrier and mark/space timing) | heavy arithmetic (no hardware multiply, about 0.5 MIPS) |
| serial consoles, SPI sensors and displays, SPI I/O expanders | bidirectional GPIO, I2C (no hardware for either) |
| tones, servo pulses, PWM dimming (all in hardware) | analog inputs (no ADC) |

Everything with a deadline under a few hundred microseconds belongs in a peripheral, and the
peripherals are built for that: the timer and PWM produce exact waveforms with no CPU work, and
the interrupt only has to set up the next event.

## 2. Pins, wiring, clock and reset

### 2.1 Tiny Tapeout pin map

| Pin | Dir | GPIO function | Peripheral function | SDK name |
|-------|---|--------|---------------------------|---------------|
| `ui[0]` | in | GPIO_IN bit 0 | timer capture input | `UI0`, `CAPTURE_PIN` |
| `ui[1]` | in | GPIO_IN bit 1 | UART RX | `UI1`, `RX` |
| `ui[2]` | in | GPIO_IN bit 2 | none | `UI2` |
| `ui[3]` | in | GPIO_IN bit 3 | SPI MISO | `UI3`, `MISO` |
| `ui[4]`-`ui[7]` | in | GPIO_IN bits 4-7 | none | `UI4`-`UI7` |
| `uo[0]` | out | GPIO_OUT bit 0 | UART TX (idles high) | `UO0`, `TX` |
| `uo[1]` | out | GPIO_OUT bit 1 | PWM0, ANDed with the timer output when the IR gate is on | `UO1`, `IR_SEND_PIN` |
| `uo[2]` | out | GPIO_OUT bit 2 | PWM1 | `UO2` |
| `uo[3]` | out | GPIO_OUT bit 3 | PWM2 | `UO3` |
| `uo[4]` | out | GPIO_OUT bit 4 | SPI CS_n (the SPI_CTRL[0] level) | `UO4`, `SS` |
| `uo[5]` | out | GPIO_OUT bit 5 | SPI SCK | `UO5`, `SCK` |
| `uo[6]` | out | GPIO_OUT bit 6 | SPI MOSI | `UO6`, `MOSI` |
| `uo[7]` | out | GPIO_OUT bit 7 | timer output | `UO7`, `TONE_PIN`, `LED_BUILTIN` |
| `uio[7:0]` | in/out | | the QSPI Pmod (section 2.2) | |

- **ALTFN.** Each `uo` pin shows its GPIO_OUT bit while its ALTFN bit is 0, and its peripheral
  function while the bit is 1. ALTFN resets to 0, so after reset every `uo` pin is a GPIO output
  driving 0.
- **Inputs.** Every `ui` pin is always readable in GPIO_IN and always feeds its peripheral. There
  is no input select to set.
- **`uio` belongs to the memory.** All eight `uio` pins are the QSPI Pmod's (section 2.2). There
  are no bidirectional user pins.
- **No pull-ups.** The chip has no internal pull-up or pull-down resistors. Buttons need an
  external resistor (the SDK examples use a 10 kΩ pull-down and a button to the supply, so a press
  is a rising edge).
- **Levels.** Logic levels are those of the Tiny Tapeout board you use (the SDK examples assume
  3.3 V). Drive LEDs above a few mA, IR LEDs and speakers through a transistor.
- **Board inputs.** On a TT demo board other sources (DIP switches, the board's microcontroller)
  can also drive `ui`; check that the pins you wire are free.

### 2.2 The QSPI Pmod

The memory board is Tiny Tapeout's QSPI Pmod on the bidirectional (`uio`) header. It carries:

| Chip | Part | Size | CS_n | Used for | Mode used |
|---|---------|---|----|-----------------|------------------------|
| flash | Winbond W25Q128JV | 16 MiB | `uio[0]` | code, constants, the `.data` image | Fast Read Quad I/O (0xEB) in continuous read mode |
| PSRAM A | AP Memory APS6404L | 8 MiB | `uio[6]` | data, stack, the CPU's registers | QPI: 0x0B read (4 wait edges), 0x02 write |
| PSRAM B | APS6404L | 8 MiB | `uio[7]` | unused (CS_n held high) | |

- **Pins:** `uio[0]` flash CS_n, `uio[1]` SD0, `uio[2]` SD1, `uio[3]` SCK, `uio[4]` SD2,
  `uio[5]` SD3, `uio[6]` PSRAM A CS_n, `uio[7]` PSRAM B CS_n (always 1). CS_n and SCK are always
  outputs; SD0-SD3 switch direction during read transactions.
- **SCK** is the core clock divided by 2: 25 MHz at 50 MHz, below every limit of both chips
  (contract 2.2, 2.3).
- **The flash's QE bit must be 1** (quad enable). First-party Pmods ship that way and TT's flasher
  sets it (contract 1); a third-party board needs QE set first (section 6.9).
- **The core never writes the flash.** It never sends write enable, erase or a status write. A
  store to a flash address does nothing.
- **Any power-up state works.** The core puts both chips into a known mode at every reset
  (section 3.2), whatever mode another design left them in.
- **PSRAM CS_n low time** (tCEM) is at most 8 µs per transaction for the APS6404L. The longest
  PSRAM transaction keeps CS_n low for 41 cycles (derived), 0.82 µs at 50 MHz.

### 2.3 Clock

- **Signoff: 50 MHz** (`clock_hz` in `info.yaml`, `CLOCK_PERIOD` 20 ns in `src/config.json`).
  STA closes at every corner with +1.85 ns of setup slack (section 9).
- **Lower clocks** work in principle: every timing in the design is in cycles. The derived lower
  bound is about 5.1 MHz, where a 41-cycle PSRAM transaction reaches the 8 µs tCEM limit. Only
  the signoff clock is simulated. The SDK takes the clock as `F_CPU` and must be built for the
  clock the board actually runs (section 6.2).
- **IO risk at 50 MHz.** The core samples read data at the end of each 20 ns SCK-high phase. The
  delay through TT's IO pads and mux to the Pmod and back is not modeled in simulation.

### 2.4 Reset

- `rst_n` is active low and synchronous. Hold it low for at least one rising clock edge.
- While `rst_n` is low: both CS_n are high, SCK is low and `uo_out` is 0. Every peripheral
  register takes its reset value.
- The board must release `rst_n` at least 150 µs after the supply is stable (the PSRAM's power-up
  time; contract 1).
- After the release the core runs the memory reset sequence (section 3.2) and starts at
  `pc = 0x0000_0000` in flash.
- Nothing depends on power-up values: every flip-flop may start random, and both memories may be
  in any mode.
- `ena` is ignored.

## 3. Memory

### 3.1 Memory map

| CPU address | Region | Access |
|--------------|------------------------------|----------------|
| `0x0000_0000`-`0x00FF_FFFF` | flash, 16 MiB | fetch, load; stores are ignored |
| `0x0100_0000`-`0x017F_FF7F` | PSRAM A | fetch, load, store |
| `0x017F_FF80`-`0x017F_FFFF` | core region: the CPU's registers (top 128 bytes of PSRAM A) | never access it |
| `0x8000_0000`-`0x8000_007F` | IO: 32 slots of 32 bits, slot = address bits 6:2 | load, store |

- **Decoding.** Bit 31 selects IO. Otherwise bit 24 selects the chip (0 flash, 1 PSRAM). Flash
  uses bits 23:0. PSRAM uses bits 22:0 and ignores bits 30:25 and 23. IO uses bits 6:2. Every
  other address is an alias of one of these.
- **Misaligned loads and stores never trap.** An n-byte access touches bytes `addr` to
  `addr + n - 1`, at no extra cost. An access that spans two regions is unspecified.
- **Coherence.** Every load and fetch returns the last value stored at that address, or the flash
  image if nothing was stored. Code can run from PSRAM (copy it there and jump). The flash never
  changes while the chip runs, so the prefetch buffer never goes stale.
- **Fetching from IO** is unspecified (the core stays live, but don't).

### 3.2 Boot and reset sequence

After `rst_n` rises, `r32v_mem` drives the reset sequence of contract 2.4 on the Pmod pins, then
the core fetches its first instruction. Cycle numbers are derived from `src/r32v_mem.v`, counted
from the last clock edge with `rst_n` low.

| Cycles | Chip | Transaction | Effect |
|---|---|--------------------|-------------------------------------|
| 1-17 | flash | 8 SCK edges with SD0-SD3 all high | leaves continuous read; in standard mode it reads as the no-op 0xFF |
| 33-37 | PSRAM | 0xF5 in QPI form (2 edges) | leaves QPI mode; in SPI mode it is an ignored partial command |
| 65-81 | PSRAM | 0x66 (SPI) | reset enable |
| 97-113 | PSRAM | 0x99 (SPI) | reset: SPI mode, initialized |
| 129-161 | flash | 0xEB, address 0 (quad), mode byte 0x20, CS_n high before the dummy edges | the flash enters continuous read: from now on each read starts directly with the address |
| 193-209 | PSRAM | 0x35 (SPI) | enters QPI mode |
| 213 | | first fetch starts at flash address 0 | |
| 305 | | first retirement (measured) | `li x1, 0`, the first instruction of both the SDK's and the test programs' crt0: a 58-cycle pair fetch and a 34-cycle register write |

The first instruction retires 305 cycles (6.1 µs) after reset, the largest retirement gap the
cosim measures (section 4.4).

**Startup code (SDK `sdk/core/crt0.S`).** Nothing on the chip is initialized except the memories'
modes, so the startup code must:

1. clear x1-x15 (the registers live in PSRAM, whose content is arbitrary after power-up);
2. set `mtvec` (its reset value is arbitrary) and `sp` = `0x017F_FF80`, the core region's base;
3. copy `.data` from its flash image to PSRAM and clear `.bss`;
4. run C++ constructors, then `r32v_init()` (peripherals, timebase, interrupts on), then `main()`.

`verif/programs/crt0.S` does the same for the test programs and also checks that `mstatus.MIE`,
`mstatus.MPIE` and `mie.MEIE` read 0 at reset.

### 3.3 Flash image format

- The image is a raw binary that goes into the flash at **offset 0**. The core starts executing at
  its first byte.
- `sdk/tools/mkimage.py` makes it from the ELF: every loadable segment at its load address,
  gaps filled with 0xFF (erased flash). `.text`, `.rodata` and `.init_array` sit where they run;
  `.data`'s initial bytes follow them.
- The SDK writes `<name>.bin` (the flash image) and `<name>.hex` (the same bytes in the `$readmemh`
  format the cosim loads). A blink sketch is 2,880 bytes; the IR remote example 6,880.
- There is no header, checksum or boot mode. A blank (all 0xFF) flash executes the word
  0xFFFFFFFF, which traps with mcause 2 to the arbitrary reset `mtvec`; the core keeps running
  but does nothing useful.

### 3.4 PSRAM layout (SDK linker script `sdk/core/link.ld`)

| CPU address | Content |
|-----------------|-------------------------------------------|
| `0x0100_0000` | `.data` (copied from flash by crt0), then `.bss` (cleared by crt0) |
| up to `0x017F_0000` | the linker checks that `.data` + `.bss` end below here |
| below `0x017F_FF80` | the stack, growing down from `0x017F_FF80` |
| `0x017F_FF80`-`0x017F_FFFF` | the core region (never touch) |

The SDK has no heap allocator (`-nostdlib`).

### 3.5 The core region

The CPU keeps its architectural state in the top 128 bytes of PSRAM A. A program access there is
unspecified (it would corrupt the CPU's registers), and the checkers stop at the first one. Its
layout, from the RTL (informational; words are stored in the core's own nibble order, not as
little-endian program data):

| PSRAM byte | CPU address | Content |
|---------|----------|----------------------------------------|
| `0x7F_FF80` | `0x017F_FF80` | scratch word used by CSR instructions |
| `0x7F_FF94` | `0x017F_FF94` | `mtvec` |
| `0x7F_FF98` | `0x017F_FF98` | `mscratch` |
| `0x7F_FF9C` | `0x017F_FF9C` | `mepc` |
| `0x7F_FFC0` | `0x017F_FFC0` | "x0" word: the spilled pc + 4 around stores and register shifts |
| `0x7F_FFC0 + 4r` | `0x017F_FFC0 + 4r` | register xr, r = 1..15 (the x11 word is unused: x11 lives on the die) |

The pc, x11, `mstatus.MIE`/`MPIE`, `mie.MEIE` and `mcause` live in flip-flops on the die.

## 4. The CPU core

### 4.1 Instruction set

- **RV32E base:** the 37 computational, load, store, branch and jump instructions, plus FENCE,
  ECALL and EBREAK. 16 registers x0-x15. No M (multiply/divide), no C (compressed), no F.
- **Zicsr:** CSRRW, CSRRS, CSRRC, CSRRWI, CSRRSI, CSRRCI on the CSRs of section 4.2.
- **Privileged:** MRET and WFI. Machine mode only.
- **FENCE** is a no-op (memory is always coherent). FENCE.I is reserved.
- **WFI** is a no-op on this core: it retires (52 cycles) and execution continues. The contract
  also allows a core to stall in WFI until an interrupt, so portable code must not rely on either
  behavior.
- **Compile with exactly `-march=rv32e_zicsr -mabi=ilp32e`.** The core does not trap most unknown
  encodings: they execute as whatever their opcode bits select (contract 4: unspecified). A MUL
  compiled with the M extension silently computes something else, and register fields name x0-x15
  with 4 bits, so x16-x31 alias x0-x15.

### 4.2 CSRs

| CSR | Number | Behavior | Reset | Lives in |
|---------------|----|-----------------------------|------|------|
| `mstatus` | 0x300 | MIE (bit 3) and MPIE (bit 7) read/write; MPP (bits 12:11) reads 3; other bits read 0 | MIE = 0, MPIE = 0 | die |
| `misa` | 0x301 | reads 0, writes ignored | | |
| `mie` | 0x304 | MEIE (bit 11) read/write; other bits read 0 | MEIE = 0 | die |
| `mtvec` | 0x305 | bits 31:2 read/write (direct mode: every trap goes to this address); bits 1:0 read 0 | arbitrary | PSRAM core region |
| `mscratch` | 0x340 | 32 bits read/write | arbitrary | PSRAM core region |
| `mepc` | 0x341 | bits 31:2 read/write; bits 1:0 read 0 | arbitrary | PSRAM core region |
| `mcause` | 0x342 | bit 31 and bits 3:0 read/write; other bits read 0 | arbitrary | die |
| `mtval` | 0x343 | reads 0, writes ignored | | |
| `mip` | 0x344 | MEIP (bit 11) is the peripheral interrupt line, read-only; other bits read 0 | | die |
| `mvendorid`, `marchid`, `mimpid`, `mhartid` | 0xF11-0xF14 | read 0 | | |

- Any other CSR number is unspecified. In this RTL some unlisted numbers alias listed ones (for
  example 0x302 decodes as `mstatus`), so use only the CSRs above.
- There are no counter CSRs (`cycle`, `time`, `instret`). Use the timer for time (the SDK's
  `micros()`).
- CSRRS and CSRRC with rs1 = x0 (or zimm = 0) behave as reads: they leave the CSR value unchanged.

### 4.3 Traps and the interrupt

All traps go to `mtvec` in direct mode. Nothing halts the core.

| Event | mcause | mepc |
|------------------------------------------|-----|--------------|
| misaligned jump target: JAL, JALR (after clearing bit 0) or a taken branch whose target has bit 1 set | 0 | the jump or branch |
| the instruction word 0x00000000 or 0xFFFFFFFF (illegal instruction) | 2 | the word's address |
| EBREAK | 3 | the EBREAK |
| ECALL | 11 | the ECALL |
| machine external interrupt | 0x8000_000B | the next instruction not executed |

- **Synchronous traps** (the first four rows): the instruction has no other effect.
- **Entry**, for every row: `mepc` as above, `mcause` set, MPIE = MIE, MIE = 0, pc = `mtvec`.
- **The interrupt** is taken at an instruction boundary when `mstatus.MIE`, `mie.MEIE` and
  `mip.MEIP` are all 1. This core takes it at the first retirement where all three hold,
  including the retirement of the CSR write or MRET that sets them.
- **MRET:** pc = `mepc`, MIE = MPIE, MPIE = 1.
- **mtval** is always 0. Misaligned loads and stores never trap (section 3.1).
- **Nesting.** There is one `mepc`: a trap inside a handler overwrites it. Save `mepc` before
  re-enabling interrupts or doing anything that can trap.
- **Latency (derived).** The line is registered in the peripheral block, and a `ui` pin edge adds
  the one-flop input synchronizer. The core then finishes the current instruction (at most 270
  cycles, section 4.6), runs the 76-cycle entry sequence and fetches the handler's first
  instruction.

### 4.4 The retirement bound

Contract 4 requires every instruction to retire within 2,048 cycles of the previous one, and the
first within 2,048 cycles of reset, the memory reset sequence included. This core has no stalls
(WFI is a no-op), so the bound holds with a wide margin:

| Gap | Cycles | Source |
|------------------|---|----------------------------------------|
| largest gap in the cosim runs (32 programs, 4 seeds) | 305 | measured: the first retirement after reset (section 3.2) |
| first retirement, worst instruction at address 0 | 483 | derived: 213 + a 270-cycle CSR instruction (the liveness fuzz measured exactly 483 on the previous champion, entry 111) |
| any later gap, worst case | 346 | derived: a 76-cycle interrupt entry, then a 270-cycle CSR instruction as the handler's first |

### 4.5 How it works: the nibble-serial slot engine

**The idea.** A register file of 15 x 32 bits in flip-flops does not fit the tile next to the
peripherals, so the core keeps x1-x10, x12-x15, `mtvec`, `mscratch` and `mepc` in the PSRAM and
moves every operand over the 4-bit QSPI bus. Its datapath is 4 bits wide too: each nibble that
arrives from the bus goes through a 4-bit ALU into a 32-bit shift register (the *ring*) in the
same clock, so an operand is processed while it streams in and no register buffers it.

**Slots.** An instruction is a short sequence of *slots*. Each slot is one quad transaction on
the Pmod, or a short internal step when no memory access is needed. SCK runs at clk/2, so E SCK
edges take 2E + 2 clocks: one gap clock with CS_n high, the edges, and one tail clock with SCK
low.

- **Read, 42 clocks:** 8 header edges (flash: 6 address nibbles and the mode byte 0x20 that keeps
  continuous read; PSRAM: command 0x0B and 6 address nibbles), 4 dummy or wait edges, 8 data
  edges. Data moves on 16 of the 42 clocks.
- **Write, 22, 26 or 34 clocks:** 8 header edges (PSRAM command 0x02 and the address), then 2, 4
  or 8 data edges for 1, 2 or 4 bytes.
- **Pair fetch, 58 clocks:** a flash read with 16 data edges, the instruction and the next word.
- **Fast slot, 10 clocks:** an operand that is x0, x11 or nothing. CS_n stays high and the
  datapath steps through 8 nibbles internally.
- **Skipped fetch, 1 clock:** the instruction came from the pair-fetch buffer.

**On-die registers.**

- **The pc** (`pcr`, 32 flip-flops) also serves as the address register. A fetch reads its
  address nibbles straight from it, and pc + 4 comes from a nibble incrementer that runs during
  another slot, so no slot exists just to update the pc.
- **x11** (`a1r`, 32 flip-flops) is register a1, chosen for CoreMark: it is the operand of the
  software multiply routine (RV32E has no multiply). A read or write of x11 is a 10-clock fast
  slot instead of a 42- or 34-clock transaction; it cut CoreMark's cycles by 11.1%.
- **Pair fetch.** A flash fetch of an instruction whose opcode bit 6 is 0 (anything except
  branches, jumps and system instructions, which rarely continue at pc + 4) clocks 8 more data
  edges, the word at pc + 4, into a 32-bit buffer. The next instruction then skips its fetch and
  only decodes, in 1 clock. Flash never changes during a run, so the buffer is always coherent.
  A fetch from PSRAM never pairs.

**Slot names** (from the header comment of `src/tt_um_fesens_r32v.v`):

| Slot | Does | Clocks |
|---|------------------------|---------------------------------|
| F | fetch the instruction | 42, 58 (pair fetch), 1 (skipped) |
| A | read rs1 into the ALU (+ immediate) | 42, or 10 for x0, x11 or no register |
| B | read rs2 into the ALU with the ring | 42, or 10 |
| BS | read rs2 as store data | 42, or 10 |
| M | load (4-byte read) or store (1, 2 or 4 bytes) | 42; 22, 26, 34; 10 for a store to flash |
| W | write rd | 34, or 10 for x0 or x11 (a shift into x0/x11 takes 11 + its rotation, up to 18) |
| W4 | write rd = pc + 4 (JAL, JALR) | 34, or 10 |
| WP | spill pc + 4 to the core region (stores, register shifts) | 34 |
| P | reload the spilled pc | 42 |
| X | update the pc, no transaction | 10 |
| C | read a CSR (core region or die bits) | 42, or 10 for die CSRs |
| WC | write a CSR; on trap entry, write `mepc` | 34, or 10 for die CSRs |
| WS, RS | write, then read back the scratch word (CSR instructions) | 34, 42 |

Section 4.6 lists the slot sequence of every instruction class. A synchronous trap runs the
slots up to its detection, then WC (`mepc` = pc) and C (pc = `mtvec`); an interrupt runs WC C
after a retirement, then the handler's F.

**A worked example:** `add a0, a2, a3` (x10 = x12 + x13).

1. F reads the word at the pc from flash (58 clocks, buffering the next word as well).
2. A reads x12 from the core region; each nibble passes the ALU into the ring, and the pc
   advances by 4 in the same strobes (42 clocks).
3. B reads x13; each nibble is added to the ring's nibble with the carry kept in one flip-flop
   (42 clocks).
4. W writes the ring to x10's word in PSRAM (34 clocks) and the instruction retires.

Total 176 clocks, or 119 if the instruction before it was a pair fetch that buffered this one.
`add a0, a1, a3` (x11 as an operand) takes 32 fewer.

### 4.6 Instruction timing (derived)

Derived from the slot sequences and slot lengths above; not measured per instruction. The cosim
measures totals (section 8). Three measured gaps match the derivation exactly: the first
retirement on 4c82e3a (305 = 213 + 58 + 34), the same on the previous champion without pair fetch
(289 = 213 + 42 + 34, ledger entry 111), and that champion's liveness-fuzz worst case (483 =
213 + a 270-cycle CSR instruction). Timing does not depend on data values, only on the
instruction, its registers, where it is fetched from and the address region of a load or store
(one exception: a shift into x0 or x11 takes up to 8 more cycles depending on the shift amount).

**Fetch (F):**

- 42 cycles for branches, JAL, JALR and system instructions (opcode bit 6 = 1), and for any fetch
  from PSRAM;
- 58 cycles for a flash fetch of any other instruction, which also buffers the next word;
- 1 cycle for that buffered next word, unless an interrupt is taken first.

**Cycles after the fetch.** "Typical" assumes every named register is x1-x10 or x12-x15 (in
PSRAM). The low end of each range uses x0 or x11 operands.

| Instructions | Slots after F | Typical | Range |
|---------------------------|----------------------|-------|----|
| LUI, LI (ADDI rd, x0, imm) | W | 34 | 10-34 |
| AUIPC | A W (A has no register) | 44 | 20-44 |
| ADDI, SLTI, SLTIU, XORI, ORI, ANDI, SLLI, SRLI, SRAI | A W | 76 | 20-76 |
| ADD, SUB, SLT, SLTU, XOR, OR, AND | A B W | 118 | 30-118 |
| SLL, SRL, SRA | WP A B W P | 194 | 106-194 |
| LB, LH, LW, LBU, LHU from flash or PSRAM | A M W | 118 | 62-118 |
| the same from IO | A W | 76 | 20-76 |
| SB, SH, SW to PSRAM | WP A BS M P | 182, 186, 194 | 118-194 |
| any store to flash (no effect) | WP A BS M P (M empty) | 170 | 106-170 |
| any store to IO | WP A P BS | 160 | 96-160 |
| BEQ, BNE, BLT, BGE, BLTU, BGEU with rs2 ≠ x0 | A B X | 94 | 30-94 |
| the same with rs2 = x0 (BEQZ, BNEZ, BLTZ, BGEZ, ...) | A (A X if not taken with target bit 1 set) | 42 | 10-52 |
| JAL with rd ≠ x0 (CALL) / JAL x0 (J) | W4 | 34 / 10 | |
| JALR with rd ≠ x0 / rd = x0 (RET, JR) | A W4 / A | 76 / 42 | 10-76 |
| FENCE, WFI | X | 10 | |
| CSR instructions | A WS C W RS WC | 172 or 228 | 116-228 |
| MRET | C | 42 | |
| ECALL, EBREAK, illegal word, misaligned JAL (trap) | WC C | 76 | |
| misaligned JALR (trap) | A WC C | 118 | |
| misaligned taken branch (trap) | A B WC C / A WC C (rs2 = x0) | 160 / 118 | |
| interrupt entry (between instructions) | WC C | 76 | |

CSR instructions take 172 cycles after the fetch on a die CSR (`mstatus`, `mie`, `mip`,
`mcause`) with registers, 228 on `mtvec`, `mscratch` or `mepc` (which live in PSRAM), and 116 at
least (a die CSR, an immediate, rd = x0).

Examples, fetch included, code in flash: `beqz a5, L` 84; `bne a4, a5, L` 136; `ret` 84;
`call f` 76; `lw a5, 0(a4)` 176, or 119 when the previous instruction buffered it;
`csrsi mstatus, 8` 158.

**Practical rules** that follow:

- Keep hot values in x11 (a1) when writing assembly; the compiler already uses it for the second
  argument.
- Branch against zero when you can: `bnez` costs 84 cycles against 136 for `bne` with two
  registers.
- Straight-line code from flash is cheaper than code from PSRAM (pair fetch only works in
  flash).
- A store costs about 1.6 times a load (194 against 118 cycles after the fetch) because the pc
  is spilled and reloaded around it.

### 4.7 Unspecified behavior

The contract leaves these unspecified; the core stays live but the result is undefined:

- reserved encodings (anything not listed in 4.1, x16-x31 register fields included);
- CSR numbers not listed in 4.2;
- any access to the core region `0x017F_FF80`-`0x017F_FFFF` (and its aliases);
- fetches from IO, misaligned IO accesses, accesses spanning two regions;
- SPI with D = 0, UART with DIV < 15.

## 5. Peripherals

### 5.1 Register access rules

- Registers are 32-bit slots at `0x8000_0000 + 4n`, n = slot (address bits 6:2; other bits ignored).
- Every implemented bit is in bits 15:0. Unimplemented bits and slots 19-31 read 0. A word load
  of an IO slot returns its 16 bits zero-extended.
- A store writes only the bytes it covers; the others keep their value. A store to bytes 2-3
  does nothing.
- **Side effects belong to byte 0:**
  - a load that covers byte 0 of UART_DATA clears RX_FULL and RX_OVR;
  - a store that covers byte 0 of UART_DATA or SPI_DATA starts a transfer;
  - a store to byte 0 of TIMER_CTRL restarts the toggle output at 0;
  - W1C (write 1 to clear) bits clear where a 1 is written.
- IO accesses that are not naturally aligned are unspecified (a halfword at byte 2 is legal).
- A store's effect is visible to the next instruction. An IO load captures the register at one
  clock edge, together with its side effect.
- **Inputs** pass a one-flop synchronizer: a pin change reaches the logic within 3 cycles
  (contract 5).

The SDK header `sdk/core/r32v.h` defines every register and bit below (`GPIO_OUT`, `TMR_EN`,
`IRQ_CMP`, ...).

### 5.2 Register map

| Slot | Address | Name | Access | Reset |
|----|-------------------|-------|-----------------|-------------|
| 0 | `0x8000_0000` | GPIO_OUT | RW | 0x0000 |
| 1 | `0x8000_0004` | GPIO_IN | RO | (the pins) |
| 2 | `0x8000_0008` | ALTFN | RW | 0x0000 |
| 3 | `0x8000_000C` | TIMER_CNT | RW | 0x0000 |
| 4 | `0x8000_0010` | TIMER_CMP | RW | 0x0000 |
| 5 | `0x8000_0014` | TIMER_CAP | RO | 0x0000 |
| 6 | `0x8000_0018` | TIMER_CTRL | RW | 0x0000 |
| 7 | `0x8000_001C` | PWM_CTRL | RW | 0x0000 |
| 8 | `0x8000_0020` | PWM_DUTY0 | RW | 0x0000 |
| 9 | `0x8000_0024` | PWM_DUTY1 | RW | 0x0000 |
| 10 | `0x8000_0028` | PWM_DUTY2 | RW | 0x0000 |
| 11 | `0x8000_002C` | UART_DATA | W: send, R: received | 0x0000 |
| 12 | `0x8000_0030` | UART_CTRL | RW + RO flags | 0x0000 |
| 13 | `0x8000_0034` | SPI_DATA | W: transfer, R: received | 0x0000 |
| 14 | `0x8000_0038` | SPI_CTRL | RW + RO flag | 0x0001 |
| 15 | `0x8000_003C` | IRQ_EN | RW | 0x0000 |
| 16 | `0x8000_0040` | IRQ_PEND | W1C + RO | 0x0008 (TXIDLE = 1) |
| 17 | `0x8000_0044` | GPIO_EDGE | RW | 0x0000 |
| 18 | `0x8000_0048` | GPIO_EPEND | W1C | 0x0000 |
| 19-31 | `0x8000_004C`-`0x8000_007C` | reserved | | read 0 |

### 5.3 GPIO

**GPIO_OUT (slot 0):** [7:0] the value of each `uo` pin whose ALTFN bit is 0, RW, reset 0. A
pin in GPIO mode takes a stored value no later than 1 cycle after the store retires, and changes
only because of such a store or an ALTFN write (contract 6).

**GPIO_IN (slot 1):** [7:0] `ui_in` after the synchronizer, RO, at most 3 cycles old when the
load samples it.

**ALTFN (slot 2)**

| Bit | 1 selects | Bit | 1 selects |
|---|----------------------------|---|--------------------------|
| 0 | UART TX on `uo[0]` | 4 | SPI CS_n on `uo[4]` |
| 1 | PWM0 (IR gate) on `uo[1]` | 5 | SPI SCK on `uo[5]` |
| 2 | PWM1 on `uo[2]` | 6 | SPI MOSI on `uo[6]` |
| 3 | PWM2 on `uo[3]` | 7 | timer output on `uo[7]` |

Reset 0: all pins GPIO, driving 0. Note that `uo[0]` is therefore low, not the UART idle level,
until software sets ALTFN[0]; a UART receiver attached to it sees a break until then.

**GPIO_EDGE (slot 17)**

| Bits | Field | Access | Reset | Description |
|----|-----|------|-----|-----------------------------------------|
| 7:0 | EN | RW | 0 | 1 = an edge on `ui[i]` sets GPIO_EPEND bit i |
| 15:8 | POL | RW | 0 | per pin: 0 = rising edge, 1 = falling edge |

**GPIO_EPEND (slot 18)**

| Bits | Field | Access | Reset | Description |
|---|---|---|---|----------------------------------------------------|
| 7:0 | PEND | W1C | 0 | set within 3 cycles of an enabled pin's selected edge; cleared by writing 1; a new edge in the same cycle wins over the clear |

There is no both-edges mode. To catch both edges, flip the pin's POL bit in the handler (the
SDK's `CHANGE` mode does this). IRQ_PEND.GPIO is the OR of all eight PEND bits.

### 5.4 Timer

**TIMER_CNT (slot 3):** [15:0] the counter, RW, reset 0. A store sets the bytes it covers and
restarts the prescaler.

**TIMER_CMP (slot 4):** [15:0] the compare value, RW, reset 0.

**TIMER_CAP (slot 5):** [15:0] the count captured at the last capture edge, RO, reset 0.

**TIMER_CTRL (slot 6)**

| Bits | Field | Access | Reset | Description |
|---|---|---|---|--------------------------------------------------|
| 3:0 | P | RW | 0 | prescale: one tick every 2^P cycles (1 to 32,768) |
| 4 | EN | RW | 0 | 1 = the prescaler and counter run |
| 5 | AUTO | RW | 0 | 1 = the counter goes to 0 at a match tick |
| 7:6 | OUT | RW | 0 | output: 00 low, 01 toggle at each match, 10 PWM (high while CNT < CMP), 11 reserved (this RTL drives 0) |
| 9:8 | CAPE | RW | 0 | capture edge on `ui[0]`: 00 off, 01 rising, 10 falling, 11 both |

**Behavior (contract 5.3):**

- **Each tick:** if CNT = CMP, set IRQ_PEND.CMP, toggle the output in toggle mode, and set CNT
  to 0 with AUTO (else CNT + 1). Otherwise CNT = CNT + 1, wrapping at 65,535.
- **Period.** With AUTO the period is (CMP + 1) x 2^P cycles. Without it the counter wraps every
  65,536 ticks and matches once per wrap.
- **Toggle output** restarts at 0 whenever byte 0 of TIMER_CTRL is written. To change only the
  capture edge, write byte 1 (`*(volatile uint8_t *)0x80000019`), as the SDK means to (see the
  SDK bug in section 6.3). In toggle mode
  with AUTO, the output is a square wave of period 2 x (CMP + 1) x 2^P cycles.
- **PWM output** is CNT < CMP. It is useful without AUTO: a period of 65,536 ticks with CMP ticks
  high. Derived example for a servo at 50 MHz: P = 4 gives 0.32 µs ticks and a 20.97 ms period;
  CMP = 4,688 gives a 1.5 ms pulse.
- **Capture:** on the selected edge of synchronized `ui[0]`, CAP = CNT and IRQ_PEND.CAP is set,
  within 3 cycles of the pin edge. Capture works while the counter is stopped.
- The output reaches `uo[7]` when ALTFN[7] = 1, and gates PWM0 on `uo[1]` when the IR gate is on.

### 5.5 PWM and the IR gate

**PWM_CTRL (slot 7)**

| Bits | Field | Access | Reset | Description |
|----|-----|-----|----|-------------------------------------------|
| 9:0 | PER | RW | 0 | the counter runs 0, 1, ..., PER, 0, ... |
| 12:10 | Q | RW | 0 | prescale: the counter steps every 2^Q cycles (1 to 128) |
| 13 | EN | RW | 0 | 1 = run; 0 = counter held at 0 and every output 0 |
| 14 | IRGATE | RW | 0 | 1 = `uo[1]` carries PWM0 AND the timer output |

**PWM_DUTY0, PWM_DUTY1, PWM_DUTY2 (slots 8-10)**

| Bits | Field | Access | Reset | Description |
|---|---|----|---|------------------------------------------------|
| 9:0 | DUTY | RW | 0 | channel i is high while counter < DUTY_i; 0 = always low; DUTY > PER = always high |

**Behavior (contract 5.4):**

- The period is (PER + 1) x 2^Q cycles. At 50 MHz: from 2 cycles (25 MHz, PER = 1) to 131,072
  cycles (381.5 Hz, PER = 1023, Q = 7). Full 10-bit resolution, PER = 1023 and Q = 0, runs at
  48.8 kHz.
- All three channels share PER and Q.
- New PER, Q or DUTY values take effect within one period.
- Channel outputs reach `uo[1]`-`uo[3]` through ALTFN[1]-[3].
- **IR gate:** with IRGATE = 1, `uo[1]` = PWM0 AND the timer output. PWM0 makes the carrier (for
  example 38 kHz at duty 1/3) and the timer, in toggle mode, switches it on for marks and off for
  spaces. Section 7 walks through it.

### 5.6 UART

**UART_DATA (slot 11)**

| Bits | Access | Description |
|------|----|---------------------------------------------------|
| 7:0 write | W | a store covering byte 0 sends the byte if TX_BUSY = 0; ignored while TX_BUSY = 1 |
| 7:0 read | R | the last byte received (reset 0); a load covering byte 0 clears RX_FULL and RX_OVR |

**UART_CTRL (slot 12)**

| Bits | Field | Access | Reset | Description |
|---|-----|----|---|---------------------------------------------|
| 12:0 | DIV | RW | 0 | bit time = DIV + 1 cycles; DIV ≥ 15 required (set it before use) |
| 13 | TX_BUSY | RO | 0 | 1 from the starting store until the stop bit ends |
| 14 | RX_FULL | RO | 0 | a received byte waits in UART_DATA |
| 15 | RX_OVR | RO | 0 | a byte arrived while RX_FULL was 1 and was dropped |

**Behavior (contract 5.5):** 8 data bits, no parity, 1 stop bit, LSB first; TX on `uo[0]`
(ALTFN[0] = 1), RX on `ui[1]`.

- **Transmit.** The start bit begins within DIV + 2 cycles after the store retires; then 8 data
  bits and the stop bit, each exactly DIV + 1 cycles. There is no FIFO: poll TX_BUSY before each
  byte, or enable the TXIDLE interrupt.
- **Receive.** A falling edge while idle starts a frame; each bit is sampled near its middle. The
  byte lands in UART_DATA and RX_FULL is set before the stop bit ends. If RX_FULL is still 1, the
  new byte is dropped (the old one stays) and RX_OVR is set. Frames within ±1% of the programmed
  rate are received.
- **Speed.** DIV = round(f_clk / baud) - 1. At 50 MHz:

| Baud | DIV | Actual baud | Error |
|---------------------------|-------|----------------|----------|
| 9,600 | 5,207 | 9,600.6 | +0.006% |
| 115,200 | 433 | 115,207.4 | +0.006% |
| 1,000,000 | 49 | 1,000,000 | 0 |
| 3,125,000 (maximum) | 15 | 3,125,000 | 0 |
| 6,104 (minimum) | 8,191 | 6,103.5 | |

At 115,200 baud a byte arrives every 4,340 cycles, about 40 instructions: the receive interrupt
must read it quickly (the SDK reads it at the sixth instruction of its handler, section 6.5).

### 5.7 SPI master

**SPI_DATA (slot 13)**

| Bits | Access | Description |
|-----|----|---------------------------------------------------|
| 7:0 write | W | a store covering byte 0 starts an 8-bit transfer if BUSY = 0; ignored while BUSY = 1 |
| 7:0 read | R | the byte received; read it after BUSY returns to 0 (reset 0) |

**SPI_CTRL (slot 14)**

| Bits | Field | Access | Reset | Description |
|---|---|---|---|------------------------------------------------|
| 0 | CS_N | RW | 1 | the level of `uo[4]` while ALTFN[4] = 1 |
| 3:1 | D | RW | 0 | SCK low 2^D cycles, then high 2^D cycles per bit; D ≥ 1 required (set it before use) |
| 4 | BUSY | RO | 0 | 1 during a transfer |

**Behavior (contract 5.6):** mode 0 (SCK idles low, data sampled on the rising edge), MSB first.
MOSI on `uo[6]` changes only while SCK (`uo[5]`) is low. MISO (`ui[3]`) is sampled at each rising
SCK edge, allowing up to 3 cycles of synchronizer delay. BUSY clears when the eighth high phase
ends with SCK low. A byte takes 16 x 2^D cycles: 32 cycles (0.64 µs) at D = 1. CS_n is plain
software control: set SPI_CTRL[0] low before the bytes and high after.

| D | SCK at 50 MHz | Byte time |
|---|--------------------------------------|---------------------|
| 1 | 12.5 MHz | 32 cycles |
| 2 | 6.25 MHz (SDK default) | 64 cycles |
| 3 | 3.125 MHz | 128 cycles |
| 7 | 195.3 kHz | 2,048 cycles |

### 5.8 Interrupt controller

**IRQ_EN (slot 15):** [4:0] source enables, RW, reset 0. Bit layout as IRQ_PEND.

**IRQ_PEND (slot 16)**

| Bit | Source | Access | Set when | Cleared by |
|---|----|----|-----------------------------|--------------------|
| 0 | CMP | W1C | a timer tick with CNT = CMP | writing 1 |
| 1 | CAP | W1C | a capture edge | writing 1 |
| 2 | RX | RO | = UART RX_FULL | a load of UART_DATA byte 0 |
| 3 | TXIDLE | RO | = NOT UART TX_BUSY (reads 1 after reset) | starting a frame |
| 4 | GPIO | RO | = OR of GPIO_EPEND | clearing the GPIO_EPEND bits |

- `mip.MEIP` = OR(IRQ_PEND & IRQ_EN), registered: at most a cycle late (contract allows 3).
- There is one interrupt line and no vector table: the handler reads IRQ_PEND (masked with
  IRQ_EN), then GPIO_EPEND, and clears each source it serves.
- TXIDLE is a level that stays 1 whenever the UART is idle: enable it only while waiting for a
  frame to finish, or the interrupt fires forever.
- A W1C flag set by a new event in the same cycle as its clear stays set.

## 6. Programming

### 6.1 Toolchain

| Item | Value |
|-----|-------------------------------------------------------|
| Compiler | xPack GNU RISC-V Embedded GCC 15.2.0 (`riscv32-unknown-elf-gcc`), macOS or Linux |
| Flags | `-march=rv32e_zicsr -mabi=ilp32e` (exactly; section 4.1) |
| Libraries | none (`-nostdlib`); `-lgcc` for software multiply, divide and shifts by variables where GCC calls them |
| Python | 3, for `sdk/tools/mkimage.py` and the tests |

RV32E has no multiply or divide instruction: GCC calls libgcc's `__mulsi3`, `__divsi3` and
friends, which take hundreds of instructions. Multiplications by constants become shifts and adds.
The SDK prints decimal numbers with a shift-and-add divide by 10.

### 6.2 The SDK

The SDK (`sdk/`) gives an Arduino-style API in C and C++.

```
make -C sdk                     # every example: sdk/build/<name>/<name>.{elf,bin,hex,lst,map}
make -C sdk blink               # one example
make -C sdk SKETCH=/abs/sketch  # one sketch: a directory of .ino/.c/.cpp files, or one file
                                # (a relative path is taken from sdk/, because of -C)
make -C sdk test                # image checks, the NEC decoder; RTL runs need Verilator (Linux)
make -C sdk F_CPU=40000000 ...  # build for another clock
```

- **Sketches.** A `.ino` file is C++ with `Arduino.h` included first, as in the Arduino IDE
  (without automatic prototypes: declare functions before use). `.c` files get the C API; `.cpp`
  files the C++ objects too. A sketch defines `setup()` and `loop()`, or its own `main()`.
- **F_CPU** (default 50,000,000) sets the timebase, baud rates, tone and IR timing; it must equal
  the board's clock and be a multiple of 50 Hz x 2^TB_P, the timebase prescale (800 Hz at
  50 MHz, where TB_P = 4; the build checks).
- **Startup order:** crt0 (section 3.2), `r32v_init()` (all peripherals to known values, the
  timebase on the timer, the compare interrupt and `mstatus.MIE` on), then `setup()` once and
  `loop()` forever.
- **Examples** (`sdk/examples/`): `blink` (LED_BUILTIN), `fade` (PWM), `serial_echo`, `button`
  (edge interrupt with debounce), `tone` (UO7 timer tone and UO2 PWM tone), `ir_remote` (NEC TV
  remote), `c_blink` (the C API).

### 6.3 SDK API summary

| Area | API |
|---|----------------------------------------------------------|
| Pins | `UO0`-`UO7`, `UI0`-`UI7`, `LED_BUILTIN` (UO7: the demo board display's decimal point), `TX`, `RX`, `SS`, `SCK`, `MOSI`, `MISO`, `IR_SEND_PIN`, `TONE_PIN`, `CAPTURE_PIN` |
| Digital | `pinMode(pin, mode)` (UOx OUTPUT returns the pin to GPIO; `INPUT_PULLUP` is accepted but there are no pull-ups), `digitalWrite`, `digitalRead` (UOx reads back GPIO_OUT) |
| PWM | `analogWrite(UO1..UO3, 0..1023)` (1023 = fully on; 48.9 kHz by default), `analogWriteFrequency(hz)` (returns the new `pwmMax()`), `pwmMax()`; other pins: HIGH from half of `pwmMax()` |
| Time | `millis()` (wraps after 49.7 days), `micros()` (wraps after 71.6 minutes), `delay(ms)`, `delayMicroseconds(us)` (busy-waits on the timer), `timerPeriodMicros(us)` (INT_TIMER period, 3,932 to 20,971 µs at 50 MHz), `yield()` (weak) |
| Interrupts | `attachInterrupt(digitalPinToInterrupt(UIx), fn, RISING/FALLING/CHANGE)`, `INT_TIMER` (every timebase period), `INT_CAPTURE` (with `captureMicros()`, `captureTicks()`), `INT_SERIAL` (a byte arrived); `detachInterrupt`, `interrupts()`, `noInterrupts()` |
| Tone | `tone(pin, hz[, ms])`, `noTone(pin)`: UO7 (timer, 24 to 2,000 Hz) or UO1-UO3 (PWM, from 382 Hz, no CPU cost) |
| Serial | `Serial.begin(baud)` (up to 3,125,000 at 50 MHz), `end`, `available`, `read`, `peek`, `write`, `print`, `println` (strings, chars, integers in BIN/OCT/DEC/HEX), `flush`; C: `serial_*`, `serial_overruns()` |
| SPI | `SPI.begin()`, `end()`, `beginTransaction(SPISettings(hz))`, `transfer(byte)`, `transfer(buf, n)`, `setClock(hz)`; `digitalWrite(SS, ...)` drives CS_n; C: `spi_begin`, `spi_end`, `spi_set_clock` (12.5 MHz to 195 kHz), `spi_transfer` |
| IR | `IrSender.begin()`, `sendNEC(addr, cmd, repeats)`, `sendNECRepeat()`, `sendNECRaw(data)`, `sendRC5(addr, cmd, toggle)`, `sendRaw(us[], n, khz)`, `isBusy()`; C: `ir_*` |
| Registers | `sdk/core/r32v.h`: every IO register and bit, `csr_read/write/set/clear(csr, v)`, `r32v_irq_save()` / `r32v_irq_restore(m)` |
| Hooks | `r32v_fault(mcause, mepc)` (weak; called on ECALL, EBREAK or an illegal word; the default spins with interrupts off) |

`attachInterrupt(INT_CAPTURE, fn, mode)` arms the timer's capture edge (TIMER_CTRL byte 1) and
the CAP interrupt; `sdk/tests/test_sdk.py` checks it on the RTL (three `ui[0]` pulses 20 ms and
30 ms apart give three captures with those intervals, within 30 µs). An earlier SDK revision
wrote the edge to the wrong address; this manual's review found it, and it is fixed.

### 6.4 Resource rules

- **One timer: the timebase.** The timer ticks every 16 cycles (0.32 µs) at 50 MHz, auto-reloads
  every 20 ms (62,500 ticks) and interrupts once per period; the counter is never rewritten after
  startup. `tone()` on UO7 and the IR sender borrow it by moving TIMER_CMP and the output mode
  while the same interrupt keeps adding up time, so `millis()` stays exact. `INT_TIMER` callbacks
  run only while nothing borrows the timer. An IR frame stops a UO7 tone; `tone(UO7)` waits for
  a frame to finish.
- **One PWM period** for UO1-UO3. `IrSender.begin()` makes it the 38 kHz carrier for good
  (`pwmMax()` becomes 658, and UO1 is the IR LED). `tone()` on UO1-UO3 takes the period over
  (refused while IR is on; `noTone()` gives it back). A timed PWM tone stops at the next timebase
  interrupt, up to 20 ms late.
- **Serial** receives into a 64-byte ring buffer in the interrupt; transmit polls TX_BUSY (no
  interrupt per byte).
- **Callbacks** run inside the interrupt with interrupts off. Keep them under about 100
  instructions (about 0.2 ms), or IR edges and UO7 tone periods come late.
- **gp and tp** (x3, x4) belong to the SDK's trap handler. Sketch assembly must not use them; the
  SDK links without `__global_pointer$` so GCC never does.

### 6.5 Interrupt costs (measured)

Measured on the RTL in cosim at 50 MHz (`sdk/tests/progs/overhead`, `make -C sdk test`):

| Activity | CPU time |
|---------------------------|---------------------------------|
| the timebase alone (one interrupt per 20 ms, 15,024 cycles each) | 1.51% |
| a tone on UO7 at 1 kHz (an interrupt per half period) | 17.0% |
| a tone on UO7 at 2 kHz (the maximum) | 32.7% |
| a tone on a PWM pin (UO1-UO3) | 0 (hardware) |
| the IR carrier | 0 (hardware) |
| an IR frame | one interrupt per mark or space edge (67 per NEC frame); not separately measured |

Why: the hardware entry is 76 cycles, but a C interrupt handler saves and restores registers in
PSRAM at about 100 cycles per instruction. The SDK therefore enters every trap in
`sdk/core/trap.S`, which handles two hot cases in assembly without a stack: a received UART byte
(read at the handler's sixth instruction, so a 17-byte back-to-back burst at 115,200 baud arrives
whole: measured) and the steady half-periods of a UO7 tone (about 30 instructions). Everything
else goes to the C handler `r32v_trap_c`.

### 6.6 Bare-metal register access

The registers are plain memory: use `sdk/core/r32v.h` (or your own `volatile` pointers). A
complete sketch that prints over the UART without the Arduino layer. Built with
`make -C sdk SKETCH=dir`, it still starts through the SDK's crt0 and `r32v_init()`, so the SDK's
timebase interrupt keeps running in the background:

```c
#include "r32v.h"

static void uart_putc(char c) {
  while (UART_CTRL & UART_TX_BUSY) {
  }
  UART_DATA = (uint8_t)c; /* byte 0: starts the frame */
}

int main(void) {
  UART_CTRL = 433;             /* 115,200 baud at 50 MHz: DIV + 1 = 434 cycles per bit */
  ALTFN = ALTFN | ALT_UART_TX; /* uo[0] carries UART TX */
  for (const char *s = "hello\r\n"; *s; s++) uart_putc(*s);
  for (;;) {
  }
}
```

In assembly, IO is one `lui` away (slot n at offset 4n):

```asm
    lui  a5, 0x80000        # IO base
    li   a4, 0x5A
    sw   a4, 0(a5)          # GPIO_OUT = 0x5A
    lw   a4, 4(a5)          # a4 = GPIO_IN
    lbu  a4, 44(a5)         # UART_DATA byte 0: the received byte, clears RX_FULL
```

### 6.7 Interrupt handlers

Every trap and the interrupt enter at `mtvec`. A minimal bare-metal handler with GCC's interrupt
attribute (it saves what it uses and returns with MRET):

```c
#include "r32v.h"

static void __attribute__((interrupt("machine"))) isr(void) {
  if (IRQ_PEND & IRQ_CMP) {
    IRQ_PEND = IRQ_CMP;         /* W1C */
    GPIO_OUT = GPIO_OUT ^ 0x80; /* toggle uo[7] */
  }
}

int main(void) {
  csr_clear(mstatus, MSTATUS_MIE); /* interrupts off while configuring */
  IRQ_EN = 0;
  csr_write(mtvec, (uint32_t)isr); /* direct mode: every trap comes here */
  TIMER_CTRL = 0;
  TIMER_CNT = 0;
  TIMER_CMP = 62499;               /* 62,500 ticks of 16 cycles = 20 ms at 50 MHz */
  IRQ_PEND = IRQ_CMP | IRQ_CAP;    /* clear stale flags */
  TIMER_CTRL = TMR_P(4) | TMR_EN | TMR_AUTO;
  IRQ_EN = IRQ_CMP;
  csr_write(mie, MIE_MEIE);
  csr_set(mstatus, MSTATUS_MIE);
  for (;;) __asm__ volatile("wfi"); /* a no-op on this core: the loop spins */
}
```

Both examples build with the SDK and were run in `progsim` (section 6.8) for this manual. Taking
`mtvec` this way replaces the SDK's handler, so `millis()`, Serial receive and the SDK's
interrupts stop working. With the SDK, use `attachInterrupt()` instead.

Handler rules:

- Check `mcause` bit 31 if exceptions are possible: 0 means an exception (`mepc` points at the
  faulting instruction; skip it with `mepc += 4` or it traps again).
- Clear every source you serve (W1C flags, a UART_DATA read, GPIO_EPEND), or the line stays high
  and the handler re-enters at once.
- Keep handlers short: about 100 cycles per instruction (section 4.6).

### 6.8 Simulating

- **progsim** (`verif/programs/progsim.cpp`): the reference ISS with a timed peripheral model at
  a fixed or random cycles-per-instruction. Any host with a C++17 compiler. Fast checks of program
  logic, UART output and interrupts.
- **RTL cosim** (`verif/cosim/run_cosim.py`): the real RTL in Verilator with the QSPI Pmod chip
  models and the pin checkers. Linux with Verilator. Exact cycle timing and pin waveforms.
- **`make -C sdk test`**: builds the examples and runs them on the RTL with stimulus, checking
  the pins (the RTL parts skip on macOS).

**progsim.**

```
mkdir -p build && c++ -std=c++17 -O2 -I verif/cosim -o build/progsim verif/programs/progsim.cpp
printf 'uart_div 433\nuart_rx 300000 104\nuart_rx 360000 105\n' > echo.stim
build/progsim sdk/build/serial_echo/serial_echo.hex --stim echo.stim --cpi 100 --max-cycles 3000000
```

The last output line is JSON with `uart_tx` (here `"BonettoR32V serial echo\r\nhi"`), the GPIO
state, the interrupt count and a trace of GPIO-mode `uo` values. The `.stim` file can also drive
`ui` (`gpio CYCLE VALUE`). progsim stops at a `jal x0, 0` self-loop, so give a sketch a loop body
(or let it run to `--max-cycles`). Its timing is a model: use `--cpi 100` to approximate this
core.

**RTL cosim** (one run, pins mode, 20 ns clock):

```
python3 verif/cosim/run_cosim.py --mode pins --prog sdk/build/blink/blink.hex \
    --clk-ns 20 --max-cycles 60000000 --dump-pins pins.txt
```

- `pins.txt` gets every `uo_out` change as `cycle hex` lines.
- A `<name>.expect.json` next to the `.hex` tells the harness how to judge the run (docs/cosim.md
  lists the fields). A sketch that loops forever needs at least `result_gpio`, a value its pins
  never show; it then ends with `timeout after N+1 cycles (--max-cycles)`, which means every pin
  and memory checker passed up to N.
- `--mode rvfi` also runs the reference ISS in lockstep and checks every retired instruction.
- `sdk/tests/nec_decode.py pins.txt` decodes NEC frames from such a dump.

### 6.9 Flashing the QSPI Pmod

**Not verified on hardware.** The steps below come from Tiny Tapeout's sources (the
tinytapeout-flasher at commit 4ce4e0c, 2026-07-31, and TT's guide "Configuring and flashing the
QSPI Pmod"). Nobody has flashed a BonettoR32V image this way yet.

1. Plug the QSPI Pmod into the demo board's bidirectional (`uio`) header. TT's guide says
   first-party Pmods work with TT04 and later demo boards and ship with the flash's QE bit set;
   third-party boards need QE set first with the MicroPython script in that guide.
2. Open the web flasher (https://tinytapeout.github.io/tinytapeout-flasher/) in a Chromium-based
   browser (it uses WebSerial). It selects TT's chip ROM project, then the demo board's RP2040
   drives `uio[0]`-`uio[3]` as plain SPI at 10 MHz, using `0x9F` (JEDEC ID; expect `ef7018`),
   `0x20` (sector erase), `0x02` (page program) and `0x03` (read-back verify).
3. Write `sdk/build/<name>/<name>.bin` at **flash offset 0** (the flasher's "Flash offset" field,
   0 by default). The core starts at flash address 0.
4. **Unconfirmed:** selecting this design afterwards (TT Commander, commander.tinytapeout.com),
   clocking it at `F_CPU` from the demo board (TT's clock specification says the RP2040 makes
   1 Hz to 66.5 MHz; how close it comes to 50 MHz is not verified), and resetting it.
5. If the board's clock is not 50 MHz, rebuild with `make -C sdk F_CPU=<Hz>` so the timebase,
   baud rates and IR timing match.

**Expected result** with `blink`: the demo board display's decimal point (`uo[7]`) blinks at 1 Hz.
With `serial_echo`: 115,200 baud 8N1 on `uo[0]` (TX) and `ui[1]` (RX), through a USB-serial
adapter at the board's logic level.

## 7. Walkthrough: an IR TV remote

This example sends real TV remote codes with almost no CPU work: the PWM makes the 38 kHz carrier,
the timer switches it on and off with cycle-exact mark and space lengths, and the CPU only queues
the next length.

### 7.1 Hardware

```
uo[1] --- 1 kΩ ---|  NPN (e.g. 2N2222)
                  |  base
     supply --- ~33 Ω --- IR LED (940 nm) --- collector
                                               emitter --- GND

buttons (example ir_remote):  supply --- button --- ui[2] / ui[4] / ui[5] --- 10 kΩ --- GND
```

- Never drive an IR LED straight from the pin. Choose the series resistor for your supply and the
  LED's rated pulse current.
- The example's buttons: UI2 power, UI4 volume up, UI5 volume down (presses are rising edges).
  LED_BUILTIN (UO7) lights while a code goes out, and Serial (115,200 baud) reports each one.

### 7.2 The NEC protocol

| Part | Mark (carrier on) | Space (carrier off) |
|----------------|-----------------------------|--------------|
| leader | 9,000 µs | 4,500 µs |
| bit 0 | 562.5 µs | 562.5 µs |
| bit 1 | 562.5 µs | 1,687.5 µs |
| stop | 562.5 µs | |
| repeat code (key held) | 9,000 µs, 2,250 µs space, 562.5 µs mark | one every 110 ms |

A frame carries 32 bits LSB first: address, inverted address, command, inverted command (a 16-bit
"extended" address replaces the first two bytes). The carrier is 38 kHz with about 1/3 duty. The
example sends the codes commonly published for LG TVs: address 0x04, power 0x08, volume up 0x02,
volume down 0x03.

### 7.3 The sketch

```cpp
// UO1 -> 1 kOhm -> NPN base; IR LED + ~33 Ohm from the supply to the collector
void setup() { IrSender.begin(); }                           // 38 kHz carrier, IR gate on
void loop()  { IrSender.sendNEC(0x04, 0x08); delay(1000); }  // LG TV power, once a second
```

`sdk/examples/ir_remote/ir_remote.ino` is the full three-button remote with repeat codes.

### 7.4 What the hardware does

```
timer output  ___|‾‾‾‾‾‾‾‾‾‾ 9 ms ‾‾‾‾‾‾‾‾|____ 4.5 ms ____|‾‾|__|‾‾|____|‾‾| ...
                 leader mark               leader space     bit 0  bit 1
PWM0          |‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_| ...  (38 kHz, 1/3 high)
uo[1] = AND   ___|‾|_|‾|_ ... _|‾|_|‾|___________________|‾|_|____|‾|_|__ ...
```

1. **`IrSender.begin()`** sets PWM0 to PER 657, Q 1: a period of 1,316 cycles (37,994 Hz at
   50 MHz), with DUTY0 = 219 (438 cycles high, 1/3). It sets PWM_CTRL.IRGATE and ALTFN[1]. The
   timer output is low between frames, so the LED stays dark.
2. **`sendNEC()`** builds the frame as 67 durations in timer ticks of 0.32 µs (the 9 ms leader is
   28,125 ticks) and hands the timer to the IR sender: it moves TIMER_CMP so the running
   timebase period ends 2,048 ticks (655 µs) later, and switches the output to toggle mode.
3. **At each match** the hardware toggles the timer output at the exact tick (mark to space or
   back) and reloads the counter. The compare interrupt only writes the next duration into
   TIMER_CMP and adds the finished period to the timebase. It has the whole current mark or space
   to do so, at least 562.5 µs (28,125 cycles).
4. **After the last mark** the timer goes back to timebase duty with its output low.
5. `millis()` stays exact throughout, because every period the IR sender uses is added to the
   time.

### 7.5 Verified behavior (measured on the RTL)

`make -C sdk test` runs `ir_remote` on the RTL (pins mode, and rvfi mode against the ISS) with
scripted button presses, and `sdk/tests/nec_decode.py` decodes `uo[1]` with cycle stamps:

- three frames decode exactly: address 0x04, commands 0x08, 0x02, 0x03, inverted bytes correct;
- holding volume up produces repeat codes 110 ms apart;
- every mark and space of the timer gate is within 20 cycles (about one 16-cycle tick) of nominal;
- the carrier period is 1,316 cycles (37,994 Hz) with 438 cycles high.

**Receiving IR** is possible in hardware (a TSOP38238-style demodulator on `ui[0]`, timer capture
on both edges, `INT_CAPTURE` with `captureMicros()`), but the SDK has no decoder yet.

## 8. Performance

### 8.1 CoreMark

| Item | Value |
|----------|--------------------------------------------------|
| Benchmark | EEMBC CoreMark, `TOTAL_DATA_SIZE` 2000, `PERFORMANCE_RUN`, one iteration |
| Build | xPack GCC 15.2.0, `-O2 -march=rv32e_zicsr -mabi=ilp32e` (`verif/programs/coremark/`) |
| Platform | the RTL in Verilator with the QSPI Pmod chip models: code in flash, data and stack in PSRAM |
| Timing | cycles between GPIO_OUT marks 0xC0 and 0xC1 around the timed iterations; the slowest of all runs (2 modes x 4 seeds) counts |
| Result (measured, 4c82e3a) | **73,047,400 cycles per iteration**: 0.6845 iterations/s at 50 MHz, 0.01369 CoreMark/MHz |
| Validation | "Correct operation validated" in the program's PSRAM log |

This is the project's score, not a reportable EEMBC result: the run rules ask for at least 10
seconds of timed run and a published report, and one iteration takes 1.46 s here.

### 8.2 Optimization history

The design is improved by a loop of hypotheses, each re-verified by every gate before it merges
(`loop/LEADERBOARD.md`). Cycles per iteration are computed from the ledger's scores.

| Entry | Change | Clock | Cycles per iteration | Iterations/s | Area µm² |
|----------|------------------------------|---|--------|-----|---|
| 100 | seed: bit-serial core, single-bit SPI, every register and the pc in PSRAM | 40 MHz | 578,875,473 | 0.0691 | 20,442.1 |
| 102 | signoff clock 50 MHz | 50 MHz | 578,875,473 | 0.0864 | 20,442.1 |
| 104 | peripheral area diet | 50 MHz | 578,875,473 | 0.0864 | 19,690.1 |
| 103 | quad I/O and a nibble-serial datapath: flash continuous read, PSRAM QPI | 50 MHz | 149,250,950 | 0.3350 | 21,146.5 |
| 106 | peripheral area diet 2 | 50 MHz | 149,250,950 | 0.3350 | 20,933.8 |
| 105 | pc on the die (also the address register) | 50 MHz | 95,700,048 | 0.5225 | 21,737.1 |
| 107 | fewer slots: branches on x0 in F A, 10-clock fast slots, LI/LUI skip A | 50 MHz | 91,730,514 | 0.5451 | 21,804.7 |
| 110 | core area diet at the same cycles (scan flops as enable flops) | 50 MHz | 91,730,514 | 0.5451 | 20,764.9 |
| 111 | x11 on the die | 50 MHz | 81,557,200 | 0.6131 | 21,986.1 |
| 112 (candidate `4c82e3a`) | pair fetch | 50 MHz | 73,047,400 | 0.6845 | 23,073.4 |

From the seed: 7.9x fewer cycles per iteration and 9.9x the score (the clock gives the rest).

### 8.3 What limits speed

- **Registers in PSRAM.** Every register operand except x0 and x11 is a 42-clock read or a
  34-clock write. `add a0, a2, a3` takes 176 cycles, 118 of them register transactions; with all
  three registers on the die it would take 88. Putting x11 on the die cost 1,221 µm² for 12.5%
  more CoreMark/s (entry 111); the tile has room for few more.
- **Transaction overhead.** A 42-clock read moves data on 16 clocks. The other 26 are the
  command/address header (16), the memory's wait edges (8) and the gaps (2). Pair fetch halves
  the header cost of straight-line fetches (entry 112: +11.6% for 1,087 µm²).
- **SCK = clk/2.** SCK comes from a flip-flop that toggles every clock, so the bus runs at 25 MHz
  quad (12.5 MB/s) at 50 MHz. The commands used allow 66 MHz (PSRAM QPI read) and 133 MHz (flash
  quad read), but 50 MHz is the IO ceiling the contract sets for TT's pads (contract 7).
- **No multiply.** CoreMark's matrix kernel multiplies through libgcc's `__mulsi3`, which loops
  5 to 6 instructions per bit of the multiplier (`andi`, `beqz`, `add`, `srli`, `slli`, `bnez`).
  The multiplier sits in x11 (a1), and three of those instructions use it: hence x11's place on
  the die.
- **Area.** The 1x2 tile is nearly full (utilization 0.803 after placement, section 9). Every
  speed-up has to pay for its flip-flops.

## 9. Physical implementation

| Item | Value |
|------|------------------------------------------------------|
| Process, library | SkyWater sky130A, `sky130_fd_sc_hd` |
| Flow | Tiny Tapeout's (tt-gds-action `ttsky26d`), LibreLane 3.0.14 Classic flow, run locally by `tools/harden_tt.sh` (docs/physical.md) |
| Tile | 1x2: die 36,347 µm², core 34,255 µm² |
| Synthesis (measured, 4c82e3a) | 23,073.4 µm², 2,336 cells, 495 flip-flops, no latches |
| Placement utilization | 0.803 |
| Timing at 20 ns (50 MHz) | setup slack +1.85 ns and hold slack +0.023 ns at every corner; 141 hold buffers |
| Signoff checks | DRC, LVS, antenna and TT precheck clean |
| Configuration | `src/config.json`: synthesis `AREA 2` with the smallest drive strengths allowed, hold margins 0.015/0.01 ns, design repair without slew/cap margins, timing-driven and routability-driven placement (overflow threshold 0.15), target density 60% |

- The hardening numbers are measured on 4c82e3a (merged to main as 5c66234) with the current
  `src/config.json`; the sweep run on the a85f6cb netlist (the same RTL) gave identical figures.
- **Design style.** One rising-edge clock, synchronous reset, no latches, no tristates, plain
  Verilog-2005 (contract 1). Core state flip-flops (the ring, instruction register, pc, x11, the
  pair-fetch buffer, counters) are `sky130_fd_sc_hd__sdfxtp_1` scan flops used as enable flops
  (Q fed back through the scan input), which are smaller than the enable flops synthesis picks.
- **Source files:** `src/tt_um_fesens_r32v.v` (top: decode, slot engine, datapath),
  `src/r32v_mem.v` (Pmod controller, reset sequence), `src/r32v_csr.v` (CSRs, trap and interrupt
  entry), `src/r32v_periph.v` (peripherals).

## 10. Verification

Every candidate passes every gate before it merges (`make eval`; docs/eval.md), except formal,
which runs at every tenth merged improvement and at signoff. Results on 4c82e3a, measured on the
project's Linux build host:

| Gate | What it checks | Result |
|---|------------------------------------------|----------------|
| lint | `tools/src_check.py` (synthesizable subset, whitelisted cells only), Verilator `-Wall` on the plain and RVFI builds | pass |
| area | TT's synthesis; no latches; 1x2 fit pre-filter (≤ 23,100 µm²) | pass, 23,073.4 µm² |
| cosim | 32 programs x 2 modes x 4 seeds = 256 runs, plus the liveness fuzz under 5 seeds (2,056 random-image sub-runs each) | 256/256, fuzz 5/5 |
| arch | riscv-arch-test 3.9.1: all 53 RV32E and privilege tests (traps included), under seeds 1-4 | 53/53 |
| equiv | the RVFI-stripped build is formally equivalent to the plain build (yosys) | pass |
| gl | TT's cocotb test on the synthesized netlist with every flip-flop starting at X: `gpio_demo` in the four memory power-up modes, `uart_hello` | 5/5 |
| formal | riscv-formal at the pins | being ported to contract v3; not run on this candidate |

**What the cosim checks** (docs/cosim.md): in rvfi mode the reference ISS runs in lockstep and
every retired instruction is compared; in pins mode only the pins are judged. In both, the
W25Q128JV and APS6404L models check every command, waveform rule, SCK rate and tCEM at the 20 ns
clock; seeds 1-4 cover the four memory power-up modes with random flip-flop state; every PSRAM
write outside the core region must carry bytes of a retired store; pin checkers time the UART,
SPI, PWM, IR gate and GPIO exactly; and a watchdog enforces the 2,048-cycle bound. The 32
programs are selftest, csr, coherence, mem_init_check, gpio_demo, coremark, seven trap programs,
six random instruction streams, and the peripheral programs irq_timer, irq_stress, wfi,
uart_echo, uart_echo_fast, spi_loop, pwm, pwm10, pwm10q, timer16, ir_loop, ir_gate and gpio_edge.

**Measured totals on 4c82e3a:** CPI 99.1 (total cycles over total instructions of the rvfi runs,
seed 1); worst retirement gap 305 cycles;
CoreMark 73,047,400 cycles per iteration.

**SDK tests** (`make -C sdk test`) run the examples on the RTL: blink and c_blink intervals, the
fade's duties, serial echo and the 17-byte burst, the button count, tone periods, NEC frames, and
the interrupt overhead of section 6.5.

## 11. Known limitations

| Limitation | Detail |
|----------|--------------------------------------------------|
| Not proven in silicon | simulation and a clean hardening only; the IO round trip through TT's pads at 50 MHz is not modeled (section 2.3) |
| Flashing unverified | the TT flasher flow, design selection and the 50 MHz demo board clock are taken from TT's sources and untested (section 6.9) |
| Formal pending | riscv-formal is being ported to contract v3 |
| Slow | about 100 cycles per instruction (0.5 MIPS at 50 MHz); about 0.2 ms per 100 instructions in a handler |
| No M extension | software multiply and divide; unknown encodings (a MUL included) execute silently as something else |
| No counters | no `cycle`/`time`/`instret` CSRs; use the timer |
| WFI is a no-op | no low-power idle: the core keeps fetching |
| One `mepc` | traps inside a handler overwrite it; no nesting without software support |
| Pins | 8 inputs, 8 outputs, no bidirectional GPIO, no pull-ups, no I2C, no ADC |
| One timer, one PWM period | shared by the SDK's timebase, UO7 tone and IR sender, and by the three PWM channels |
| Memory | 8 MiB of PSRAM used (PSRAM B unused); the flash is read-only at run time; the top 128 bytes of PSRAM belong to the CPU |
| UART pin at reset | `uo[0]` is a GPIO driving 0 until software selects the UART (ALTFN[0]) |
| CoreMark | one-iteration project score, not an EEMBC-reportable result |
