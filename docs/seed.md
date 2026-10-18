# The seed core (champion zero)

**History.** This page describes the contract v2 seed (`docs/contract-v2.md`), champion zero of
the area loop. The contract v3 MCU seed (plan Task 8) that replaced it is described in the header
comment of `src/tt_um_fesens_r32v.v` and in its unit tests, `src/test_v3/`.

`src/tt_um_fesens_r32v.v` was a deliberately plain, correct RV32E. Every gate was proven against
it first; the area loop shrank it later. The interface was `docs/contract-v2.md`; this page only
describes how the seed implemented it.

## Datapath

- **Register file:** 15 x 32 flip-flops (x1..x15), no reset; x0 reads as 0.
- **ALU:** one 32-bit adder/subtractor, compare (signed and unsigned), logic ops and a
  single-cycle barrel shifter. Separate adders compute `pc + 4`, the PC-relative target
  (JAL, branches, AUIPC) and `rs1 + imm` (load/store address, JALR target).
- **SPI:** one 56-bit shift register `sr` holds `{command, address, data}` and shifts out of
  bit 55 (MOSI) with MISO shifted into bit 0, plus a 6-bit bit counter. After an n-byte READ
  the data sits in `sr[8n-1:0]`, first byte highest; a byte swap turns it into the
  little-endian value.
- **Instruction register** `ir` (32 bits), `pc` (32 bits), GPIO output register (8 bits).

Only `state`, `cs_n`, `sck`, `pc` and `uo_out` have a (synchronous) reset. Everything else is
loaded before it is used, so power-up values never matter.

## States

| State | Cycles | What happens |
|---|---|---|
| `S_FETCH` | 1 | CS_n high. The next edge drops CS_n and loads `sr = {0x03, pc[15:0], 0}`. |
| `S_FSPI` | 112 | Fetch READ: 56 bits, 2 cycles per bit. |
| `S_FEND` | 1 | SCK low. The next edge raises CS_n and loads `ir`. |
| `S_EXEC` | 1 | Strict decode, register read, ALU, target, trap check. A RAM load or store drops CS_n and loads `sr = {0x03 or 0x02, addr[15:0], store bytes}`; a GPIO load samples `ui_in` into `sr`. |
| `S_MSPI` | 48 + 16n | Data READ or WRITE of n = 1, 2 or 4 bytes at the exact byte address. |
| `S_MEND` | 1 | The next edge raises CS_n. |
| `S_MGAP` | 1 | CS_n high for a whole cycle before retirement. |
| `S_WB` | 1 | `rvfi_valid`. The next edge writes rd, `pc` and `uo_out`, then goes to `S_FETCH` (or `S_HALT` after a trap). |
| `S_HALT` | forever | After a fatal trap. CS_n high, SCK low, until reset. |

Reset holds `S_FETCH`, so the first edge with `rst_n` high already starts the first fetch at
`pc = 0`.

## SPI timing

Let edge `e0` be the one where CS_n falls, and N = 24 + 8n the transaction's bit count
(N = 56 for a fetch).

- `e0`: CS_n falls with SCK low; MOSI shows bit 0 (`sr[55]`).
- Bit `i`: SCK rises at edge `e0 + 1 + 2i` (MOSI unchanged there) and falls at `e0 + 2 + 2i`.
  The falling edge shifts `sr`: MOSI moves to bit `i + 1` and, for READ data bits, MISO is
  captured (SCK was high in the cycle before, as contract rule 3 requires).
- `e0 + 2N + 1`: CS_n rises with SCK low before and after.

MOSI is forced to 0 while CS_n is high. While the bit counter is 32 or more, a 0 is shifted
in instead of MISO, so MOSI never echoes MISO noise while CS_n is low. Misaligned accesses and
the 64 KB wrap need no logic: the RAM's sequential mode delivers bytes `addr[15:0] + k` mod
2^16. GPIO accesses (`addr[31] = 1`) never touch SPI.

## Cycle counts

Measured in the RISCV_FORMAL build by `src/test_seed_smoke.py` (cycles from the previous
retirement to this one, identical across runs; written to `build/seed_smoke/cycles_seed*.json`):

| Class | Cycles |
|---|---|
| ALU (OP, OP-IMM, LUI, AUIPC), branches (taken or not), JAL, JALR, FENCE, GPIO loads and stores, traps | 116 |
| RAM load or store, 1 byte | 182 |
| RAM load or store, 2 bytes | 198 |
| RAM load or store, 4 bytes | 230 |
| First instruction after reset release | 114 |

So 116 = 1 + 112 + 1 + 1 + 1, and a RAM access adds 2 x (24 + 8n) + 2. The worst gap is 230
cycles, well under the 1,024-cycle latency cap.

## Decoding

Strict: anything that is not one of RV32I's 37 instructions or FENCE takes the trap path. That
includes ECALL, EBREAK, the all-zeros and all-ones words, any register field naming x16-x31
in a field the format uses, wrong funct3/funct7 values, SLLI/SRLI/SRAI with `imm[5] = 1`,
FENCE.I and every other SYSTEM or custom opcode. Every MISC-MEM encoding with `funct3 = 000`
is a FENCE and retires as a no-op, whatever its fm, pred, succ, rd and rs1 fields hold (the ISA
says base implementations ignore them). JAL, JALR (after clearing bit 0) and taken branches
whose target has bit 1 set trap too.

A trap retires with `rvfi_trap = rvfi_halt = 1`, no register write, no memory traffic and no
GPIO change, then the core sits in `S_HALT`.

## RVFI timing (RISCV_FORMAL builds only)

- `rvfi_valid` is high for the single `S_WB` cycle. All RVFI fields are combinational from
  `ir`, the register file, `pc` and `sr`, which are stable from `S_EXEC` to `S_WB`;
  `rvfi_order` is the only RVFI-only flop.
- CS_n is high in the `rvfi_valid` cycle and in the cycle before it, so every transaction of
  the instruction has ended at least one full cycle before retirement. CS_n also stays high
  for the whole cycle after `rvfi_valid` (`S_FETCH`); the next fetch drops it on the edge that
  ends that cycle.
- A GPIO load samples `ui_in` at the end of `S_EXEC`, one cycle before `rvfi_valid`.
- A GPIO store that writes byte `X[1:0] = 0` updates `uo_out` at the edge ending `S_WB`:
  `uo_out` shows the new value from the cycle after `rvfi_valid`.
- Register fields report what was read or written: rs1 and rs2 only for formats that use
  them (including a trapping JALR or branch), 0 otherwise, and `rd_addr = rd_wdata = 0` for
  x0 or a trap. Memory fields use the exact first-byte address with LSB-aligned masks and data;
  GPIO accesses use the same fields.

## Size

A rough number, not the loop's score: `yosys -p "read_verilog src/tt_um_fesens_r32v.v; synth
-top tt_um_fesens_r32v; stat"` reports 4,934 generic cells, 631 of them flip-flops.

## Tests

`.venv/bin/python -m pytest -v src/test_seed_smoke.py` runs the cocotb smoke test (Icarus,
`-g2005`) in the plain and the RISCV_FORMAL builds. See the module docstring for what it
checks.
