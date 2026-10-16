`define AS (*
`define AE *)
module tt_um_fesens_r32v(input wire clk, input wire [1:0] s, output reg [7:0] uo_out);
  always @(*) begin
    `AS full_case `AE
    case (s)
      2'd0: uo_out = 8'd1;
      2'd1: uo_out = 8'd2;
    endcase
  end
endmodule
