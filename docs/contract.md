# BonettoR32V contract (v2)

v2 (2026-10-17, the user's decision): architectural state may live in a reserved region of the
SPI RAM (section 3), Subservient-style; per-instruction SPI traffic is generalized to allow it
(section 6). Reserved-encoding decode stays spec-minimum: strict decode is not required.

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
  mid-run reset is out of scope. The design should still deassert CS_n, but no checker
  tests it.
- Single clock domain: every flip-flop is clocked on the rising edge of `clk`.
- No latches. sky130 latches only time safely (STA sees them) when enabled through a
  clock-gating cell from `clk`, and the formal and cosim tools can't read the PDK's UDP cell
  models. A later contract version may add clock-gated latches with contract-owned cell models when the loop
  needs them.
- No negative-edge flip-flops, no internal tristates, no combinational loops.
- The design must not rely on power-up values. Registers, the register file included, may start
  unknown. Checkers randomize them (cosim) or leave them as X (gate-level test).
- Plain synthesizable Verilog-2005 only. No instantiated cells; synthesis maps to
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
  - **Synthesizable subset**, enforced by `tools/src_check.py`. `docs/eval.md` has the full list.
    The main rules:
    - no delay controls (`#` only for `module name #(` parameter ports and `name #(`
      instantiations);
    - every `always` takes an edge list or `@(*)` / `@*`, never a level-sensitive list like
      `@(a or b)`;
    - no `$` system tasks or functions except `$signed` and `$unsigned` (so no `$clog2`: write
      literal localparams);
    - no tasks, no strings, no real numbers, no escaped identifiers;
    - no SystemVerilog constructs;
    - no hierarchical references (`.` only in named port connections);
    - no gate primitives;
    - no special net types (only `wire`, `reg`, `integer`, `genvar`, `parameter`, `localparam`);
    - functions must assign every local before reading it, on every path.
  - Every top-level `src/*.v` file is part of the design, whatever its name (`test_*.v`
    included). Unit tests live in subdirectories (`src/test_*/`) or in non-`.v` files. The gates
    read the design files in sorted order, and the lead keeps `info.yaml` `source_files` equal to
    that list.

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

- **Core region: RAM bytes 0xFF80-0xFFFF (128 bytes), in every 64 KB alias.** It is reserved for
  the core's architectural state: x1..x15, the pc and core scratch, in any layout and bit order the
  core likes. The core may also keep any of that state on the die instead.
  - A program fetch or data access that touches a region byte is UNSPECIFIED: the core may return
    or change its own state, but it must still retire or trap and halt (liveness applies). Checkers
    stop comparing a run at the first retirement whose fetch or data access touches the region
    (only liveness and the SPI waveform rules still apply after it). Test programs never touch the
    region (stack top 0xFF80).
  - The region's contents at reset are arbitrary, like on-die registers (section 1). The pc still
    starts at 0 (section 1), so a core keeping the pc in the region must write it before use.
  - Checkers exclude the region from final-RAM comparisons.
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
  architectural effect: no register write in RVFI and no program-memory write on the pins (it may
  still make core-region transactions before it retires, since the region is core-private and
  nothing observes it after the halt). Then the core never retires again and never starts another
  SPI WRITE. Its `rvfi_pc_wdata` is unspecified.
- **Reserved encodings are UNSPECIFIED.** That covers every other encoding that isn't one of the
  instructions above, including RV32E encodings whose register fields name x16-x31. The core may
  give them any effect, but two rules hold:
  - It must either retire them or trap and halt. It may never hang on one: liveness applies.
  - Its RVFI trace must stay consistent: register reads return the last value written, PCs chain,
    and memory traffic matches the pins.
  Cosim programs never contain reserved encodings.
- **Latency cap.** The first instruction retires within 1,024 cycles of reset release, and every
  later one within 1,024 cycles of the previous retirement. A halted core is exempt.
  - **Counting:** number the rising clock edges so that edge 0 is the last edge at which `rst_n`
    is sampled low.
  - A retirement happens at edge e when `rvfi_valid(e) = 1` (the header's notation: the value
    right after edge e). The first retirement must happen at an edge numbered 1,024 or lower.
  - Each later retirement must come at most 1,024 edges after the previous one.
  - Every checker (cosim watchdog and liveness fuzz, formal liveness) uses this count.
  - The cap applies to every instruction, including reserved encodings and accesses whose
    effect is unspecified (region accesses, misaligned IO): they must retire or trap in time.
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
- **Register fields** report the register index the core actually read or wrote. A field the
  instruction's specification doesn't use reports 0 with rdata 0, even if the core read a
  register for it, and a write to x0 reports `rd_addr = 0, rd_wdata = 0`.
- **Memory fields** use riscv-formal's misaligned convention: `rvfi_mem_addr` is the exact address
  of the first byte, the masks are LSB-aligned (`4'b0001`, `4'b0011` or `4'b1111`), and byte lane
  `k` is the byte at `mem_addr + k`. IO accesses use the same fields.
- **Transaction order.** Every SPI transaction belongs to the instruction that retires next. All of
  an instruction's SPI traffic finishes before its `rvfi_valid` cycle, with CS_n high at that
  cycle. A transaction is either:
  - a **region transaction**: every byte it addresses, `(A + k) mod 2^16`, is in 0xFF80-0xFFFF
    (it may not wrap out of the region). Any number, READ or WRITE, in any order relative to the
    instruction's other transactions. A region WRITE still ends on a byte boundary (rule 5);
  - or a **program transaction**, which touches no region byte:
    - exactly one **fetch**: a READ of exactly that instruction's 4 bytes (`pc[15:0] + k`,
      k < 4), no more;
    - for a RAM load, READs that read only that load's bytes and together read every one of them;
    - for a RAM store, WRITEs that write only that store's bytes, each byte exactly once (a store
      may be split into several WRITEs);
    - nothing else (IO accesses never appear on the pins).
  - or a **null READ**: a READ whose CS_n rises after its full 24-bit header with no data bit
    clocked. It touches no byte; any number are allowed, anywhere.
  - A READ's partly clocked last byte counts as addressed (so a fetch with one extra bit fails,
    and a region READ that clocks a bit past 0xFFFF is not a region transaction).
- **Fetch and load completeness.** Every retired instruction is fetched by its own SPI READ
  covering all 4 of its bytes: no caching or reuse of earlier fetches. Every RAM load reads all of
  its bytes over SPI.
- **Register values.** `rvfi_rs1_rdata` / `rvfi_rs2_rdata` report the values the core actually
  read, from the die or from the region. Register consistency (a read returns the last value
  written) is checked on the core plus its RAM: the formal RAM model is a real memory for the
  region, with an arbitrary initial value.
- **On a trapping retirement**, the checkers compare only `rvfi_trap`, `rvfi_halt`, `rvfi_insn`
  and `rvfi_pc_rdata`, plus the absence of side effects (`rd_addr = 0`, no program-memory write).
  The register-read fields aren't compared, except on a trapping branch or JALR: there the trap
  depends on the operands, so `rs1`/`rs2` addr and rdata must be truthful and are checked (the
  register-consistency check covers those reads too).

## 7. What a hypothesis may change

- `src/**` (except `src/config.json`), and its own unit tests in `src/test_*`.
- Physical hypotheses: `physical/**`, when the lead assigns one.

Everything else is contract: `verif/`, `tools/`, `test/`, `third_party/`, `info.yaml`,
`src/config.json`, `docs/contract.md`, `CLAUDE.md`, `Makefile`, `.github/`.
