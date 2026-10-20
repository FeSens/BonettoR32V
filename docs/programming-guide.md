# BonettoR32V programming guide

BonettoR32V is an RV32E + Zicsr microcontroller in a 1x2 Tiny Tapeout tile: code runs from the
flash of TT's QSPI Pmod, data lives in its PSRAM, and it has GPIO, a 16-bit timer, three 10-bit
PWM channels with an IR gate, a UART, an SPI master and one interrupt line. `docs/contract.md` is
the authoritative interface (section numbers below are its own); `sdk/` gives an Arduino-style
API. Code is fetched over serial flash, so an instruction averages about 100 cycles (99.0 over the
cosim programs, CoreMark included), about 2 µs at 50 MHz: budget interrupt work accordingly.

## 1. Memory map and boot (contract 3, 4)

| CPU address | Region |
|---|---|
| `0x0000_0000`-`0x00FF_FFFF` | flash, 16 MB, read-only: code, constants, the `.data` image |
| `0x0100_0000`-`0x017F_FF7F` | PSRAM A, 8 MB: `.data`, `.bss`, stack (top `0x017F_FF80`, growing down) |
| `0x017F_FF80`-`0x017F_FFFF` | core region: the CPU's own state; never access it |
| `0x8000_0000 + 4n` | IO slot n (only `addr[6:2]` decodes) |

Other addresses alias (bit 31 = 0: bit 24 picks flash or PSRAM). Stores to flash do nothing;
misaligned accesses never trap. After reset the core puts both memory chips in a known mode and
starts at `pc = 0`. The SDK's `crt0.S` clears x1-x15, sets `mtvec` and `sp`, copies `.data`,
clears `.bss` (PSRAM starts arbitrary), runs constructors, calls `r32v_init()` (peripherals to
known values, the timebase, interrupts on), then `main()`: `setup()` once, `loop()` forever.
Nothing may rely on power-up values.

## 2. Pins (contract 1, 5.2)

| Output | ALTFN bit = 1 | SDK name | Input | Feeds | SDK name |
|---|---|---|---|---|---|
| `uo[0]` | UART TX (idle 1) | `UO0`, `TX` | `ui[0]` | timer capture | `UI0`, `CAPTURE_PIN` |
| `uo[1]` | PWM0, AND timer output if IR gate | `UO1`, `IR_SEND_PIN` | `ui[1]` | UART RX | `UI1`, `RX` |
| `uo[2]`, `uo[3]` | PWM1, PWM2 | `UO2`, `UO3` | `ui[3]` | SPI MISO | `UI3`, `MISO` |
| `uo[4]`-`uo[6]` | SPI CS_n, SCK, MOSI | `SS`, `SCK`, `MOSI` | others | GPIO only | `UI2`, `UI4`-`UI7` |
| `uo[7]` | timer output | `UO7`, `TONE_PIN`, `LED_BUILTIN` | | | |

A `uo` pin with ALTFN bit 0 shows its GPIO_OUT bit. Every `ui` pin always reads in GPIO_IN and
always feeds its peripheral. `uio[7:0]` belong to the QSPI Pmod.

## 3. Registers (contract 5): 32-bit slots at `0x8000_0000 + 4·slot`, reset 0 unless noted

| Slot | Name | Fields |
|---|---|---|
| 0, 1, 2 | GPIO_OUT, GPIO_IN, ALTFN | [7:0]; GPIO_IN read-only, synchronized (at most 3 cycles stale) |
| 3, 4, 5 | TIMER_CNT, CMP, CAP | [15:0]; writing CNT restarts the prescaler; CAP read-only |
| 6 | TIMER_CTRL | [3:0] P (tick every 2^P cycles), [4] enable, [5] auto-reload, [7:6] output 00 low / 01 toggle on match / 10 high while CNT < CMP, [9:8] capture edge on `ui[0]` (off, rise, fall, both) |
| 7 | PWM_CTRL | [9:0] PER, [12:10] Q, [13] enable, [14] IR gate |
| 8, 9, 10 | PWM_DUTY0-2 | [9:0]; high while counter < DUTY; DUTY > PER: always high |
| 11 | UART_DATA | write [7:0]: send; read [7:0]: last byte received |
| 12 | UART_CTRL | [12:0] DIV (bit = DIV+1 cycles, DIV ≥ 15), [13] TX_BUSY, [14] RX_FULL, [15] RX_OVR (read-only) |
| 13 | SPI_DATA | write: 8-bit mode-0 transfer, MSB first; read: byte received |
| 14 | SPI_CTRL | [0] CS_n on `uo[4]` (reset 1), [3:1] D (SCK half period 2^D cycles, D ≥ 1), [4] BUSY |
| 15, 16 | IRQ_EN, IRQ_PEND | [0] CMP (W1C), [1] CAP (W1C), [2] RX = RX_FULL, [3] TXIDLE = !TX_BUSY, [4] GPIO = OR of GPIO_EPEND |
| 17, 18 | GPIO_EDGE, GPIO_EPEND | EDGE [7:0] enables, [15:8] polarity (1 falling); EPEND [7:0] flags (W1C) |

