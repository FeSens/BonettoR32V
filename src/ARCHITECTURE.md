# BonettoR32V hypothesis 001: bit-serial RV32E

The core in `tt_um_fesens_r32v.v` is a bit-serial RV32E. It has one module, plain Verilog-2005
and the contract v1 preprocessor subset. Every 32-bit value is a circular shift register, and
the datapath handles one bit per clock.

## 1. Microarchitecture

### Rings and the phase counter

- `ph` is a free-running 5-bit phase counter. It is reset to 0, and the reset leaves it in step
  with every ring.
- Every 32-bit ring rotates right by one on every clock: the 15 registers x1..x15 (`rfq`, 480
  flops) and the pc (`pcr`). The bit at the tap (bit 0) at a given clock is bit `ph` of the
  value.
- A register write is a mux at the ring's input: `r <= {we ? w : r[0], r[31:1]}`. The register
  file has no per-bit enables and no read muxes inside a ring. rs1, rs2 and the pc are 16:1 tap
  selections.
- A read and a write of the same bit in the same clock reads the old value. So `rd == rs1`
  needs no special case.
- Every instruction retires at `ph = 0`, where every ring holds its value in order. The RVFI
  shadow logic reads the rings directly at that point.

### Instruction register and decode

- `irs` (32 flops) shifts in MISO during the fetch.
- The 23LC512 sends byte 0 first and each byte MSB first. So after 32 shifts the bytes sit in
  reverse order, and `insn` is a pure rewiring of `irs`.
- Legality is the seed's strict decode: anything that isn't an RV32E instruction traps. That
  includes register fields x16-x31, FENCE.I and the bad funct7 values.
- The datapath class signals use only the few opcode bits that tell the legal classes apart.
- The immediate bit for the current phase comes from three shared selectors:
  - `insn[20 + ph]`
  - `insn[7 + ph]`
  - `insn[ph]`

  A small per-format mux on top of them picks the bit, so the core builds no parallel
  immediate.

### Serial ALU

- Operand A is selected from {0, rs1, pc} and operand B from {0, rs2, imm}.
- One full adder handles add and subtract. Its carry flop `cy` starts from `sub` at `ph = 0`.
- Logic ops use SERV's formula: `((a ^ b) & ~f3[0]) | (f3[1] & a & b)`.
- SLT/SLTU and the branch compares come from the subtract carry. The final `lt` is
  `(a & s) ^ ~(b & s) ^ cout`, where `s` says the compare is signed. BEQ/BNE accumulate
  equality in one flop (`cmp`).
- A separate serial incrementer makes pc + 4, with its carry in flop `c4`. The pc ring takes
  either pc + 4 or the ALU sum.

### The byte ring and the SPI

- `bufr` is an 8-bit ring that also rotates every clock. Bit `i` of `bufr` holds the value
  written at index `ph + i` (mod 8).
- Address bytes, store data and load data run at a slow SCK with a 7-clock period.
  - 7 is -1 mod 8, so consecutive SPI bits meet the ring at positions that walk backwards.
  - A fixed tap therefore reads or writes a byte in reverse order. That converts the
    datapath's LSB-first bits to the memory's MSB-first order with no extra flops.
- The slot grid is `d = ph[2:0] + j[2:0]`:
  - MOSI loads from `bufr[0]` at `d = 7`.
  - SCK is high while `d = 1`, and a received bit goes into the ring then.
  - The bit counter `j` advances at `d = 5`.
- The 8 command bits and the 32 instruction bits run at SCK = clk/2. Command bits come from
  logic; instruction bits shift straight into `irs`.
- Each SPI transaction sends the address as two bytes. The high byte travels through `bufr`
  while the low byte is computed, which costs no extra storage:
  - The fetch address is pc[15:0], taken from the pc ring.
  - The data address rs1 + imm is computed serially in the first execute pass, and only bits
    15..0 are kept in `bufr`.
- MOSI only changes at clocks where SCK stays low or falls, so it never changes on a rising
  SCK edge. CS_n only changes while SCK is low.

### Execution passes

- P1 is the first 32-clock pass. It does everything for ALU ops, LUI/AUIPC, JAL and FENCE.
- P2 is a second pass for the classes that need one:
  - **SLT/SLTU:** writes the compare result into rd.
  - **Branches:** P1 computes the compare, then P2 adds the offset to the pc if the branch is
    taken.
  - **JALR:** P1 checks target[1], then P2 writes rd = pc + 4 and pc = (rs1 + imm) & ~1.
  - **GPIO loads and stores:** run in P2.
  - **RAM loads and stores:** pass through P2 for one clock, then start the SPI data
    transaction.
- **Shifts:**
  - P1 copies rs1 into rd. It also loads `j` through the ALU with -shamt (left shifts) or
    +shamt (right shifts).
  - Each later P2 pass rotates rd left through `bufr`, by 8 or by the 1..7 that remain. It adds
    that amount to `j`, and the shift is done when `j` wraps to 0.
  - So a left shift by s rotates left by s, and a right shift rotates left by 32 - s.
  - In P1 and in the first P2 pass, bits that must not wrap around are replaced with the fill
    (0, or the sign for SRA) as they enter the delay line. Those are bits at index >= j for a
    left shift and index < j for a right shift. Flop `c4` holds the sign during a shift.

### Loads and stores

- A load receives byte k into `bufr` (state LRX), then writes it into rd during the 8-clock
  window `ph[4:3] = k` (state LWR).
- On the last byte, LWR keeps writing the sign or zero fill through `ph = 31`.
- A store loads byte k of rs2 into `bufr` during its window (state SLD), then sends it (state
  SEM).
