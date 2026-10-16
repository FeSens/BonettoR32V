// src_check fixture: realistic bit-serial RTL inside the restricted preprocessor (contract
// section 1). It must pass src_check (static and differential), Verilator -Wall lint in both
// builds, and the equivalence gate.
`default_nettype none
`timescale 1ns/1ps

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
    localparam integer W = 8;
    localparam [1:0] S_LOAD = 2'd0, S_SHIFT = 2'd1, S_DONE = 2'd2;

    // Input synchronizers, one per bit.
    wire [W-1:0] ui_sync;
    genvar gi;
    generate
        for (gi = 0; gi < W; gi = gi + 1) begin : g_sync
            reg q;
            always @(posedge clk) q <= ui_in[gi];
            assign ui_sync[gi] = q;
        end
    endgenerate

    function [W-1:0] reverse;
        input [W-1:0] x;
        integer k;
        begin
            for (k = 0; k < W; k = k + 1) reverse[k] = x[W-1-k];
        end
    endfunction

    reg [1:0]   state;
    reg [2:0]   bitcnt;
    reg         carry;
    reg [W-1:0] a_sr, b_sr, sum_sr, gpio_q;
    wire        s_bit, c_next;

    r32v_serial_add #(.INVERT(0)) u_add (
        .a(a_sr[0]), .b(b_sr[0]), .cin(carry), .s(s_bit), .cout(c_next)
    );

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state  <= S_LOAD;
            bitcnt <= 3'd0;
            carry  <= 1'b0;
            gpio_q <= {W{1'b0}};
        end else begin
            case (state)
                S_LOAD: begin
                    state  <= S_SHIFT;
                    bitcnt <= 3'd0;
                    carry  <= 1'b0;
                end
                S_SHIFT: begin
                    carry  <= c_next;
                    bitcnt <= bitcnt + 3'd1;
                    if (bitcnt == 3'd7) state <= S_DONE;
                end
                default: begin
                    gpio_q <= reverse(sum_sr);
                    state  <= S_LOAD;
                end
            endcase
        end
    end

    always @(posedge clk) begin
        if (state == S_LOAD) begin
            a_sr <= ui_sync;
            b_sr <= gpio_q;
        end else if (state == S_SHIFT) begin
            a_sr   <= {1'b0, a_sr[W-1:1]};
            b_sr   <= {1'b0, b_sr[W-1:1]};
            sum_sr <= {s_bit, sum_sr[W-1:1]};
        end
    end

    reg [W-1:0] out_mux;
    always @(*) begin
        case (state)
            S_DONE:  out_mux = sum_sr;
            default: out_mux = gpio_q;
        endcase
    end

    assign uo_out  = out_mux;
    assign uio_out = {4'b0000, state[0], 1'b0, state[1], 1'b1};
    assign uio_oe  = 8'b0000_1011;

`ifdef RISCV_FORMAL
    reg [63:0] order_q;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) order_q <= 64'd0;
        else if (state == S_DONE) order_q <= order_q + 64'd1;
    end
    assign rvfi_valid     = (state == S_DONE);
    assign rvfi_order     = order_q;
    assign rvfi_insn      = {24'd0, sum_sr};
    assign rvfi_trap      = 1'b0;
    assign rvfi_halt      = 1'b0;
    assign rvfi_intr      = 1'b0;
    assign rvfi_mode      = 2'd3;
    assign rvfi_ixl       = 2'd1;
    assign rvfi_rs1_addr  = 5'd0;
    assign rvfi_rs2_addr  = 5'd0;
    assign rvfi_rs1_rdata = 32'd0;
    assign rvfi_rs2_rdata = 32'd0;
    assign rvfi_rd_addr   = 5'd0;
    assign rvfi_rd_wdata  = 32'd0;
    assign rvfi_pc_rdata  = order_q[31:0];
    assign rvfi_pc_wdata  = order_q[31:0] + 32'd4;
    assign rvfi_mem_addr  = 32'd0;
    assign rvfi_mem_rmask = 4'd0;
    assign rvfi_mem_wmask = 4'd0;
    assign rvfi_mem_rdata = 32'd0;
    assign rvfi_mem_wdata = {24'd0, gpio_q};
`endif

    wire _unused = &{ena, uio_in, 1'b0};
endmodule
`default_nettype wire
