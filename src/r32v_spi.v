/*
 * BonettoR32V SPI master (docs/contract.md section 5): mode 0, MSB first, SCK = f_clk / 2^(D+1),
 * a software-driven CS_n. Registers SPI_DATA and SPI_CTRL. r32v_periph owns the bus decode and
 * the shared write data (r32v_periph's header has the conventions).
 *
 * 2^D cycles low, 2^D high per bit.
 * sph = {BUSY, half-period count}: a write loads 1_0000, each half period adds 1, and the
 * sixteenth (the eighth falling edge) wraps it to 0, which clears BUSY. Bit 0 is SCK.
 * sdr holds {bits still to send, bits received, the slot for the next MISO sample}: MOSI is
 * its top bit (0 when idle), every falling SCK edge shifts it left, and the cycle after each
 * rising edge writes the slot with MISO as synchronized at that rising edge (contract 5.6:
 * the pin value at the rising edge). After the eighth falling edge sdr[8:1] is the received
 * byte, which SPI_DATA reads. The sample and the falling edge meet only at D = 0
 * (unspecified), where the sample goes straight to bit 1.
 *
 * Copyright 2026 Felipe Sens Bonetto
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module r32v_spi (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       rst,       // ~rst_n
    input  wire       be0,       // a store covers byte 0
    input  wire       w0,        // byte 0 store, or reset
    input  wire [7:0] z0,        // byte 0 store data, 0 in reset
    input  wire       s_sdat,    // slot SPI_DATA
    input  wire       s_sctl,    // slot SPI_CTRL
    input  wire       miso,      // ui[3], synchronized
    output reg        scs,       // CS_n (reset 1)
    output reg  [2:0] sd,        // D
    output wire       sbusy,
    output wire       ssck,
    output wire       mosi,
    output wire [7:0] rxd        // the received byte
);

    always @(posedge clk) begin
        if (w0 & (s_sctl | rst)) {sd, scs} <= {z0[3:1], z0[0] | rst};
    end

    reg  [8:0] sdr;
    reg  [4:0] sph;
    reg        srp;                                    // the cycle after a rising SCK edge
    reg  [6:0] sdc;
    assign     sbusy   = sph[4];
    assign     ssck    = sph[0];
    wire       s_go    = be0 & s_sdat & ~sbusy;
    wire [7:0] sdc_run = {~|sdc[6:0], ~|sdc[5:0], ~|sdc[4:0], ~|sdc[3:0], ~|sdc[2:0], ~|sdc[1:0], ~sdc[0], 1'b1};
    wire       s_half  = sbusy & sdc_run[sd];
    always @(posedge clk) begin
        sdc <= s_go ? 7'h7f : sdc - 7'd1;                // inverted count
        // no reset on srp or sdr[0]: SPI_DATA reads sdr[8:1], and s_go clears sdr[0]
        srp <= s_half & ~ssck;
        if (s_go | rst)
            sdr <= {z0, 1'b0};                           // the reset loads 0 the same way
        else if (s_half & ssck)
            sdr <= {sdr[7:1], srp ? miso : sdr[0], 1'b0};
        else if (srp)
            sdr[0] <= miso;
        if (!rst_n)
            sph <= 5'd0;
        else if (s_go)
            sph <= 5'b10000;
        else if (s_half)
            sph <= sph + 5'd1;
    end
    assign mosi = sdr[8] & sbusy;
    assign rxd  = sdr[8:1];

endmodule

`default_nettype wire
