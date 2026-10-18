# BonettoR32V contract (v3: the MCU)

v3 (2026-10-17) turns the core into a microcontroller. It is built from the user-approved draft
`docs/superpowers/specs/2026-10-17-mcu-v3-draft.md`. Changes from v2 (`docs/contract-v2.md`):
- **Memory board:** TT's QSPI Pmod. Code runs from its flash, and data, the stack and the core
  region live in its PSRAM.
- **Peripherals:** GPIO, a timer with compare and capture, 3 PWM channels, a UART, an SPI master
  and an interrupt controller.
- **Traps:** exceptions and one interrupt line trap to `mtvec` through a minimal Zicsr. Nothing
  halts any more.
- **Bus rules:** loosened to "legal for the real chips, and coherent". This allows prefetching,
  caching and quad mode, now that the goal is performance.

This file is the exact interface every candidate implements and every checker assumes. Only the
lead changes it, and all checkers move with it. Hypothesis agents never edit it.

Notation: all design logic is clocked by `clk`. `X(t)` is the value of `X` right after rising
edge `t`. `T` is the signoff clock period (section 7); "8 µs" means `floor(8000 ns / T)` cycles,
which is 320 at 40 MHz.

## 1. Top module, pins, reset

```verilog
module tt_um_fesens_r32v (
    input  wire [7:0] ui_in,  output wire [7:0] uo_out,
    input  wire [7:0] uio_in, output wire [7:0] uio_out, output wire [7:0] uio_oe,
    input  wire ena, input wire clk, input wire rst_n
`ifdef RISCV_FORMAL
    , output wire rvfi_valid, ...   // section 8, written out (no macros)
`endif
);
```

| Pin | Dir | Function |
|---|---|---|
| `uio[0]` | out | flash CS_n (W25Q128JV) |
| `uio[1]` | io | SD0 (MOSI in single-SPI commands) |
| `uio[2]` | io | SD1 (MISO in single-SPI commands) |
| `uio[3]` | out | SCK |
| `uio[4]` | io | SD2 (flash WP_n while QE=0; QE is 1 here, section 2.4) |
| `uio[5]` | io | SD3 (flash HOLD_n while QE=0) |
| `uio[6]` | out | PSRAM A CS_n (APS6404L) |
| `uio[7]` | out | PSRAM B CS_n: constant 1 |
| `uo[7:0]` | out | GPIO out or the peripheral functions (section 5) |
| `ui[7:0]` | in | GPIO in and peripheral inputs (section 5) |

- `uio_oe[0] = uio_oe[3] = uio_oe[6] = uio_oe[7] = 1` and `uio_out[7] = 1` at all times.
- `uio_oe[1], [2], [4], [5]` may change at any time, subject to the contention rule (2.1.6).
- **Reset:** `rst_n` is low for at least one rising edge.
  - While it is low: both CS_n are 1, SCK is 0 and `uo_out` is 0.
  - Every peripheral register takes its reset value (section 5).
  - After the release, execution starts at `pc = 0x0000_0000`.
  - Checkers assert reset only at the start of a run. A mid-run reset is out of scope.
- **Design rules: unchanged from v2 section 1.** One rising-edge clock and no latches. No negative
  edges, internal tristates or combinational loops. Plain Verilog-2005 and the restricted
  preprocessor. No `initial` blocks or initializers. Module names `tt_um_fesens_r32v` or `r32v_*`.
  `tools/src_check.py` enforces the synthesizable subset. Every top-level `src/*.v` file is part of
  the design.
- **No power-up values.** Core state may start unknown, the register file included. The design
  must reach correct behavior from reset alone. Checkers randomize every flip-flop (cosim) or
  leave it as X (gate-level test).
- **EEI assumptions about the board:**
  - `rst_n` is released at least 150 µs after VDD is stable.
  - The flash's QE bit is 1 (first-party Pmods ship that way, and TT's flasher sets it).
  - The memories may be in any mode another design left them in (2.4).

