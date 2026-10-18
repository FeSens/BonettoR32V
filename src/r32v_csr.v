/*
 * BonettoR32V CSRs and trap/interrupt sequencing (docs/contract.md section 4).
 *
 * Where the CSRs live:
 * - mtvec, mscratch and mepc are words of the core region (PSRAM 0x7F_FF94, 0x7F_FF98,
 *   0x7F_FF9C): `region` = 1 and `idx` = 01, 10, 11. Bits 1:0 of mtvec and mepc are masked when
 *   read into the ring (`mask_lo2`), so they read 0 whatever was stored, the reset content
 *   included.
 * - mstatus.MIE/MPIE, mie.MEIE and mcause (bit 31 and bits 3:0) are flops here; mip.MEIP is the
 *   interrupt line. A C slot reads them through `rd_die` (bit kb); the WC slot's END captures
 *   the new value's bits from the ring (`wb`).
 * - misa, mtval, the id CSRs and every unlisted number read 0 and ignore writes (unlisted
 *   numbers may alias a listed CSR: contract 4 leaves them unspecified).
 *
 * Sequencing: a synchronous trap (`trap_go`, at the END of the slot that detects it) or an
 * interrupt (`irq_take`, at a retirement) sets `tm`. The core then runs [P] WC C WP with
 * idx forced: WC writes mepc <- pc, C reads mtvec, WP writes the pc. `seq_end` (the WP's END)
 * clears tm. MRET's C reads mepc. Entry: MPIE <- MIE, MIE <- 0, mcause <- cause. MRET's
 * retirement: MIE <- MPIE, MPIE <- 1. The interrupt is taken at a retirement where the next
 * MIE, the next MEIE and the line are all 1 (so a CSR write or MRET that changes them counts).
 *
 * Copyright 2026 Felipe Sens Bonetto
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module r32v_csr (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [11:0] csr_num,    // insn[31:20]
    input  wire        p_mret,     // the instruction is MRET
    input  wire        s_wc,       // the slot is WC (else C), for idx in a trap sequence
    input  wire [4:0]  kb,         // C slot: bit index of the strobe
    output wire        region,
    output wire [1:0]  idx,
    output wire        mask_lo2,
    output wire        rd_die,
    // events, each valid in an END cycle
    input  wire        trap_go,    // a synchronous trap starts
    input  wire [3:0]  cause,      // its mcause
    input  wire        retire,     // an instruction retires
    input  wire        wc_end,     // a CSR instruction's WC ends: capture the die bits
    input  wire [6:0]  wb,         // ring bits {31, 11, 7, 3, 2, 1, 0} at that END
    input  wire        seq_end,    // the trap or interrupt sequence's WP ends
    input  wire        irq,        // mip.MEIP
    output reg         tm,         // in a trap or interrupt sequence
    output reg         intr        // ... entered by an interrupt
);

    reg       mie, mpie, meie, mc31;
    reg [3:0] mc;

    // Decode (exact on the contract-4 numbers)
    wire b11 = csr_num[11], b6 = csr_num[6], b2 = csr_num[2], b1 = csr_num[1], b0 = csr_num[0];
    wire _unused = &{csr_num[10:7], csr_num[5:3], 1'b0};
    wire c_mstatus = ~b11 & ~b6 & ~b2 & ~b0;
    wire c_mie     = ~b11 & ~b6 &  b2 & ~b0;
    wire c_mtvec   = ~b11 & ~b6 &  b2 &  b0;
    wire c_mscr    = ~b11 &  b6 & ~b2 & ~b1 & ~b0;
    wire c_mepc    = ~b11 &  b6 & ~b2 & ~b1 &  b0;
    wire c_mcause  = ~b11 &  b6 & ~b2 &  b1 & ~b0;
    wire c_mip     = ~b11 &  b6 &  b2 & ~b0;

    assign region   = tm | p_mret | c_mtvec | c_mscr | c_mepc;
    assign idx      = tm ? {s_wc, 1'b1} : p_mret ? 2'b11 : {b6, b0};
    assign mask_lo2 = idx[0] & (kb[4:1] == 4'd0);       // mtvec, mepc: bits 1:0 read 0

    wire k3  = (kb == 5'd3);
    wire k7  = (kb == 5'd7);
    wire k11 = (kb == 5'd11);
    wire k12 = (kb == 5'd12);
    wire k31 = (kb == 5'd31);
    wire klo = (kb[4:2] == 3'd0);
    assign rd_die = (c_mstatus & ((k3 & mie) | (k7 & mpie) | k11 | k12)) |
                    (c_mie & k11 & meie) |
                    (c_mip & k11 & irq) |
                    (c_mcause & ((k31 & mc31) | (klo & mc[kb[1:0]])));

    // Next MIE / MEIE as written at this END (for the interrupt decision)
    wire ws = wc_end & c_mstatus;
    // (an interrupt taken at the same END enters after them)
    wire mie_n  = ws ? wb[3] : (retire & p_mret) ? mpie : mie;
    wire meie_n = (wc_end & c_mie) ? wb[5] : meie;
    wire irq_take = retire & ~tm & mie_n & meie_n & irq;

    wire go = trap_go | irq_take;
    always @(posedge clk) begin
        if (!rst_n) begin
            mie  <= 1'b0;
            mpie <= 1'b0;                               // arbitrary by contract; 0 matches the ISSs
            meie <= 1'b0;
            tm   <= 1'b0;
            intr <= 1'b0;
        end else begin
            mie  <= go ? 1'b0 : mie_n;
            meie <= meie_n;
            if (go)
                mpie <= mie_n;
            else if (ws)
                mpie <= wb[4];
            else if (retire & p_mret)
                mpie <= 1'b1;
            if (go) begin
                tm   <= 1'b1;
                intr <= irq_take;
            end else if (seq_end) begin
                tm   <= 1'b0;
                intr <= 1'b0;
            end
        end
        if (go) begin
            mc31 <= irq_take;
            mc   <= irq_take ? 4'd11 : cause;
        end else if (wc_end & c_mcause) begin
            mc31 <= wb[6];
            mc   <= wb[3:0];
        end
    end

endmodule

`default_nettype wire
