`default_nettype none
`timescale 1ns / 1ps

/*
 * Tiny Tapeout testbench: tt_um_fesens_r32v with the 23LC512 SPI RAM model on uio (contract
 * section 1: uio[0] CS_n, uio[1] MOSI, uio[2] MISO, uio[3] SCK). test.py drives clk, rst_n, ena
 * and ui_in and checks uo_out.
 *
 * Gate level: GL_TEST. TT's CI netlist has VPWR/VGND ports (the Makefile defines USE_POWER_PINS);
 * an unpowered synthesis netlist has none (the Makefile defines NO_POWER_PINS).
 */
module tb ();

  // Dump the signals to a FST file. You can view it with gtkwave or surfer.
  initial begin
    $dumpfile("tb.fst");
    $dumpvars(0, tb);
    #1;
  end

  reg clk;
  reg rst_n;
  reg ena;
  reg [7:0] ui_in;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;
  wire miso;
  // The unused uio inputs are X: a design that reads them fails.
  wire [7:0] uio_in = {5'bxxxxx, miso, 2'bxx};
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

  spiram_23lc512 #(.HEX("gpio_demo.hex")) ram (
      .clk  (clk),
      .rst_n(rst_n),
      .cs_n (uio_out[0]),
      .sck  (uio_out[3]),
      .mosi (uio_out[1]),
      .miso (miso)
  );

endmodule