## 2. Memory interface (TT QSPI Pmod)

### 2.1 Waveform rules (every chip, every command)

Let S = SCK, C_f / C_r = flash / PSRAM A CS_n, and SDk = the data lines.
1. **At most one CS_n is low.** `C_f(t) = 0` and `C_r(t) = 0` never both hold.
2. **CS_n changes only while SCK is low:** a change at edge t needs S(t-1) = S(t) = 0. Each
   CS_n stays high for at least one cycle between transactions (tCPH ≥ 18 ns).
3. **SCK is 0 whenever both CS_n are 1.** Its high and low phases each last at least one cycle.
   Per-command maximum rates are listed below; checkers convert them to cycles at period T.
4. **Rising SCK edges.** A rising SCK edge is an edge t with S(t-1) = 0 and S(t) = 1 while a
   CS_n is low.
   - The selected memory samples every line it reads at that edge.
   - Each line the core drives must be stable across it: `uio_out(t) = uio_out(t-1)` on those
     bits.
5. **Memory outputs are valid only while SCK is high.** A memory drives its output bit or nibble
   for clock k from the rising edge of clock k until SCK falls. While SCK is low, every line the
   core does not drive carries noise. The core samples with flip-flops at an edge t where
   S(t-1) = 1.
6. **No contention.** In any cycle where the selected memory drives a line (its data-out phase,
   per the command tables), the core must not drive it: `uio_oe` is 0 on that bit.
7. **PSRAM CS_n low lasts at most 8 µs** (tCEM, standard grade): 320 cycles at 40 MHz. The flash
   has no limit.
8. **Clock counting.** Command, address, mode and dummy fields count SCK rising edges. A quad
   field moves 4 bits per edge as the nibble `{SD3, SD2, SD1, SD0}`, MSB nibble first. Single
   fields move 1 bit per edge, MSB first, on SD0 (core to memory) or SD1 (memory to core).
9. **Writes end on a byte boundary.** A quad byte is 2 edges. Reads may end at any edge.
   - A read's last, partly clocked byte counts as read.
   - Data bytes go to sequential addresses from the start address A: flash `(A + k) mod 2^24`,
     PSRAM `(A + k) mod 2^23` (linear burst).
10. **Command legality.** Any command, mode or sequence not listed for the chip's current state
    is a protocol violation. So is a read or write to the PSRAM before it is reset after power-up
    (2.4). A transaction that ends inside its command field is ignored by either chip.

### 2.2 Flash: W25Q128JV, 16 MB, `C_f`

| Command | Fields after the 8-bit command (single on SD0) | Max SCK |
|---|---|---|
| `0x03` Read | address 24 single, data out single on SD1 | 50 MHz |
| `0x0B` Fast Read | address 24 single, 8 dummy edges, data single on SD1 | 133 MHz |
| `0x6B` Fast Read Quad Output | address 24 single, 8 dummy edges, data quad | 133 MHz |
| `0xEB` Fast Read Quad I/O | address 24 quad (6 edges), mode byte M quad (2 edges), 4 dummy edges, data quad | 133 MHz |
| `0xFF` Mode reset | none | 133 MHz |
| `0x02` Page Program | address 24 single, then whole bytes: **ignored** (no write enable is ever sent) | 133 MHz |

- **Continuous read.** If M[5:4] = `10`, the flash enters continuous read, and the next
  transaction has no command: it starts directly with the quad address, then M.
  - Any other M value at edge 2 of the mode field leaves continuous read when that transaction
    ends, whether or not it was completed.
  - A transaction that clocks all four lines high for 8 edges therefore always leaves the flash
    in standard mode. In standard mode those 8 edges read as command `0xFF`, which is a no-op.
- The memory drives the data lines only in the data phase. The core never sends a write enable
  (`0x06`), an erase or a status write. Flash contents never change during a run.

