/*
 * BonettoR32V MCU peripherals (docs/contract.md section 5): GPIO with edge flags (r32v_gpio),
 * a 16-bit timer with compare, capture and an output (r32v_timer), 3 PWM channels (r32v_pwm), a
 * UART, 8N1 (r32v_uart) and an SPI master, mode 0 (r32v_spi), plus the interrupt line. Each
 * peripheral is its own module with its own registers; this one holds the bus decode, the input
 * synchronizers, the shared write data, ALTFN (the pin mux), IRQ_EN and the interrupt line, and
 * the one read mux.
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
 * Inputs pass through a one-flop synchronizer (s2 is the synchronized value, s3 the previous one
 * for edge detection): an input change reaches the logic 1 cycle later, within contract 5's
 * 3 cycles. At 50 MHz the flop has most of a 20 ns cycle to resolve before anything samples it.
 *
 * Resets: every register a load can read takes its contract reset value. Flops whose reset value
 * nothing can observe have none: GPIO_IN's flop, the edge-detect flops of the pins whose edge
 * users are all disabled at reset (only the UART RX pin's resets, so no false start bit shows),
 * the prescalers and bit timers, the UART receiver's shift register, and the toggle flop (the
 * TIMER_CTRL write that selects toggle mode clears it). Read/write registers, the timer counter,
 * the UART transmitter and the SPI shift register are plain enable flops: the reset is folded
 * into the shared write data (0 while rst_n is low) and into each write or load enable.
 *
 * Internal counters nothing reads (the timer, PWM and SPI prescalers, the PWM counter) hold
 * the complement of the count: they count down, restart at all ones, and test "low bits all
 * ones" as "low bits all zero". The UART receiver ends on a marker bit that walks through its
 * shift register instead of a bit counter. The SPI master keeps the byte it sends and the byte
 * it receives in one shift register, and one counter holds its BUSY flag, the SCK level and the
 * bit index.
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
    // s3 has no reset except for the UART RX pin: an edge on any other pin right after reset
    // reaches nothing, since every user (GPIO_EDGE enables, the capture edge select) resets to off
    reg [7:0] s2, s3;
    always @(posedge clk) begin
        s2 <= ui_in;
        s3 <= s2;
        if (!rst_n)
            s3[1] <= 1'b0;                             // no false RX start bit after reset
    end
    wire [7:0] chg = s2 ^ s3;                         // a synchronized edge on each pin

    // ------------------------------------------------------------------------------------
    // Shared write data and the registers of this module (ALTFN, IRQ_EN; reset 0)
    // ------------------------------------------------------------------------------------
    // Plain enable flops: the reset is folded into the shared write data z0/z1 (0 while rst_n is
    // low) and into every write enable, so each bit maps to one enable flop with no logic of its own.
    // Every peripheral module takes these signals and writes its own registers the same way.
    wire       rst = ~rst_n;
    wire [7:0] z0  = d0 & {8{rst_n}};
    wire [7:0] z1  = d1 & {8{rst_n}};
    wire       w0  = be0 | rst;
    wire       w1  = be1 | rst;
    reg  [7:0] alt;
    reg  [4:0] ien;
    always @(posedge clk) begin
        if (w0 & (s_alt | rst)) alt <= z0;
        if (w0 & (s_ien | rst)) ien <= z0[4:0];
    end

    // ------------------------------------------------------------------------------------
    // Peripherals
    // ------------------------------------------------------------------------------------
    wire [7:0] gpo, een, epol, epend;
    r32v_gpio u_gpio (
        .clk(clk), .rst_n(rst_n), .rst(rst), .w0(w0), .w1(w1), .z0(z0), .z1(z1),
        .be0(be0), .d0(d0), .s_gpo(s_gpo), .s_edge(s_edge), .s_epnd(s_epnd),
        .s2(s2), .chg(chg),
        .gpo(gpo), .een(een), .epol(epol), .epend(epend)
    );

    wire [15:0] tcnt, tcmp, tcap;
    wire [9:0]  tctl;
    wire        p_cmp, p_cap, t_out;
    r32v_timer u_timer (
        .clk(clk), .rst_n(rst_n), .rst(rst), .be0(be0), .be1(be1), .w0(w0), .w1(w1),
        .z0(z0), .z1(z1), .d0(d0),
        .s_tcnt(s_tcnt), .s_tcmp(s_tcmp), .s_tctl(s_tctl), .s_pend(s_pend),
        .cap_in(s2[0]), .cap_chg(chg[0]),
        .tcnt(tcnt), .tcmp(tcmp), .tcap(tcap), .tctl(tctl),
        .p_cmp(p_cmp), .p_cap(p_cap), .t_out(t_out)
    );

    wire [14:0] pctl;
    wire [9:0]  duty0, duty1, duty2;
    wire [2:0]  pwm;
    r32v_pwm u_pwm (
        .clk(clk), .rst(rst), .w0(w0), .w1(w1), .z0(z0), .z1(z1),
        .s_pctl(s_pctl), .s_dut0(s_dut0), .s_dut1(s_dut1), .s_dut2(s_dut2), .t_out(t_out),
        .pctl(pctl), .duty0(duty0), .duty1(duty1), .duty2(duty2), .pwm(pwm)
    );

    wire [12:0] div;
    wire [7:0]  rdat;
    wire        rfull, rovr, tx_busy, tx_pin;
    r32v_uart u_uart (
        .clk(clk), .rst_n(rst_n), .rst(rst), .be0(be0), .w0(w0), .w1(w1), .z0(z0), .z1(z1),
        .rd0(rd0), .s_udat(s_udat), .s_uctl(s_uctl), .rx(s2[1]), .rx_chg(chg[1]),
        .div(div), .rdat(rdat), .rfull(rfull), .rovr(rovr), .tx_busy(tx_busy), .tx_pin(tx_pin)
    );

    wire       scs, sbusy, ssck, mosi;
    wire [2:0] sd;
    wire [7:0] srxd;
    r32v_spi u_spi (
        .clk(clk), .rst_n(rst_n), .rst(rst), .be0(be0), .w0(w0), .z0(z0),
        .s_sdat(s_sdat), .s_sctl(s_sctl), .miso(s2[3]),
        .scs(scs), .sd(sd), .sbusy(sbusy), .ssck(ssck), .mosi(mosi), .rxd(srxd)
    );

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
    wire [7:0] fn = {t_out, mosi, ssck, scs, pwm, tx_pin};
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
            5'd13: rdata = {8'd0, srxd};
            5'd14: rdata = {11'd0, sbusy, sd, scs};
            5'd15: rdata = {11'd0, ien};
            5'd16: rdata = {11'd0, pend};
            5'd17: rdata = {epol, een};
            5'd18: rdata = {8'd0, epend};
            default: rdata = 16'd0;
        endcase
    end

endmodule

`default_nettype wire
