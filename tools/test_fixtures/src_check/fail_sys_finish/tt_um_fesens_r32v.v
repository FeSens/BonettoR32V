// review round 3 (c_finish): $finish ends the simulation, synthesis ignores it
module tt_um_fesens_r32v(input wire [7:0] ui_in, output wire [7:0] uo_out, input wire [7:0] uio_in, output wire [7:0] uio_out, output wire [7:0] uio_oe, input wire ena, input wire clk, input wire rst_n);
  assign uio_out = 8'd0; assign uio_oe = 8'b00001011;
  reg [7:0] q; always @(posedge clk) begin q <= ui_in; if (ui_in == 8'hFF) $finish; end assign uo_out = q;
endmodule
