// iCE40UP5K fit check: the TT pins as FPGA pins, uio through tristate SB_IO (as TT's breakout does).
module fpga_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    inout  wire [7:0] uio
);
    wire [7:0] uio_in, uio_out, uio_oe;
    genvar i;
    generate for (i = 0; i < 8; i = i + 1) begin : g_uio
        SB_IO #(.PIN_TYPE(6'b1010_01), .PULLUP(1'b0)) io (
            .PACKAGE_PIN(uio[i]), .OUTPUT_ENABLE(uio_oe[i]),
            .D_OUT_0(uio_out[i]), .D_IN_0(uio_in[i]));
    end endgenerate
    tt_um_fesens_r32v dut (
        .ui_in(ui_in), .uo_out(uo_out), .uio_in(uio_in), .uio_out(uio_out), .uio_oe(uio_oe),
        .ena(1'b1), .clk(clk), .rst_n(rst_n));
endmodule
