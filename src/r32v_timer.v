/*
 * BonettoR32V timer (docs/contract.md section 5): a 16-bit counter with a 2^P prescaler,
 * compare (match interrupt, auto-reload), capture on ui[0] and an output on uo[7] (toggle or
 * PWM). Registers TIMER_CNT, TIMER_CMP, TIMER_CAP and TIMER_CTRL, and the compare and capture
 * pending flags of IRQ_PEND. r32v_periph owns the bus decode and the shared write data
 * (r32v_periph's header has the conventions).
 *
 * Copyright 2026 Felipe Sens Bonetto
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module r32v_timer (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        rst,       // ~rst_n
    input  wire        be0,       // a store covers byte 0
    input  wire        be1,       // a store covers byte 1
    input  wire        w0,        // byte 0 store, or reset
    input  wire        w1,        // byte 1 store, or reset
    input  wire [7:0]  z0,        // byte 0 store data, 0 in reset
    input  wire [7:0]  z1,        // byte 1 store data, 0 in reset
    input  wire [7:0]  d0,        // byte 0 store data
    input  wire        s_tcnt,    // slot TIMER_CNT
    input  wire        s_tcmp,    // slot TIMER_CMP
    input  wire        s_tctl,    // slot TIMER_CTRL
    input  wire        s_pend,    // slot IRQ_PEND
    input  wire        cap_in,    // ui[0], synchronized
    input  wire        cap_chg,   // an edge on it
    output reg  [15:0] tcnt,
    output reg  [15:0] tcmp,
    output reg  [15:0] tcap,
    output reg  [9:0]  tctl,
    output reg         p_cmp,
    output reg         p_cap,
    output wire        t_out
);

    always @(posedge clk) begin
        if (w0 & (s_tcmp | rst)) tcmp[7:0]  <= z0;
        if (w1 & (s_tcmp | rst)) tcmp[15:8] <= z1;
        if (w0 & (s_tctl | rst)) tctl[7:0]  <= z0;
        if (w1 & (s_tctl | rst)) tctl[9:8]  <= z1[1:0];
    end

    wire [3:0] tp_sel = tctl[3:0];
    wire       t_en   = tctl[4];
    wire       t_ar   = tctl[5];
    wire [1:0] t_mode = tctl[7:6];
    wire [1:0] t_cap  = tctl[9:8];
    reg  [14:0] tp;
    reg         tq;
    wire        wr_cnt = (be0 | be1) & s_tcnt;
    // tp holds the complement of the prescaler count: tick when its low P bits are all zero
    wire [15:0] tp_run = {~|tp[14:0], ~|tp[13:0], ~|tp[12:0], ~|tp[11:0], ~|tp[10:0], ~|tp[9:0],
                          ~|tp[8:0], ~|tp[7:0], ~|tp[6:0], ~|tp[5:0], ~|tp[4:0], ~|tp[3:0],
                          ~|tp[2:0], ~|tp[1:0], ~tp[0], 1'b1};
    wire        tick   = t_en & tp_run[tp_sel];
    wire        match  = (tcnt == tcmp);
    wire        cap_ev = cap_chg & (cap_in ? t_cap[0] : t_cap[1]);
    wire [15:0] tcnt_i = tcnt + 16'd1;
    // next count for the enable flops: the written bytes (0 in reset), else the tick's step
    wire [15:0] t_nx   = wr_cnt ? {z1, z0} : (tcnt_i & {16{~((match & t_ar) | rst)}});
    always @(posedge clk) begin
        tp <= (wr_cnt | ~t_en) ? 15'h7fff : tp - 15'd1;   // inverted count
        if (rst | (wr_cnt ? be0 : tick)) tcnt[7:0]  <= t_nx[7:0];
        if (rst | (wr_cnt ? be1 : tick)) tcnt[15:8] <= t_nx[15:8];
        if (!rst_n) begin
            tcap  <= 16'd0;
            p_cmp <= 1'b0;
            p_cap <= 1'b0;
        end else begin
            // no reset: only toggle mode shows tq, and the write that selects it clears tq
            if (be0 & s_tctl)
                tq <= 1'b0;                                 // the mode is written: restart at 0
            else if (tick & match & (t_mode == 2'b01))
                tq <= ~tq;
            if (cap_ev)
                tcap <= tcnt;
            p_cmp <= (p_cmp & ~(be0 & s_pend & d0[0])) | (tick & match);
            p_cap <= (p_cap & ~(be0 & s_pend & d0[1])) | cap_ev;
        end
    end
    assign t_out = (t_mode == 2'b01) ? tq : (t_mode == 2'b10) ? (tcnt < tcmp) : 1'b0;

endmodule

`default_nettype wire