### 2.3 PSRAM: APS6404L, 8 MB, `C_r`

**SPI mode** (the 8-bit command is single on SD0):

| Command | Fields | Max SCK |
|---|---|---|
| `0x03` Read | address 24 single, data single on SD1 | 33 MHz |
| `0x0B` Fast Read | address 24 single, 8 wait edges, data single | 84 MHz |
| `0xEB` Fast Read Quad | address 24 quad, 6 wait edges, data quad | 84 MHz |
| `0x02` Write | address 24 single, data single (bytes) | 84 MHz |
| `0x38` Quad Write | address 24 quad, data quad (bytes) | 84 MHz |
| `0x35` Enter QPI | none: the chip switches to QPI mode | 84 MHz |
| `0x66` then `0x99` | reset enable, then reset, as two transactions: the chip goes to SPI mode, initialized | 84 MHz |

**QPI mode** (the command is quad, 2 edges):

| Command | Fields | Max SCK |
|---|---|---|
| `0x0B` Fast Read | address quad, 4 wait edges, data quad | 66 MHz |
| `0xEB` Fast Read | address quad, 6 wait edges, data quad | 84 MHz |
| `0x02` or `0x38` Write | address quad, data quad (bytes) | 84 MHz |
| `0xF5` Exit QPI | none: the chip switches to SPI mode | 84 MHz |
| `0x66` then `0x99` | reset enable, reset (quad form): the chip goes to SPI mode, initialized | 84 MHz |

- **Address bits.** The PSRAM uses A[22:0] and ignores A[23].
- **Reset timing.** After `0x99`, CS_n stays high for at least 50 ns before the next command
  (tRST): 2 cycles at 40 MHz.
- **Ignored transactions.** A transaction that ends inside its command field is ignored. In
  particular, the QPI-form `0xF5` (2 edges) is ignored in SPI mode.
- **Burst mode.** Wrap toggling (`0xC0`) is not allowed; bursts stay linear.

### 2.4 Power-up state and the reset sequence

The memories start in an arbitrary mode. Checkers pick it per seed and cover every combination:
- flash: standard, or continuous read;
- PSRAM: SPI mode not yet reset, or QPI mode.

The core must make every chip usable before relying on it. The PSRAM needs a reset (`0x66`,
`0x99`) in the form matching its mode; the datasheet requires one after power-up anyway. The
flash must leave continuous read. One sequence that works from every state:
1. Flash: 8 edges with SD0-SD3 all driven high.
2. PSRAM: `0xF5` in QPI form (2 edges: it leaves QPI mode, and in SPI mode it is an ignored
   partial command), then `0x66` and `0x99` in SPI form.

The sequence counts toward the first instruction's latency (section 4).

## 3. Memory map

| Address | Region |
|---|---|
| `addr[31] = 0`, `addr[24] = 0` | flash byte `addr[23:0]`: read-only, 16 MB, aliased |
| `addr[31] = 0`, `addr[24] = 1` | PSRAM A byte `addr[22:0]`: 8 MB, aliased (bits 30:25 and 23 ignored) |
| `addr[31] = 1` | IO (section 5): slot `addr[6:2]`, other bits ignored |

- **Fetch** reads flash or PSRAM, chosen by `addr[24]`. A fetch from IO is UNSPECIFIED, but the
  core must stay live.
- **Loads** read flash, PSRAM or IO. **Stores** write PSRAM or IO.
  - A store to flash has no effect on memory. The core either issues nothing or a `0x02`
    without write enable, which the flash ignores.
- **Misaligned loads and stores never trap.** An access of n bytes at `addr` touches the bytes at
  `addr + k` for `k < n`, as in v2. An access whose bytes span two regions is UNSPECIFIED (no
  test does it).
