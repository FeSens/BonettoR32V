/*
 * BonettoR32V MCU seed (contract v3, docs/contract-v3.md): the hypothesis-005 bit-serial RV32E
 * core with x1..x15, the pc, mtvec, mscratch and mepc in the PSRAM core region, on TT's QSPI Pmod
 * (code runs from flash, data in PSRAM), with a minimal Zicsr, traps and one interrupt to mtvec,
 * and the contract-5 peripherals.
 *
 * Modules: r32v_mem (reset sequence, the 24-bit address register, pins), r32v_csr (CSR state and
 * the trap/interrupt flags), r32v_periph (IO slots, the interrupt line). This file: the slot
 * engine, the serial datapath, decode and sequencing.
 *
 * Ring. The buffer (bfr) is a 33-bit ring that rotates right on every clock; a free-running
 * phase counter ph counts 0..32, and the bit at ring position i shows phase ph + i (mod 33).
 * Every slot starts its CMD state at phase 2, so data bit k of any slot is strobed at phase
 * 2k + 1, and a value in the ring keeps bit k at phase 2k + 1 (bits 0..15 on the odd phases,
 * 16..31 on the even ones, phase 32 unused). Register traffic streams straight into the ring
 * input and out of a fixed tap; 33 is odd, so a time offset of 2s clocks is a shift by s bits.
 *
 * Transactions (slots). Every transaction: CS_n low, 8 command bits (0x03 READ / 0x02 WRITE),
 * 24 address bits, data, all single SPI at SCK = clk/2; 132 clocks (4 laps) for 32 data bits,
 * 99 for 8 or 16. Program transactions (F, M) take their address from r32v_mem's register and
 * their chip from address bit 24; core-region transactions go to the PSRAM at
 * 0x7F_FF80 | {bit 6, word select, 00}. A slot whose register is x0 (or that has none) keeps
 * CS_n high and runs the same timing.
 *   P   READ  x0 (the pc)                     -> ring, address register
 *   A   READ  rs1 -> serial ALU (+imm; AUIPC/JAL: ring + imm; CSR: rs1 or zimm) -> ring,
 *             address register (loads/stores); an IO load captures the slot into the ring at
 *             A's END
 *   PX  READ  x0 -> ring + (taken ? imm : 4) (branch); READ scratch -> ring (JALR)
 *   B   READ  rs2 -> serial ALU with the ring (OP, branches, shift amount)
 *   BS  READ  rs2 -> ring (store data); an IO store writes the slot at BS's END
 *   C   READ  a CSR: core-region word (masked bits read 0) or the die bits -> ring
 *   RS  READ  scratch, combined with the ring (CSRRW / CSRRS / CSRRC) -> ring
 *   F   READ  pc[23:0] -> instruction register
 *   M   READ/WRITE the load/store address; data into the ring / out of the ring's byte taps
 *   WP  WRITE x0 <- ring;  WP4: x0 <- ring + 4;  W4: rd (JAL) or scratch (JALR) <- ring + 4
 *   W   WRITE rd <- ring (forward, load bytes reversed and extended, shifted, SLT)
 *   WS  WRITE scratch <- ring;  WC: the CSR <- ring
 * Sequences: ALU P F WP4 A [B] W; load P F WP4 A M W (IO: A W); store P F WP4 A BS M (IO: A BS);
 * branch P F A B PX WP; JAL P F W4 A WP; JALR P F W4 A WP [PX W]; FENCE, WFI P F WP4;
 * CSR P F WP4 A WS C W RS WC; MRET P F C WP. A synchronous trap (decode after F, JALR after A,
 * branch after B) runs [P] WC(mepc <- pc) C(mtvec) WP and retires; an interrupt (at a
 * retirement) runs P WC C WP and continues with the handler's P. Until the first retirement P
 * (and a branch's PX) read pc = 0 without a transaction.
 *
 * Copyright 2026 Felipe Sens Bonetto
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tt_um_fesens_r32v (
    input  wire [7:0]  ui_in,
    output wire [7:0]  uo_out,
    input  wire [7:0]  uio_in,
    output wire [7:0]  uio_out,
    output wire [7:0]  uio_oe,
    input  wire        ena,
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

    // Slot types: sl[4:3] = 00 READ into the ring, 01 program address, 1x WRITE from the ring
    localparam [4:0] SL_P  = 5'b00000, SL_A  = 5'b00001, SL_PX = 5'b00010, SL_B  = 5'b00011,
                     SL_BS = 5'b00100, SL_C  = 5'b00101, SL_RS = 5'b00110,
                     SL_F  = 5'b01000, SL_M  = 5'b01001,
                     SL_WP = 5'b10000, SL_WP4 = 5'b10001, SL_W4 = 5'b10010, SL_W = 5'b10011,
                     SL_WS = 5'b10100, SL_WC = 5'b10101;
    // Engine phases: 1x0 = command/address or data (SCK runs), ep[2] = CS_n low
    localparam [2:0] E_IDLE = 3'b000,    // CS_n high, wait for phase 1
                     E_PW   = 3'b001,    // shift pre-roll: wait for the reference phase
                     E_PC   = 3'b010,    // shift pre-roll: count j up to 63
                     E_CMD  = 3'b100,    // command and address bits
                     E_DATA = 3'b101,    // data bits
                     E_END  = 3'b111;    // SCK low, then CS_n high

    reg  [4:0]  sl;
    reg  [2:0]  ep;
    reg  [5:0]  ph;     // 0..32
    reg  [5:0]  j;      // bit counter (32..63 command/address, 0.. data), shift pre-roll
    reg         sck, mosi;
    reg         first;  // until the first retirement: P (and a branch's PX) read pc = 0
    reg         cy;     // ALU carry; sign latch in W; carry of the + 4 (WP4, W4)
    reg         cmp;    // compare; JALR target[1] == 0; shift fill state
    reg  [32:0] bfr;    // operand/result ring
    reg  [30:0] irs;    // instruction register, in SPI order, without insn[1]

    wire _unused = &{ena, uio_in[7:3], uio_in[1:0], 1'b0};

    // ------------------------------------------------------------------------------------
    // Decode, spec-minimum (contract 4). Traps: insn[0] = 0 (the all-zeros word) and
    // opcode[6] & [4] & [2] (the all-ones word) are illegal (2), ECALL (11), EBREAK (3), a
    // misaligned JAL (0). Every other encoding executes as the class its opcode bits select;
    // register fields use 4 bits; insn[1] is not stored.
    // ------------------------------------------------------------------------------------
    wire [31:0] insn  = {irs[7:0], irs[15:8], irs[23:16], irs[30:25], 1'b1, irs[24]};
    wire        _unused_insn1 = insn[1];
    wire [2:0]  f3    = insn[14:12];
    wire [3:0]  rd_f  = insn[10:7];
    wire [3:0]  rs1_f = insn[18:15];
    wire [3:0]  rs2_f = insn[23:20];

    wire c_br    = insn[6] & ~insn[4] & ~insn[2];
    wire c_sys   = insn[6] &  insn[4] & ~insn[2];
    wire c_jalr  = insn[6] &  insn[2] & ~insn[3];
    wire c_jal   = insn[6] &  insn[2] &  insn[3];
    wire c_mem   = ~insn[6] & ~insn[4] & ~insn[2];
    wire c_fence = ~insn[6] & ~insn[4] & insn[2];
    wire c_st    = c_mem & insn[5];
    wire c_ld    = c_mem & ~insn[5];
    wire c_u     = ~insn[6] & insn[4] & insn[2];
    wire c_pcrel = (c_u & ~insn[5]) | c_jal;            // AUIPC, JAL: ring (pc) + imm
    wire c_alu   = ~insn[6] & insn[4] & ~insn[2];
    wire c_op    = c_alu & insn[5];
    wire c_sh    = c_alu & (f3[1:0] == 2'b01);
    wire c_slt   = c_alu & (f3[2:1] == 2'b01);
    wire sh_left = ~f3[2];
    wire [1:0] nm1 = {f3[1], f3[1] | f3[0]};            // access bytes - 1
    wire priv    = c_sys & (f3[1:0] == 2'b00);          // ECALL, EBREAK, MRET, WFI
    wire c_csr   = c_sys & (f3[1:0] != 2'b00);
    wire p_mret  = priv & insn[21];
    wire p_trap  = priv & ~insn[21] & ~insn[22];        // ECALL (insn[20] = 0), EBREAK
    wire trap_ill = ~insn[0] | (insn[6] & insn[4] & insn[2]);
    wire trap_dec = trap_ill | (c_jal & insn[21]) | p_trap;
    wire [3:0] cause = {p_trap & ~trap_ill & ~insn[20], 1'b0, trap_ill | p_trap, p_trap & ~trap_ill};

    wire fR   = ~sl[4] & ~sl[3];
    wire fM   = ~sl[4] &  sl[3];
    wire fW   =  sl[4];
    wire sP   = (sl == SL_P);
    wire sA   = (sl == SL_A);
    wire sPX  = (sl == SL_PX);
    wire sB   = (sl == SL_B);
    wire sBS  = (sl == SL_BS);
    wire sC   = (sl == SL_C);
    wire sRS  = (sl == SL_RS);
    wire sF   = (sl == SL_F);
    wire sM   = (sl == SL_M);
    wire sWP  = (sl == SL_WP);
    wire sWP4 = (sl == SL_WP4);
    wire sW4  = (sl == SL_W4);
    wire sW   = (sl == SL_W);
    wire sWS  = (sl == SL_WS);
    wire sWC  = (sl == SL_WC);
    wire shB  = sB & c_sh;
    wire e_cd = ep[2] & ~ep[1];                    // CMD or DATA: SCK runs
    wire e_end = (ep == E_END);

    // submodule wires
    wire        tm, intr, irq, booting;
    wire        csr_region, mask_lo2, rd_die;
    wire [1:0]  csr_idx;
    wire        io, mar_bit;
    wire [6:0]  mar_lo;
    wire [15:0] io_rdata;
    wire        active;
    wire        miso_m;

    // ------------------------------------------------------------------------------------
    // Bit index of the current data strobe: bit k meets phase 2k + 1 in every slot.
    // ------------------------------------------------------------------------------------
    wire [4:0] kb = {~ph[0], ph[4:1]};
    wire       k0 = (ph[4:0] == 5'd1);                // ph <= 32: ph[5] is 1 only at 32

    // Shared immediate selectors: insn[20 + i] (i = k for k < 12, k - 20 for the U-type k >= 20),
    // insn[7 + k] (k < 5), insn[k] (k = 12..19) and zimm insn[15 + k] (k < 5).
    wire [3:0]  ki   = kb[3:0] - {1'b0, kb[4], 2'b00};
    wire [15:0] va   = {4'd0, insn[31:20]};
    wire [7:0]  vb   = {3'd0, insn[11:7]};
    wire [7:0]  vm   = {insn[15:12], insn[19:16]};
    wire [7:0]  vz   = {3'd0, insn[19:15]};
    wire        m_a  = va[ki];
    wire        m_b  = vb[kb[2:0]];
    wire        m_m  = vm[kb[2:0]];
    wire        m_z  = vz[kb[2:0]];
    wire        k_hi = kb[4] | (kb[3] & kb[2]);                       // k >= 12
    wire        k_20 = kb[4] & (kb[3] | kb[2]);                       // k >= 20
    wire        k11  = (kb == 5'd11);
    wire        k_lo5 = ~kb[4] & ~kb[3] & ~(kb[2] & (kb[1] | kb[0]));  // k < 5
    wire        sign = insn[31];
    wire        imm_bit =
        c_u   ? (k_20 ? m_a : (k_hi & m_m)) :
        c_jal ? (k_20 ? sign : k_hi ? m_m : (k0 ? 1'b0 : k11 ? insn[20] : m_a)) :
        c_sys ? (f3[2] & k_lo5 & m_z) :
        k_hi  ? sign :
        c_br  ? (k0 ? 1'b0 : k11 ? insn[7] : k_lo5 ? m_b : m_a) :
        c_st  ? (k_lo5 ? m_b : m_a) :
                m_a;

    // ------------------------------------------------------------------------------------
    // Serial ALU (strobe = SCK-high clock of a data bit)
    // ------------------------------------------------------------------------------------
    wire stb    = ep[0] & sck;                       // SCK is high only in CMD and DATA
    wire taken  = cmp ^ f3[0];
    wire bimm_a = ~(c_op | c_br | c_sh);
    wire a_bit  = (sB | (sA & c_pcrel)) ? bfr[0] : miso_m;
    wire b_raw  = sA ? (bimm_a & imm_bit) :
                  sB ? ((c_sh & ~c_op) ? imm_bit : miso_m) :
                  (sPX & c_br & (taken ? imm_bit : (ph[4:0] == 5'd5)));
    // B holds only OP, shifts (sub unused) and branches: SUB, SLT, SLTU and every branch subtract
    wire sub    = sA ? c_slt : (sB & (c_br | (~f3[2] & (f3[1] | insn[30]))));
    wire b_x    = b_raw ^ sub;
    wire cin    = k0 ? sub : cy;
    wire pp     = a_bit ^ b_x;
    wire gg     = a_bit & b_x;
    wire sum    = pp ^ cin;
    wire cout   = gg | (cin & pp);
    wire boolr  = (pp & ~f3[0]) | (f3[1] & gg);
    wire sgnd   = c_br ? ~f3[1] : ~f3[0];
    wire lt     = cout ^ (pp | ~sgnd);
    wire sumj   = sum & ~(k0 & c_jalr);              // JALR clears target bit 0
    wire eqacc  = ~sumj & (cmp | k0);
    wire ocmp   = (c_br & ~f3[2]) ? eqacc : lt;
    // logic ops: XORI, ORI, ANDI in A; XOR, OR, AND in B (a B result with f3 = 001..011 is unused)
    wire useb   = (sA & c_alu & ~insn[5] & f3[2] & ~c_sh) | (sB & f3[2]);
    wire alu_w  = useb ? boolr : sumj;

    // C: a core-region CSR word (bits 1:0 of mtvec and mepc read 0) or the die bits;
    // RS: CSRRW takes the scratch word (rs1 or zimm), CSRRS ors it in, CSRRC clears its bits
    wire c_bit  = csr_region ? (miso_m & ~mask_lo2) : rd_die;
    wire rs_val = f3[1] ? (f3[0] ? (bfr[0] & ~miso_m) : (bfr[0] | miso_m)) : miso_m;
    wire din    = sC ? c_bit : sRS ? rs_val : alu_w;

    // ------------------------------------------------------------------------------------
    // Ring byte taps, select a = j[2:0]: bit a of each byte of a value read into the ring, at
    // the strobe before bit a (store data, MSB first) or before result bit a (load data,
    // reversed within each byte). Position 16 - 4a (mod 33).
    // ------------------------------------------------------------------------------------
    reg bfm;
    always @(*) begin
        case (j[2:0])
            3'd0: bfm = bfr[16];
            3'd1: bfm = bfr[12];
            3'd2: bfm = bfr[8];
            3'd3: bfm = bfr[4];
            3'd4: bfm = bfr[0];
            3'd5: bfm = bfr[29];
            3'd6: bfm = bfr[25];
            default: bfm = bfr[21];
        endcase
    end
    wire       wtap = bfr[2];   // W family forward emission: bit k at its emission clock

    // ------------------------------------------------------------------------------------
    // W family data: bit k = j[4:0] at the emission strobe (the SCK-high clock before bit k)
    // ------------------------------------------------------------------------------------
    wire wem    = fW & sck & ~j[5];
    wire sh_evt = sh_left ? ph[5] : (ph[4:0] == 5'd30);  // 30: source 32 passes; 32: source 0
    wire shfill = sh_left ? ~(cmp | sh_evt) : (cmp | sh_evt);
    // loads: IO slots have 16 bits, so an IO LW is an LHU
    wire [1:0] nm1_l = io ? {1'b0, nm1[0]} : nm1;
    wire ldfill = (j[4:3] > nm1_l);
    wire fillv  = ~f3[2] & cy & ~(io & f3[1]);
    wire wfill  = c_sh ? shfill : ldfill;
    wire wd     = c_ld  ? (ldfill ? fillv : bfm) :
                  c_slt ? ((j[4:0] == 5'd0) & cmp) :
                  c_sh  ? (shfill ? (insn[30] & cy) : wtap) :
                          wtap;
    wire winc   = sWP4 | sW4;
    wire wpl    = wtap ^ (cy & winc);
    wire wdo    = sW ? wd : wpl;

    // ------------------------------------------------------------------------------------
    // MOSI: command, address (program: the address register; core region: 0xFFFF80 |
    // {bit 6, word, 00}), data
    // ------------------------------------------------------------------------------------
    wire       wr      = fW | (sM & c_st);
    wire       csr_sl  = sC | sWC;
    wire       scr     = (sPX & ~c_br) | (sW4 & ~c_jal) | sWS | sRS;   // the scratch word 0x80
    wire       regarea = ~scr & ~csr_sl;                                // x0..x15 at 0xC0 + 4r
    wire [3:0] rsel    = sA ? rs1_f : (sB | sBS) ? rs2_f : (sW | (sW4 & c_jal)) ? rd_f :
                         csr_sl ? {2'b01, csr_idx} : 4'd0;
    reg        ra_bit;
    always @(*) begin
        case (j[2:0])
            3'd0: ra_bit = 1'b1;
            3'd1: ra_bit = regarea;
            3'd2: ra_bit = rsel[3];
            3'd3: ra_bit = rsel[2];
            3'd4: ra_bit = rsel[1];
            3'd5: ra_bit = rsel[0];
            default: ra_bit = 1'b0;
        endcase
    end
    wire cmdbit  = (j[2:1] == 2'b11) & ~(j[0] & wr);
    wire mosi_ca = (j[4:3] == 2'b00) ? cmdbit : fM ? mar_bit : ((j[4:3] != 2'b11) | ra_bit);
    wire mosi_d  = fM ? bfm : wdo;
    wire mosi_nx = j[5] ? mosi_ca : mosi_d;

    // ------------------------------------------------------------------------------------
    // Slot control
    // ------------------------------------------------------------------------------------
    wire mask = (sA & (c_u | c_jal | (rs1_f == 4'd0) | (c_csr & f3[2]))) |
                (sB & ~(c_op | c_br)) |
                ((sB | sBS) & (rs2_f == 4'd0)) |
                ((sW | sW4) & (rd_f == 4'd0)) |
                (first & (sP | (sPX & c_br))) |
                (csr_sl & ~csr_region);
    assign active = ep[2] & ~mask;
    assign miso_m = uio_in[2] & active;               // 0 in a masked slot
    // data length: 8 bits (LB, SB), 16 (LH, SH) or 32; the data phase ends at the first strobe
    // with j >= length (j counts the data bits up from 1)
    wire len8  = sM & (nm1 == 2'b00);
    wire len16 = sM & (nm1 == 2'b01);
    wire dend  = shB ? (ph[4:0] == 5'd30) : (j[5] | (j[4] & (len16 | len8)) | (j[3] & len8));

    // next slot after END
    reg [4:0] nsl;
    reg       nret, npre, tgo;
    always @(*) begin
        nsl  = SL_W;
        nret = 1'b0;
        npre = 1'b0;
        tgo  = 1'b0;
        case (sl)
            SL_P: nsl = tm ? SL_WC : SL_F;
            SL_F: begin
                if (trap_dec) begin
                    tgo = 1'b1;
                    nsl = SL_WC;
                end else
                    nsl = c_br ? SL_A : (c_jal | c_jalr) ? SL_W4 : p_mret ? SL_C : SL_WP4;
            end
            SL_WP4: begin
                nret = c_fence | priv;                 // FENCE, WFI (MRET, ECALL, EBREAK never get here)
                nsl  = SL_A;
            end
            SL_W4: nsl = SL_A;
            SL_A: begin
                if (c_jalr & ~cmp) begin
                    tgo = 1'b1;
                    nsl = SL_P;
                end else if (c_jal | c_jalr)
                    nsl = SL_WP;
                else if (c_br | c_op | c_sh)
                    nsl = SL_B;
                else if (c_st)
                    nsl = SL_BS;
                else if (c_ld & ~io)
                    nsl = SL_M;
                else if (c_csr)
                    nsl = SL_WS;
            end
            SL_B: begin
                if (c_sh)
                    npre = 1'b1;
                else if (c_br) begin
                    if (taken & insn[8]) begin
                        tgo = 1'b1;
                        nsl = SL_P;
                    end else
                        nsl = SL_PX;
                end
            end
            SL_BS: begin
                nret = io;
                nsl  = SL_M;
            end
            SL_M: begin
                nret = c_st;
                nsl  = SL_W;
            end
            SL_PX: nsl = c_br ? SL_WP : SL_W;
            SL_WP: begin
                if (tm) begin
                    nret = ~intr;
                    nsl  = SL_P;
                end else begin
                    nret = ~(c_jalr & (rd_f != 4'd0));
                    nsl  = SL_PX;
                end
            end
            SL_WS: nsl = SL_C;
            SL_C:  nsl = (tm | p_mret) ? SL_WP : SL_W;
            SL_W: begin
                nret = ~c_csr;
                nsl  = SL_RS;
            end
            SL_RS: nsl = SL_WC;
            SL_WC: begin
                nret = ~tm;
                nsl  = SL_C;
            end
            default: nret = 1'b1;
        endcase
    end

    // ------------------------------------------------------------------------------------
    // IO: an IO load captures the slot into the ring at A's END (atomic: the read side effect
    // happens at the same edge); an IO store writes rs2[15:0] from the ring at BS's END.
    // In an END cycle (phase 31) bit k of a value read into the ring is at position 2k + 3, and
    // load data wire bit m (bytes in address order, MSB first) belongs at position 2m + 2.
    // ------------------------------------------------------------------------------------
    wire        io_re = e_end & sA & c_ld & io;
    wire        io_we = e_end & sBS & io;
    wire [1:0]  off   = mar_lo[1:0];
    wire [7:0]  iob0  = off[1] ? 8'd0 : off[0] ? io_rdata[15:8] : io_rdata[7:0];
    wire [7:0]  iob1  = (off == 2'd0) ? io_rdata[15:8] : 8'd0;
    wire [15:0] io_wdata = {bfr[0], bfr[31], bfr[29], bfr[27], bfr[25], bfr[23], bfr[21], bfr[19],
                            bfr[17], bfr[15], bfr[13], bfr[11], bfr[9], bfr[7], bfr[5], bfr[3]};

    // ------------------------------------------------------------------------------------
    // Ring (no reset: nothing reads it before it is written)
    // ------------------------------------------------------------------------------------
    wire        we32 = stb & (fR | (sM & c_ld)) & ~shB;
    wire [32:0] rot  = {we32 ? din : bfr[0], bfr[32:1]};
    reg  [32:0] ring_n;
    integer     ri;
    always @(*) begin
        ring_n = rot;
        if (io_re) begin
            for (ri = 0; ri < 8; ri = ri + 1) begin
                ring_n[2 * ri + 2]  = iob0[7 - ri];
                ring_n[2 * ri + 18] = iob1[7 - ri];
            end
        end
    end
    always @(posedge clk) begin
        bfr <= ring_n;
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
        if (~ep[2])                                     // between transactions
            cy <= 1'b0;
        else if (sW) begin
            if (wem & ~wfill)
                cy <= c_ld ? bfm : wtap;
        end else if (wem)
            cy <= (j[4:0] == 5'd1) | (cy & wtap);
        else if (stb & fR)
            cy <= cout;

        if (stb & sA) begin
            if (c_slt)
                cmp <= ocmp;
            else if ((ph[4:2] == 3'd0) & ph[0])             // bits 0 and 1
                cmp <= eqacc;                               // JALR target[1] == 0
        end else if (stb & sB)                          // SLT, branches; a shift starts its
            cmp <= ocmp & ~c_sh;                        // W fill state at 0
        else if (sW & c_sh & wem & sh_evt)
            cmp <= 1'b1;

        // bit counter
        if ((ep == E_IDLE) | ((ep == E_PC) & (j == 6'd63)))
            j <= 6'd32;
        else if (((ep == E_CMD) | ((ep == E_DATA) & ~shB)) & ~sck)
            j <= j + 6'd1;
        else if (ep == E_PC)
            j <= j + 6'd1;
        else if (stb & shB & k_lo5)                 // bits 0..4
            j <= {b_raw ^ ~sh_left, j[5:1]};

        // MOSI changes only where SCK stays low or falls
        if (e_cd & ~sck)
            mosi <= mosi;
        else if (e_cd)
            mosi <= mosi_nx;
        else
            mosi <= 1'b0;
    end

    // ------------------------------------------------------------------------------------
    // Control (reset)
    // ------------------------------------------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            sl    <= SL_P;
            ep    <= E_IDLE;
            ph    <= 6'd0;
            sck   <= 1'b0;
            first <= 1'b1;
        end else begin
            ph  <= ph[5] ? 6'd0 : ph + 6'd1;
            sck <= e_cd & ~sck;
            case (ep)
                E_IDLE: if ((ph[4:0] == 5'd1) & ~booting)
                    ep <= E_CMD;
                E_CMD: if (sck & ~j[5])                     // j wrapped to 0: data
                    ep <= E_DATA;
                E_DATA: if (sck & dend)
                    ep <= E_END;
                E_END: begin
                    sl <= nret ? SL_P : nsl;
                    ep <= npre ? E_PW : E_IDLE;
                    if (nret)
                        first <= 1'b0;
                end
                E_PW: if (sh_left ? (ph[1] & ph[0]) : ph[5]) ep <= E_PC;   // from 32: 3 (left), 32
                E_PC: if (j == 6'd63) ep <= E_CMD;
                default: ep <= E_IDLE;
            endcase
        end
    end

    // ------------------------------------------------------------------------------------
    // Submodules
    // ------------------------------------------------------------------------------------
    r32v_mem u_mem (
        .clk(clk), .rst_n(rst_n),
        .cap(stb & (sP | sA)), .cap_a(stb & sA), .kb(kb), .din(din), .j(j[4:0]),
        .mar_bit(mar_bit), .mar_lo(mar_lo), .io(io), .booting(booting),
        .active(active), .prog(fM), .sck(sck), .mosi(mosi),
        .uio_out(uio_out), .uio_oe(uio_oe)
    );

    r32v_csr u_csr (
        .clk(clk), .rst_n(rst_n),
        .csr_num(insn[31:20]), .p_mret(p_mret), .s_wc(sWC), .kb(kb),
        .region(csr_region), .idx(csr_idx), .mask_lo2(mask_lo2), .rd_die(rd_die),
        .trap_go(e_end & tgo), .cause(cause), .retire(e_end & nret),
        .wc_end(e_end & sWC & ~tm),
        .wb({bfr[32], bfr[25], bfr[17], bfr[9], bfr[7], bfr[5], bfr[3]}),
        .seq_end(e_end & sWP & tm), .irq(irq),
        .tm(tm), .intr(intr)
    );

    r32v_periph u_periph (
        .clk(clk), .rst_n(rst_n), .ui_in(ui_in), .uo_out(uo_out),
        .slot(mar_lo[6:2]), .off(off), .size(f3[1:0]), .we(io_we), .re(io_re),
        .wdata(io_wdata), .rdata(io_rdata), .irq(irq)
    );

    // ------------------------------------------------------------------------------------
    // RVFI (formal and cosim builds only; observes, never drives the pins)
    // ------------------------------------------------------------------------------------
`ifdef RISCV_FORMAL
    reg         r_i1;                                   // insn[1], not stored by the design
    reg [63:0]  r_order;
    reg         r_v, r_tr, r_intr, r_ipend;
    reg [31:0]  r_rs1, r_rs2, r_rd, r_pc, r_npc, r_mrd;
    integer     mi;
    wire [4:0]  dj = j[4:0] - 5'd1;
    always @(posedge clk) begin
        if (!rst_n) begin
            r_order <= 64'd0;
            r_v     <= 1'b0;
            r_ipend <= 1'b0;
        end else begin
            r_v  <= e_end & nret;
            r_tr <= e_end & nret & tm;
            if (r_v)
                r_order <= r_order + 64'd1;
            // a trap or interrupt sequence ends: the next retirement is the handler's first
            // instruction (rvfi_intr = 1 for both, contract 8). A synchronous trap's own
            // retirement comes at this same END and still reports the old value.
            if (e_end & sWP & tm)
                r_ipend <= 1'b1;
            else if (e_end & nret)
                r_ipend <= 1'b0;
            if (e_end & nret)
                r_intr <= r_ipend;
        end
        if (stb & sP)
            r_pc <= {miso_m, r_pc[31:1]};
        if (stb & sF & (j == 6'd7))
            r_i1 <= uio_in[2];
        if (wem & (sWP | sWP4))
            r_npc <= {wdo, r_npc[31:1]};
        if (stb & sA)
            r_rs1 <= {miso_m, r_rs1[31:1]};
        if (stb & (sB | sBS))
            r_rs2 <= {miso_m, r_rs2[31:1]};
        if (wem & (sW | (sW4 & c_jal)))
            r_rd <= {wdo, r_rd[31:1]};
        for (mi = 0; mi < 32; mi = mi + 1)
            if (stb & sM & c_ld & ({dj[4:3], ~dj[2:0]} == mi[4:0]))
                r_mrd[mi] <= uio_in[2];
        if (io_re)
            r_mrd <= {16'd0, io_rdata} >> {off, 3'b000};
    end

    wire [31:0] imm_i = {{20{insn[31]}}, insn[31:20]};
    wire [31:0] imm_s = {{20{insn[31]}}, insn[31:25], insn[11:7]};
    wire        r_rs1e = ~trap_dec & (c_alu | c_mem | c_br | c_jalr | (c_csr & ~f3[2]));
    wire        r_rs2e = ~trap_dec & (c_br | c_st | c_op);
    wire        r_rde  = ~r_tr & (c_alu | c_u | c_jal | c_jalr | c_ld | c_csr) & (rd_f != 4'd0);
    wire        r_mem  = ~r_tr & c_mem;
    wire [31:0] maddr  = r_rs1 + (c_st ? imm_s : imm_i);
    wire [3:0]  r_mask = (f3[1:0] == 2'b00) ? 4'b0001 : (f3[1:0] == 2'b01) ? 4'b0011 : 4'b1111;
    wire [31:0] lmask  = {{8{r_mask[3]}}, {8{r_mask[2]}}, {8{r_mask[1]}}, {8{r_mask[0]}}};

    assign rvfi_valid     = r_v;
    assign rvfi_order     = r_order;
    assign rvfi_insn      = {insn[31:2], r_i1, insn[0]};
    assign rvfi_trap      = r_tr;
    assign rvfi_halt      = 1'b0;
    assign rvfi_intr      = r_intr;
    assign rvfi_mode      = 2'd3;
    assign rvfi_ixl       = 2'd1;
    assign rvfi_rs1_addr  = r_rs1e ? {1'b0, rs1_f} : 5'd0;
    assign rvfi_rs2_addr  = r_rs2e ? {1'b0, rs2_f} : 5'd0;
    assign rvfi_rs1_rdata = r_rs1e ? r_rs1 : 32'd0;
    assign rvfi_rs2_rdata = r_rs2e ? r_rs2 : 32'd0;
    assign rvfi_rd_addr   = r_rde ? {1'b0, rd_f} : 5'd0;
    assign rvfi_rd_wdata  = r_rde ? r_rd : 32'd0;
    assign rvfi_pc_rdata  = r_pc;
    assign rvfi_pc_wdata  = r_npc;
    assign rvfi_mem_addr  = r_mem ? maddr : 32'd0;
    assign rvfi_mem_rmask = (r_mem & c_ld) ? r_mask : 4'd0;
    assign rvfi_mem_wmask = (r_mem & c_st) ? r_mask : 4'd0;
    assign rvfi_mem_rdata = (r_mem & c_ld) ? (r_mrd & lmask) : 32'd0;
    assign rvfi_mem_wdata = (r_mem & c_st) ? (r_rs2 & lmask) : 32'd0;
`endif

endmodule

`default_nettype wire
