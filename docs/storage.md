# Register-file storage: flip-flops versus latches (lead experiment, 2026-10-16)

**Setup.** yosys 0.64, `synth -flatten`, `dfflibmap` + `abc` against the sky130_fd_sc_hd
`tt_025C_1v80` liberty (copied from ORFS). The numbers are synthesized cell area, before
placement. The RTL is in the lead's scratchpad (`rfexp/`).

| Variant | Storage | Read and write logic | Total |
|---|---|---|---|
| 15 x 32 always-rotating flip-flop shift registers, two 15:1 serial taps, a 2:1 mux per register input | 480 x dfxtp_1 = 9,609 µm² | 15 x mux2 = 169, taps ~355 | **10,133 µm²** (measured) |
| 15 x 32 latch array, serial read of bit `bitp` from rs1 and rs2, parallel write from a 32-bit staging register | 480 x dlxtp_1 = 7,207 µm² (yosys left them unmapped; computed at 15.01 each) | 4,235 µm² measured (92 mux4_2 = 2,070) | **~11,442 µm² + staging 641-961 = ~12,100 µm²** |

**Conclusion.** For a serial core, the latch array's read muxes cost more than the latches save
over flip-flops. The rotating flip-flop file needs no read mux per bit and no enable, which makes
it the floor with standard cells, at about 10.1k µm² for the register file alone.

**Ideas kept for later**, because they could beat it:
- A latch ring with a few "bubbles". 36 latches hold 32 bits, and rotation runs through shared
  clock-gated enables. That saves roughly 100 µm² per register, about 1.5k µm² total, but it's
  slow (about 256 steps per word read) and needs contract v1.1 with careful latch timing.
- Check whether LibreLane's sky130 flow keeps dfxtp_1 (20.02 µm²) or upsizes it. NanoV's netlist
  shows dfxtp_2 (21.27 µm²), which adds 600 µm² over 480 bits.

## Update: what the real flow allows (2026-10-16)

`~/.ciel/sky130A/libs.tech/openlane/sky130_fd_sc_hd/no_synth.cells` (the PDK version LibreLane
3.0.14 uses) excludes `sky130_fd_sc_hd__dfxtp_1` and `edfxtp_1` from synthesis. Every inferred
flip-flop therefore becomes `dfxtp_2` at 21.27 µm², which H001's LibreLane-engine area confirms.
`dlxtp_1` (15.01 µm²) is not excluded.

Re-pricing the two register files at the real flip-flop cost:

| Variant | Storage | Logic | Total |
|---|---|---|---|
| Rotating flip-flops (dfxtp_2) | 480 x 21.27 = 10,210 µm² | ~520 µm² | ~10,730 µm² |
| Latch array, serial read | 480 x 15.01 = 7,207 µm² | ~4,235 µm² + staging ~700 | ~12,140 µm² |

Rotating flip-flops still win, by about 1.4k µm².

Two levers follow:
- **(a) Instantiate `dfxtp_1` explicitly.** It would save 1.25 µm² per flip-flop (~730 µm² on
  H001's 582). It needs contract v1.1: an allowed-cell list with contract-owned behavioral models
  for formal, cosim and equiv, plus the PDK model for the gate-level test. First, find out why
  the PDK excludes it (drive and slew? hold?).
- **(b) The latch bubble ring (backlog X1).** It gains more now that latches are 6.26 µm² per bit
  cheaper than flip-flops.