- **Core region: PSRAM bytes `0x7F_FF80-0x7F_FFFF`** (CPU address `0x017F_FF80-0x017F_FFFF` and
  its aliases).
  - It holds the core's state as the core likes: x1..x15, the pc, CSRs, the IR, temporaries.
    Its reset content is arbitrary.
  - A program access to it is UNSPECIFIED (stay live), and checkers stop comparing at the first
    one. Programs place the stack top at `0x017F_FF80`.
- **Coherence.** Every load and fetch returns the value most recently stored to that address by
  this program, or the loaded image if nothing was stored. Caches, prefetch buffers and write
  buffers are allowed as long as this holds. The flash never changes.
- **Loaded image.** The test loads the program into flash before reset. PSRAM content outside
  the region starts arbitrary: crt0 copies `.data` from flash and clears `.bss`.

## 4. Execution environment (EEI)

- **ISA:** RV32E unprivileged (37 instructions plus FENCE, ECALL, EBREAK), plus the Zicsr
  instructions (CSRRW, CSRRS, CSRRC, CSRRWI, CSRRSI, CSRRCI), MRET and WFI. Machine mode only.
- **CSRs.** Other CSR numbers are UNSPECIFIED: a core may read 0, ignore writes or trap as
  illegal, but must stay live and consistent. Programs use only the CSRs below.

| CSR | Number | Behavior |
|---|---|---|
| `mstatus` | 0x300 | MIE (bit 3) and MPIE (bit 7) read/write; MPP (bits 12:11) reads 3; other bits read 0 |
| `misa` | 0x301 | reads 0, writes ignored |
| `mie` | 0x304 | MEIE (bit 11) read/write; other bits read 0 |
| `mtvec` | 0x305 | bits 31:2 read/write (direct mode); bits 1:0 read 0 |
| `mscratch` | 0x340 | 32-bit read/write |
| `mepc` | 0x341 | bits 31:2 read/write; bits 1:0 read 0 |
| `mcause` | 0x342 | bit 31 and bits 3:0 read/write; other bits read 0 |
| `mtval` | 0x343 | reads 0, writes ignored |
| `mip` | 0x344 | MEIP (bit 11) is the interrupt line (section 5.8), read-only; other bits read 0 |
| `mvendorid` `marchid` `mimpid` `mhartid` | 0xF11-0xF14 | read 0 |

- **CSR instructions** follow the spec: CSRRW with rd = x0 does not need to read, and CSRRS/C
  with rs1 = x0 (or zimm = 0) do not write. The reset values of `mtvec`, `mepc`, `mscratch` and
  `mcause` are arbitrary. `mstatus.MIE`, `mstatus.MPIE` and `mie.MEIE` reset to 0.
- **Synchronous traps** (the instruction has no other effect):
  - ECALL: `mcause = 11`.
  - EBREAK: `mcause = 3`.
  - JAL, JALR or a taken branch whose target has `target[1] = 1` (JALR clears bit 0 first):
    `mcause = 0`.
  - The words `0x00000000` and `0xFFFFFFFF`: `mcause = 2`.
  - Each sets `mepc = pc`, `MPIE = MIE`, `MIE = 0`, and jumps to `mtvec`.
- **Interrupts.** The external interrupt (`mcause = 0x8000_000B`) is taken at an instruction
  boundary when `mstatus.MIE`, `mie.MEIE` and `mip.MEIP` are all 1.
  - `mepc` gets the pc of the next instruction not executed. MPIE and MIE update as for a trap,
    and execution jumps to `mtvec`.
  - If those three hold continuously, the core takes the interrupt before retiring more than one
    further instruction.
- **MRET:** `pc = mepc`, `MIE = MPIE`, `MPIE = 1`.
- **WFI** retires and may stall until `mip.MEIP & mie.MEIE` (whatever MIE says). Treating it as
  a no-op is also legal.
- **Reserved encodings are UNSPECIFIED.** That covers every encoding not listed, x16-x31 fields
  included. The core may trap them (`mcause = 2`) or give them any consistent effect, but must
  stay live. FENCE (`funct3 = 000`, any other fields) is a no-op. FENCE.I is reserved.
