/*
 * 23LC512-compatible SPI RAM model for the Tiny Tapeout test (docs/contract.md v2, section 2).
 *
 * 64 KB, SPI mode 0, sequential mode: READ 0x03 and WRITE 0x02 with a 16-bit address, then data
 * bytes at (A + k) mod 2^16. The contents come from a $readmemh file (one byte per line), the HEX
 * parameter or +SPIRAM_HEX=<path>; the core region 0xFF80-0xFFFF (contract section 3) then gets
 * random bytes (REGION_SEED or +SPIRAM_REGION_SEED=<n>).
 *
 * The rules are defined at the core's rising clk edges, so the model samples CS_n, SCK and MOSI at
 * every rising clk edge (in RTL and in gate-level simulation the outputs have settled by then)
 * and stops the simulation with $fatal on any violation:
 *   1. CS_n changes only while SCK is low (before and after the edge);
 *   2. MOSI doesn't change on a rising SCK edge, and is 0 or 1 there (command, address, WRITE data);
 *   4. the command is 0x03 or 0x02; a transaction doesn't end before its command and address;
 *   5. a WRITE ends on a byte boundary with at least one data byte;
 *   also: CS_n = 1 and SCK = 0 after every clk edge with rst_n low, and no X/Z on CS_n or SCK
 *   once reset is released.
 * and the transaction classes of contract section 6, as far as the pins alone show them (the
 * cosim checks each transaction against its instruction; this catches what synthesis might
 * break in a design that passed it):
 *   - every byte a transaction addresses (a READ's partly clocked last byte included) is in the
 *     core region 0xFF80-0xFFFF, or none is: a transaction doesn't wrap out of the region or run
 *     into it. A READ with no data bit (a null READ) addresses nothing;
 *   - a program READ addresses at most 4 bytes (a fetch is exactly 4, a load at most 4);
 *   - between two program READs (each instruction starts with its fetch, a program READ in the
 *     test program), program WRITEs (one store's) write at most 4 bytes, none of them twice.
 * MISO (rule 3) carries the response bit from the moment SCK rises until it falls, and is X while
 * SCK is low, outside a READ's data phase and while CS_n is high. The model computes the next bit
 * one clk after the previous rising SCK edge, which is before the next one (SCK phases last at
 * least one clk cycle each).
 *
 * Copyright 2026 Felipe Sens Bonetto
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none
`timescale 1ns / 1ps

module spiram_23lc512 #(
    parameter HEX = "gpio_demo.hex",
    parameter REGION_SEED = 1    // the core region's random reset contents (+SPIRAM_REGION_SEED=)
) (
    input  wire clk,     // the core's clock: the rules are stated at its rising edges
    input  wire rst_n,
    input  wire cs_n,
    input  wire sck,
    input  wire mosi,
    output reg  miso
);
    reg [7:0]  mem [0:65535];
    reg [8*1024-1:0] path;

    // sampled at the previous rising clk edge: the outputs after core edge t-1
    reg        c_q, s_q, m_q;
    reg        rst_q;      // rst_n at core edge t-1
    reg        started;    // one rising clk edge seen
    // transaction state
    reg        in_txn;
    reg [31:0] nbits;      // rising SCK edges in this transaction
    reg [7:0]  cmd;
    reg [15:0] addr;
    reg [7:0]  wbyte;
    reg        next_bit;   // MISO value for the next rising SCK edge (X when none)
    // contract section 6 classes
    reg        t_region, t_prog;   // this transaction addressed a region / a program byte
    reg [15:0] wa [0:3];           // program bytes written since the last program READ
    integer    wcount;
    reg [15:0] ba;                 // the byte a data bit addresses
    integer    j;

    integer i;
    integer seed;
    initial begin
        for (i = 0; i < 65536; i = i + 1) mem[i] = 8'h00;
        if (!$value$plusargs("SPIRAM_HEX=%s", path)) path = HEX;
        $readmemh(path, mem);
        // Contract v2 section 3: the core region 0xFF80-0xFFFF holds the core's private state and
        // starts arbitrary, like on-die registers; programs never touch it. Random bytes, so a
        // core that relies on its reset contents fails. Region transactions are plain memory
        // traffic here: the model needs nothing else for them.
        if (!$value$plusargs("SPIRAM_REGION_SEED=%d", seed)) seed = REGION_SEED;
        for (i = 16'hFF80; i < 65536; i = i + 1) mem[i] = $random(seed);
        miso     = 1'bx;
        next_bit = 1'bx;
        in_txn   = 1'b0;
        started  = 1'b0;
        nbits    = 0;
        wcount   = 0;
    end

    // rule 3: MISO is valid only while SCK is high
    always @(posedge sck) miso = (cs_n === 1'b0) ? next_bit : 1'bx;
    always @(negedge sck) miso = 1'bx;
    always @(posedge cs_n) miso = 1'bx;

    always @(posedge clk) begin
        // the values seen here are the core's outputs after its previous edge
        if (started && !rst_q) begin
            if (cs_n !== 1'b1 || sck !== 1'b0)
                $fatal(1, "spiram: during reset CS_n = %b, SCK = %b (must be 1, 0)", cs_n, sck);
            in_txn <= 1'b0;
            wcount = 0;
        end else if (started) begin
            if ((cs_n !== 1'b0 && cs_n !== 1'b1) || (sck !== 1'b0 && sck !== 1'b1))
                $fatal(1, "spiram: CS_n = %b, SCK = %b after reset (X or Z)", cs_n, sck);
            // rule 1
            if (cs_n !== c_q && (s_q !== 1'b0 || sck !== 1'b0))
                $fatal(1, "spiram: CS_n changed while SCK was high");
            if (c_q === 1'b1 && cs_n === 1'b0) begin
                in_txn   <= 1'b1;
                nbits    <= 0;
                next_bit <= 1'bx;
                t_region <= 1'b0;
                t_prog   <= 1'b0;
            end
            if (c_q === 1'b0 && cs_n === 1'b1 && in_txn) begin
                // rules 4 and 5 at the end of a transaction
                if (nbits < 24)
                    $fatal(1, "spiram: transaction ended after %0d bits, before its command and address were complete", nbits);
                if (cmd == 8'h02 && (nbits == 24 || (nbits - 24) % 8 != 0))
                    $fatal(1, "spiram: WRITE ended mid-byte or without data (%0d data bits)", nbits - 24);
                in_txn   <= 1'b0;
                next_bit <= 1'bx;
            end
            if (in_txn && cs_n === 1'b0 && s_q === 1'b0 && sck === 1'b1) begin
                // a rising SCK edge: rule 2, then the bit
                if (mosi !== m_q)
                    $fatal(1, "spiram: MOSI changed on a rising SCK edge");
                if ((nbits < 24 || cmd == 8'h02) && mosi !== 1'b0 && mosi !== 1'b1)
                    $fatal(1, "spiram: MOSI = %b on a rising SCK edge", mosi);
                if (nbits < 8) begin
                    cmd <= {cmd[6:0], mosi};
                    if (nbits == 7 && {cmd[6:0], mosi} != 8'h03 && {cmd[6:0], mosi} != 8'h02)
                        $fatal(1, "spiram: invalid command 0x%02x", {cmd[6:0], mosi});
                end else if (nbits < 24) begin
                    addr <= {addr[14:0], mosi};
                end else if (cmd == 8'h02) begin
                    wbyte <= {wbyte[6:0], mosi};
                    if ((nbits - 24) % 8 == 7)
                        mem[(addr + (nbits - 24) / 8) & 16'hFFFF] <= {wbyte[6:0], mosi};
                end
                if (nbits >= 24 && (nbits - 24) % 8 == 0) begin
                    // the first bit of byte k = (nbits - 24) / 8: it is addressed from now on
                    ba = (addr + (nbits - 24) / 8) & 16'hFFFF;
                    if (ba >= 16'hFF80) begin
                        if (t_prog)
                            $fatal(1, "spiram: transaction at 0x%04x runs from program RAM into the core region 0xFF80-0xFFFF (byte 0x%04x; contract section 6)", addr, ba);
                        t_region <= 1'b1;
                    end else begin
                        if (t_region)
                            $fatal(1, "spiram: transaction at 0x%04x wraps out of the core region 0xFF80-0xFFFF (byte 0x%04x; contract section 6)", addr, ba);
                        t_prog <= 1'b1;
                        if (cmd == 8'h03) begin
                            if (nbits >= 24 + 32)
                                $fatal(1, "spiram: program READ at 0x%04x addresses a fifth byte (0x%04x): a fetch reads exactly 4, a load at most 4 (contract section 6)", addr, ba);
                            wcount = 0;   // a program READ: the previous store is over
                        end else begin
                            for (j = 0; j < wcount; j = j + 1)
                                if (wa[j] == ba)
                                    $fatal(1, "spiram: program byte 0x%04x written a second time since the last program READ (a store writes each byte once; contract section 6)", ba);
                            if (wcount == 4)
                                $fatal(1, "spiram: a fifth program byte (0x%04x) written since the last program READ (a store writes at most 4; contract section 6)", ba);
                            wa[wcount] = ba;
                            wcount = wcount + 1;
                        end
                    end
                end
                // the bit for the next rising SCK edge: READ data bit j = nbits + 1 - 24
                if (cmd == 8'h03 && nbits == 23)            // this edge completed the address
                    next_bit <= mem[{addr[14:0], mosi}][7];
                else if (cmd == 8'h03 && nbits >= 24)
                    next_bit <= mem[(addr + (nbits - 23) / 8) & 16'hFFFF][7 - (nbits - 23) % 8];
                else
                    next_bit <= 1'bx;
                nbits <= nbits + 1;
            end
        end
        c_q     <= cs_n;
        s_q     <= sck;
        m_q     <= mosi;
        rst_q   <= rst_n;
        started <= 1'b1;
    end
endmodule

`default_nettype wire