Timer tick: if CNT = CMP, set IRQ_PEND.CMP, toggle the output (toggle mode), reload 0
(auto-reload); else CNT + 1. PWM: a counter steps every 2^Q cycles through 0..PER, period
(PER+1)·2^Q cycles; new settings apply within a period. IR gate: `uo[1]` = PWM0 AND timer output.
Stores write only the bytes they cover. Side effects belong to byte 0: reading UART_DATA clears
RX_FULL and RX_OVR; writing UART_DATA or SPI_DATA starts a transfer (ignored while busy).

## 4. Interrupts and traps (contract 4, 5.8)

`mip.MEIP = OR(IRQ_PEND & IRQ_EN)`. When `mstatus.MIE` (bit 3), `mie.MEIE` (bit 11) and MEIP
are 1 at an instruction boundary: `mcause = 0x8000_000B`, `mepc` = next instruction, MPIE = MIE,
MIE = 0, jump to `mtvec` (direct, 4-byte aligned). Exceptions trap there too: ECALL (`mcause`
11), EBREAK (3), a jump or taken branch to an address with bit 1 set (0), the words `0x00000000`
and `0xFFFFFFFF` (2). MRET returns with MIE = MPIE. WFI may wait for `MEIP & MEIE`. A handler
reads IRQ_PEND, then GPIO_EPEND, and clears its source: 1 to CMP, CAP or the EPEND bit, a read of
UART_DATA for RX. TXIDLE is set whenever the UART is idle: enable it only to wait for one frame.

## 5. SDK (`sdk/`)

`make -C sdk` builds every example and `make -C sdk SKETCH=dir` one sketch (`.ino`, `.c`, `.cpp`)
into `sdk/build/<name>/<name>.{elf,bin,hex,lst}` with the xPack `riscv32-unknown-elf-gcc`
15.2.0 (macOS or Linux). The `.bin` is the flash image from address 0. `F_CPU` (default
50000000, the 20 ns `CLOCK_PERIOD` of `src/config.json`, contract 7) must be
the board's clock. Examples: blink, fade, serial_echo, button, tone, ir_remote, c_blink.

| Area | API (C names in `Arduino.h`: `serial_*`, `spi_*`, `ir_*`) |
|---|---|
| Digital | `pinMode`, `digitalWrite`, `digitalRead` (UO pins read back GPIO_OUT) |
| PWM | `analogWrite(UO1..UO3, 0..1023)` (1023 = on, 48.9 kHz), `analogWriteFrequency(hz)`, `pwmMax()` |
| Time | `millis`, `micros`, `delay`, `delayMicroseconds` |
| Interrupts | `attachInterrupt(digitalPinToInterrupt(UIx), fn, RISING/FALLING/CHANGE)`, `INT_TIMER`, `INT_CAPTURE` (`captureMicros`), `INT_SERIAL`; `detachInterrupt`, `interrupts`, `noInterrupts` |
| Tone | `tone(pin, hz[, ms])`, `noTone(pin)`: UO7 or UO1-UO3 |
| Serial, SPI | `Serial.begin/available/read/peek/write/print/println/flush`; `SPI.begin/transfer/beginTransaction` |
| IR | `IrSender.begin()`, `sendNEC(addr, cmd, repeats)`, `sendNECRepeat`, `sendNECRaw`, `sendRC5`, `sendRaw` |

