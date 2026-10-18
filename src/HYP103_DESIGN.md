# Hypothesis 103: quad bus and nibble-serial datapath (design note, checkpoint)

Status: design only, nothing built yet. Numbers below are projected, not measured.

## Bus (contract 2), SCK = clk/2, a slot of E SCK edges takes 2E + 2 cycles

- **Boot (r32v_mem), from all four power-up states:**
  1. flash: 8 edges, SD0-SD3 high (leaves continuous read, or the no-op 0xFF);
  2. PSRAM: 0xF5 in QPI form, then 0x66 and 0x99 in SPI form (SPI mode, initialized), then 0x35
     in SPI form (QPI mode), after tRST;
  3. flash: 0xEB (single), 6 quad address edges, M = 0x20 (2 edges), then CS_n high inside the
     dummy field (a read may end at any edge): continuous read.
  About 230 cycles.
- **Flash read** (fetch F, flash loads): continuous read, no command: 6 address + 2 M (0x20, so it
  stays in continuous read) + 4 dummy + quad data. 32 bits = 20 edges = 42 cycles (was 132).
- **PSRAM read** (P, A, B, PX, C, RS, PSRAM loads and fetches): QPI 0x0B: 2 command + 6 address +
  4 wait + quad data. 32 bits = 20 edges = 42 cycles. A shift's B reads one byte: 30 cycles.
- **PSRAM write** (WP, WP4, W4, W, WS, WC, stores): QPI 0x38: 2 + 6 + quad data. 32 bits = 34
  cycles, SH 26, SB 22.
- Loads always read 4 bytes (LB/LH cost +12/+8 cycles, 3.7 % of CoreMark instructions), so the
  ring layout after M is the same for every size. A store to flash issues nothing (a 0x02 is not
  possible in continuous read; contract 3 allows nothing).
- The core drives SD0-SD3 during the 8 header edges, releases them for the dummy/wait and data
  edges of a read, and drives again from the next CS_n-low edge. A masked slot (x0 operand or
  target, the first P) skips the header: data strobes only, 18 cycles.

## Datapath

- Nibble-serial: a 4-bit ALU (add, sub, boolean, compare) steps once per SCK edge. The ring is 32
  bits (8 nibbles) and shifts by one nibble at each data strobe (enable flops), so there is no
  phase counter and no alignment wait; `ph` goes away.
- Core-region words are stored LSB nibble first. Program data comes high nibble first in each
  byte: a 2:1 nibble tap (ring[7:4] on even strobes, ring[31:28] on odd ones) swaps them, in W for
  loads and in M for stores. The IR shifts 4 bits per strobe. The immediate is a 4-bit mux by
  nibble index. The MAR captures nibbles and emits them MSB first in the header.
- Shifts: no extra slot and no pre-roll. W rotates the ring by q nibbles (shamt = 4q + r) during
  its own 16-cycle header, then a 4-bit funnel ({ring[7:4], ring[3:0]} >> amount) with zone fill
  emits the result. shamt comes from the instruction (immediate forms) or from B reading one byte
  of rs2 into the MAR (register forms). SRA's sign is latched in `cmp` during A.
- Two cheap sequencing changes: a branch with rs2 = x0 (21 % of CoreMark instructions) compares in
  A and skips B; the shift-immediate forms drop the masked B slot.
- pc, x1..x15 and the CSR words stay in the core region (no overlap with hypothesis 101).

## Projection (CoreMark mix from the ISS, whole run)

Mix: shift-imm 22.2 %, ALU-imm 23.6 %, ALU-reg 10.0 %, branches 25.8 % (rs2 = x0 21.3 %), loads
10.7 %, stores 3.7 %, JAL/JALR 3.5 %.

| class | slots | cycles |
|---|---|---|
| ALU-imm, shift-imm | P F WP4 A W | 194 |
| ALU-reg | P F WP4 A B W | 236 |
| load / SW | P F WP4 A M W / P F WP4 A BS M | 236 |
| branch, rs2 = x0 | P F A PX WP | 202 |
| branch, other | P F A B PX WP | 244 |
| JAL, LUI, li | one masked slot | 170 |
| JALR rd = 0 | P F W4 A WP | 194 |

The same model gives the champion's CPI (762). New CPI about 207: about 157 M cycles per
iteration, about 0.25 iterations/s (3.7x). Worst retirement gap about 500 (interrupt entry plus a
CSR instruction); first retirement about 450.

Area: projected +1,100 to +1,600 um2 (ring enables +320; 4-bit ALU, immediate, emission and
funnel about +800; header nibble mux and boot +250; csr +50; `ph` removed -120): 21,550 to
22,050 um2 against the 22,000 um2 pre-filter. Fallbacks if it lands over: a 9-nibble free-running
ring (no enables, -240 um2, about +10 % cycles), dropping the masked-slot shortening, or small
peripheral savings.
