/*
 * Harness self-test fixture (not a design; test_cosim_harness.py only): a playback stub with the
 * contract v3 ports. It executes nothing. It replays two scripts given as plusargs, so one
 * Verilator build per mode serves every scenario:
 *   +PINS=<file>  pin changes: records of 2 hex words, {cycle} {uio_oe[23:16], uio_out[15:8],
 *                 uo_out[7:0]}; the pins take the value after rising edge `cycle` (cycles count
 *                 from reset release: the last edge with rst_n low is cycle 0) and hold it.
 *   +RVFI=<file>  retirements (RISCV_FORMAL build): records of 18 hex words, {cycle} {order lo}
 *                 {order hi} {insn} {flags: trap 0, halt 1, intr 2, mode 5:4, ixl 7:6}
 *                 {rs1_addr} {rs2_addr} {rs1_rdata} {rs2_rdata} {rd_addr} {rd_wdata} {pc_rdata}
 *                 {pc_wdata} {mem_addr} {mem_rmask} {mem_wmask} {mem_rdata} {mem_wdata};
 *                 rvfi_valid is 1 for the one cycle after rising edge `cycle`.
 * Each file ends with a record whose cycle is ffffffff. Without a file the pins stay idle (both
 * CS_n high, SCK low, uo_out 0) and nothing retires.
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
    localparam NP = 16384;  // pin records
    localparam NR = 4096;   // retirement records

    reg [31:0] pn [0:2*NP-1];
    reg [8*1024-1:0] pfile;
    reg [31:0] cnt;
    reg [15:0] pi;
    reg [7:0] uo_r, uio_r, oe_r;

    wire _unused = &{ui_in, uio_in, ena, 1'b0};

    initial begin
        pn[0] = 32'hffffffff;
        if ($value$plusargs("PINS=%s", pfile)) $readmemh(pfile, pn);
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            cnt <= 32'd0;
            pi <= 16'd0;
            uo_r <= 8'h00;
            uio_r <= 8'hc1;
            oe_r <= 8'hc9;
        end else begin
            cnt <= cnt + 32'd1;
            if (pn[{pi, 1'b0}] == cnt + 32'd1) begin
                {oe_r, uio_r, uo_r} <= pn[{pi, 1'b1}][23:0];
                pi <= pi + 16'd1;
            end
        end
    end

    assign uo_out = uo_r;
    assign uio_out = uio_r;
    assign uio_oe = oe_r;

`ifdef RISCV_FORMAL
    reg [31:0] rv [0:18*NR-1];
    reg [8*1024-1:0] rfile;
    reg [15:0] ri;
    reg [31:0] base;
    reg [17*32-1:0] f;
    reg v;

    initial begin
        rv[0] = 32'hffffffff;
        if ($value$plusargs("RVFI=%s", rfile)) $readmemh(rfile, rv);
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            ri <= 16'd0;
            base <= 32'd0;
            v <= 1'b0;
        end else begin
            v <= rv[base] == cnt + 32'd1;
            if (rv[base] == cnt + 32'd1) begin
                f <= {rv[base + 17], rv[base + 16], rv[base + 15], rv[base + 14], rv[base + 13],
                      rv[base + 12], rv[base + 11], rv[base + 10], rv[base + 9], rv[base + 8],
                      rv[base + 7], rv[base + 6], rv[base + 5], rv[base + 4], rv[base + 3],
                      rv[base + 2], rv[base + 1]};
                ri <= ri + 16'd1;
                base <= base + 32'd18;
            end
        end
    end

    // f holds words 1..17 of the record: word k at bits [32(k-1) +: 32]
    assign rvfi_valid     = v;
    assign rvfi_order     = {f[1*32 +: 32], f[0*32 +: 32]};
    assign rvfi_insn      = f[2*32 +: 32];
    assign rvfi_trap      = f[3*32 + 0];
    assign rvfi_halt      = f[3*32 + 1];
    assign rvfi_intr      = f[3*32 + 2];
    assign rvfi_mode      = f[3*32 + 4 +: 2];
    assign rvfi_ixl       = f[3*32 + 6 +: 2];
    assign rvfi_rs1_addr  = f[4*32 +: 5];
    assign rvfi_rs2_addr  = f[5*32 +: 5];
    assign rvfi_rs1_rdata = f[6*32 +: 32];
    assign rvfi_rs2_rdata = f[7*32 +: 32];
    assign rvfi_rd_addr   = f[8*32 +: 5];
    assign rvfi_rd_wdata  = f[9*32 +: 32];
    assign rvfi_pc_rdata  = f[10*32 +: 32];
    assign rvfi_pc_wdata  = f[11*32 +: 32];
    assign rvfi_mem_addr  = f[12*32 +: 32];
    assign rvfi_mem_rmask = f[13*32 +: 4];
    assign rvfi_mem_wmask = f[14*32 +: 4];
    assign rvfi_mem_rdata = f[15*32 +: 32];
    assign rvfi_mem_wdata = f[16*32 +: 32];
`endif
endmodule