- **Latency cap:** 2,048 cycles. Counting starts at edge 0, the last edge with `rst_n` sampled
  low.
  - The first retirement comes at an edge ≤ 2,048, the memory reset sequence included. Each later
    retirement comes ≤ 2,048 edges after the previous one.
  - Exempt: a WFI stall while `mip.MEIP & mie.MEIE` is 0.
  - The cap applies to every instruction, UNSPECIFIED ones included.
- **Program end (checkers):** a program ends by retiring `jal x0, 0` (0x0000006F) twice in a row
  at the same pc. The harness then runs 2,048 more cycles so write buffers drain, and compares
  memory and pins.

## 5. Peripherals (IO space)

**Register access:**
- 32-bit slots at `0x8000_0000 + 4n`. Register bit i sits in byte `i/8`.
- A load returns the whole slot on its byte lanes. Unimplemented bits and slots read 0.
- A store writes only the bytes it covers; the bytes it doesn't cover keep their value.
- Side effects happen only for the byte named:
  - Reading byte 0 of UART_DATA clears RX_FULL and RX_OVR.
  - A W1C (write-1-to-clear) bit clears when 1 is written to it.
  - Writing byte 0 of UART_DATA or SPI_DATA starts a transfer.
- IO accesses that aren't naturally aligned (`addr % n != 0`) are UNSPECIFIED. A halfword at slot byte
  2 is legal and covers bytes 2-3.

**Timing:**
- Peripheral inputs pass through synchronizers. An input change becomes visible to the logic
  within 3 cycles.
- A store's register effect is visible to the next instruction.
- Counters run freely between instructions.

### 5.1 Map

| Slot | Name | Bits (reset 0 unless noted) |
|---|---|---|
| 0 | GPIO_OUT | [7:0] value for `uo` pins whose ALTFN bit is 0 |
| 1 | GPIO_IN | [7:0] `ui_in` (synchronized), read-only |
| 2 | ALTFN | [7:0] 1 = the pin carries its peripheral function (table 5.2) |
| 3 | TIMER_CNT | [15:0] counter; a write sets it and restarts the prescaler |
| 4 | TIMER_CMP | [15:0] compare value |
| 5 | TIMER_CAP | [15:0] captured count, read-only |
| 6 | TIMER_CTRL | [3:0] prescale P, [4] enable, [5] auto-reload, [7:6] output mode (00 low, 01 toggle on match, 10 PWM: high while CNT < CMP), [9:8] capture edge on `ui[0]` (00 off, 01 rise, 10 fall, 11 both) |
| 7 | PWM_CTRL | [9:0] period PER, [12:10] prescale Q, [13] enable, [14] IR gate |
| 8, 9, 10 | PWM_DUTY0-2 | [9:0] duty |
| 11 | UART_DATA | write [7:0]: send; read [7:0]: last received byte |
| 12 | UART_CTRL | [12:0] DIV (bit time = DIV+1 cycles), [13] TX_BUSY ro, [14] RX_FULL ro, [15] RX_OVR ro |
| 13 | SPI_DATA | write [7:0]: transfer; read [7:0]: last received byte |
| 14 | SPI_CTRL | [0] CS_n level on `uo[4]` (reset 1), [3:1] divider D, [4] BUSY ro |
| 15 | IRQ_EN | [4:0] source enables |
| 16 | IRQ_PEND | [0] CMP (W1C), [1] CAP (W1C), [2] RX = RX_FULL (ro), [3] TXIDLE = !TX_BUSY (ro), [4] GPIO = OR of GPIO_EPEND (ro) |
| 17 | GPIO_EDGE | [7:0] per-pin edge enable, [15:8] polarity (0 rising, 1 falling) |
| 18 | GPIO_EPEND | [7:0] per-pin edge flags (W1C) |

