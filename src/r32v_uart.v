/*
 * BonettoR32V UART (docs/contract.md section 5): 8N1, DIV+1 cycles per bit, a transmitter and
 * a receiver with a one-byte buffer and an overrun flag. Registers UART_DATA and UART_CTRL.
 * r32v_periph owns the bus decode and the shared write data (r32v_periph's header has the
 * conventions).
 *
 * Copyright 2026 Felipe Sens Bonetto
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module r32v_uart (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        rst,       // ~rst_n
    input  wire        be0,       // a store covers byte 0
    input  wire        w0,        // byte 0 store, or reset
    input  wire        w1,        // byte 1 store, or reset
    input  wire [7:0]  z0,        // byte 0 store data, 0 in reset
    input  wire [4:0]  z1,        // byte 1 store data bits 4:0, 0 in reset
    input  wire        rd0,       // a load covers byte 0
    input  wire        s_udat,    // slot UART_DATA
    input  wire        s_uctl,    // slot UART_CTRL
    input  wire        rx,        // ui[1], synchronized
    input  wire        rx_chg,    // an edge on it
    output reg  [12:0] div,
    output reg  [7:0]  rdat,
    output reg         rfull,
    output reg         rovr,
    output wire        tx_busy,
    output wire        tx_pin
);

    always @(posedge clk) begin
        if (w0 & (s_uctl | rst)) div[7:0]  <= z0;
        if (w1 & (s_uctl | rst)) div[12:8] <= z1[4:0];
    end

    // ------------------------------------------------------------------------------------
    // Transmitter: {stop, data, start} shifted out LSB first, DIV+1 cycles per bit
    // ------------------------------------------------------------------------------------
    reg  [9:0]  tsr;
    reg  [12:0] tbc;
    assign      tx_busy = |tsr;
    wire        tx_go   = be0 & s_udat & ~tx_busy;
    always @(posedge clk) begin
        // the reset loads 0 (idle) through the start path. Only a busy tsr shifts: tbc has no
        // reset and is unknown until the first start, and an idle tsr must not depend on it
        // (gate-level X propagation otherwise turns the idle tsr, and TX_BUSY, into X)
        if (tx_go | rst)
            tsr <= {rst_n, z0, 1'b0};
        else if (tx_busy & (tbc == 13'd0))
            tsr <= {1'b0, tsr[9:1]};
        if (tx_go | (tbc == 13'd0))
            tbc <= div;
        else
            tbc <= tbc - 13'd1;
    end
    assign tx_pin = tsr[0] | ~tx_busy;

    // ------------------------------------------------------------------------------------
    // Receiver: a falling edge starts a frame; samples at mid-bit (DIV/2, then DIV+1)
    // ------------------------------------------------------------------------------------
    reg         ract;
    reg  [12:0] rcnt;
    reg  [9:0]  rsr;
    wire        r_start = ~ract & rx_chg & ~rx;
    wire        r_smp   = ract & (rcnt == 13'd0);
    wire        r_stop  = r_smp & rsr[0];
    wire        r_clr   = rd0 & s_udat;
    wire        full_k  = rfull & ~r_clr;                // reading byte 0 comes first
    always @(posedge clk) begin
        // rcnt reloads at zero whether or not a frame is active (idle, nothing samples it)
        if (r_start)
            rcnt <= {1'b0, div[12:1]};
        else if (rcnt == 13'd0)
            rcnt <= div;
        else
            rcnt <= rcnt - 13'd1;
        // rsr: a start loads the marker into bit 9; each sample shifts in at the top, so after
        // the start bit and 8 data bits the marker is in bit 0 and the byte in bits 9:2, and the
        // next sample (the stop bit) ends the frame
        if (r_start | r_smp)
            rsr <= {r_start | rx, rsr[9:1] & {9{~r_start}}};
        if (!rst_n) begin
            ract  <= 1'b0;
            rfull <= 1'b0;
            rovr  <= 1'b0;
            rdat  <= 8'd0;
        end else begin
            if (r_start)
                ract <= 1'b1;
            else if (r_stop)
                ract <= 1'b0;
            if (r_stop & ~full_k)
                rdat <= rsr[9:2];
            rfull <= full_k | r_stop;
            rovr  <= (rovr & ~r_clr) | (r_stop & full_k);
        end
    end

endmodule

`default_nettype wire
