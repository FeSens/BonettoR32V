// Equivalence-gate fixture: like pass_observer, but the RVFI code reads shared nets through
// RVFI-only alias wires whose names sort before the originals (a_obs = acc, core.a_ptr = ptr).
// With `opt_clean -purge` the RVFI build would keep the alias names, so the shared registers
// would go unmatched. tools/equiv.py must PASS, with every cell proven by equiv_simple.
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
    ,
    output wire        rvfi_valid,
    output wire [63:0] rvfi_order,
    output wire [31:0] rvfi_insn,
    output wire        rvfi_trap,
    output wire        rvfi_halt,
    output wire        rvfi_intr,
    output wire [1:0]  rvfi_mode,
    output wire [1:0]  rvfi_ixl,
    output wire [4:0]  rvfi_rs1_addr,
    output wire [4:0]  rvfi_rs2_addr,
    output wire [31:0] rvfi_rs1_rdata,
    output wire [31:0] rvfi_rs2_rdata,
    output wire [4:0]  rvfi_rd_addr,
    output wire [31:0] rvfi_rd_wdata,
    output wire [31:0] rvfi_pc_rdata,
    output wire [31:0] rvfi_pc_wdata,
    output wire [31:0] rvfi_mem_addr,
    output wire [3:0]  rvfi_mem_rmask,
    output wire [3:0]  rvfi_mem_wmask,
    output wire [31:0] rvfi_mem_rdata,
    output wire [31:0] rvfi_mem_wdata
`endif
);
    wire [7:0] gpio;
    wire       sck;
    wire       retire;
    wire [7:0] acc;

    r32v_fx_core core (
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
    wire [7:0] a_obs = acc;
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
    wire _unused_rvfi = &{acc_q, 1'b0};

    assign rvfi_valid     = valid_q;
    assign rvfi_order     = order_q;
    assign rvfi_insn      = {24'd0, a_obs};
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
