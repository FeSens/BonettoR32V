`define P1 ini
`define P2 tial
module tt_um_fesens_r32v(input wire clk, input wire d, output wire [7:0] uo_out);
  reg r;
  `P1`P2 r = 1'b1;
  always @(posedge clk) r <= d;
  assign uo_out = {7'd0, r};
endmodule