- Misaligned accesses and the 64 KB wrap come for free: the core sends the exact byte address
  and the 23LC512 counts sequentially.
- GPIO (addr[31] = 1) never touches the SPI pins:
  - A GPIO load writes ui_in (or 0) into rd in P2. `cmp` holds the "address[1:0] == 0" flag.
  - A GPIO store loads `bufr` with rs2[7:0] in P2, and `uo_out` takes it at retirement.

### Traps

- Decode traps (illegal encodings, ECALL, EBREAK, JAL with imm[1] = 1) skip execution. They
  retire right after the fetch, with `rvfi_trap = rvfi_halt = 1`.
- A taken branch with offset bit 1 set, or a JALR with target[1] = 1, traps in P2. Its register
  and pc writes are gated off.
- After a trap the state machine sits in HALT: no more retirements and no SPI traffic.

### Reset

- Only the control flops have a reset: `st`, `ph`, `cs_n`, `sck`, `mem` and `uo_out`.
- After reset, state CLR writes 0 into the pc ring for one rotation.
- Nothing else is read before it is written.
- The x registers aren't cleared. RV32E leaves their reset value unspecified, and the checkers
  randomize them.

## 2. Timeline and cycle counts

### One instruction, from the previous retirement (`ph = 0`)

| Cycles | State | What happens |
|---|---|---|
| 1..6 | IDLE | wait |
| 7..22 | CMD | CS_n low, 8 command bits at clk/2 |
| 23..78 | AHI | address byte 1 (pc[15:8], from `bufr`), slow SCK |
| 79..110 | ALD | `bufr` <- address byte 0 |
| 111..166 | ALO | address byte 0, slow SCK |
| 167..230 | FDAT | 32 instruction bits at clk/2 |
| 231..255 | FEND | CS_n high, wait for `ph = 31` |
| 256..287 | P1 | first execute pass |
| 288 | RET | retire (or 288..319 P2, then retire at 320) |

### Measured cycles per instruction

These are cycles from the previous retirement, measured with `harness_rvfi` (stderr, per
class) on `selftest`, the trap programs and CoreMark.

| Class | Cycles |
|---|---|
| ALU reg/imm (not SLT), LUI, AUIPC, JAL, FENCE | 288 |
| SLT, SLTI, SLTU, SLTIU | 320 |
| Branch (taken or not), JALR | 320 |
| Shift by 0 / 1..8 / 9..16 / 17..24 / 25..31 (left; a right shift by s costs as left by 32 - s) | 288 / 320 / 352 / 384 / 416 |
| GPIO load or store (any width) | 320 |
| RAM load or store: byte / half / word | 544 / 608 / 736 |
| Decode trap (illegal, ECALL, EBREAK, JAL misaligned) | 256 |
| Branch or JALR misaligned-target trap | 320 |

- The maximum is 736 cycles (LW/SW). That's under the 1,024-cycle latency cap.
- The first instruction retires 32 cycles (CLR) plus its own count after reset release.
- Program averages measured with the same harness:
  - `selftest`: 845,793 cycles, 2,247 retirements, CPI 376.4.
  - CoreMark: 295,064,225 cycles, 791,682 retirements, CPI 372.7.

## 3. Flip-flop budget (582)

| Group | Flops | Purpose |
|---|---|---|
| `rfq` | 480 | x1..x15 rings |
| `pcr` | 32 | pc ring |
| `irs` | 32 | instruction register |
| `bufr` | 8 | byte ring: bit reversal, address high byte, store/load byte, shift delay line, GPIO store byte |
| `uo_out` | 8 | GPIO out register (contract) |
| `st` | 5 | state (binary encoding, used bitwise) |
| `ph` | 5 | phase |
| `j` | 5 | SPI bit counter, shift amount counter, load byte index |
| `cs_n`, `sck`, `mosi` | 3 | registered SPI outputs |
| `mem` | 1 | current transaction or pass is a data access |
| `cy` | 1 | ALU carry, load sign |
| `cmp` | 1 | compare, address[1:0] == 0, JALR target[1], first shift pass |
| `c4` | 1 | pc + 4 carry, SRA sign |
| **Total** | **582** | |

## 4. Area

Area was measured with the LibreLane 3.0.14 synthesis engine of the area tool (setup/physical
`tools/area.py --engine librelane`), sky130_fd_sc_hd, tt_025C_1v80:

- **Total:** 17,503 µm², 1,150 cells, 582 flops.
- **Flops:** 582 x `dfxtp_2` = 12,379 µm². That is 71% of the total, and it is the floor of
  this architecture.
- **Combinational logic:** 5,124 µm². Here is how it splits by the flop group each cone feeds
  (gates shared by several groups are split evenly):

| Cone | µm² |
|---|---|
| register file input muxes and tap selects | 1,022 |
| state machine | 780 |
| byte ring inputs | 515 |
| `j` | 478 |
| `cmp` (compares, flags) | 371 |
| `uo_out` load (includes the trap decode it depends on) | 323 |
| pc ring input | 316 |
| instruction register hold muxes | 377 |
| `cy` | 257 |
| `mem` | 244 |
| SPI outputs and the rest | 441 |

For comparison, the seed on main measured with the same engine and the same harness:

| | Seed | This core |
|---|---|---|
| Area | 47,725 µm² | 17,503 µm² |
| Cells | 4,065 | 1,150 |
| Flops | 631 | 582 |
| CPI on CoreMark | 130.8 | 372.7 |
| Max cycles per instruction | 230 | 736 |

That makes this core 2.73x smaller and 2.85x slower on CoreMark.
