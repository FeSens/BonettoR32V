/*
 * Harness self-test mutant tcem_stall (ruling R12; test_cosim_harness.py::test_tcem_mutant_fails):
 * a stub with the contract v3 ports that brings the PSRAM out of any power-up mode (contract 2.4:
 * QPI-form 0xF5, then 0x66 and 0x99 in SPI form), then starts a 0x03 read of PSRAM 0x000000 and,
 * after the 24-bit address, holds PSRAM CS_n low with SCK paused for 400 cycles before clocking
 * the data byte, the way a bit-serial core that stalls mid-load would. CS_n low lasts about 480
 * cycles, past tCEM = 8 us = 320 cycles at 25 ns (contract 2.1.7), so the cosim must fail with
 * "tCEM" in both modes. Nothing retires (the RVFI outputs are 0).
 */
`default_nettype none

module tt_um_fesens_r32v (
    input  wire [7:0]  ui_in,
    output wire [7:0]  uo_out,
    input  wire [7:0]  uio_in,
    output wire [7:0]  uio_out,
    output wire [7:0]  uio_oe,
    input  wire        ena,
    input  wire        clk,
    input  wire        rst_n
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
    localparam [15:0] S0 = 16'd8,    // QPI-form 0xF5: 2 quad edges
                      S1 = 16'd16,   // 0x66
                      S2 = 16'd36,   // 0x99
                      S3 = 16'd60,   // 0x03 + address 0x000000: 32 edges
                      S4 = 16'd524;  // after the 400-cycle stall: 8 data edges
    localparam STALL = 400;

    reg [15:0] c;  // cycles since reset release
    always @(posedge clk) begin
        if (!rst_n) c <= 16'd0;
        else if (c != 16'hffff) c <= c + 16'd1;
    end

    wire [15:0] d0 = c - S0, d1 = c - S1, d2 = c - S2, d3 = c - S3, d4 = c - S4;
    wire in0 = c >= S0 && d0 <= 16'd4;
    wire in1 = c >= S1 && d1 <= 16'd16;
    wire in2 = c >= S2 && d2 <= 16'd16;
    wire in3 = c >= S3 && c <= S4 + 16'd16;
    // SCK is high on the odd cycles of each bit field: rising edges at offsets 1, 3, 5, ...
    wire sck = (in0 && d0[0] && d0 < 16'd4) || (in1 && d1[0] && d1 < 16'd16) ||
               (in2 && d2[0] && d2 < 16'd16) || (in3 && d3[0] && d3 < 16'd64) ||
               (in3 && c >= S4 && d4[0] && d4 < 16'd16);
    wire [7:0] c66 = 8'h66, c99 = 8'h99;
    wire [31:0] rd = 32'h03000000;  // command 0x03, address 0x000000
    wire sd0 = in1 ? c66[3'd7 - d1[3:1]] :
               in2 ? c99[3'd7 - d2[3:1]] :
               (in3 && d3 < 16'd64) ? rd[5'd31 - d3[5:1]] : 1'b0;
    wire [3:0] nib = d0 < 16'd2 ? 4'hf : 4'h5;
    wire cs_r = !(in0 || in1 || in2 || in3);

    assign uio_out = in0 ? {1'b1, cs_r, nib[3], nib[2], sck, nib[1], nib[0], 1'b1}
                         : {1'b1, cs_r, 1'b0, 1'b0, sck, 1'b0, sd0, 1'b1};
    assign uio_oe = in0 ? 8'hff : {6'b110010, in1 || in2 || in3, 1'b1};
    assign uo_out = 8'h00;

    wire _unused = &{ui_in, uio_in, ena, STALL[0], 1'b0};

`ifdef RISCV_FORMAL
    assign rvfi_valid = 1'b0;
    assign rvfi_order = 64'd0;
    assign rvfi_insn = 32'd0;
    assign rvfi_trap = 1'b0;
    assign rvfi_halt = 1'b0;
    assign rvfi_intr = 1'b0;
    assign rvfi_mode = 2'd3;
    assign rvfi_ixl = 2'd1;
    assign rvfi_rs1_addr = 5'd0;
    assign rvfi_rs2_addr = 5'd0;
    assign rvfi_rs1_rdata = 32'd0;
    assign rvfi_rs2_rdata = 32'd0;
    assign rvfi_rd_addr = 5'd0;
    assign rvfi_rd_wdata = 32'd0;
    assign rvfi_pc_rdata = 32'd0;
    assign rvfi_pc_wdata = 32'd0;
    assign rvfi_mem_addr = 32'd0;
    assign rvfi_mem_rmask = 4'd0;
    assign rvfi_mem_wmask = 4'd0;
    assign rvfi_mem_rdata = 32'd0;
    assign rvfi_mem_wdata = 32'd0;
`endif
endmodule
