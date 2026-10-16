// review round 3 (c_real): real variable and real literals
module tt_um_fesens_r32v(input wire [7:0] ui_in, output wire [7:0] uo_out, input wire [7:0] uio_in, output wire [7:0] uio_out, output wire [7:0] uio_oe, input wire ena, input wire clk, input wire rst_n);
  assign uio_out = 8'd0; assign uio_oe = 8'b00001011;
  real r; always @(posedge clk) r <= 1.5; assign uo_out = r > 1.0 ? 8'd1 : 8'd0;
endmodule
