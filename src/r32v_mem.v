/*
 * BonettoR32V memory controller for TT's QSPI Pmod (docs/contract-v3.md sections 2 and 3).
 *
 * - Reset sequence (contract 2.4), before the core starts: flash CS_n low for 8 SCK edges with
 *   SD0-SD3 high (leaves continuous read; in standard mode it is command 0xFF), then PSRAM
 *   0xF5 in QPI form (2 edges: leaves QPI; an ignored partial command in SPI mode), then 0x66
 *   and 0x99 in SPI form. One 32-cycle window per transaction, counted by `b`, which resets to
 *   127 so every CS_n is high during reset. The core waits for `booting` to fall (cycle 121).
 * - Address register `mar`: the low 24 bits of a program address (the pc during the P slot, a
 *   load/store address during the A slot), shifted in LSB first at the serial ALU's strobes;
 *   bit 24 goes to `ps` (1 = PSRAM, 0 = flash) and bit 31 to `io`. During a header the core
 *   reads `mar_bit` = mar[23 - n] for address bit n (index ~j[4:0]).
 * - Pins: single SPI on SD0 (MOSI) / SD1 (MISO), SCK = clk/2 while a CS_n is low. Program
 *   transactions (F, M) select the chip by `ps`; core-region transactions always use the
 *   PSRAM. SD2/SD3 are driven high outside the reset sequence (no memory ever drives them in
 *   the single-SPI commands used); SD1 is driven only during the reset sequence. PSRAM B's
 *   CS_n is constant 1.
 *
 * Copyright 2026 Felipe Sens Bonetto
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module r32v_mem (
    input  wire        clk,
    input  wire        rst_n,
    // address capture (serial, from the core)
    input  wire        cap,        // a P or A data strobe
    input  wire        cap_a,      // ... of the A slot (io is captured only there)
    input  wire [4:0]  kb,         // its bit index
    input  wire        din,        // its result bit
    input  wire [4:0]  j,          // header bit counter: address bit ~j[4:0]
    output wire        mar_bit,
    output wire [6:0]  mar_lo,     // IO slot and byte offset
    output reg         io,
    output wire        booting,
    // transaction (from the core's engine)
    input  wire        active,     // a CS_n is low (CMD, DATA, END of an unmasked slot)
    input  wire        prog,       // program transaction (F, M): chip by ps
    input  wire        sck,
    input  wire        mosi,
    output wire [7:0]  uio_out,
    output wire [7:0]  uio_oe
);

    // ------------------------------------------------------------------------------------
    // Address register
    // ------------------------------------------------------------------------------------
    reg [23:0] mar;
    reg        ps;
    always @(posedge clk) begin
        if (cap & ~(kb[4] & kb[3]))                     // bits 0..23
            mar <= {din, mar[23:1]};
        if (cap & (kb == 5'd24))
            ps <= din;
        if (cap_a & (kb == 5'd31))
            io <= din;
    end
    wire [31:0] marx = {8'd0, mar};
    assign mar_bit = marx[~j];                           // j >= 8 in an address field
    assign mar_lo  = mar[6:0];

    // ------------------------------------------------------------------------------------
    // Reset sequence: window w = b[6:5], position p = b[4:0]
    // ------------------------------------------------------------------------------------
    reg  [6:0] b;
    reg        done;
    wire [1:0] w    = b[6:5];
    wire [4:0] p    = b[4:0];
    wire [1:0] e    = p[2:1];                           // edge index (low bits) within the command
    wire       lo8  = ~p[4] | (p[3:0] == 4'd0);         // p <= 16: 8 edges, then SCK low
    wire       lo2  = ~p[4] & ~p[3] & (~p[2] | (p[1:0] == 2'd0));   // p <= 4: 2 edges
    wire       blo  = ~done & ((w == 2'd1) ? lo2 : lo8);
    wire       bsck = blo & p[0];
    wire       bcsf = ~(blo & (w == 2'd0));
    wire       bcsr = ~(blo & (w != 2'd0));
    // SD0: flash 1s, 0xF5 nibbles F, 5 (SD0 = 1, 1), 0x66 = 0110 0110, 0x99 = 1001 1001
    wire       bsd0 = ~w[1] | (e[0] ^ e[1] ^ w[0]);
    // SD1 and SD3: 1, except the second nibble (5) of 0xF5
    wire       bsd13 = ~((w == 2'd1) & p[1]);
    always @(posedge clk) begin
        if (!rst_n) begin
            b    <= 7'h7F;
            done <= 1'b0;
        end else if (!done) begin
            b    <= b + 7'd1;
            done <= (b == 7'd120);                      // 0x99 ended at 113: tRST satisfied
        end
    end
    assign booting = ~done;

    // ------------------------------------------------------------------------------------
    // Pins
    // ------------------------------------------------------------------------------------
    wire csf = done ? ~(active & prog & ~ps) : bcsf;
    wire csr = done ? ~(active & ~(prog & ~ps)) : bcsr;
    wire sk  = done ? (sck & active) : bsck;
    wire sd0 = done ? mosi : bsd0;
    wire sd1 = bsd13;                                   // driven only while booting
    wire sd3 = done | bsd13;
    assign uio_out = {1'b1, csr, sd3, 1'b1, sk, sd1, sd0, csf};
    assign uio_oe  = {5'b11111, booting, 2'b11};

endmodule

`default_nettype wire