Resource rules:
- **One timer, the timebase:** a tick every 16 cycles at 50 MHz, auto-reload every 20 ms, one
  compare interrupt per period; the counter is never rewritten. `tone()` on UO7 and the IR sender
  borrow it by moving TIMER_CMP and the output mode while the interrupt keeps counting, so
  `millis()` stays exact. `INT_TIMER` callbacks run only in timebase mode. An IR frame stops a
  UO7 tone; `tone(UO7)` waits for a frame to end.
- **One PWM period** for UO1-UO3: `IrSender.begin()` makes it the 38 kHz carrier for good
  (`pwmMax()` becomes 658, UO1 is the IR LED); `tone()` on UO1-UO3 takes it (refused while IR is
  on; `noTone` restores it). A timed PWM tone stops at the next timebase interrupt (up to 20 ms
  late).
- **CPU cost**, measured on the RTL in cosim at 50 MHz (`sdk/build/overhead.json`): the timebase
  1.5 % (one interrupt per 20 ms, about 15,000 cycles); a UO7 tone 17.0 % at 1 kHz and 32.7 % at
  2 kHz, its limit (an interrupt per half period, mostly in the 30-instruction assembly path of
  `sdk/core/trap.S`); PWM tones and the IR carrier nothing; an IR frame one interrupt per mark or
  space edge (67 per NEC frame).
- **Serial** receives into a 64-byte ring through the same assembly path, which reads each byte
  at its sixth instruction: a 17-byte burst at 115,200 baud with no gaps arrives whole (test).
- **Callbacks** run in the interrupt with interrupts off: keep them under about 100 instructions,
  or IR edges and UO7 tone periods come late. `gp` and `tp` belong to the trap handler: sketch
  assembly must not use them.

## 6. IR remote in a few lines

```cpp
// UO1 -> 1 kOhm -> NPN base; IR LED + ~33 Ohm from the supply to the collector
void setup() { IrSender.begin(); }                           // 38 kHz carrier, IR gate on
void loop()  { IrSender.sendNEC(0x04, 0x08); delay(1000); }  // LG TV power, once a second
```

PWM0 makes the carrier (PER 657, Q 1, DUTY0 219: 1,316 cycles, duty 1/3), PWM_CTRL[14] ANDs it
with the timer output, and the timer toggles with each mark or space length in TIMER_CMP: the CPU
only writes the next TIMER_CMP in the compare interrupt, and the hardware times every edge.
`make -C sdk test` runs the examples on the RTL (cosim, pins mode; serial_echo and ir_remote also
against the ISS) and `sdk/tests/nec_decode.py` decodes the frames from `uo[1]` with cycle stamps:
leader, 32 bits, address and command, repeats every 110 ms, and every mark and space of the gate
within 20 cycles (about one timer tick) of nominal.

## 7. Flashing the QSPI Pmod

From TT's sources (tinytapeout-flasher at commit 4ce4e0c, 2026-07-31; TT's guide "Configuring
and flashing the QSPI Pmod"), not yet tried on hardware:
- The Pmod plugs into the demo board's BIDIR (`uio`) header. First-party Pmods work with TT04 and
  later demo boards and ship with the flash's QE bit set (the EEI assumes QE = 1); third-party
  ones need QE set with the MicroPython script in TT's guide.
- The web flasher (https://tinytapeout.github.io/tinytapeout-flasher/, a Chromium browser for
  WebSerial) selects TT's chip ROM project, drives `uio[0]`-`uio[3]` from the RP2040 as plain SPI
  at 10 MHz and uses `0x9F` (JEDEC ID, expect `ef7018`), `0x20` (sector erase), `0x02` (page
  program) and `0x03` (read-back verify).
- It writes the chosen `.bin` at its "Flash offset" field, 0 by default. Use 0 with
  `sdk/build/<name>/<name>.bin`: the core starts at flash address 0.
- Power-cycle the board before flashing if BonettoR32V has run (it leaves the flash in continuous
  read, which the flasher doesn't exit), and before running it after a design that may have left
  the flash in QPI, deep power-down or wrap mode. Select the project after flashing.
- Clock: 6.25 to 50 MHz (simulated at 50, 12 and 6.25 MHz); never stop it while the design runs.

Unconfirmed: selecting this design afterwards (TT Commander, commander.tinytapeout.com) and
clocking it at `F_CPU` from the demo board (TT's clock spec: the RP2040 makes 1 Hz to 66.5 MHz;
how close it gets to 50 MHz is unverified).