### 5.2 Pin functions (ALTFN = 1)

| Pin | Function | Pin | Function |
|---|---|---|---|
| `uo[0]` | UART TX (idle 1) | `ui[0]` | timer capture input |
| `uo[1]` | PWM0, ANDed with the timer output when the IR gate is set | `ui[1]` | UART RX |
| `uo[2]` | PWM1 | `ui[3]` | SPI MISO |
| `uo[3]` | PWM2 | other `ui` | GPIO only |
| `uo[4]` | SPI CS_n (SPI_CTRL[0]) | | |
| `uo[5]` | SPI SCK | | |
| `uo[6]` | SPI MOSI | | |
| `uo[7]` | timer output | | |

Inputs are always readable through GPIO_IN and always feed their peripheral.

### 5.3 Timer

- **Ticks.** While enabled, a prescaler counts cycles, and a tick happens every `2^P` cycles.
- **Each tick:**
  - If CNT = CMP: set IRQ_PEND.CMP, toggle the output in toggle mode, and set CNT to 0 if
    auto-reload is on (else CNT + 1).
  - Otherwise CNT becomes CNT + 1 (mod 2^16).
- **Output modes.** In PWM mode the output is `CNT < CMP`, updated on every tick. Mode 00 drives
  0. Toggle mode starts at 0 when the mode is written.
- **Capture.** On the selected edge of synchronized `ui[0]`, CAP takes CNT and IRQ_PEND.CAP is
  set, within 3 cycles of the pin edge. Capture works even while the counter is disabled.

### 5.4 PWM

- **Counter.** While enabled, a counter `pc` steps once every `2^Q` cycles, through
  0, 1, …, PER, 0, ….
- **Outputs.** Channel i drives `pc < DUTY_i`. A duty above PER means always 1, and 0 means
  always 0. With the PWM disabled, every output is 0.
- **Rate.** The period is `(PER+1)·2^Q` cycles.
- **Changing settings.** New PER, Q or DUTY values take effect within one period. Checkers
  measure only after a full period has passed.

### 5.5 UART (8N1, LSB first)

- **Transmit.**
  - A write to UART_DATA while TX_BUSY = 0 sets TX_BUSY = 1. TX_BUSY reads 1 from that write
    until the stop bit ends, so a read during the frame returns 1 even if the frame is shorter
    than an instruction.
  - The start bit begins within `DIV+2` cycles after the store retires. Then come the start
    bit, 8 data bits and the stop bit, each exactly DIV+1 cycles.
  - TX_BUSY clears when the stop bit ends.
  - A write while TX_BUSY = 1 is ignored.
- **Receive.**
  - `ui[1]` is synchronized. A falling edge while idle starts a frame, and each bit is sampled
    near its middle.
  - When the stop bit has been sampled, the byte goes to UART_DATA and RX_FULL is set, before
    the stop bit ends.
  - If RX_FULL is already 1, the byte is dropped and RX_OVR is set.
  - Frames at exactly DIV+1 cycles per bit must be received, and the harness sends at most ±1%
    off that rate.
- **Speed range.** DIV ≥ 15 is required. At 40 MHz this covers 4,883 to 2.5 M baud.

### 5.6 SPI master (mode 0, MSB first)

- A write to SPI_DATA while BUSY = 0 sets BUSY = 1 and shifts 8 bits.
- **Each bit:**
  1. MOSI (`uo[6]`) is set while SCK (`uo[5]`) is low.
  2. SCK stays low for `2^D` cycles, then high for `2^D` cycles.
  3. The bit shifted in is the MISO pin value (`ui[3]`) at the rising SCK edge, or up to 3 cycles
     after it (synchronizer delay allowed).
- BUSY clears after the eighth high phase ends with SCK low. SPI_DATA then reads the received
  byte.
