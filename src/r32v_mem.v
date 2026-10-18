/*
 * BonettoR32V memory controller for TT's QSPI Pmod (docs/contract.md sections 2 and 3), quad.
 *
 * - Reset sequence (contract 2.4), before the core starts, one 32-cycle window per transaction
 *   (counter `b`, reset to 255 = idle, so both CS_n are high during reset; the core waits for
 *   `booting` to fall):
 *     w0       flash: 8 edges with SD0-SD3 high (leaves continuous read, or the no-op 0xFF);
 *     w1       PSRAM: 0xF5 in QPI form (2 edges; an ignored partial command in SPI mode);
 *     w2, w3   PSRAM: 0x66, 0x99 in SPI form (SPI mode, initialized);
 *     w4-w5    flash: 0xEB, 6 quad address edges, M = 0x20, then CS_n high inside the dummy
 *              field: the flash is in continuous read from here on;
 *     w6       PSRAM: 0x35 in SPI form (QPI mode from here on).
 * - Program address: the core supplies `pa` (bit 24 the chip, 1 = PSRAM): the pc for a fetch,
 *   the load or store address for M. `fz` (the first fetch) zeroes the address and picks the
 *   flash. A's data strobes capture a load/store address's bit 24 into `ps` (M's chip, steady
 *   while the ring takes the data) and bit 31 into `io`.
 * - Transactions: the core loads `sdo` with a header nibble (hdr_ld, index hidx) or a data
 *   nibble (dat_ld) at the clocks where SCK stays or goes low. Headers:
 *     flash (program access with ps = 0, continuous read): A[23:20] .. A[3:0], 0x2, 0x0 (M);
 *     PSRAM (QPI): 0x0, 0xB (read) or 0x2 (write), A[23:20] .. A[3:0]; core-region words at
 *     0xFFFF80 | {1, regarea, rsel, 00} (A[23] is ignored).
 *   `sdoe` drives SD0-SD3 from each slot's start; a read releases them after its 8 header
 *   edges, for the dummy/wait edges and the data. PSRAM B's CS_n is constant 1.
 *
 * Copyright 2026 Felipe Sens Bonetto
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module r32v_mem (
    input  wire        clk,
    input  wire        rst_n,
    // address flags (from the A slot's data strobes)
    input  wire        cap_a,      // an A data strobe
    input  wire [2:0]  kn,         // its nibble index
    input  wire [3:0]  din4,       // its result nibble
    output reg         io,
    output reg         ps,
    output wire        booting,
    // transaction (from the core's engine)
    input  wire        active,     // a CS_n is low (an unmasked slot past its gap)
    input  wire        prog,       // program transaction (F, M): address and chip from pa
    input  wire [24:0] pa,
    input  wire        fz,         // the first fetch: address 0 in flash
    input  wire        sck,
    input  wire        gap,        // the cycle between transactions
    input  wire        hdr_ld,     // load header nibble hidx into sdo
    input  wire [2:0]  hidx,
    input  wire        h_wr,       // the slot writes
    input  wire [4:0]  h_reg,      // core-region word {regarea, rsel[3:0]}
    input  wire        dat_ld,     // load data nibble wd4 into sdo
    input  wire [3:0]  wd4,
    input  wire        oe_off,     // a read's header ends: release SD0-SD3
    output wire [7:0]  uio_out,
    output wire [7:0]  uio_oe
);

    // ------------------------------------------------------------------------------------
    // Address flags
    // ------------------------------------------------------------------------------------
    wire _unused_din = &{din4[2:1], 1'b0};
    always @(posedge clk) begin
        if (cap_a & (kn == 3'd6))
            ps <= din4[0];
        if (cap_a & (kn == 3'd7))
            io <= din4[3];
    end

    // ------------------------------------------------------------------------------------
    // Header nibbles
    // ------------------------------------------------------------------------------------
    wire        flash = prog & ~(pa[24] & ~fz);
    wire        az    = fz & ~(hidx[2] & hidx[1]);      // the first fetch's address nibbles: 0
    reg  [3:0] hn;
    always @(*) begin
        case ({flash, hidx})
            4'b1000: hn = pa[23:20];
            4'b1001: hn = pa[19:16];
            4'b1010: hn = pa[15:12];
            4'b1011: hn = pa[11:8];
            4'b1100: hn = pa[7:4];
            4'b1101: hn = pa[3:0];
            4'b1110: hn = 4'h2;                         // M = 0x20: stay in continuous read
            4'b1111: hn = 4'h0;
            4'b0000: hn = 4'h0;
            4'b0001: hn = h_wr ? 4'h2 : 4'hB;           // QPI 0x02 write, 0x0B read
            4'b0010: hn = prog ? pa[23:20] : 4'hF;
            4'b0011: hn = prog ? pa[19:16] : 4'hF;
            4'b0100: hn = prog ? pa[15:12] : 4'hF;
            4'b0101: hn = prog ? pa[11:8]  : 4'hF;
            4'b0110: hn = prog ? pa[7:4]   : {1'b1, h_reg[4:2]};
            default: hn = prog ? pa[3:0]   : {h_reg[1:0], 2'b00};
        endcase
    end

    reg [3:0] sdo;
    reg       sdoe;
    always @(posedge clk) begin
        if (hdr_ld)
            sdo <= hn & {4{~az}};
        else if (dat_ld)
            sdo <= wd4;
        else if (gap)
            sdo <= 4'h0;
        if (gap)
            sdoe <= 1'b1;
        else if (oe_off)
            sdoe <= 1'b0;
    end

    // ------------------------------------------------------------------------------------
    // Reset sequence: window w = b[7:5], position p = b[4:0]; w4-w5 is one 64-cycle window
    // ------------------------------------------------------------------------------------
    reg  [7:0] b;
    reg        done;
    wire [2:0] w    = b[7:5];
    wire [4:0] p    = b[4:0];
    wire       fe   = (w[2:1] == 2'b10);                 // w4, w5: flash enter, position b[5:0]
    wire       fl   = (w == 3'd0) | fe;                  // a flash window
    wire [2:0] e    = p[3:1];                           // edge index in an 8-edge window
    wire [3:0] ef   = b[4:1];                           // edge index in the flash-enter window
    wire       lo8  = ~p[4] | (p[3:0] == 4'd0);         // p <= 16: 8 edges, then SCK low
    wire       lo2  = ~p[4] & ~p[3] & (~p[2] | (p[1:0] == 2'd0));   // p <= 4: 2 edges
    wire       lo16 = ~b[5] | (p == 5'd0);              // b[5:0] <= 32: 16 edges
    wire       blo  = ~done & (fe ? lo16 : (w == 3'd1) ? lo2 : (w != 3'd7) & lo8);
    wire       bsck = blo & p[0];
    wire       bcsf = ~(blo & fl);
    wire       bcsr = ~(blo & ~fl);
    // SD0 of the single commands: 0x66 = 0110 0110, 0x99 = 1001 1001, 0x35 = 0011 0101,
    // 0xEB = 1110 1011
    wire       c66  = e[0] ^ e[1];
    wire       c35  = e[2] ? e[0] : e[1];
    wire       ceb  = ~((ef[2:0] == 3'd3) | (ef[2:0] == 3'd5));
    reg  [3:0] bsd;
    always @(*) begin
        case (w)
            3'd1:    bsd = p[1] ? 4'h5 : 4'hF;          // QPI 0xF5
            3'd2:    bsd = {3'b111, c66};
            3'd3:    bsd = {3'b111, ~c66};
            3'd4, 3'd5:                                 // 0xEB, address 0, M = 0x20
                     bsd = ef[3] ? {2'b00, ef[2:0] == 3'd6, 1'b0} : {3'b000, ceb};
            3'd6:    bsd = {3'b111, c35};
            default: bsd = 4'hF;
        endcase
    end
    always @(posedge clk) begin
        if (!rst_n) begin
            b    <= 8'hFF;                              // idle (w7) during reset
            done <= 1'b0;
        end else if (!done) begin
            b    <= b + 8'd1;
            done <= (b == 8'd211);                      // 0x35 ended at 209
        end
    end
    assign booting = ~done;

    // ------------------------------------------------------------------------------------
    // Pins
    // ------------------------------------------------------------------------------------
    wire       csf = done ? ~(active & flash) : bcsf;
    wire       csr = done ? ~(active & ~flash) : bcsr;
    wire       sk  = done ? (sck & active) : bsck;
    wire [3:0] sd  = done ? sdo : bsd;
    wire       oe  = ~done | sdoe;
    assign uio_out = {1'b1, csr, sd[3], sd[2], sk, sd[1], sd[0], csf};
    assign uio_oe  = {2'b11, oe, oe, 1'b1, oe, oe, 1'b1};

endmodule

`default_nettype wire
