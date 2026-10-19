# QSPI Pmod audit against primary sources (pre-tapeout)

Every gate checks the design against our own models of the memory chips (verif/cosim/pmod.h, the
formal monitors, contract section 2), written from datasheets. A wrong model would let every gate
pass and the silicon fail. On 2026-10-19 (main fa98a50) the lead had an agent audit those models,
the contract and the RTL against the datasheets, TT's documents and the Pmod's schematic. The lead
re-checked the key facts in the downloaded sources. This file keeps the findings and what was done
about each.

## Sources

| Id | Source |
|---|---|
| PMOD | mole99/qspi-pmod v2.2 schematic and README, https://github.com/mole99/qspi-pmod |
| TTPIN | TT pinouts spec, "QSPI Flash and PSRAM", https://tinytapeout.com/specs/pinouts/ |
| TTGUIDE | TT guide "Configuring and flashing the QSPI Pmod", https://tinytapeout.com/guides/configure-flash-qspi-pmod/ |
| TTGPIO | TT GPIO spec (limits, TT 3.5 round-trip scope captures), https://tinytapeout.com/specs/gpio/ |
| FLASHER | TinyTapeout/tinytapeout-flasher, src/ttcontrol/ttflash.py |
| DTR | Winbond W25Q128JV-DTR datasheet Rev C (the IM part on the Pmod) |
| JVF | Winbond W25Q128JV datasheet Rev F |
| APS | AP Memory APS6404L-3SQR datasheet Rev 2.7 |
| TQV | tinyQV cpu/qspi_ctrl.v (Michael Bell; same Pmod), https://github.com/MichaelBell/tinyQV |

## Mismatches

| # | Finding | Evidence | Action |
|---|---|---|---|
| M1 | The flash is the W25Q128JV**SIM** (IM part): factory QE = 0, QPI capable. TT's store Pmods ship with QE = 1 because TT sets it; TT's flasher never writes the status register. The contract said "TT's flasher sets it". | PMOD (MPN W25Q128JVSIM), TTGUIDE ("25Q128JVSM", ID ef7018, "enabled by default on all first-party QSPI Pmods"), JVF 7.1.4, DTR 8.2.13, FLASHER | Contract EEI and manual corrected: QE = 1 is required; TT store Pmods have it; other boards run TT's activation script once. |
| M2 | The IM part has modes the boot does not undo: QPI (38h), deep power-down (B9h), dual I/O continuous read, wrapped bursts (77h), volatile status writes. A design selected earlier on the same board could leave one of them. | DTR 6.1.4, 8.2.11, 8.2.15, 8.2.24, 8.2.38-39 | Documented (contract EEI, manual 2.2 and section 11): power-cycle the board after a design that may use them. No RTL change. |
| M3 | PSRAM tCPH (CE# high between bursts) is 18 ns min. The core gave one gap clock, 20 ns at 50 MHz, and TT's pads delay rising edges about 4 ns more than falling ones, so the pulse at the PSRAM could be about 16 ns. tinyQV waits 2 cycles before reselecting a RAM. Neither model checked tCPH. | APS Table 10, TTGPIO (20.4 ns rising vs 16.3 ns falling round trip), TQV ("Allow 2 cycles before reselecting the same RAM") | Contract 2.1.2 and both models now check it (ceil((18 + 4) ns / T) cycles); RTL gives 2 gap clocks between PSRAM transactions (hyp 113). |

## Unverified (datasheets silent or ambiguous)

| # | Finding | Action |
|---|---|---|
| U1 | The boot entered EBh continuous read with a read aborted after the mode bits, before the dummy clocks. The datasheet documents entry through a read and exit by an abort after M, not entry by an abort. If the flash did not latch it, the chip would never boot, on every power-up. | Contract 2.2 and both models now require the 4 dummy and at least 2 data edges after M5-4 = 10; the boot completes that read with SD0-3 released (hyp 113). |
| U2 | A transaction that ends inside its command field is ignored (the boot's QPI-form F5 to a PSRAM in SPI mode). | Kept: the same sequence as SparkFun's widely used RP2350 APS6404L init. |
| U3 | Winbond notes "4-bytes address alignment for Quad Read" next to the frequency tables; flash byte loads use any address. | Kept: read as a maximum-frequency condition (the core runs SCK at 25 MHz, the part at up to 133 MHz). tinyQV fetches compressed instructions from this flash with EBh at 2-byte-aligned addresses (an inference from its design, not measured here). |
| U4 | The PSRAM reset is not stated to clear a Wrap-32 setting left by another design. | Low impact (only misaligned PSRAM accesses that cross a 32-byte boundary); covered by the power-cycle advice. |

## Verified OK

- Pin map (PMOD, TTPIN, TTGUIDE agree with contract 1, info.yaml and r32v_mem.v): uio[0] flash
  CS_n, [1] SD0, [2] SD1, [3] SCK, [4] SD2, [5] SD3, [6] PSRAM A CS_n, [7] PSRAM B CS_n. CS_n and
  SCK are always outputs; only SD0-SD3 switch direction.
- Every command field by field: flash EBh 1-4-4 (6 address, 2 mode, 4 dummy edges), mode bits
  M5-4 = 10, the FFh exit; PSRAM QPI 0Bh read (4 wait edges), QPI 02h write, SPI 66h/99h reset,
  SPI 35h enter quad, QPI F5h exit quad.
- tRST (1.58 µs vs 50 ns), tCEM (41 clocks max), tPU (EEI 150 µs), flash tSHSL1 (10 ns), setup and
  hold of commands and write data at the chips, bus turnaround, maximum command rates (SCK 25 MHz),
  image at flash offset 0 (the flasher's default).
- Read timing at 50 MHz (paper): the memory launches a nibble on a falling SCK edge and the core
  samples it 40 ns later. With TT 3.5's 20.4 ns round trip, flash tCLQV 6 ns, PSRAM tACLK 5.5 ns
  (+1 ns for the shared bus load) and about 2 ns inside the core: setup slack +11.1 to +11.6 ns;
  with a 35 % slower pad (27.5 ns round trip) about +4 ns. Hold slack is at least 1.5 ns at any
  clock. TT publishes no TTSKY26d corner data, so this stays a paper estimate.

## Platform limits (every TT design on this Pmod)

The PSRAM's clock edge rate (tKHKL ≤ 1.5 ns), its 15 pF load limit and (if read strictly) its
45-55 % clock duty cycle are not met by TT's pads and the shared Pmod bus. No RTL can change that;
TT reports the Pmod's clock driven at 32 MHz successfully.

## Other notes

- Clock floor: about 5.2 MHz from tCEM (cosim: 6.25 MHz passes, 5 MHz fails). Never stop the clock
  while the design runs.
- CS_n and SCK come from combinational logic; a sub-nanosecond glitch at some slot boundaries is
  possible in principle. TT's mux and pads filter pulses that short, and a 1-edge transaction would
  be an ignored partial command. Low risk; not changed.
- Re-flashing after a run needs a power cycle (the flasher does not leave continuous read).
- After flashing, select the project (the flasher leaves the RP2040 driving uio[0], [1], [3]).
