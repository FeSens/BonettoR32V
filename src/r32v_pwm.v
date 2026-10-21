/*
 * BonettoR32V PWM (docs/contract.md section 5): three 10-bit channels sharing a 2^Q prescaler
 * and a counter 0..PER, each with its duty compare; channel 0 can be gated by the timer output
 * (the IR carrier gate, PWM_CTRL.IRG). Registers PWM_CTRL and PWM_DUTY0-2. r32v_periph owns the
 * bus decode and the shared write data (r32v_periph's header has the conventions).
 *
 * Copyright 2026 Felipe Sens Bonetto
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module r32v_pwm (
    input  wire        clk,
    input  wire        rst,       // ~rst_n
    input  wire        w0,        // byte 0 store, or reset
    input  wire        w1,        // byte 1 store, or reset
    input  wire [7:0]  z0,        // byte 0 store data, 0 in reset
    input  wire [6:0]  z1,        // byte 1 store data bits 6:0, 0 in reset
    input  wire        s_pctl,    // slot PWM_CTRL
    input  wire        s_dut0,    // slots PWM_DUTY0-2
    input  wire        s_dut1,
    input  wire        s_dut2,
    input  wire        t_out,     // the timer output (IR gate)
    output reg  [14:0] pctl,
    output reg  [9:0]  duty0,
    output reg  [9:0]  duty1,
    output reg  [9:0]  duty2,
    output wire [2:0]  pwm        // channel outputs, channel 0 after the IR gate
);

    always @(posedge clk) begin
        if (w0 & (s_pctl | rst)) pctl[7:0]  <= z0;
        if (w1 & (s_pctl | rst)) pctl[14:8] <= z1[6:0];
        if (w0 & (s_dut0 | rst)) duty0[7:0] <= z0;
        if (w1 & (s_dut0 | rst)) duty0[9:8] <= z1[1:0];
        if (w0 & (s_dut1 | rst)) duty1[7:0] <= z0;
        if (w1 & (s_dut1 | rst)) duty1[9:8] <= z1[1:0];
        if (w0 & (s_dut2 | rst)) duty2[7:0] <= z0;
        if (w1 & (s_dut2 | rst)) duty2[9:8] <= z1[1:0];
    end

    wire [9:0] per   = pctl[9:0];
    wire [2:0] q_sel = pctl[12:10];
    wire       p_en  = pctl[13];
    wire       irg   = pctl[14];
    reg  [6:0] pq;
    reg  [9:0] pcn;                                    // ~pc (pq is inverted too)
    // pc < x  <=>  pcn + x carries out of bit 9
    wire       p_lp  = |(({1'b0, pcn} + {1'b0, per})   >> 10);
    wire       p_l0  = |(({1'b0, pcn} + {1'b0, duty0}) >> 10);
    wire       p_l1  = |(({1'b0, pcn} + {1'b0, duty1}) >> 10);
    wire       p_l2  = |(({1'b0, pcn} + {1'b0, duty2}) >> 10);
    wire [7:0] pq_run = {~|pq[6:0], ~|pq[5:0], ~|pq[4:0], ~|pq[3:0], ~|pq[2:0], ~|pq[1:0], ~pq[0], 1'b1};
    wire       ptick  = pq_run[q_sel];
    always @(posedge clk) begin
        pq <= p_en ? pq - 7'd1 : 7'h7f;                  // inverted count
        if (~p_en | ptick)
            pcn <= (pcn - 10'd1) | {10{~(p_en & p_lp)}};
    end
    assign pwm = {p_en & p_l2, p_en & p_l1, p_en & p_l0 & (t_out | ~irg)};

endmodule

`default_nettype wire
