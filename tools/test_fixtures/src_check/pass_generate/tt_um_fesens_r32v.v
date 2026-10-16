`default_nettype none
`timescale 1ns/1ps
module tt_um_fesens_r32v(input wire clk, input wire rst_n, input wire [7:0] ui_in, output wire [7:0] uo_out);
  localparam [3:0] S_IDLE = 4'd0, S_RUN = 4'd1;
  parameter W = 8;
  function [7:0] f;
    input [7:0] x;
    integer k;
    begin
      f = 8'd0;
      for (k = 0; k < 8; k = k + 1) f[k] = x[7-k];
    end
  endfunction
  genvar i;
  reg [7:0] q;
  generate for (i = 0; i < W; i = i + 1) begin : g
    always @(posedge clk) if (!rst_n) q[i] <= 1'b0; else q[i] <= ui_in[i] ^ q[(i+1)%W];
  end endgenerate
  assign uo_out = f(q);
endmodule
`default_nettype wire
