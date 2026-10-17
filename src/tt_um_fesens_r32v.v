/*
 * BonettoR32V hypothesis 002, variant 2: bit-serial RV32E with the register file AND the pc in
 * the SPI RAM.
 *
 * x1..x15 live in the 23LC512 at 0xFFC0 + 4*r, the pc in the x0 slot at 0xFFC0, and 0xFF80 is a
 * scratch word (JALR with rd != 0). The die keeps the instruction register, ONE operand/result
 * buffer, the SPI engine and uo_out. See src/RF_IN_RAM.md, section 8.
 *
 * Ring. The buffer (bfr) is a 33-bit ring that rotates right on every clock. A free-running phase
 * counter ph counts 0..32; the bit at ring position i shows phase ph + i (mod 33). Logical bit k
 * of a value sits at phase 2k (mod 33): bits 0..16 at even phases, 17..31 at odd phases, phase 31
 * unused. SPI data at SCK = clk/2 moves one bit every 2 clocks, so a data phase whose first bit
 * meets phase 0 hits bit k at phase 2k: register traffic streams straight into and out of the
 * ring with no per-bit enables. 33 is odd, so a time offset of 2s clocks is a shift by s bits:
 * shifts are a delayed emission. A store keeps its 16-bit RAM address on the even phases and 16
 * data bits on the odd phases (bit k at phase 2k + 1).
 *
 * Transactions (slots). Every SPI transaction has the same frame: 8 command bits, the address
 * high byte, a one-clock gap, the low byte, then data, all at SCK = clk/2. A slot starts at a
 * phase set by its family (sl[3:2]), so every tap below is a fixed ring position:
 *   P   READ  x0 (the pc)                              -> buffer
 *   A   READ  rs1 -> serial ALU (+imm; AUIPC/JAL: buffer + imm) -> buffer
 *   PX  READ  x0 -> buffer + (taken ? imm : 4) (branch); READ scratch -> buffer (JALR)
 *   B   READ  rs2 -> serial ALU with the buffer
 *   BH  READ  rs2 (BH2: rs2 + 2, and address + 2 in place) -> buffer odd phases (store data)
 *   F   READ  pc[15:0] (buffer even-tap mux)           -> instruction register
 *   M   READ/WRITE address from the buffer even taps, store data from the odd taps (M2: high half)
 *   WP  WRITE x0 <- buffer;  WP4: x0 <- buffer + 4;  W4: rd (JAL) or scratch (JALR) <- buffer + 4
 *   W   WRITE rd <- buffer (forward, load-reversed, or shifted)
 * A slot whose register is x0 (or that has no register) keeps CS_n high and runs the same timing.
 * After reset a boot WP writes pc = 0. A trap halts in the retire state.
 *
 * Copyright 2026 Felipe Sens Bonetto
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tt_um_fesens_r32v (
    input  wire [7:0]  ui_in,     // GPIO in
    output wire [7:0]  uo_out,    // GPIO out
    input  wire [7:0]  uio_in,    // uio[2] = SPI MISO
    output wire [7:0]  uio_out,   // uio[0] = CS_n, uio[1] = MOSI, uio[3] = SCK
    output wire [7:0]  uio_oe,
    input  wire        ena,       // ignored
    input  wire        clk,
    input  wire        rst_n
`ifdef RISCV_FORMAL
    ,
    output wire        rvfi_valid,
    output wire [63:0] rvfi_order,
    output wire [31:0] rvfi_insn,
    output wire        rvfi_trap,
    output wire        rvfi_halt,
    output wire        rvfi_intr,
    output wire [1:0]  rvfi_mode,
    output wire [1:0]  rvfi_ixl,
    output wire [4:0]  rvfi_rs1_addr,
    output wire [4:0]  rvfi_rs2_addr,
    output wire [31:0] rvfi_rs1_rdata,
    output wire [31:0] rvfi_rs2_rdata,
    output wire [4:0]  rvfi_rd_addr,
    output wire [31:0] rvfi_rd_wdata,
    output wire [31:0] rvfi_pc_rdata,
    output wire [31:0] rvfi_pc_wdata,
    output wire [31:0] rvfi_mem_addr,
    output wire [3:0]  rvfi_mem_rmask,
    output wire [3:0]  rvfi_mem_wmask,
    output wire [31:0] rvfi_mem_rdata,
    output wire [31:0] rvfi_mem_wdata
`endif
);

    // Slot types. sl[3:2] is the family: 00 READ into the serial ALU (start phase 16), 01 READ
    // into the odd phases (17), 10 address from the buffer (2), 11 WRITE from the buffer (W: 5;
    // WP, WP4, W4: 18, straight from bfr[0]). Binary codes, used bitwise below.
    localparam [3:0] SL_P  = 4'b0000, SL_A   = 4'b0001, SL_PX = 4'b0010, SL_B  = 4'b0011,
                     SL_BH = 4'b0100, SL_BH2 = 4'b0110,
                     SL_F  = 4'b1000, SL_M   = 4'b1001, SL_M2 = 4'b1011,
                     SL_WP = 4'b1100, SL_WP4 = 4'b1101, SL_W4 = 4'b1110, SL_W  = 4'b1111;
    // Engine phases: 1x0 = command/address or data (SCK runs)
    localparam [2:0] E_IDLE = 3'b000,    // CS_n high, wait for the slot's start phase
                     E_PW   = 3'b001,    // shift pre-roll: wait for the reference phase
                     E_PC   = 3'b010,    // shift pre-roll: count j up to 63
                     E_RET  = 3'b011,    // retire; a trap stays here (halt)
                     E_CMD  = 3'b100,    // command and address bits
                     E_DATA = 3'b101,    // data bits
                     E_GAP  = 3'b110,    // one clock between the address bytes
                     E_END  = 3'b111;    // SCK low, then CS_n high

    reg  [3:0]  sl;
    reg  [2:0]  ep;
    reg  [5:0]  ph;     // 0..32
    reg  [5:0]  j;      // bit counter (40..63 command/address, 0.. data), shift pre-roll
    reg         sck, mosi;
    reg         mem;    // load/store goes to RAM (address bit 31 = 0); 1 during the boot slot
    reg         cy;     // ALU carry; sign latch in W
    reg         cmp;    // compare; address[1:0] == 0; JALR target[1]; shift fill state
    reg         c4;     // carry of the on-the-fly + 4 (W family) and + 2 (BH2)
    reg  [32:0] bfr;    // operand/result buffer ring
    reg  [30:0] irs;    // instruction register, in SPI order, without insn[1]
    reg  [7:0]  gpo;

    assign uo_out  = gpo;
    assign uio_out = {4'b0000, sck & ~cs_n, 1'b0, mosi, cs_n};
    assign uio_oe  = 8'b0000_1011;

    wire _unused = &{ena, uio_in[7:3], uio_in[1:0], 1'b0};

    // ------------------------------------------------------------------------------------
    // Decode, spec-minimum (contract section 4). It traps on insn[0] = 0 (the all-zeros word),
    // opcode[6:4] = 111 (SYSTEM: ECALL, EBREAK; the all-ones word) and misaligned JAL. Every other
    // encoding executes as the class its opcode bits select; register fields use 4 bits. insn[1]
    // is not stored: no design logic reads it.
    // ------------------------------------------------------------------------------------
    wire [31:0] insn  = {irs[7:0], irs[15:8], irs[23:16], irs[30:25], 1'b1, irs[24]};
    wire [2:0]  f3    = insn[14:12];
    wire [3:0]  rd_f  = insn[10:7];
    wire [3:0]  rs1_f = insn[18:15];
    wire [3:0]  rs2_f = insn[23:20];

    // Datapath classes, from the opcode bits that tell the RV32E classes apart
    wire c_br    = insn[6] & ~insn[2];
    wire c_jalr  = insn[6] & insn[2] & ~insn[3];
    wire c_jal   = insn[6] & insn[3];
    wire c_mem   = ~insn[6] & ~insn[4] & ~insn[2];
    wire c_fence = ~insn[6] & ~insn[4] & insn[2];
    wire c_st    = c_mem & insn[5];
    wire c_ld    = c_mem & ~insn[5];
    wire c_u     = ~insn[6] & insn[4] & insn[2];
    wire c_pcrel = (c_u & ~insn[5]) | c_jal;            // AUIPC, JAL: buffer (pc) + imm
    wire c_alu   = ~insn[6] & insn[4] & ~insn[2];
    wire c_op    = c_alu & insn[5];
    wire c_sh    = c_alu & (f3[1:0] == 2'b01);
    wire c_slt   = c_alu & (f3[2:1] == 2'b01);
    wire sh_left = ~f3[2];
    wire [1:0] nm1 = {f3[1], f3[1] | f3[0]};   // access bytes - 1
    wire trap_dec  = ~insn[0] | (insn[6] & insn[5] & insn[4]) | (c_jal & insn[21]);

    wire fR   = ~sl[3] & ~sl[2];
    wire fO   = ~sl[3] &  sl[2];
    wire fM   =  sl[3] & ~sl[2];
    wire fW   =  sl[3] &  sl[2];
    wire sA   = fR & ~sl[1] &  sl[0];
    wire sPX  = fR &  sl[1] & ~sl[0];
    wire sB   = fR &  sl[1] &  sl[0];
    wire sF   = fM & ~sl[0];
    wire sMM  = fM &  sl[0];
    wire sW   = fW &  sl[1] &  sl[0];
    wire sW4  = fW &  sl[1] & ~sl[0];
    wire hi   = sl[1] & (fO | fM);                 // BH2, M2
    wire winc = fW & (sl[1] ^ sl[0]);              // WP4, W4
    wire wpln = fW & ~(sl[1] & sl[0]);             // WP, WP4, W4
    wire shB  = sB & c_sh;
    wire e_cd = ep[2] & ~ep[1];                    // CMD or DATA: SCK runs
    // CS_n is low in CMD, GAP, DATA and END (ep[2]) unless the slot is masked
    wire cs_n;
    wire miso_m;

    // ------------------------------------------------------------------------------------
    // Bit index of the current strobe in the ALU slots (family 00): bit k meets phase 2k.
    // ------------------------------------------------------------------------------------
    wire [4:0] kb = ph[0] ? {1'b1, ph[4:1] + 4'd1} : ph[5:1];
    wire       k0 = (ph == 6'd0);

    wire [15:0] va   = {4'd0, insn[31:20]};
    wire [7:0]  vb   = {3'd0, insn[11:7]};
    wire        m_a  = va[kb[3:0]];
    wire        m_b  = vb[kb[2:0]];
    wire        m_c  = insn[kb];
    wire        k_hi = kb[4] | (kb[3] & kb[2]);                       // k >= 12
    wire        k11  = (kb == 5'd11);
    wire        k_lo5 = ~kb[4] & ~kb[3] & ~(kb[2] & (kb[1] | kb[0]));  // k < 5
    wire        sign = insn[31];
    wire        imm_bit =
        c_u   ? (k_hi & m_c) :
        c_jal ? (k_hi ? ((~kb[4] | (kb[3:2] == 2'b00)) ? m_c : sign) :
                        (k0 ? 1'b0 : k11 ? insn[20] : m_a)) :
        k_hi  ? sign :
        c_br  ? (k0 ? 1'b0 : k11 ? insn[7] : k_lo5 ? m_b : m_a) :
        c_st  ? (k_lo5 ? m_b : m_a) :
                m_a;

    // ------------------------------------------------------------------------------------
    // Serial ALU (strobe = SCK-high clock of a data bit)
    // ------------------------------------------------------------------------------------
    wire stb    = (ep == E_DATA) & sck;
    wire taken  = cmp ^ f3[0];
    wire bimm_a = ~(c_op | c_br | c_sh);
    wire a_bit  = (sB | (sA & c_pcrel)) ? bfr[0] : miso_m;
    wire b_raw  = sA ? (bimm_a & imm_bit) :
                  sB ? ((c_sh & ~c_op) ? imm_bit : miso_m) :
                  (sPX & c_br & (taken ? imm_bit : (ph == 6'd4)));
    wire sub    = sA ? c_slt : (sB & ((c_op & insn[30] & (f3 == 3'b000)) | c_slt | c_br));
    wire b_x    = b_raw ^ sub;
    wire cin    = k0 ? sub : cy;
    wire sum    = a_bit ^ b_x ^ cin;
    wire cout   = (a_bit & b_x) | (cin & (a_bit ^ b_x));
    wire boolr  = ((a_bit ^ b_raw) & ~f3[0]) | (f3[1] & a_bit & b_raw);
    wire sgnd   = c_br ? ~f3[1] : ~f3[0];
    wire lt     = (a_bit & sgnd) ^ ~(b_raw & sgnd) ^ cout;
    wire eqacc  = ~sum & (cmp | k0);
    wire ocmp   = (c_br & ~f3[2]) ? eqacc : lt;
    wire trap   = trap_dec | (c_br & taken & insn[8]) | (c_jalr & cmp);

    // ------------------------------------------------------------------------------------
    // Ring tap muxes for MSB-first emission, select a = j[2:0]: address bit 15 - a / 7 - a from
    // the even phases (bfm), store data bit 7 - a of each byte from the odd phases (bfo), load
    // data bit a of each byte (reversed, bfm), or select 0 for a forward emission.
    // ------------------------------------------------------------------------------------
    wire       fwd  = fW & ~(sW & c_ld);
    wire [2:0] msel = j[2:0] & {3{~fwd}};
    reg bfm, bfo;
    always @(*) begin
        case (msel)
            3'd0: bfm = bfr[13];
            3'd1: bfm = bfr[9];
            3'd2: bfm = bfr[5];
            3'd3: bfm = bfr[1];
            3'd4: bfm = bfr[30];
            3'd5: bfm = bfr[26];
            3'd6: bfm = bfr[22];
            default: bfm = bfr[18];
        endcase
        case (j[2:0])
            3'd0: bfo = bfr[31];
            3'd1: bfo = bfr[27];
            3'd2: bfo = bfr[23];
            3'd3: bfo = bfr[19];
            3'd4: bfo = bfr[15];
            3'd5: bfo = bfr[11];
            3'd6: bfo = bfr[7];
            default: bfo = bfr[3];
        endcase
    end

    // ------------------------------------------------------------------------------------
    // W family data: bit k = j[4:0] at the emission strobe (the SCK-high clock before bit k)
    // ------------------------------------------------------------------------------------
    wire wem    = fW & sck & ~j[5] & e_cd;
    wire sh_evt = (ph == {3'b010, sh_left, ~sh_left, 1'b0});  // 18: source 32 passes; 20: source 0
    wire shfill = sh_left ? ~(cmp | sh_evt) : (cmp | sh_evt);
    wire ldfill = (j[4:3] > nm1);
    wire io_w   = (j[4:3] == 2'b00) ? (cmp & ui_in[j[2:0]]) : ((f3 == 3'b000) & cmp & ui_in[7]);
    wire wfill  = c_sh ? shfill : ldfill;
    wire wd     = c_ld  ? (mem ? (ldfill ? (~f3[2] & cy) : bfm) : io_w) :
                  c_slt ? ((j[4:0] == 5'd0) & cmp) :
                  c_sh  ? (shfill ? (insn[30] & cy) : bfm) :
                          bfm;
    wire wpl    = (bfr[0] ^ (c4 & winc)) & ~mem;  // WP, WP4, W4; the boot WP writes 0
    wire wdo    = sW ? wd : wpl;

    // BH2: address + 2 on the even phases, in the SCK-low clocks between the data strobes
    wire inc2   = (ep == E_DATA) & ~sck & hi & fO;
    wire cin2   = (ph == 6'd2) | c4;

    // ------------------------------------------------------------------------------------
    // MOSI
    // ------------------------------------------------------------------------------------
    wire       wr    = fW | (sMM & c_st);
    wire       scr   = c_jalr & (sPX | sW4);       // the scratch word: address bit 6 = 0
    wire [3:0] rsel  = sA ? rs1_f : (sB | fO) ? rs2_f : (sW | (sW4 & c_jal)) ? rd_f : 4'd0;
    reg        ra_bit;
    always @(*) begin
        case (j[2:0])
            3'd0: ra_bit = 1'b1;
            3'd1: ra_bit = ~scr;
            3'd2: ra_bit = rsel[3];
            3'd3: ra_bit = rsel[2];
            3'd4: ra_bit = rsel[1];
            3'd5: ra_bit = rsel[0];
            3'd6: ra_bit = hi;
            default: ra_bit = 1'b0;
        endcase
    end
    wire cmdbit  = (j[2:1] == 2'b11) & ~(j[0] & wr);
    wire mosi_ca = (j[4:3] == 2'b01) ? cmdbit : fM ? bfm : (~j[3] | ra_bit);
    wire mosi_d  = fM ? bfo : wdo;
    wire mosi_nx = j[5] ? mosi_ca : mosi_d;

    // ------------------------------------------------------------------------------------
    // Slot control
    // ------------------------------------------------------------------------------------
    // the slot starts (CS_n falls) after the IDLE clock at this phase: 15, 16, 1, 4 (W), 17
    wire [5:0] tgt = {1'b0, fO | wpln, fR, fR | sW, fR, fR | fM | wpln};
    wire mask = (sA & (c_u | c_jal | (rs1_f == 4'd0))) |
                (sB & ~(c_op | c_br)) |
                ((sB | fO) & (rs2_f == 4'd0)) |
                (fW & sl[1] & (rd_f == 4'd0));
    assign cs_n   = ~ep[2] | mask;
    assign miso_m = uio_in[2] & ~cs_n;   // 0 in a masked (x0) slot
    wire [5:0] dlen = (fM & ~sF) ? {c_ld & (nm1 == 2'b11), (nm1 == 2'b01) | (c_st & (nm1 == 2'b11)),
                                    nm1 == 2'b00, 3'b000} :
                      fO ? {~(f3[1] & mem), f3[1] & mem, 4'b0000} : 6'd32;
    wire dend = shB ? (ph == 6'd29) : (j == dlen);

    // next slot after END
    reg [3:0] nsl;
    reg       nret, npre;
    always @(*) begin
        nsl  = SL_W;
        nret = 1'b0;
        npre = 1'b0;
        case (sl)
            SL_P: nsl = SL_F;
            SL_F: begin
                nret = trap_dec;
                nsl  = c_br ? SL_A : (c_jal | c_jalr) ? SL_W4 : SL_WP4;
            end
            SL_WP4: begin
                nret = c_fence;
                nsl  = SL_A;
            end
            SL_W4: nsl = SL_A;
            SL_BH: begin
                nsl  = SL_M;
                nret = ~mem;
            end
            SL_A: begin
                if (c_jalr & cmp)
                    nret = 1'b1;
                else if (c_jal | c_jalr)
                    nsl = SL_WP;
                else if (c_br | c_op | c_sh)
                    nsl = SL_B;
                else if (c_st)
                    nsl = SL_BH;
                else if (c_ld & mem)
                    nsl = SL_M;
            end
            SL_B: begin
                if (c_sh)
                    npre = 1'b1;
                else if (c_br) begin
                    nsl  = SL_PX;
                    nret = taken & insn[8];
                end
            end
            SL_PX: nsl = c_br ? SL_WP : SL_W;
            SL_WP: begin
                nsl  = mem ? SL_P : SL_PX;
                nret = ~mem & ~(c_jalr & (rd_f != 4'd0));
            end
            SL_M: begin
                nsl  = c_ld ? SL_W : SL_BH2;
                nret = c_st & ~f3[1];
            end
            SL_BH2: nsl = SL_M2;
            default: nret = 1'b1;     // M2, W
        endcase
    end

    // ------------------------------------------------------------------------------------
    // Ring (no reset: nothing reads it before it is written)
    // ------------------------------------------------------------------------------------
    wire useb   = (sA & c_alu & ~insn[5] & f3[2] & ~c_sh) | (sB & (f3 != 3'b000));
    wire alu_w  = useb ? boolr : (sum & ~(k0 & c_jalr));
    wire lo16   = ~j[5] & (~j[4] | (j[3:0] == 4'd0));   // data bits 0..15 (j = bit + 1)
    wire bfr_we = inc2 | (stb & (fR ? ~shB : fO ? lo16 : (sMM & c_ld)));
    wire bfr_w  = inc2 ? (bfr[0] ^ cin2) : fR ? alu_w : miso_m;

    always @(posedge clk) begin
        bfr <= {bfr_we ? bfr_w : bfr[0], bfr[32:1]};
        // the tail (insn[7:2]) skips the last strobe, so insn[1] (the strobe before) drops out
        if (sF & stb) begin
            irs[24:0] <= {irs[23:0], uio_in[2]};
            if (~j[5])
                irs[30:25] <= irs[29:24];
        end
    end

    // ------------------------------------------------------------------------------------
    // Datapath flags (no reset)
    // ------------------------------------------------------------------------------------
    always @(posedge clk) begin
        if (sW) begin
            if (wem & ~wfill)
                cy <= bfm;
        end else if (stb)
            cy <= cout;

        if (stb & sA) begin
            if (c_slt)
                cmp <= ocmp;
            else if (c_jalr & (ph == 6'd2))
                cmp <= sum;                                 // target[1]
            else if (c_mem & ((ph == 6'd0) | (ph == 6'd2)))
                cmp <= eqacc;                               // address[1:0] == 0
        end else if (stb & sB & (c_slt | c_br))
            cmp <= ocmp;
        else if (sW & c_sh) begin
            if ((ep == E_CMD) & j[5])
                cmp <= 1'b0;
            else if (wem & sh_evt)
                cmp <= 1'b1;
        end

        if (wem)
            c4 <= (j[4:0] == 5'd1) | (c4 & bfr[0]);
        else if (inc2)
            c4 <= bfr[0] & cin2;
        else if (ep == E_CMD)
            c4 <= 1'b0;

        // bit counter
        if (((ep == E_IDLE) & (ph == tgt)) | ((ep == E_PC) & (j == 6'd63)))
            j <= 6'd40;
        else if (((ep == E_CMD) | ((ep == E_DATA) & ~shB)) & ~sck)
            j <= j + 6'd1;
        else if (ep == E_PC)
            j <= j + 6'd1;
        else if (stb & shB & ~ph[0] & (ph < 6'd10))
            j <= {b_raw ^ ~sh_left, j[5:1]};

        // MOSI changes only where SCK stays low or falls
        if (e_cd & ~sck)
            mosi <= mosi;
        else if (e_cd | (ep == E_GAP))
            mosi <= mosi_nx;
        else
            mosi <= 1'b0;
    end

    // ------------------------------------------------------------------------------------
    // Control (reset)
    // ------------------------------------------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            sl   <= SL_WP;
            ep   <= E_IDLE;
            ph   <= 6'd17;                                // the boot WP starts at once
            sck  <= 1'b0;
            mem  <= 1'b1;
            gpo  <= 8'd0;
        end else begin
            ph  <= (ph == 6'd32) ? 6'd0 : ph + 6'd1;
            sck <= e_cd & ~sck;
            if (stb & sA & c_mem & (ph == 6'd29))
                mem <= ~sum;                                // address bit 31 = 0: RAM
            case (ep)
                E_IDLE: if (ph == tgt) begin
                    ep   <= E_CMD;
                end
                E_CMD: if (sck) begin
                    if (j == 6'd56)
                        ep <= E_GAP;
                    else if (j == 6'd0)
                        ep <= E_DATA;
                end
                E_GAP:  ep <= E_CMD;
                E_DATA: if (sck & dend) ep <= E_END;
                E_END: begin
                    sl   <= nsl;
                    ep   <= npre ? E_PW : nret ? E_RET : E_IDLE;
                    if (fW & ~sl[1] & ~sl[0])
                        mem <= 1'b0;                        // WP: ends the boot slot
                end
                E_PW: if (ph == {3'b000, sh_left, 2'b10}) ep <= E_PC;
                E_PC: if (j == 6'd63) ep <= E_CMD;
                default: begin                              // E_RET
                    if (c_st & ~mem & cmp & ~trap)
                        gpo <= {bfr[16], bfr[14], bfr[12], bfr[10], bfr[8], bfr[6], bfr[4], bfr[2]};
                    mem <= 1'b0;
                    sl  <= SL_P;
                    if (~trap)
                        ep <= E_IDLE;
                end
            endcase
        end
    end

    // ------------------------------------------------------------------------------------
    // RVFI (formal and cosim builds only; observes, never drives the pins)
    // ------------------------------------------------------------------------------------
`ifdef RISCV_FORMAL
    wire        sP       = fR & ~sl[1] & ~sl[0];
    reg         r_i1;                                   // insn[1], not stored by the design

    reg [63:0] r_order;
    reg        r_halt;
    reg [31:0] r_rs1, r_rs2, r_rd, r_pc, r_npc, r_mrd;
    integer    mi;
    wire [4:0] dj    = j[4:0] - 5'd1;
    wire       r_ret = (ep == E_RET) & ~r_halt;
    always @(posedge clk) begin
        if (!rst_n) begin
            r_order <= 64'd0;
            r_halt  <= 1'b0;
        end else if (r_ret) begin
            r_order <= r_order + 64'd1;
            r_halt  <= trap;
        end
        if (stb & sP)
            r_pc <= {miso_m, r_pc[31:1]};
        if (stb & sF & (j == 6'd7))
            r_i1 <= uio_in[2];
        if (wem & ~sl[1])
            r_npc <= {wdo, r_npc[31:1]};                    // WP, WP4
        if (stb & sA)
            r_rs1 <= {miso_m, r_rs1[31:1]};
        if (stb & (sB | fO))
            r_rs2 <= {miso_m, r_rs2[31:1]};
        if (wem & sl[1] & (sl[0] | c_jal))
            r_rd <= {wdo, r_rd[31:1]};                      // W, or W4 of a JAL
        for (mi = 0; mi < 32; mi = mi + 1)
            if (stb & sMM & c_ld & ({dj[4:3], ~dj[2:0]} == mi[4:0]))
                r_mrd[mi] <= uio_in[2];
    end

    wire [31:0] imm_i = {{20{insn[31]}}, insn[31:20]};
    wire [31:0] imm_s = {{20{insn[31]}}, insn[31:25], insn[11:7]};
    wire [31:0] imm_b = {{20{insn[31]}}, insn[7], insn[30:25], insn[11:8], 1'b0};
    wire [31:0] imm_j = {{12{insn[31]}}, insn[19:12], insn[20], insn[30:21], 1'b0};
    wire [31:0] jalr_t = (r_rs1 + imm_i) & 32'hFFFF_FFFE;
    // a decode trap reports pc + 4 (a misaligned JAL its target), as the reference ISS does
    wire        r_jal  = ({insn[6:2], r_i1, insn[0]} == 7'b1101111);
    wire [31:0] npc_t  = (trap_dec & ~r_jal) ? r_pc + 32'd4 : c_jal ? r_pc + imm_j : c_jalr ? jalr_t :
                         (c_br & taken) ? r_pc + imm_b : r_pc + 32'd4;

    wire        r_rs1e = ~trap_dec & (c_alu | c_mem | c_br | c_jalr);
    wire        r_rs2e = ~trap_dec & (c_br | c_st | c_op);
    wire        r_rde  = ~trap & (c_alu | c_u | c_jal | c_jalr | c_ld) & (rd_f != 4'd0);
    wire        r_mem  = ~trap & c_mem;
    wire [31:0] maddr  = r_rs1 + (c_st ? imm_s : imm_i);
    wire [3:0]  r_mask = (f3[1:0] == 2'b00) ? 4'b0001 : (f3[1:0] == 2'b01) ? 4'b0011 : 4'b1111;
    wire [31:0] lmask  = {{8{r_mask[3]}}, {8{r_mask[2]}}, {8{r_mask[1]}}, {8{r_mask[0]}}};
    wire [31:0] mrd    = maddr[31] ? {24'd0, cmp ? ui_in : 8'd0} : r_mrd;

    assign rvfi_valid     = r_ret;
    assign rvfi_order     = r_order;
    assign rvfi_insn      = {insn[31:2], r_i1, insn[0]};
    assign rvfi_trap      = trap;
    assign rvfi_halt      = trap;
    assign rvfi_intr      = 1'b0;
    assign rvfi_mode      = 2'd3;
    assign rvfi_ixl       = 2'd1;
    assign rvfi_rs1_addr  = r_rs1e ? {1'b0, rs1_f} : 5'd0;
    assign rvfi_rs2_addr  = r_rs2e ? {1'b0, rs2_f} : 5'd0;
    assign rvfi_rs1_rdata = r_rs1e ? r_rs1 : 32'd0;
    assign rvfi_rs2_rdata = r_rs2e ? r_rs2 : 32'd0;
    assign rvfi_rd_addr   = r_rde ? {1'b0, rd_f} : 5'd0;
    assign rvfi_rd_wdata  = r_rde ? r_rd : 32'd0;
    assign rvfi_pc_rdata  = r_pc;
    assign rvfi_pc_wdata  = trap ? npc_t : r_npc;
    assign rvfi_mem_addr  = r_mem ? maddr : 32'd0;
    assign rvfi_mem_rmask = (r_mem & c_ld) ? r_mask : 4'd0;
    assign rvfi_mem_wmask = (r_mem & c_st) ? r_mask : 4'd0;
    assign rvfi_mem_rdata = (r_mem & c_ld) ? (mrd & lmask) : 32'd0;
    assign rvfi_mem_wdata = (r_mem & c_st) ? (r_rs2 & lmask) : 32'd0;
`endif

endmodule

`default_nettype wire
