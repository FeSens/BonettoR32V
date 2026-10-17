`default_nettype none
module r32v_sub(input wire [7:0] a, output wire [7:0] y); parameter [7:0] K = 8'd1; assign y = a + K; endmodule
