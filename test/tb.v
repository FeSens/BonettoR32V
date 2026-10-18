`default_nettype none
`timescale 1ns / 1ps

/*
 * Tiny Tapeout testbench for tt_um_fesens_r32v, contract v3 (docs/contract.md).
 *
 * The TT QSPI Pmod (flash on uio[0], SD0/SD1 on uio[1:2], SCK on uio[3], SD2/SD3 on uio[4:5],
 * PSRAM A on uio[6], PSRAM B CS_n on uio[7]) is not a Verilog model: test.py runs the C++ chip
 * models (verif/cosim/pmod.h) through ctypes (pmod_ctypes.py), feeds them uio_out/uio_oe once per
 * clock and drives uio_in from them, as the cosim harness does.
 *
 * Each cocotb test gets its own copy of the design (a slot: s0, s1, ...), so every test starts
 * from Icarus X power-up (no initial values anywhere, contract 1). A slot whose clock never
 * toggles costs nothing, so the idle copies don't slow the active one down.
 *
 * Gate level: GL_TEST. TT's CI netlist has VPWR/VGND ports (the Makefile defines USE_POWER_PINS);
 * an unpowered synthesis netlist has none (the Makefile defines NO_POWER_PINS).
 *
 * Waves: `make WAVES=1` (cocotb's own dump: sim_build/<rtl|gl>/tb.fst). Off by default: the runs
 * are long and the files big.
 */
module tb_slot ();
  reg clk;
  reg rst_n;
  reg ena;
  reg [7:0] ui_in;
  reg [7:0] uio_in;  // from the Pmod model, set by test.py at every falling clk edge
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;
  wire [23:0] outs = {uo_out, uio_out, uio_oe};  // one read per sample in test.py
`ifdef GL_TEST
`ifndef NO_POWER_PINS
  wire VPWR = 1'b1;
  wire VGND = 1'b0;
`endif
`endif

  tt_um_fesens_r32v user_project (
      // Include power ports for the Gate Level test:
`ifdef GL_TEST
`ifndef NO_POWER_PINS
      .VPWR(VPWR),
      .VGND(VGND),
`endif
`endif
      .ui_in  (ui_in),    // Dedicated inputs
      .uo_out (uo_out),   // Dedicated outputs
      .uio_in (uio_in),   // IOs: Input path
      .uio_out(uio_out),  // IOs: Output path
      .uio_oe (uio_oe),   // IOs: Enable path (active high: 0=input, 1=output)
      .ena    (ena),      // enable - goes high when design is selected
      .clk    (clk),      // clock
      .rst_n  (rst_n)     // not reset
  );
endmodule

module tb ();
  // one slot per cocotb test (test.py SLOTS)
  tb_slot s0 ();
  tb_slot s1 ();
  tb_slot s2 ();
  tb_slot s3 ();
  tb_slot s4 ();
endmodule
