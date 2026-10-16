# BonettoR32V contract (v1)

This file is the exact interface every candidate design must implement and every checker
(formal, cosim, gate-level test, ISS) assumes. Changing it is a contract bump: only the lead does
it, and all checkers move together. Hypothesis agents never edit it.

Notation: all design logic is clocked by `clk`. For an output or input `X`, `X(t)` is its value
after the rising clock edge `t` (so a flip-flop updated at edge `t` reads its inputs' `X(t-1)`).

## 1. Top module and pins

```verilog
module tt_um_fesens_r32v (
    input  wire [7:0] ui_in,    // GPIO in
    output wire [7:0] uo_out,   // GPIO out
    input  wire [7:0] uio_in,   // uio[2] = SPI MISO
    output wire [7:0] uio_out,  // uio[0] = CS_n, uio[1] = MOSI, uio[3] = SCK
    output wire [7:0] uio_oe,
    input  wire       ena,      // ignored (always 1 when powered)
    input  wire       clk,
    input  wire       rst_n
`ifdef RISCV_FORMAL
    , output wire rvfi_valid, ...   // the RVFI ports of section 6, written out (no macros)
`endif
);
```

| Pin | Dir | Function |
|---|---|---|
| `uio[0]` | out | SPI CS_n |
| `uio[1]` | out | SPI MOSI |
| `uio[2]` | in | SPI MISO |
| `uio[3]` | out | SPI SCK |
| `uio[7:4]` | in | unused, reserved for peripherals |
| `uo[7:0]` | out | GPIO out register |
| `ui[7:0]` | in | GPIO in |

- `uio_oe` is constant `8'b0000_1011` at all times. `uio_out[7:4]` and `uio_out[2]` are constant 0.
- Reset: `rst_n` is low for at least one rising edge. Every requirement below holds from the first
  rising edge at which `rst_n` is low. While `rst_n` is low, CS_n = 1, SCK = 0 and `uo_out` = 0.
  After the release, execution starts at `pc = 0`. Checkers assert reset only at the start of a run
  and check the rules below from the first rising edge after `rst_n` is first sampled low. A
  mid-run reset is out of scope for v1. The design should still deassert CS_n, but no checker
  tests it.
- Single clock domain: every flip-flop is clocked on the rising edge of `clk`.
- No latches in v1. sky130 latches only time safely (STA sees them) when enabled through a
  clock-gating cell from `clk`, and the formal and cosim tools can't read the PDK's UDP cell
  models. Contract v1.1 adds clock-gated latches with contract-owned cell models when the loop
  needs them.
- No negative-edge flip-flops, no internal tristates, no combinational loops.
- The design must not rely on power-up values. Registers, the register file included, may start
  unknown. Checkers randomize them (cosim) or leave them as X (gate-level test).
- Plain synthesizable Verilog-2005 only. No instantiated cells in v1; synthesis maps to
  `sky130_fd_sc_hd`.
- **Restricted preprocessor**, so every tool (Verilator, yosys, Icarus, LibreLane) sees the same
  design:
  - The only compiler directives allowed in `src/*.v` are `` `ifdef RISCV_FORMAL ``,
    `` `ifndef RISCV_FORMAL ``, `` `else ``, `` `endif ``, `` `default_nettype `` and
    `` `timescale ``. That means no `` `define ``, macro uses, `` `include ``, `` `undef ``,
    `` `elsif `` or anything else. Use `parameter`/`localparam` instead.
  - No `initial` blocks, and no initializers on `reg` or `integer` declarations (parameters are
    fine).
  - No attribute instances (`(* ... *)`) other than the `@(*)` sensitivity list.
  - No tool metacomments (`verilator`, `synopsys`, `synthesis`, `pragma`, `yosys` or
    `translate_` directives in comments).
  - Module names: `tt_um_fesens_r32v`, or anything starting with `r32v_`. Nothing else, so no
    design module can shadow a checker's module.
  - Continuous-assignment net declarations (`wire w = expr;`) are fine; they aren't power-up
    values.
  - Every `src/*.v` file is part of the design. The gates read them in sorted order, and the lead
    keeps `info.yaml` `source_files` equal to that list.

## 2. SPI memory protocol (23LC512-compatible, mode 0)

The core is the SPI controller. Let S = SCK, M = MOSI, C = CS_n, I = MISO.

Waveform rules, which every check enforces:
1. **CS_n changes only while SCK is low.** C(t) != C(t-1) requires S(t-1) = 0 and S(t) = 0.
2. **Rising SCK edges.** A rising SCK edge is an edge `t` with S(t-1) = 0 and S(t) = 1, while C = 0.
   The memory samples MOSI there. MOSI must not change on that edge: M(t) = M(t-1).
3. **MISO is valid only while SCK is high.** The memory drives bit `k` of its response on I from
   the rising SCK edge that clocks bit `k` until SCK falls. While SCK is low, I is undefined: the
   checkers drive noise on it. A core samples MISO with a flip-flop at an edge `t` where S(t-1) = 1.
4. **Transaction framing.** A transaction runs from C falling to C rising. Its rising SCK edges
   carry, MSB first:
   - an 8-bit command: 0x03 READ or 0x02 WRITE (anything else is a protocol violation);
   - a 16-bit address A;
   - data bytes, MSB first within each byte. Byte `k` is at address `(A + k) mod 2^16`
     (sequential mode, wrapping at 64 KB).
   For a READ, the memory drives the data bits on MISO. For a WRITE, the core drives them on MOSI.
5. **WRITE transactions end only on a byte boundary.** The number of data bits is a positive
   multiple of 8, and every byte sent is written. A READ may end at any bit, and any number of
   bytes may be read.
6. The core sets the SCK rate: high and low phases are at least one clock cycle each, with no
   maximum. CS_n may stay low between bits for as long as the core likes. There's no ready signal:
   the memory never stalls.

The memory is 64 KB. Its contents are whatever the test loaded before reset was released.

## 3. Memory map

- **Instruction fetch.** Fetches read SPI RAM at `pc[15:0]`, whatever `pc[31:16]` is. The RAM
  aliases every 64 KB.
- **Data, `addr[31] = 0`: SPI RAM.** An access of `n` bytes (1, 2 or 4) at effective address
  `addr` touches the RAM bytes `(addr[15:0] + k) mod 2^16`, for `k < n`. Misaligned addresses are
  legal and behave exactly this way.
- **Data, `addr[31] = 1`: GPIO.**
  - A read of the byte at an address `X` with `X[1:0] = 0` returns `ui_in`. Every other IO byte
    reads 0. For an access of `n` bytes, byte `k` corresponds to address `X = addr + k`.
  - A store that writes the byte at an address `X` with `X[1:0] = 0` sets `uo_out` to that byte.
    Writes to other IO bytes are ignored.
  - Misaligned IO accesses (`addr[1:0] != 0` with `n > 1`) are unspecified; no test uses them.
  - An IO access never appears on the SPI pins.
- **`ui_in` timing.** An IO load samples `ui_in` after its own fetch transaction has completed
  (CS_n has risen after the fetch) and before its own retirement. The cosim changes `ui_in` only
  shortly after retirements (within 2 cycles), and formal holds it constant.
- **`uo_out` timing.** `uo_out` changes only as the result of an IO store that writes byte 0. It
  holds the new value no later than one cycle after that store retires.

## 4. Execution environment (EEI)

- **ISA.** RV32E unprivileged: RV32I's 37 computational, memory and control instructions plus
  FENCE, ECALL and EBREAK. Only x0-x15 exist, and x0 reads as 0.
- **Fatal traps.** These instructions trap:
  - ECALL and EBREAK;
  - JAL, JALR or a taken branch whose target has `target[1] = 1` (JALR clears bit 0 first);
  - the all-zeros word `0x00000000` and the all-ones word `0xFFFFFFFF`.
  A trapping instruction retires with `rvfi_trap = 1` and `rvfi_halt = 1`. It has no
  architectural effect: no register write and no memory write, on the pins or in RVFI. Then the
  core never retires again and never starts another SPI WRITE. Its `rvfi_pc_wdata` is
  unspecified.
- **Reserved encodings are UNSPECIFIED.** That covers every other encoding that isn't one of the
  instructions above, including RV32E encodings whose register fields name x16-x31. The core may
  give them any effect, but two rules hold:
  - It must either retire them or trap and halt. It may never hang on one: liveness applies.
  - Its RVFI trace must stay consistent: register reads return the last value written, PCs chain,
    and memory traffic matches the pins.
  Cosim programs never contain reserved encodings.
- **Latency cap.** The first instruction retires within 1,024 cycles of reset release, and every
  later one within 1,024 cycles of the previous retirement. A halted core is exempt.
- **FENCE** is a no-op that retires normally. That covers every MISC-MEM encoding with
  `funct3 = 000`, whatever its fm, pred, succ, rs1 and rd fields hold (x16-x31 included):
  FENCE.TSO, PAUSE and the rest. The spec requires base implementations to ignore those fields.
  FENCE.I (`funct3 = 001`, Zifencei) isn't part of RV32E, so it's reserved.
- **Misaligned loads and stores never trap** (section 3).
- **Program termination.** Test programs end with EBREAK. The checkers detect the halt and read
  results from RAM and GPIO.

## 5. Signoff and physical rules

- STA closes at a 50 ns clock period (20 MHz). It runs at every corner TT's flow checks, with
  TT's IO constraints.
- The cell library is `sky130_fd_sc_hd`. The flow is TT's: LibreLane 3.0.14, sky130A.
- Physical results count only when clean: DRC (magic and klayout), LVS, antenna, setup and hold at
  all corners, and the precheck.

## 6. RVFI (formal and cosim builds only)

Ports exist only under `` `ifdef RISCV_FORMAL ``, written out explicitly (macros aren't allowed in
`src/`). Every checker compiles `src/` with exactly one define for the RVFI build: `RISCV_FORMAL`. Logic under that define may only compute RVFI
outputs; nothing that drives a pin may depend on it. The equivalence gate enforces this: the RVFI
build with its RVFI ports stripped must be equivalent to the plain build. The pin-level cosim and
the gate-level test also run the plain build.

The core has one retirement channel (NRET = 1), with riscv-formal's standard names:
`rvfi_valid, rvfi_order[63:0], rvfi_insn[31:0], rvfi_trap, rvfi_halt, rvfi_intr, rvfi_mode[1:0],
rvfi_ixl[1:0], rvfi_rs1_addr[4:0], rvfi_rs2_addr[4:0], rvfi_rs1_rdata[31:0],
rvfi_rs2_rdata[31:0], rvfi_rd_addr[4:0], rvfi_rd_wdata[31:0], rvfi_pc_rdata[31:0],
rvfi_pc_wdata[31:0], rvfi_mem_addr[31:0], rvfi_mem_rmask[3:0], rvfi_mem_wmask[3:0],
rvfi_mem_rdata[31:0], rvfi_mem_wdata[31:0]`.

Conventions:
- **One instruction per pulse.** `rvfi_valid` pulses for one cycle per retired instruction, and
  `rvfi_order` counts retirements from 0. `rvfi_intr = 0`, `rvfi_mode = 3`, `rvfi_ixl = 1`.
- **Register fields** report the register index the core actually read or wrote. Unused fields
  report 0 with rdata 0, and a write to x0 reports `rd_addr = 0, rd_wdata = 0`.
- **Memory fields** use riscv-formal's misaligned convention: `rvfi_mem_addr` is the exact address
  of the first byte, the masks are LSB-aligned (`4'b0001`, `4'b0011` or `4'b1111`), and byte lane
  `k` is the byte at `mem_addr + k`. IO accesses use the same fields.
- **Transaction order.** Every SPI transaction belongs to the instruction that retires next. A
  fetch may only read that instruction's bytes, and a load or store may only move that
  instruction's data. All of an instruction's SPI traffic finishes before its `rvfi_valid` cycle,
  with CS_n high at that cycle.
- **Fetch and load completeness.** Every retired instruction is fetched by its own SPI READ
  covering all 4 of its bytes: no caching or reuse of earlier fetches. Every RAM load reads all of
  its bytes over SPI.
- **On a trapping retirement**, the checkers compare only `rvfi_trap`, `rvfi_halt`, `rvfi_insn`
  and `rvfi_pc_rdata`, plus the absence of side effects (`rd_addr = 0`, no memory write). The
  register-read fields aren't compared.

## 7. What a hypothesis may change

- `src/**` (except `src/config.json`), and its own unit tests in `src/test_*`.
- Physical hypotheses: `physical/**`, when the lead assigns one.

Everything else is contract: `verif/`, `tools/`, `test/`, `third_party/`, `info.yaml`,
`src/config.json`, `docs/contract.md`, `CLAUDE.md`, `Makefile`, `.github/`.
