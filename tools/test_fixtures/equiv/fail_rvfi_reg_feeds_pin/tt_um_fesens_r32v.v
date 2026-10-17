// Equivalence-gate MUTANT: a register that exists only in the RISCV_FORMAL build (a 16-bit
// counter, reset to 0) inverts uo_out when it reaches 0xBEEF, tens of thousands of cycles
// after reset, far beyond any BMC depth. tools/equiv.py must FAIL on this design.
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

    // THE MUTATION: state that exists only in this build feeds a pin.
    reg [15:0] dbg_q;
    always @(posedge clk or negedge rst_n) if (!rst_n) dbg_q <= 16'd0; else dbg_q <= dbg_q + 16'd1;
    assign uo_out = (dbg_q == 16'hBEEF) ? ~gpio : gpio;
`else
    assign uo_out = gpio;
`endif

    wire _unused = &{ena, uio_in[7:3], uio_in[1:0], 1'b0};
endmodule
