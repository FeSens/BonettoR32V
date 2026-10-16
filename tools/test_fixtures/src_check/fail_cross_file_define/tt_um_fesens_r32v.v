module tt_um_fesens_r32v(input wire clk, input wire [7:0] ui_in, output wire [7:0] uo_out);
`ifdef FAST_PATH
  assign uo_out = ui_in;
`else
  assign uo_out = ~ui_in;
`endif
endmodule
