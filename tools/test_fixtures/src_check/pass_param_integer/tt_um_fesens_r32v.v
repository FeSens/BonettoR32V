module tt_um_fesens_r32v(input wire clk, output wire [7:0] uo_out);
  parameter integer N = 4;
  localparam integer DEPTH = 16;
  assign uo_out = N + DEPTH;
endmodule
