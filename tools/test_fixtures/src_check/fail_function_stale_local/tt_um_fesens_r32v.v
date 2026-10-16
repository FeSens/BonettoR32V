// round 3: function local read before written; Icarus RTL gives a5, the netlist xx
`default_nettype none
module tt_um_fesens_r32v(input wire [7:0] ui_in, output wire [7:0] uo_out, input wire [7:0] uio_in, output wire [7:0] uio_out, output wire [7:0] uio_oe, input wire ena, input wire clk, input wire rst_n);
  assign uio_out = 8'd0; assign uio_oe = 8'b00001011;
  wire _unused = &{uio_in, ena, rst_n, 1'b0};
  function [7:0] f; input [7:0] a; reg [7:0] t; begin f = t; t = a; end endfunction
  reg [7:0] q; always @(posedge clk) q <= f(ui_in); assign uo_out = q;
endmodule
