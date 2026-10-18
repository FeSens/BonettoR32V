/*
 * BonettoR32V MCU peripherals (docs/contract.md section 5): GPIO with edge flags, a 16-bit
 * timer with compare, capture and an output, 3 PWM channels, a UART (8N1) and an SPI master
 * (mode 0), plus the interrupt line. One register file, one read mux.
 *
 * Bus (from the core): a load or store of slot `slot` (address bits 6:2) at byte offset `off`
 * (bits 1:0) of size `size` (funct3[1:0]). `we` is a one-cycle store strobe with the store data
 * in `wdata` (rs2 bits 15:0; no slot has bits above 15). `re` is a one-cycle load strobe, at the
 * same edge the core captures `rdata` (the slot's bits 15:0), so a load is atomic and its side
 * effect (reading UART_DATA byte 0 clears RX_FULL and RX_OVR) happens exactly then.
 * A store writes byte lane 0 when off = 0, and byte lane 1 when off = 1 (with wdata[7:0]) or
 * when off = 0 and the access is wider than a byte. Lanes 2 and 3 hold no bits. Misaligned IO
 * accesses are unspecified (contract 5).
 *
 * Inputs pass through a 2-flop synchronizer (s2 is the synchronized value, s3 the previous one
 * for edge detection): an input change reaches the logic 2 cycles later.
 *
 * Copyright 2026 Felipe Sens Bonetto
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module r32v_periph (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [7:0]  ui_in,
    output wire [7:0]  uo_out,
    input  wire [4:0]  slot,
    input  wire [1:0]  off,
    input  wire [1:0]  size,
    input  wire        we,
    input  wire        re,
    input  wire [15:0] wdata,
    output reg  [15:0] rdata,
    output reg         irq
);

    // ------------------------------------------------------------------------------------
    // Bus decode
    // ------------------------------------------------------------------------------------
    wire       be0 = we & (off == 2'd0);
    wire       be1 = we & ((off == 2'd1) | ((off == 2'd0) & (size != 2'd0)));
    wire [7:0] d0  = wdata[7:0];
    wire [7:0] d1  = off[0] ? wdata[7:0] : wdata[15:8];
    wire       rd0 = re & (off == 2'd0);              // the load covers byte 0

    wire s_gpo  = (slot == 5'd0);
    wire s_alt  = (slot == 5'd2);
    wire s_tcnt = (slot == 5'd3);
    wire s_tcmp = (slot == 5'd4);
    wire s_tctl = (slot == 5'd6);
    wire s_pctl = (slot == 5'd7);
    wire s_dut0 = (slot == 5'd8);
    wire s_dut1 = (slot == 5'd9);
    wire s_dut2 = (slot == 5'd10);
    wire s_udat = (slot == 5'd11);
    wire s_uctl = (slot == 5'd12);
    wire s_sdat = (slot == 5'd13);
    wire s_sctl = (slot == 5'd14);
    wire s_ien  = (slot == 5'd15);
    wire s_pend = (slot == 5'd16);
    wire s_edge = (slot == 5'd17);
    wire s_epnd = (slot == 5'd18);

    // ------------------------------------------------------------------------------------
    // Input synchronizers
    // ------------------------------------------------------------------------------------
    reg [7:0] s1, s2, s3;
    always @(posedge clk) begin
        if (!rst_n) begin
            s1 <= 8'd0;
            s2 <= 8'd0;
            s3 <= 8'd0;
        end else begin
            s1 <= ui_in;
            s2 <= s1;
            s3 <= s2;
        end
    end
    wire [7:0] chg = s2 ^ s3;                         // a synchronized edge on each pin

    // ------------------------------------------------------------------------------------
    // Configuration registers (reset 0, SPI CS_n reset 1)
    // ------------------------------------------------------------------------------------
    reg [7:0]  gpo, alt;
    reg [15:0] tcmp;
    reg [9:0]  tctl;
    reg [14:0] pctl;
    reg [9:0]  duty0, duty1, duty2;
    reg [12:0] div;
    reg        scs;
    reg [2:0]  sd;
    reg [4:0]  ien;
    reg [7:0]  een, epol;
    always @(posedge clk) begin
        if (!rst_n) begin
            gpo   <= 8'd0;
            alt   <= 8'd0;
            tcmp  <= 16'd0;
            tctl  <= 10'd0;
            pctl  <= 15'd0;
            duty0 <= 10'd0;
            duty1 <= 10'd0;
            duty2 <= 10'd0;
            div   <= 13'd0;
            scs   <= 1'b1;
            sd    <= 3'd0;
            ien   <= 5'd0;
            een   <= 8'd0;
            epol  <= 8'd0;
        end else begin
            if (be0 & s_gpo)  gpo <= d0;
            if (be0 & s_alt)  alt <= d0;
            if (be0 & s_tcmp) tcmp[7:0]  <= d0;
            if (be1 & s_tcmp) tcmp[15:8] <= d1;
            if (be0 & s_tctl) tctl[7:0]  <= d0;
            if (be1 & s_tctl) tctl[9:8]  <= d1[1:0];
            if (be0 & s_pctl) pctl[7:0]  <= d0;
            if (be1 & s_pctl) pctl[14:8] <= d1[6:0];
            if (be0 & s_dut0) duty0[7:0] <= d0;
            if (be1 & s_dut0) duty0[9:8] <= d1[1:0];
            if (be0 & s_dut1) duty1[7:0] <= d0;
            if (be1 & s_dut1) duty1[9:8] <= d1[1:0];
            if (be0 & s_dut2) duty2[7:0] <= d0;
            if (be1 & s_dut2) duty2[9:8] <= d1[1:0];
            if (be0 & s_uctl) div[7:0]   <= d0;
            if (be1 & s_uctl) div[12:8]  <= d1[4:0];
            if (be0 & s_sctl) {sd, scs}  <= d0[3:0];
            if (be0 & s_ien)  ien  <= d0[4:0];
            if (be0 & s_edge) een  <= d0;
            if (be1 & s_edge) epol <= d1;
        end
    end

    // ------------------------------------------------------------------------------------
    // GPIO edge flags (W1C; a new edge wins over the clear)
    // ------------------------------------------------------------------------------------
    reg [7:0] epend;
    wire [7:0] eset = een & chg & (s2 ^ epol);
    always @(posedge clk) begin
        if (!rst_n)
            epend <= 8'd0;
        else
            epend <= (epend & ~({8{be0 & s_epnd}} & d0)) | eset;
    end

    // ------------------------------------------------------------------------------------
    // Timer: prescaler 2^P, counter, compare, capture on ui[0], output
    // ------------------------------------------------------------------------------------
    wire [3:0] tp_sel = tctl[3:0];
    wire       t_en   = tctl[4];
    wire       t_ar   = tctl[5];
    wire [1:0] t_mode = tctl[7:6];
    wire [1:0] t_cap  = tctl[9:8];
    reg  [14:0] tp;
    reg  [15:0] tcnt, tcap;
    reg         tq, p_cmp, p_cap;
    wire        wr_cnt = (be0 | be1) & s_tcnt;
    // tick when the low P prescaler bits are all ones
    wire [15:0] tp_run = {&tp[14:0], &tp[13:0], &tp[12:0], &tp[11:0], &tp[10:0], &tp[9:0],
                          &tp[8:0], &tp[7:0], &tp[6:0], &tp[5:0], &tp[4:0], &tp[3:0],
                          &tp[2:0], &tp[1:0], tp[0], 1'b1};
    wire        tick   = t_en & tp_run[tp_sel];
    wire        match  = (tcnt == tcmp);
    wire        cap_ev = chg[0] & (s2[0] ? t_cap[0] : t_cap[1]);
    wire [15:0] tcnt_i = tcnt + 16'd1;
    always @(posedge clk) begin
        tp <= (wr_cnt | ~t_en) ? 15'd0 : tp + 15'd1;
        if (!rst_n) begin
            tcnt  <= 16'd0;
            tcap  <= 16'd0;
            tq    <= 1'b0;
            p_cmp <= 1'b0;
            p_cap <= 1'b0;
        end else begin
            if (wr_cnt) begin
                if (be0) tcnt[7:0]  <= d0;
                if (be1) tcnt[15:8] <= d1;
            end else if (tick)
                tcnt <= (match & t_ar) ? 16'd0 : tcnt_i;
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
    wire t_out = (t_mode == 2'b01) ? tq : (t_mode == 2'b10) ? (tcnt < tcmp) : 1'b0;

    // ------------------------------------------------------------------------------------
    // PWM: prescaler 2^Q, counter 0..PER, three duty compares
    // ------------------------------------------------------------------------------------
    wire [9:0] per   = pctl[9:0];
    wire [2:0] q_sel = pctl[12:10];
    wire       p_en  = pctl[13];
    wire       irg   = pctl[14];
    reg  [6:0] pq;
    reg  [9:0] pcnt;
    wire [7:0] pq_run = {&pq[6:0], &pq[5:0], &pq[4:0], &pq[3:0], &pq[2:0], &pq[1:0], pq[0], 1'b1};
    wire       ptick  = pq_run[q_sel];
    always @(posedge clk) begin
        pq <= p_en ? pq + 7'd1 : 7'd0;
        if (!p_en)
            pcnt <= 10'd0;
        else if (ptick)
            pcnt <= (pcnt >= per) ? 10'd0 : pcnt + 10'd1;
    end
    wire pwm0 = p_en & (pcnt < duty0);
    wire pwm1 = p_en & (pcnt < duty1);
    wire pwm2 = p_en & (pcnt < duty2);

    // ------------------------------------------------------------------------------------
    // UART transmitter: {stop, data, start} shifted out LSB first, DIV+1 cycles per bit
    // ------------------------------------------------------------------------------------
    reg  [9:0]  tsr;
    reg  [12:0] tbc;
    wire        tx_busy = |tsr;
    wire        tx_go   = be0 & s_udat & ~tx_busy;
    always @(posedge clk) begin
        if (!rst_n)
            tsr <= 10'd0;
        else if (tx_go)
            tsr <= {1'b1, d0, 1'b0};
        else if (tx_busy & (tbc == 13'd0))
            tsr <= {1'b0, tsr[9:1]};
        if (tx_go | (tbc == 13'd0))
            tbc <= div;
        else
            tbc <= tbc - 13'd1;
    end
    wire tx_pin = tsr[0] | ~tx_busy;

    // ------------------------------------------------------------------------------------
    // UART receiver: a falling edge starts a frame; samples at mid-bit (DIV/2, then DIV+1)
    // ------------------------------------------------------------------------------------
    reg         ract, rfull, rovr;
    reg  [3:0]  rbc;
    reg  [12:0] rcnt;
    reg  [7:0]  rsr, rdat;
    wire        r_start = ~ract & chg[1] & ~s2[1];
    wire        r_smp   = ract & (rcnt == 13'd0);
    wire        r_stop  = r_smp & (rbc == 4'd9);
    wire        r_clr   = rd0 & s_udat;
    wire        full_k  = rfull & ~r_clr;                // reading byte 0 comes first
    always @(posedge clk) begin
        if (r_start)
            rcnt <= {1'b0, div[12:1]};
        else if (r_smp)
            rcnt <= div;
        else
            rcnt <= rcnt - 13'd1;
        if (r_start)
            rbc <= 4'd0;
        else if (r_smp)
            rbc <= rbc + 4'd1;
        if (r_smp & (rbc != 4'd0) & (rbc <= 4'd8))         // data bits 0..7, LSB first
            rsr <= {s2[1], rsr[7:1]};
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
                rdat <= rsr;
            rfull <= full_k | r_stop;
            rovr  <= (rovr & ~r_clr) | (r_stop & full_k);
        end
    end

    // ------------------------------------------------------------------------------------
    // SPI master, mode 0, MSB first: 2^D cycles low, 2^D high per bit. MOSI shifts out of sdr at
    // the falling SCK edges. MISO (synchronized, so 2 cycles late) is captured into srx three
    // cycles after each rising edge (srp is that delay line): the value the pin had in the first
    // high cycle. BUSY stays 1 until the eighth capture.
    // ------------------------------------------------------------------------------------
    reg  [7:0] sdr, srx;
    reg        sbusy, ssck;
    reg  [2:0] sbc, srp;
    reg  [6:0] sdc;
    wire       s_busy  = sbusy | (|srp);
    wire       s_go    = be0 & s_sdat & ~s_busy;
    wire [7:0] sdc_run = {&sdc[6:0], &sdc[5:0], &sdc[4:0], &sdc[3:0], &sdc[2:0], &sdc[1:0], sdc[0], 1'b1};
    wire       s_half  = sbusy & sdc_run[sd];
    always @(posedge clk) begin
        sdc <= s_go ? 7'd0 : sdc + 7'd1;
        if (s_go)
            sbc <= 3'd0;
        else if (s_half & ssck)
            sbc <= sbc + 3'd1;
        if (s_go)
            sdr <= d0;
        else if (s_half & ssck)
            sdr <= {sdr[6:0], 1'b0};
        if (!rst_n) begin
            srx   <= 8'd0;
            sbusy <= 1'b0;
            ssck  <= 1'b0;
            srp   <= 3'd0;
        end else begin
            srp <= {srp[1:0], s_half & ~ssck};
            if (srp[2])
                srx <= {srx[6:0], s2[3]};
            if (s_go)
                sbusy <= 1'b1;
            else if (s_half & ssck & (sbc == 3'd7))
                sbusy <= 1'b0;
            if (s_go)
                ssck <= 1'b0;
            else if (s_half)
                ssck <= ~ssck;
        end
    end

    // ------------------------------------------------------------------------------------
    // Interrupt line (registered: MEIP is at most a cycle late)
    // ------------------------------------------------------------------------------------
    wire [4:0] pend = {|epend, ~tx_busy, rfull, p_cap, p_cmp};
    always @(posedge clk) begin
        if (!rst_n)
            irq <= 1'b0;
        else
            irq <= |(pend & ien);
    end

    // ------------------------------------------------------------------------------------
    // Pins
    // ------------------------------------------------------------------------------------
    wire [7:0] fn = {t_out, sdr[7], ssck, scs, pwm2, pwm1, pwm0 & (t_out | ~irg), tx_pin};
    assign uo_out = (alt & fn) | (~alt & gpo);

    // ------------------------------------------------------------------------------------
    // Read mux
    // ------------------------------------------------------------------------------------
    always @(*) begin
        case (slot)
            5'd0:  rdata = {8'd0, gpo};
            5'd1:  rdata = {8'd0, s2};
            5'd2:  rdata = {8'd0, alt};
            5'd3:  rdata = tcnt;
            5'd4:  rdata = tcmp;
            5'd5:  rdata = tcap;
            5'd6:  rdata = {6'd0, tctl};
            5'd7:  rdata = {1'b0, pctl};
            5'd8:  rdata = {6'd0, duty0};
            5'd9:  rdata = {6'd0, duty1};
            5'd10: rdata = {6'd0, duty2};
            5'd11: rdata = {8'd0, rdat};
            5'd12: rdata = {rovr, rfull, tx_busy, div};
            5'd13: rdata = {8'd0, srx};
            5'd14: rdata = {11'd0, s_busy, sd, scs};
            5'd15: rdata = {11'd0, ien};
            5'd16: rdata = {11'd0, pend};
            5'd17: rdata = {epol, een};
            5'd18: rdata = {8'd0, epend};
            default: rdata = 16'd0;
        endcase
    end

endmodule

`default_nettype wire