- D ≥ 1 is required (D = 0 is UNSPECIFIED): the harness's device holds each MISO bit from the
  rising edge through the 3 cycles after it, which needs a high phase of at least 2 cycles.
- A write while BUSY is ignored.
- CS_n (`uo[4]`) is just SPI_CTRL[0].

### 5.7 GPIO edges

- An enabled pin's selected edge (synchronized) sets its GPIO_EPEND bit within 3 cycles.
- Each bit is W1C.

### 5.8 Interrupt line

- `mip.MEIP = OR(IRQ_PEND[4:0] & IRQ_EN[4:0])`, sampled at most 3 cycles late.
- Software finds the source in IRQ_PEND, then GPIO_EPEND.

## 6. Pin timing for GPIO

- `uo` pins in GPIO mode take a stored GPIO_OUT value no later than 1 cycle after the store
  retires. They change only because of such a store, or because of an ALTFN write.
- GPIO_IN reads `ui_in` as sampled during the load's execution, at most 3 cycles stale.

## 7. Signoff and score

- **Signoff clock: 40 MHz** (T = 25 ns, `CLOCK_PERIOD` in `src/config.json`), the user's target.
  - STA closes at every TT corner. The lead may move it in the performance phase, up to the
    memory limits of section 2 and a 50 MHz IO ceiling, unless a measurement proves more.
- **Physical:** the flow is TT's (LibreLane 3.0.14, sky130A, `sky130_fd_sc_hd`), in a **1x2 tile**.
  The user chose it on 2026-10-17: the full peripheral set measured about 20k µm² against about
  11.5k for a 1x1.
  Results count only when clean: DRC, LVS, antenna, setup and hold at every corner, and the
  precheck.
- **Build phase:** the MCU passes every gate and hardens clean in 1x2.
- **Performance phase:**
  - **Score:** CoreMark iterations per second, which is `f_clk / cycles per iteration`. Cycles
    come from the cosim CoreMark run (code in flash, data in PSRAM, the real chip models).
  - **Constraints:** every gate passes, the candidate hardens clean in 1x2, and every peripheral
    stays.
  - **Tiebreak:** synthesized area.
  - **Formal** runs at final signoff and on every tenth merged improvement. Every other gate runs
    on every candidate.

## 8. RVFI (formal and cosim builds only)

The ports and the equivalence rule are as in v2 section 6: the same names, NRET = 1, and logic
under `RISCV_FORMAL` that only observes. What changes:
- **`rvfi_intr = 1`** on the first instruction of a trap handler, whether entered by a trap or
  an interrupt. `rvfi_mode = 3`, `rvfi_ixl = 1`.
- **A synchronous trap** retires the trapping instruction with `rvfi_trap = 1`, `rd_addr = 0`,
  no memory write and `rvfi_pc_wdata = mtvec`. `rvfi_halt` is always 0.
- **An interrupt** retires nothing for the interrupted instruction. The handler's first
  instruction carries `rvfi_intr = 1` and `pc_rdata = mtvec`. The previous retirement's
  `pc_wdata` is the interrupted pc.
- **Register fields** follow v2:
  - Fields the instruction doesn't use report 0.
  - CSR instructions report rs1 for the register forms and 0 for the immediate forms, and `rd`
    gets the old CSR value.
  - A trapping branch or JALR reports truthful rs1 and rs2.
- **Memory fields** follow v2's misaligned convention. IO accesses use them too. CSR
  instructions, MRET and WFI report masks 0.
- **Bus attribution (v2's per-instruction transaction rules) is removed.** In its place, the
  checkers enforce:
  - section 2 on every cycle;
  - every PSRAM write writes only core-region bytes, or bytes of retired stores at their
    addresses with their data;
  - coherence (section 3), checked through the RVFI and ISS comparison.

## 9. What a hypothesis may change

`src/**` (except `src/config.json`) and its own tests in `src/test_*`. Physical hypotheses touch
`physical/**` when the lead assigns one. Everything else is contract.
