`define SIMD_WIDTH 4
`define SIMPLE_DECODE
module tt_um_fesens_r32v(input wire clk, output wire [7:0] uo_out);
`ifdef SIMPLE_DECODE
  assign uo_out = `SIMD_WIDTH;
`endif
endmodule
