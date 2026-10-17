/*
 * BonettoR32V hypothesis 002: bit-serial RV32E with the register file in the SPI RAM.
 *
 * x1..x15 live in the 23LC512 at 0xFFC0 + 4*r (the x0 slot at 0xFFC0 is core scratch). The die
 * keeps the pc, the instruction register, ONE operand/result buffer, the SPI engine and uo_out.
 * See src/RF_IN_RAM.md for the design and the cycle counts.
 *
 * Rings. The pc (pcr) and the buffer (bfr) are 33-bit rings that rotate right on every clock. A
 * free-running phase counter ph counts 0..32; the bit at ring position i shows phase ph + i (mod
 * 33). Logical bit k of a value sits at phase 2k (mod 33): bits 0..16 at even phases, 17..31 at
 * odd phases, phase 31 unused. SPI data at SCK = clk/2 moves one bit every 2 clocks, so a data
 * phase whose first bit meets phase 0 hits bit k at phase 2k: register traffic streams straight
 * into and out of the rings with no per-bit enables. 33 is odd, so a time offset of 2s clocks is
 * a shift by s bits: shifts are a delayed emission.
 *
 * Transactions (slots). Every SPI transaction has the same frame: 8 command bits, the address
 * high byte, a one-clock gap, the low byte, then data, all at SCK = clk/2. A slot starts at a
 * slot-specific phase, so every tap below is a fixed ring position:
 *   F fetch      READ  pc[15:0] (pc ring tap mux)          -> instruction register
 *   A rs1        READ  0xFFC0 + 4*rs1                      -> serial ALU (+imm) -> buffer
 *   B rs2        READ  0xFFC0 + 4*rs2                      -> serial ALU with the buffer
 *   S spill      WRITE 0xFFC0 (pc), RAM stores only
 *   M memory     READ/WRITE address from the buffer tap mux, store data from the pc tap mux
 *   R restore    READ  0xFFC0 -> pc ring, RAM stores only
 *   W rd         WRITE 0xFFC0 + 4*rd from the buffer (forward, load-reversed, or shifted)
 *   U update     no SPI: branch pc update
 * A slot whose register is x0 (or that has no register) keeps CS_n high and runs the same timing.
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

    // Slot types. Binary codes, used bitwise below (which also keeps synthesis from
    // re-encoding the register one-hot): 0xx READ slots, A/B/U = 0 with a bit in [1:0].
    localparam [2:0] SL_F = 3'b000, SL_A = 3'b001, SL_U = 3'b010, SL_B = 3'b011,
                     SL_S = 3'b100, SL_W = 3'b101, SL_M = 3'b110, SL_R = 3'b111;
    // Engine phases: 01x0 = command/address or data (SCK runs)
    localparam [3:0] E_IDLE = 4'b0000,   // CS_n high, wait for the slot's start phase
                     E_PW   = 4'b0001,   // shift pre-roll: wait for the reference phase
                     E_PC   = 4'b0010,   // shift pre-roll: count j up to 63
                     E_RET  = 4'b0011,   // retire
                     E_CMD  = 4'b0100,   // command and address bits
                     E_DATA = 4'b0101,   // data bits
                     E_GAP  = 4'b0110,   // one clock between the address bytes
                     E_END  = 4'b0111,   // SCK low, then CS_n high
                     E_CLR  = 4'b1000,   // after reset: pc <- 0
                     E_HALT = 4'b1001;

    reg  [2:0]  sl;
    reg  [3:0]  ep;
    reg  [5:0]  ph;     // 0..32
    reg  [5:0]  j;      // bit counter (40..63 command/address, 0.. data), shift pre-roll
    reg         cs_n, sck, mosi;
    reg         mem;    // load/store goes to RAM (address bit 31 = 0)
    reg         cy;     // ALU carry; sign latch in W
    reg         cmp;    // compare; address[1:0] == 0; JALR target[1]; shift fill state
    reg         c4;     // pc + 4 carry
    reg  [32:0] pcr;    // pc ring
    reg  [32:0] bfr;    // operand/result buffer ring
    reg  [31:0] irs;    // instruction register, in SPI order
    reg  [7:0]  gpo;

    assign uo_out  = gpo;
    assign uio_out = {4'b0000, sck & ~cs_n, 1'b0, mosi, cs_n};
    assign uio_oe  = 8'b0000_1011;

    wire _unused = &{ena, uio_in[7:3], uio_in[1:0], 1'b0};
    wire miso_m  = uio_in[2] & ~cs_n;   // 0 in a masked (x0) slot

    // ------------------------------------------------------------------------------------
    // Decode (strict legality, as hypothesis 001)
    // ------------------------------------------------------------------------------------
    wire [31:0] insn  = {irs[7:0], irs[15:8], irs[23:16], irs[31:24]};
    wire [2:0]  f3    = insn[14:12];
    wire [3:0]  rd_f  = insn[10:7];
    wire [3:0]  rs1_f = insn[18:15];
    wire [3:0]  rs2_f = insn[23:20];

    wire [6:0] opcode   = insn[6:0];
    wire [6:0] funct7   = insn[31:25];
    wire is_lui    = (opcode == 7'b0110111);
    wire is_auipc  = (opcode == 7'b0010111);
    wire is_jal    = (opcode == 7'b1101111);
    wire is_jalr   = (opcode == 7'b1100111);
    wire is_branch = (opcode == 7'b1100011);
    wire is_load   = (opcode == 7'b0000011);
    wire is_store  = (opcode == 7'b0100011);
    wire is_opimm  = (opcode == 7'b0010011);
    wire is_op     = (opcode == 7'b0110011);
    wire is_fence  = (opcode == 7'b0001111);
    wire f7_zero   = (funct7 == 7'b0000000);
    wire f7_alt    = (funct7 == 7'b0100000);
    wire ok_jalr   = is_jalr & (f3 == 3'b000);
    wire ok_branch = is_branch & (f3[2:1] != 2'b01);
    wire ok_load   = is_load & (f3 != 3'b011) & (f3[2:1] != 2'b11);
    wire ok_store  = is_store & ~f3[2] & (f3[1:0] != 2'b11);
    wire ok_opimm  = is_opimm & ((f3 == 3'b001) ? f7_zero :
                                 (f3 == 3'b101) ? (f7_zero | f7_alt) : 1'b1);
    wire ok_op     = is_op & (f7_zero | (f7_alt & ((f3 == 3'b000) | (f3 == 3'b101))));
    wire ok_fence  = is_fence & (f3 == 3'b000);
    wire uses_rd   = is_lui | is_auipc | is_jal | is_jalr | is_load | is_opimm | is_op;
    wire uses_rs1  = is_jalr | is_branch | is_load | is_store | is_opimm | is_op;
    wire uses_rs2  = is_branch | is_store | is_op;
    wire regs_ok   = ~(uses_rd & insn[11]) & ~(uses_rs1 & insn[19]) & ~(uses_rs2 & insn[24]);
    wire legal     = regs_ok & (is_lui | is_auipc | is_jal | ok_jalr | ok_branch | ok_load |
                                ok_store | ok_opimm | ok_op | ok_fence);
    wire trap_dec  = ~legal | (is_jal & insn[21]);

    // Datapath classes (legal instructions only)
    wire c_br    = insn[6] & ~insn[2];
    wire c_jalr  = insn[6] & insn[2] & ~insn[3];
    wire c_jal   = insn[6] & insn[3];
    wire c_mem   = ~insn[6] & ~insn[4] & ~insn[2];
    wire c_fence = ~insn[6] & ~insn[4] & insn[2];
    wire c_st    = c_mem & insn[5];
    wire c_ld    = c_mem & ~insn[5];
    wire c_u     = ~insn[6] & insn[4] & insn[2];
    wire c_lui   = c_u & insn[5];
    wire c_alu   = ~insn[6] & insn[4] & ~insn[2];
    wire c_op    = c_alu & insn[5];
    wire c_sh    = c_alu & (f3[1:0] == 2'b01);
    wire c_slt   = c_alu & (f3[2:1] == 2'b01);
    wire sh_left = ~f3[2];
    wire [1:0] nm1 = {f3[1], f3[1] | f3[0]};   // access bytes - 1

    wire sA = (sl == SL_A);
    wire sB = (sl == SL_B);
    wire sU = (sl == SL_U);
    wire sM = (sl == SL_M);
    wire sW = (sl == SL_W);
    wire sS = (sl == SL_S);
    wire shB = sB & c_sh;
    wire s_abu = ~sl[2] & (sl[1] | sl[0]);     // A, B or U
    wire e_cd  = ~ep[3] & ep[2] & ~ep[1];      // CMD or DATA: SCK runs

    // ------------------------------------------------------------------------------------
    // Bit index of the current strobe in the aligned slots (A, B, U): bit k meets phase 2k.
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
    // Serial ALU (strobe = SCK-high clock of a data bit) and pc + 4
    // ------------------------------------------------------------------------------------
    wire stb = (ep == E_DATA) & sck;
    wire bimm_a = ~(c_op | c_br | c_sh);
    wire a_bit = sA ? ((c_u & ~c_lui) | c_jal ? pcr[0] : c_lui ? 1'b0 : miso_m) :
                 sB ? bfr[0] : pcr[0];
    wire b_raw = sA ? (bimm_a & imm_bit) :
                 sB ? ((c_sh & ~c_op) ? imm_bit : miso_m) : imm_bit;
    wire sub   = sA ? c_slt : sB ? ((c_op & insn[30] & (f3 == 3'b000)) | c_slt | c_br) : 1'b0;
    wire b_x   = b_raw ^ sub;
    wire cin   = k0 ? sub : cy;
    wire sum   = a_bit ^ b_x ^ cin;
    wire cout  = (a_bit & b_x) | (cin & (a_bit ^ b_x));
    wire boolr = ((a_bit ^ b_raw) & ~f3[0]) | (f3[1] & a_bit & b_raw);
    wire sgnd  = c_br ? ~f3[1] : ~f3[0];
    wire lt    = (a_bit & sgnd) ^ ~(b_raw & sgnd) ^ cout;
    wire eqacc = ~sum & (cmp | k0);
    wire ocmp  = (c_br & ~f3[2]) ? eqacc : lt;
    wire inc   = pcr[0] ^ (c4 & ~k0);
    wire taken = cmp ^ f3[0];
    wire trap  = trap_dec | (c_br & taken & insn[8]) | (c_jalr & cmp);

    // ------------------------------------------------------------------------------------
    // Ring tap muxes for MSB-first emission. Select a = j[2:0]: address bit 15 - a / 7 - a,
    // store data bit 7 - a of each byte, load data bit a of each byte (reversed), or 0 for
    // a forward emission.
    // ------------------------------------------------------------------------------------
    wire       fwd  = sS | (sW & ~c_ld);
    wire [2:0] msel = j[2:0] & {3{~fwd}};
    reg pcm, bfm;
    always @(*) begin
        case (msel)
            3'd0: pcm = pcr[30];
            3'd1: pcm = pcr[26];
            3'd2: pcm = pcr[22];
            3'd3: pcm = pcr[18];
            3'd4: pcm = pcr[14];
            3'd5: pcm = pcr[10];
            3'd6: pcm = pcr[6];
            default: pcm = pcr[2];
        endcase
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
    end

    // ------------------------------------------------------------------------------------
    // W data: bit k = j[4:0] at the emission strobe (the SCK-high clock before bit k)
    // ------------------------------------------------------------------------------------
    wire wem    = sW & sck & ~j[5] & e_cd;
    wire sh_evt = (ph == {3'b010, sh_left, ~sh_left, 1'b0});  // 18: source 32 passes; 20: source 0
    wire shfill = sh_left ? ~(cmp | sh_evt) : (cmp | sh_evt);
    wire ldfill = (j[4:3] > nm1);
    wire io_w   = (j[4:3] == 2'b00) ? (cmp & ui_in[j[2:0]]) : ((f3 == 3'b000) & cmp & ui_in[7]);
    wire wfill  = c_sh ? shfill : ldfill;
    wire wd     = c_ld  ? (mem ? (ldfill ? (~f3[2] & cy) : bfm) : io_w) :
                  c_slt ? ((j[4:0] == 5'd0) & cmp) :
                  c_sh  ? (shfill ? (insn[30] & cy) : bfm) :
                          bfm;

    // ------------------------------------------------------------------------------------
    // MOSI
    // ------------------------------------------------------------------------------------
    wire       wr    = sS | sW | (sM & c_st);
    wire [3:0] rsel  = sA ? rs1_f : sB ? rs2_f : sW ? rd_f : 4'd0;
    reg        ra_bit;
    always @(*) begin
        case (j[2:0])
            3'd2: ra_bit = rsel[3];
            3'd3: ra_bit = rsel[2];
            3'd4: ra_bit = rsel[1];
            3'd5: ra_bit = rsel[0];
            3'd6, 3'd7: ra_bit = 1'b0;
            default: ra_bit = 1'b1;
        endcase
    end
    wire cmdbit  = (j[2:1] == 2'b11) & ~(j[0] & wr);
    wire mosi_ca = (j[4:3] == 2'b01) ? cmdbit :
                   (sl == SL_F) ? pcm : sM ? bfm :
                   (~j[3] | ra_bit);
    wire mosi_d  = (sS | sM) ? pcm : wd;
    wire mosi_nx = j[5] ? mosi_ca : mosi_d;

    // ------------------------------------------------------------------------------------
    // Slot control
    // ------------------------------------------------------------------------------------
    reg [5:0] tgt;      // the slot starts (CS_n falls) after the IDLE clock at this phase
    always @(*) begin
        case (sl)
            SL_F:    tgt = 6'd17;
            SL_S:    tgt = 6'd20;
            SL_M:    tgt = 6'd1;
            SL_W:    tgt = 6'd4;
            default: tgt = 6'd15;
        endcase
    end
    wire mask = (sA & (~uses_rs1 | (rs1_f == 4'd0))) |
                (sB & (~(c_op | c_br | c_st) | (rs2_f == 4'd0))) |
                (sW & (rd_f == 4'd0)) | sU;
    wire dend = shB ? (ph == 6'd29) :
                sM  ? (j == {nm1 == 2'b11, nm1 == 2'b01, nm1 == 2'b00, 3'b000}) :
                      (j == 6'd32);

    // next slot after END
    reg [2:0] nsl;
    reg       nret, npre;
    always @(*) begin
        nsl  = SL_W;
        nret = 1'b0;
        npre = 1'b0;
        case (sl)
            SL_F: begin
                nsl  = SL_A;
                nret = trap_dec;
            end
            SL_A: begin
                if (c_fence | (c_jalr & cmp))
                    nret = 1'b1;
                else if (c_mem)
                    nsl = mem ? (c_st ? SL_S : SL_M) : (c_st ? SL_B : SL_W);
                else if (c_op | c_br | c_sh | c_jalr)
                    nsl = SL_B;
            end
            SL_B: begin
                if (c_sh)
                    npre = 1'b1;
                else if (c_br) begin
                    nsl  = SL_U;
                    nret = taken & insn[8];
                end else if (c_st) begin
                    nsl  = SL_M;
                    nret = ~mem;
                end
            end
            SL_S: nsl = SL_B;
            SL_M: nsl = c_ld ? SL_W : SL_R;
            default: nret = 1'b1;     // R, W, U
        endcase
    end

    // ------------------------------------------------------------------------------------
    // Rings (no reset: nothing reads them before they are written)
    // ------------------------------------------------------------------------------------
    reg bfr_we, bfr_w, pcr_we, pcr_w;
    always @(*) begin
        bfr_we = 1'b0;
        bfr_w  = sum;
        pcr_we = 1'b0;
        pcr_w  = inc;
        if (ep == E_CLR) begin
            pcr_we = 1'b1;
            pcr_w  = 1'b0;
        end else if (stb) begin
            case (sl)
                SL_A: begin
                    bfr_we = 1'b1;
                    bfr_w  = c_jal ? inc : (c_alu & ~insn[5] & f3[2] & ~c_sh) ? boolr : sum;
                    pcr_we = ~(c_jalr | c_br);
                    pcr_w  = c_jal ? sum : inc;
                end
                SL_B: begin
                    bfr_we = ~c_sh & ~(c_st & mem);
                    bfr_w  = c_jalr ? inc : c_st ? miso_m : (f3 == 3'b000) ? sum : boolr;
                    pcr_we = c_jalr | (c_st & mem);
                    pcr_w  = c_jalr ? (bfr[0] & ~k0) : miso_m;
                end
                SL_M: begin
                    bfr_we = c_ld;
                    bfr_w  = miso_m;
                end
                SL_R: begin
                    pcr_we = 1'b1;
                    pcr_w  = miso_m;
                end
                SL_U: begin
                    pcr_we = 1'b1;
                    pcr_w  = taken ? sum : inc;
                end
                default: ;
            endcase
        end
    end

    always @(posedge clk) begin
        pcr <= {pcr_we ? pcr_w : pcr[0], pcr[32:1]};
        bfr <= {bfr_we ? bfr_w : bfr[0], bfr[32:1]};
        if ((sl == SL_F) & stb)
            irs <= {irs[30:0], uio_in[2]};
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

        if (stb & s_abu)
            c4 <= (ph == 6'd2) | (c4 & pcr[0]);

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
            sl   <= SL_F;
            ep   <= E_CLR;
            ph   <= 6'd0;
            cs_n <= 1'b1;
            sck  <= 1'b0;
            mem  <= 1'b0;
            gpo  <= 8'd0;
        end else begin
            ph  <= (ph == 6'd32) ? 6'd0 : ph + 6'd1;
            sck <= e_cd & ~sck;
            if (stb & sA & c_mem & (ph == 6'd29))
                mem <= ~sum;                                // address bit 31 = 0: RAM
            case (ep)
                E_IDLE: if (ph == tgt) begin
                    cs_n <= mask;
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
                    cs_n <= 1'b1;
                    sl   <= nsl;
                    ep   <= npre ? E_PW : nret ? E_RET : E_IDLE;
                end
                E_PW: if (ph == {3'b000, sh_left, 2'b10}) ep <= E_PC;
                E_PC: if (j == 6'd63) begin
                    cs_n <= (rd_f == 4'd0);
                    ep   <= E_CMD;
                end
                E_RET: begin
                    if (c_st & ~mem & cmp & ~trap)
                        gpo <= {bfr[16], bfr[14], bfr[12], bfr[10], bfr[8], bfr[6], bfr[4], bfr[2]};
                    mem <= 1'b0;
                    sl  <= SL_F;
                    ep  <= trap ? E_HALT : E_IDLE;
                end
                E_CLR: if (ph == 6'd32) ep <= E_IDLE;
                E_HALT: ;
                default: ep <= E_HALT;
            endcase
        end
    end

    // ------------------------------------------------------------------------------------
    // RVFI (formal and cosim builds only; observes, never drives the pins)
    // ------------------------------------------------------------------------------------
`ifdef RISCV_FORMAL
    genvar gi;
    // logical bit m of a ring is at phase 2m mod 33, which sits at position (2m - ph) mod 33
    wire [65:0] pcd   = {pcr, pcr};
    wire [65:0] pcsh  = pcd >> (6'd33 - ph);
    wire [32:0] pcrot = pcsh[32:0];
    wire [31:0] pcv;
    generate
        for (gi = 0; gi < 32; gi = gi + 1) begin : g_pcv
            assign pcv[gi] = pcrot[(2 * gi) % 33];
        end
    endgenerate

    reg [63:0] r_order;
    reg [31:0] r_rs1, r_rs2, r_rd, r_pc, r_mrd;
    integer    mi;
    wire [5:0] dj = j - 6'd1;
    always @(posedge clk) begin
        if (!rst_n)
            r_order <= 64'd0;
        else if (ep == E_RET)
            r_order <= r_order + 64'd1;
        if ((sl == SL_F) & (ep == E_END))
            r_pc <= pcv;
        if (stb & sA)
            r_rs1 <= {miso_m, r_rs1[31:1]};
        if (stb & sB)
            r_rs2 <= {miso_m, r_rs2[31:1]};
        if (wem)
            r_rd <= {wd, r_rd[31:1]};
        for (mi = 0; mi < 32; mi = mi + 1)
            if (stb & sM & c_ld & ({dj[4:3], ~dj[2:0]} == mi[4:0]))
                r_mrd[mi] <= uio_in[2];
    end

    wire [31:0] imm_i = {{20{insn[31]}}, insn[31:20]};
    wire [31:0] imm_s = {{20{insn[31]}}, insn[31:25], insn[11:7]};
    wire [31:0] imm_b = {{20{insn[31]}}, insn[7], insn[30:25], insn[11:8], 1'b0};
    wire [31:0] imm_j = {{12{insn[31]}}, insn[19:12], insn[20], insn[30:21], 1'b0};
    wire [31:0] jalr_t = (r_rs1 + imm_i) & 32'hFFFF_FFFE;
    wire [31:0] npc_t  = is_jal ? r_pc + imm_j : is_jalr ? jalr_t :
                         (is_branch & taken) ? r_pc + imm_b : r_pc + 32'd4;

    wire        r_rs1e = legal & uses_rs1;
    wire        r_rs2e = legal & uses_rs2;
    wire        r_rde  = legal & uses_rd & (rd_f != 4'd0) & ~trap;
    wire        r_mem  = ~trap & (is_load | is_store);
    wire [31:0] maddr  = r_rs1 + (is_store ? imm_s : imm_i);
    wire [3:0]  r_mask = (f3[1:0] == 2'b00) ? 4'b0001 : (f3[1:0] == 2'b01) ? 4'b0011 : 4'b1111;
    wire [31:0] lmask  = {{8{r_mask[3]}}, {8{r_mask[2]}}, {8{r_mask[1]}}, {8{r_mask[0]}}};
    wire [31:0] mrd    = maddr[31] ? {24'd0, cmp ? ui_in : 8'd0} : r_mrd;

    assign rvfi_valid     = (ep == E_RET);
    assign rvfi_order     = r_order;
    assign rvfi_insn      = insn;
    assign rvfi_trap      = trap;
    assign rvfi_halt      = trap;
    assign rvfi_intr      = 1'b0;
    assign rvfi_mode      = 2'd3;
    assign rvfi_ixl       = 2'd1;
    assign rvfi_rs1_addr  = r_rs1e ? insn[19:15] : 5'd0;
    assign rvfi_rs2_addr  = r_rs2e ? insn[24:20] : 5'd0;
    assign rvfi_rs1_rdata = r_rs1e ? r_rs1 : 32'd0;
    assign rvfi_rs2_rdata = r_rs2e ? r_rs2 : 32'd0;
    assign rvfi_rd_addr   = r_rde ? insn[11:7] : 5'd0;
    assign rvfi_rd_wdata  = r_rde ? r_rd : 32'd0;
    assign rvfi_pc_rdata  = r_pc;
    assign rvfi_pc_wdata  = trap ? npc_t : pcv;
    assign rvfi_mem_addr  = r_mem ? maddr : 32'd0;
    assign rvfi_mem_rmask = (r_mem & is_load) ? r_mask : 4'd0;
    assign rvfi_mem_wmask = (r_mem & is_store) ? r_mask : 4'd0;
    assign rvfi_mem_rdata = (r_mem & is_load) ? (mrd & lmask) : 32'd0;
    assign rvfi_mem_wdata = (r_mem & is_store) ? (r_rs2 & lmask) : 32'd0;
`endif

endmodule

`default_nettype wire
