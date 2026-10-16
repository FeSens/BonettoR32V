// review round 3 (d_wand): wired-AND net with two drivers
`default_nettype none
module tt_um_fesens_r32v(input wire [7:0] ui_in, output wire [7:0] uo_out, input wire [7:0] uio_in, output wire [7:0] uio_out, output wire [7:0] uio_oe, input wire ena, input wire clk, input wire rst_n);
  assign uio_out = 8'd0; assign uio_oe = 8'b00001011;
  wire _unused = &{ui_in, uio_in, ena, rst_n, clk, 1'b0};
  wand t; assign t = ui_in[0]; assign t = ui_in[1]; assign uo_out = {7'd0, t};
endmodule
