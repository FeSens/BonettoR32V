// review round 3 (d_noclk): bare `always` (Icarus refuses it, Verilator and yosys accept it)
`default_nettype none
module tt_um_fesens_r32v(input wire [7:0] ui_in, output wire [7:0] uo_out, input wire [7:0] uio_in, output wire [7:0] uio_out, output wire [7:0] uio_oe, input wire ena, input wire clk, input wire rst_n);
  assign uio_out = 8'd0; assign uio_oe = 8'b00001011;
  wire _unused = &{ui_in, uio_in, ena, rst_n, clk, 1'b0};
  reg [7:0] q; always q = ui_in; assign uo_out = q;
endmodule
