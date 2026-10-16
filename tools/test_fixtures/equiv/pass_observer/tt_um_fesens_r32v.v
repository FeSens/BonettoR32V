// Equivalence-gate fixture: a tiny core whose RISCV_FORMAL logic only observes.
// tools/equiv.py must PASS on this design, and it is Verilator -Wall clean in both builds.
`default_nettype none

module tt_um_fesens_r32v (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
`ifdef RISCV_FORMAL
    , `RVFI_OUTPUTS
`endif
);
    wire [7:0] gpio;
    wire       sck;
    wire       retire;
    wire [7:0] acc;

    fx_core core (
        .clk    (clk),
        .rst_n  (rst_n),
        .din    (ui_in),
        .miso   (uio_in[2]),
        .gpio   (gpio),
        .sck    (sck),
        .retire (retire),
        .acc    (acc)
    );

    assign uo_out  = gpio;
    assign uio_out = {4'b0000, sck, 1'b0, ^acc, ~retire};
    assign uio_oe  = 8'b0000_1011;

`ifdef RISCV_FORMAL
    // Observation only: an order counter and copies of internal state.
    reg [63:0] order_q;
    reg        valid_q;
    reg [7:0]  acc_q;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            order_q <= 64'd0;
            valid_q <= 1'b0;
        end else begin
            valid_q <= retire;
            if (valid_q) order_q <= order_q + 64'd1;
        end
    end
    always @(posedge clk) acc_q <= acc;

    assign rvfi_valid     = valid_q;
    assign rvfi_order     = order_q;
    assign rvfi_insn      = {24'd0, acc_q};
    assign rvfi_trap      = 1'b0;
    assign rvfi_halt      = 1'b0;
    assign rvfi_intr      = 1'b0;
    assign rvfi_mode      = 2'd3;
    assign rvfi_ixl       = 2'd1;
    assign rvfi_rs1_addr  = 5'd0;
    assign rvfi_rs2_addr  = 5'd0;
    assign rvfi_rs1_rdata = 32'd0;
    assign rvfi_rs2_rdata = 32'd0;
    assign rvfi_rd_addr   = 5'd1;
    assign rvfi_rd_wdata  = {24'd0, gpio};
    assign rvfi_pc_rdata  = order_q[31:0];
    assign rvfi_pc_wdata  = order_q[31:0] + 32'd4;
    assign rvfi_mem_addr  = 32'd0;
    assign rvfi_mem_rmask = 4'd0;
    assign rvfi_mem_wmask = 4'd0;
    assign rvfi_mem_rdata = 32'd0;
    assign rvfi_mem_wdata = 32'd0;
`endif

    wire _unused = &{ena, uio_in[7:3], uio_in[1:0], 1'b0};
endmodule
