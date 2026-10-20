/*
 * BonettoR32V GPIO (docs/contract.md section 5): GPIO_OUT and the per-pin edge flags
 * (GPIO_EDGE's enables and polarities, GPIO_EPEND's W1C flags). GPIO_IN is the synchronized
 * input itself, and ALTFN with the pin mux stays in r32v_periph, which owns the bus decode and
 * the shared write data (r32v_periph's header has the conventions).
 *
 * Copyright 2026 Felipe Sens Bonetto
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module r32v_gpio (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       rst,       // ~rst_n
    input  wire       w0,        // byte 0 store, or reset
    input  wire       w1,        // byte 1 store, or reset
    input  wire [7:0] z0,        // byte 0 store data, 0 in reset
    input  wire [7:0] z1,        // byte 1 store data, 0 in reset
    input  wire       be0,       // a store covers byte 0
    input  wire [7:0] d0,        // its data
    input  wire       s_gpo,     // slot GPIO_OUT
    input  wire       s_edge,    // slot GPIO_EDGE
    input  wire       s_epnd,    // slot GPIO_EPEND
    input  wire [7:0] s2,        // the synchronized inputs
    input  wire [7:0] chg,       // a synchronized edge on each pin
    output reg  [7:0] gpo,
    output reg  [7:0] een,
    output reg  [7:0] epol,
    output reg  [7:0] epend
);

    always @(posedge clk) begin
        if (w0 & (s_gpo | rst))  gpo  <= z0;
        if (w0 & (s_edge | rst)) een  <= z0;
        if (w1 & (s_edge | rst)) epol <= z1;
    end

    // ------------------------------------------------------------------------------------
    // Edge flags (W1C; a new edge wins over the clear)
    // ------------------------------------------------------------------------------------
    wire [7:0] eset = een & chg & (s2 ^ epol);
    always @(posedge clk) begin
        if (!rst_n)
            epend <= 8'd0;
        else
            epend <= (epend & ~({8{be0 & s_epnd}} & d0)) | eset;
    end

endmodule

`default_nettype wire
