// review round 3 (esc1): Icarus expands the macro inside this escaped identifier
module tt_um_fesens_r32v(input wire clk, output wire [7:0] uo_out);
  wire \a`ifdef__ICARUS__ = 1'b1;
  assign uo_out = {7'd0, \a`ifdef__ICARUS__ };
endmodule
