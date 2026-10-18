/*
 * BonettoR32V MCU (contract v3, docs/contract.md): a nibble-serial RV32E core with x1..x15,
 * mtvec, mscratch and mepc in the PSRAM core region and the pc on the die, on TT's QSPI Pmod in
 * quad mode (code runs from flash in continuous read, data in PSRAM in QPI mode), with a minimal
 * Zicsr, traps and one interrupt to mtvec, and the contract-5 peripherals.
 *
 * Modules: r32v_mem (reset sequence, the transaction chip, header nibbles, pins), r32v_csr (CSR
 * state and the trap/interrupt flags), r32v_periph (IO slots, the interrupt line). This file:
 * the slot engine, the nibble-serial datapath, the pc, decode and sequencing.
 *
 * Slots. Every slot is one quad transaction at SCK = clk/2: E SCK edges take 2E + 2 clocks
 * (a gap clock with CS_n high, the edges, a tail clock with SCK low).
 *   read:  8 header edges (r32v_mem), 4 dummy/wait edges, 8 data edges: 42 clocks
 *   write: 8 header edges, 2/4/8 data edges: 22/26/34 clocks
 * The counter c counts SCK edges: it starts at 52 (read) or 56 (write) and wraps to 0 at the
 * first data edge, so c is the data nibble index in the data phase. A masked slot (x0, or no
 * register) keeps CS_n high and SCK low and runs only its 8 data strobes, one per clock: the
 * internal sck stays high through its data phase (10 clocks). Data moves at the clock where SCK
 * falls (the strobe): a read samples nibble c there, a write loads the nibble for the next
 * rising edge.
 *
 * Ring. ring is a 32-bit shift register of 8 nibbles. A read slot shifts its result nibble in at
 * the top at each data strobe; a write slot rotates it (head ring[3:0] to the top) and emits
 * from it. After 8 strobes a value sits with nibble k at ring[4k+3:4k]. Core-region words are
 * stored LSB nibble first; program data arrives high nibble first in each byte, so loads (in W)
 * and stores (in M) emit ring[7:4] on even strobes and ring[31:28] on odd ones. Loads always
 * read 4 bytes. Shifts: W rotates the ring by q nibbles (shamt = 4q + r) during its own header,
 * then emits ({ring[7:4], ring[3:0]} >> r) with zero or sign fill.
 *
 * pc. pcr is a second 8-nibble shift register: it holds the pc with nibble k at pcr[4k+3:4k],
 * so the fetch header reads its address nibbles (and bit 24, the chip) straight from it. A slot
 * that updates the pc shifts the new nibble in at the top at each of its 8 data strobes and
 * reads the old one at the head (pk4). Until the first retirement every read of it gives 0
 * (`first`); the first instruction writes all of it. pcr is also the address register: a load's
 * M header takes the address from the ring (A leaves it there); a store spills pc + 4 to the
 * core region's x0 word (WP), leaves its address in pcr for M and the IO slot, and reloads the pc
 * (P); a register shift does the same around B, which leaves rs2[4:0] (the amount) in pcr. An
 * IO store restores the pc before BS, so its write lands at its retirement. A taken branch on
 * x0 retires with its target in the ring instead (`tk`): the next F takes its address from the
 * ring and moves it into pcr at its strobes; an interrupt entry's WC writes it to mepc.
 *
 *   F   READ  pc -> instruction register; at the same strobes ring <- pc + 4
 *   P   READ  x0 (the spilled pc + 4) -> pc; the ring rotates by 2 nibbles (an IO store's address
 *             nibbles 0-1 to the top)
 *   A   READ  rs1 -> ALU (+imm; AUIPC: pc + imm; CSR: rs1 or zimm; branches: rs1 + 0) -> ring
 *             (a store: and pcr); at the same strobes pc <- the ring's pc + 4, except for
 *             branches, JALR and stores; JALR: pc <- the target, or the pc rotates if the target
 *             traps (the ring keeps pc + 4); an IO load captures the slot into the ring at A's
 *             tail. A branch on x0 compares rs1 with 0 beside the ALU, which computes pc + imm
 *             into the ring, and moves pc + 4 into pcr (not when imm[1] = 1: that target traps
 *             if taken, so the pc stays; untaken, X adds 4)
 *   B   READ  rs2 -> ALU with the ring (OP, branches); register shifts: rs2 -> pcr
 *   BS  READ  rs2 -> ring (store data); an IO store takes nibbles 0-5 only (rs2[15:0] lands in
 *             ring[23:8], under the address nibbles 0-1 in ring[7:0]) and writes the slot at BS's
 *             tail
 *   C   READ  a CSR: core-region word (masked bits read 0) or the die bits -> ring; in a trap
 *             sequence (mtvec) and in MRET (mepc) -> pc
 *   RS  READ  scratch, combined with the ring (CSRRW / CSRRS / CSRRC) -> ring
 *   X   no transaction (masked, 10 clocks): pc <- pc + (taken branch ? imm : 4)
 *   M   READ/WRITE the load/store address (4 bytes / the store's bytes); a store to flash is
 *             masked
 *   W4  WRITE rd <- the ring (pc + 4); JAL: pc <- pc + imm
 *   WP  WRITE x0 <- the ring (pc + 4, the spill)
 *   W   WRITE rd <- ring (forward, load bytes extended, shifted, SLT); LI (ADDI rd, x0, imm)
 *             and LUI: rd <- the immediate, and pc <- the ring's pc + 4 at the same strobes
 *   WS  WRITE scratch <- ring;  WC: the CSR <- ring (in a trap sequence: mepc <- pc)
 * Sequences: ALU F A [B] W (register shifts F WP A B W P; LI, LUI F W); load F A M W (IO: F A
 * W); store F WP A BS M P (IO: F WP A P BS); branch F A B X, on x0 F A (imm[1] = 1, untaken:
 * F A X); JAL F W4; JALR F A W4 (rd = x0: F A); FENCE, WFI F X;
 * CSR F A WS C W RS WC; MRET F C. A
 * synchronous trap (decode after F, JALR after A, branch after A or B) leaves the pc unchanged,
 * runs WC(mepc <- pc) C(pc <- mtvec) and retires; an interrupt (at a retirement) runs WC C and
 * continues with the handler's F.
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
    // (X: no transaction, always masked)
    localparam [4:0] SL_P  = 5'b00000, SL_A  = 5'b00001, SL_X  = 5'b00010, SL_B  = 5'b00011,
                     SL_BS = 5'b00100, SL_C  = 5'b00101, SL_RS = 5'b00110,
                     SL_F  = 5'b01000, SL_M  = 5'b01001,
                     SL_WP = 5'b10000, SL_W4 = 5'b10010, SL_W = 5'b10011, SL_WS = 5'b10100,
                     SL_WC = 5'b10101;
    // Engine phases
    localparam [1:0] E_GAP  = 2'b00,     // CS_n high, SCK low (one clock; waits while booting)
                     E_HDR  = 2'b01,     // header and dummy edges
                     E_DATA = 2'b10,     // data edges
                     E_TAIL = 2'b11;     // SCK low, CS_n still low: the slot's events

    reg  [4:0]  sl;
    reg  [1:0]  ep;
    reg  [5:0]  c;      // SCK edge counter: 52/56..63 header, 0..7 data
    reg         sck;
    reg         first;  // until the first retirement: the pc reads 0
    reg         tk;     // a taken branch on x0 retired: the pc is in the ring, not in pcr
    reg         cy;     // ALU carry; sign latch of a load's W
    reg         cmp;    // compare; JALR target[1] == 0; a shift's sign bit
    reg         mk;     // the slot is masked (mask, registered at the gap clock)
    reg  [31:0] ring;   // operand/result ring
    reg  [31:0] ir;     // instruction register, in wire nibble order
    reg  [31:0] pcr;    // the pc

    wire _unused = &{ena, uio_in[7:6], uio_in[3], uio_in[0], 1'b0};

    // ------------------------------------------------------------------------------------
    // Decode, spec-minimum (contract 4). Traps: insn[0] = 0 (the all-zeros word) and
    // opcode[6] & [4] & [2] (the all-ones word) are illegal (2), ECALL (11), EBREAK (3), a
    // misaligned JAL (0). Every other encoding executes as the class its opcode bits select;
    // register fields use 4 bits.
    // ------------------------------------------------------------------------------------
    wire [31:0] insn  = {ir[7:4], ir[3:0], ir[15:12], ir[11:8], ir[23:20], ir[19:16],
                         ir[31:28], ir[27:24]};
    wire [2:0]  f3    = insn[14:12];
    wire [3:0]  rd_f  = insn[10:7];
    wire [3:0]  rs1_f = insn[18:15];
    wire [3:0]  rs2_f = insn[23:20];
    wire        _unused_insn = insn[1];
    wire        spill = c_st | (c_op & c_sh);          // stores and register shifts spill the pc

    wire c_br    = insn[6] & ~insn[4] & ~insn[2];
    wire c_sys   = insn[6] &  insn[4] & ~insn[2];
    wire c_jalr  = insn[6] &  insn[2] & ~insn[3];
    wire c_jal   = insn[6] &  insn[2] &  insn[3];
    wire c_mem   = ~insn[6] & ~insn[4] & ~insn[2];
    wire c_fence = ~insn[6] & ~insn[4] & insn[2];
    wire c_st    = c_mem & insn[5];
    wire c_ld    = c_mem & ~insn[5];
    wire c_u     = ~insn[6] & insn[4] & insn[2];
    wire c_pcrel = c_u & ~insn[5];                      // AUIPC: ring (pc) + imm
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
    wire rs2z    = (rs2_f == 4'd0);
    wire nb      = c_br & rs2z;                         // a branch on x0: no B, no X
    wire nbw     = nb & ~insn[8];                       // ... whose A moves pc + 4 into pcr
    // LI (ADDI rd, x0, imm) and LUI: no A; W writes the immediate and moves pc + 4 into pcr
    wire liw     = (c_alu & ~insn[5] & (f3 == 3'b000) & (rs1_f == 4'd0)) | (c_u & insn[5]);

    wire fR   = ~sl[4] & ~sl[3];
    wire fM   = ~sl[4] &  sl[3];
    wire fW   =  sl[4];
    wire sP   = (sl == SL_P);
    wire sA   = (sl == SL_A);
    wire sX   = (sl == SL_X);
    wire sB   = (sl == SL_B);
    wire sBS  = (sl == SL_BS);
    wire sC   = (sl == SL_C);
    wire sRS  = (sl == SL_RS);
    wire sF   = (sl == SL_F);
    wire sM   = (sl == SL_M);
    wire sW4  = (sl == SL_W4);
    wire sW   = (sl == SL_W);
    wire sWS  = (sl == SL_WS);
    wire sWC  = (sl == SL_WC);
    wire shB  = sB & c_sh;
    wire gap  = (ep == E_GAP);
    wire hdr  = (ep == E_HDR);
    wire e_end = (ep == E_TAIL);
    wire wr   = fW | (sM & c_st);                       // the slot writes

    // submodule wires
    wire        tm, intr, irq, take, booting;
    wire        csr_region, mask_lo2;
    wire [3:0]  rd_die;
    wire [1:0]  csr_idx;
    wire        io, ps;
    wire [15:0] io_rdata;

    // ------------------------------------------------------------------------------------
    // Slot control
    // ------------------------------------------------------------------------------------
    wire csr_sl = sC | sWC;
    wire mask = (sA & (c_u | (rs1_f == 4'd0) | (c_csr & f3[2]))) |
                ((sB | sBS) & rs2z) |
                ((sW | sW4) & (rd_f == 4'd0)) |
                (csr_sl & ~csr_region) |
                (sM & c_st & ~ps) |                     // a store to flash issues nothing
                sX;
    // every input of mask is constant from a slot's gap to its tail, so the slot uses mk
    wire active = ~gap & ~mk;
    wire wact   = wr & ~mk;                             // the slot drives data on the bus
    wire [3:0] nib   = {uio_in[5], uio_in[4], uio_in[2], uio_in[1]};   // {SD3, SD2, SD1, SD0}
    wire [3:0] nib_m = nib & {4{active}};               // 0 in a masked slot

    // strobes: stb = the clock where SCK is high (it falls at its edge)
    wire       stb   = sck;
    wire [5:0] cn    = c + 6'd1;
    wire [2:0] nlast = (sM & c_st) ? {nm1, 1'b1} : 3'd7;
    wire       dend  = stb & ~c[5] & (c[2:0] == nlast); // the last data edge's strobe
    // data strobe: a read samples nibble c; a write (on the bus) loads nibble c + 1 for the
    // next rising edge; a masked slot of either kind steps like a read
    wire       dstb  = wact ? (stb & ~cn[5] & ~dend) : (stb & ~c[5]);
    wire [2:0] n     = wact ? cn[2:0] : c[2:0];         // data nibble index at dstb (a masked
                                                        // W4 still updates the pc)
    wire       n0    = (n == 3'd0);

    // ------------------------------------------------------------------------------------
    // Immediate nibble n (bits 4n+3 .. 4n)
    // ------------------------------------------------------------------------------------
    wire       sign = insn[31];
    wire       zi   = c_sys & f3[2];                    // CSR immediate forms: zimm
    reg  [3:0] imm4;
    always @(*) begin
        case (n)
            3'd0: imm4 = c_u ? 4'd0 : c_jal ? {insn[23:21], 1'b0} : c_sys ? (insn[18:15] & {4{zi}}) :
                         c_br ? {insn[10:8], 1'b0} : c_st ? insn[10:7] : insn[23:20];
            3'd1: imm4 = c_u ? 4'd0 : c_sys ? {3'b000, zi & insn[19]} :
                         (c_br | c_st) ? {insn[27:25], insn[11]} : insn[27:24];
            3'd2: imm4 = (c_u | c_sys) ? 4'd0 : c_jal ? {insn[20], insn[30:28]} :
                         c_br ? {insn[7], insn[30:28]} : insn[31:28];
            3'd3: imm4 = (c_u | c_jal) ? insn[15:12] : {4{sign & ~c_sys}};
            3'd4: imm4 = (c_u | c_jal) ? insn[19:16] : {4{sign & ~c_sys}};
            3'd5: imm4 = c_u ? insn[23:20] : {4{sign & ~c_sys}};
            3'd6: imm4 = c_u ? insn[27:24] : {4{sign & ~c_sys}};
            default: imm4 = c_u ? insn[31:28] : {4{sign & ~c_sys}};
        endcase
    end

    // ------------------------------------------------------------------------------------
    // Nibble ALU (also the pc's arithmetic: F's pc + 4, X, W4 of JAL)
    // ------------------------------------------------------------------------------------
    wire       taken  = cmp ^ f3[0];
    wire [3:0] four   = {1'b0, n0, 2'b00};
    wire [3:0] pk4    = pcr[3:0] & {4{~first}};         // pc nibble n (0 before the 1st retirement)
    wire       bimm_a = ~(c_op | c_sh | (c_br & ~rs2z));
    // the pc operand: pcr, or the ring in a fetch after a taken branch on x0 (tk)
    wire       a_pc   = (sF & ~tk) | (sA & (c_pcrel | nb)) | sX | (sW4 & c_jal);
    wire [3:0] a4     = a_pc ? pk4 : (sB | sW4 | (sF & tk)) ? ring[3:0] : nib_m;
    wire [3:0] b4     = sA ? (imm4 & {4{bimm_a}}) :
                        sB ? nib_m :
                        sF ? four :
                        sX ? ((c_br & taken) ? imm4 : four) :
                        (sW4 & c_jal) ? imm4 : 4'd0;
    // A: SLTI/SLTIU compare rs1 with imm; B: SUB, SLT, SLTU and branches subtract
    wire       sub    = sA ? c_slt : (sB & (c_br | (~f3[2] & (f3[1] | insn[30]))));
    wire [3:0] b_x    = b4 ^ {4{sub}};
    wire       cin    = n0 ? sub : cy;
    wire [4:0] add    = {1'b0, a4} + {1'b0, b_x} + {4'd0, cin};
    wire [3:0] sum    = add[3:0];
    wire       cout   = add[4];
    wire [3:0] pp     = a4 ^ b_x;
    wire [3:0] gg     = a4 & b_x;
    wire [3:0] boolr  = (pp & {4{~f3[0]}}) | (gg & {4{f3[1]}});
    wire       sgnd   = c_br ? ~f3[1] : ~f3[0];
    wire       lt     = cout ^ (pp[3] | ~sgnd);
    wire [3:0] sumj   = {sum[3:1], sum[0] & ~(n0 & c_jalr)};   // JALR clears target bit 0
    wire       eqacc  = ~(|sumj) & (cmp | n0);
    wire       ocmp   = (c_br & ~f3[2]) ? eqacc : lt;
    // A of a branch compares rs1 with 0 beside the ALU: equal (all nibbles 0), signed less
    // (rs1[31], the last nibble's), unsigned less (never); B recomputes it when rs2 != x0
    wire       zc     = f3[2] ? (~f3[1] & nib_m[3]) : (~(|nib_m) & (cmp | n0));
    // logic ops: XORI, ORI, ANDI in A; XOR, OR, AND in B (a B result with f3 = 001..011 is unused)
    wire       useb   = (sA & c_alu & ~insn[5] & f3[2] & ~c_sh) | (sB & f3[2]);
    wire [3:0] alu_w  = useb ? boolr : sumj;

    // C: a core-region CSR word (bits 1:0 of mtvec and mepc read 0) or the die bits;
    // RS: CSRRW takes the scratch word (rs1 or zimm), CSRRS ors it in, CSRRC clears its bits
    wire [3:0] c4     = csr_region ? (nib_m & {2'b11, {2{~mask_lo2}}}) : rd_die;
    wire [3:0] rs4    = f3[1] ? (f3[0] ? (ring[3:0] & ~nib_m) : (ring[3:0] | nib_m)) : nib_m;
    wire [3:0] din4   = sC ? c4 : sRS ? rs4 : alu_w;

    // ------------------------------------------------------------------------------------
    // Write family emission, nibble n at the data strobe (before the ring rotates)
    // ------------------------------------------------------------------------------------
    // program data (load bytes in W, store bytes in M): byte nibbles swapped
    wire [3:0] tap4   = n[0] ? ring[31:28] : ring[7:4];
    // loads: IO slots have 16 bits, so an IO LW is an LHU
    wire [1:0] nm1_l  = io ? {1'b0, nm1[0]} : nm1;
    wire       ldfill = (n[2:1] > nm1_l);
    wire       fillv  = ~f3[2] & cy & ~(io & f3[1]);
    // shifts: shamt = 4q + r; right: the ring is rotated by q, emit ({next, head} >> r);
    // left: rotated by -q - (r != 0), emit ({next, head} >> (4 - r) mod 4)
    wire [4:0] shamt  = c_op ? pcr[4:0] : insn[24:20];
    wire [2:0] q      = shamt[4:2];
    wire [1:0] r      = shamt[1:0];
    wire       rnz    = |r;
    wire [2:0] rotn   = sh_left ? (~q + {2'b00, ~rnz}) : q;
    wire [1:0] amt    = sh_left ? (~r + 2'd1) : r;
    wire [3:0] qh     = {1'b0, q} + {3'b000, rnz};
    wire       hv     = sh_left ? ({1'b0, n} >= qh) : (n <= ~q);
    wire       nv     = sh_left ? (n >= q) : (n < ~q);
    wire       shf    = ~sh_left & insn[30] & cmp;      // SRA fill: rs1[31], latched in A
    wire [3:0] hd     = hv ? ring[3:0] : {4{shf}};
    wire [2:0] nx     = nv ? ring[6:4] : {3{shf}};
    reg  [3:0] fun;
    always @(*) begin
        case (amt)
            2'd0: fun = hd;
            2'd1: fun = {nx[0], hd[3:1]};
            2'd2: fun = {nx[1:0], hd[3:2]};
            default: fun = {nx, hd[3]};
        endcase
    end
    wire [3:0] wd4    = c_ld  ? (ldfill ? {4{fillv}} : tap4) :
                        c_slt ? {3'b000, n0 & cmp} :
                        c_sh  ? fun :
                        liw   ? imm4 :
                                ring[3:0];
    // a trap sequence's WC (mepc): the pc (from the ring after a taken branch on x0: tk); W4,
    // WP (pc + 4), WS, WC: the ring
    wire [3:0] wdo4   = sW ? wd4 : fM ? tap4 : (sWC & tm & ~tk) ? pk4 : ring[3:0];

    // ------------------------------------------------------------------------------------
    // Next slot after the tail
    // ------------------------------------------------------------------------------------
    reg [4:0] nsl;
    reg       nret, tgo;
    always @(*) begin
        nsl  = SL_W;
        nret = 1'b0;
        tgo  = 1'b0;
        case (sl)
            SL_F: begin
                if (trap_dec) begin
                    tgo = 1'b1;
                    nsl = SL_WC;
                end else
                    nsl = c_jal ? SL_W4 : p_mret ? SL_C : (c_fence | priv) ? SL_X :
                          spill ? SL_WP : liw ? SL_W : SL_A;
            end
            SL_WP: nsl = SL_A;
            SL_A: begin
                if (c_jalr & ~cmp) begin
                    tgo = 1'b1;
                    nsl = SL_WC;
                end else if (c_jalr) begin
                    nret = (rd_f == 4'd0);              // nothing to link
                    nsl  = SL_W4;
                end
                else if (c_br) begin
                    if (~rs2z)
                        nsl = SL_B;
                    else if (taken & insn[8]) begin
                        tgo = 1'b1;
                        nsl = SL_WC;
                    end else if (insn[8])
                        nsl = SL_X;
                    else
                        nret = 1'b1;                    // taken: the target is in the ring (tk)
                end else if (c_op)
                    nsl = SL_B;
                else if (c_st)
                    nsl = io ? SL_P : SL_BS;
                else if (c_ld & ~io)
                    nsl = SL_M;
                else if (c_csr)
                    nsl = SL_WS;
            end
            SL_B: begin
                if (c_br) begin
                    if (taken & insn[8]) begin
                        tgo = 1'b1;
                        nsl = SL_WC;
                    end else
                        nsl = SL_X;
                end
            end
            SL_P: begin
                nret = ~(c_st & io);                    // an IO store's P comes before BS
                nsl  = SL_BS;
            end
            SL_BS: begin
                nret = io;
                nsl  = SL_M;
            end
            SL_M:  nsl = c_st ? SL_P : SL_W;
            SL_WS: nsl = SL_C;
            SL_C: begin
                if (tm | p_mret) begin
                    nret = ~(tm & intr);                // an interrupt entry retires nothing
                    nsl  = SL_F;
                end
            end
            SL_W: begin
                nret = ~c_csr & ~spill;
                nsl  = c_csr ? SL_RS : SL_P;
            end
            SL_RS: nsl = SL_WC;
            SL_WC: begin
                nret = ~tm;
                nsl  = SL_C;
            end
            default: nret = 1'b1;                       // X, W4
        endcase
    end

    // ------------------------------------------------------------------------------------
    // IO: an IO load captures the slot into the ring at A's tail (atomic: the read side effect
    // happens at the same edge), as M would leave 4 bytes; an IO store writes rs2[15:0] from the
    // ring at BS's tail, its retirement.
    // ------------------------------------------------------------------------------------
    wire        io_re = e_end & sA & c_ld & io;
    wire        io_we = e_end & sBS & io;            // data: rs2[15:0] in ring[23:8]
    // the IO slot: the address's nibbles 0-1 are in the ring (A; an IO store: P, BS)
    wire [6:0]  mar_lo = ring[6:0];
    wire [1:0]  off   = mar_lo[1:0];
    wire [7:0]  iob0  = off[1] ? 8'd0 : off[0] ? io_rdata[15:8] : io_rdata[7:0];
    wire [7:0]  iob1  = (off == 2'd0) ? io_rdata[15:8] : 8'd0;

    // ------------------------------------------------------------------------------------
    // Ring and instruction register (no reset: nothing reads them before they are written)
    // ------------------------------------------------------------------------------------
    wire ring_in  = dstb & (fR | sF | (sM & c_ld)) & ~shB & ~(sA & c_jalr) & ~sP &
                    ~(sBS & io & n[2] & n[1]);
    wire hrot     = stb & hdr & cn[5] & sW & c_sh & (cn[2:0] <= rotn);
    wire ring_rot = (dstb & (wr | (sP & ~n[2] & ~n[1]))) | hrot;
    // pcr writes: A (the ring's pc + 4, also for a branch on x0 unless imm[1] = 1; a store: its
    // address; JALR: the target), B of a register shift (rs2), P (the spilled pc + 4), X, W4 of
    // JAL, W of LI and LUI (the ring's pc + 4), and C in a trap sequence or MRET. F and a trap
    // sequence's WC read the pc nibble by nibble: pcr rotates; so does JALR's A when the target
    // traps (target[1] is sumj[1] at nibble 0, then ~cmp), leaving the pc. The write enable is
    // decode only (no ALU term). A fetch after a taken branch on x0 (tk) moves the ring's target
    // into pcr.
    wire       jtrap = c_jalr & (n0 ? sumj[1] : ~cmp);
    wire       prot = sF | (sWC & tm);
    wire       pwr  = (sA & (~c_br | nbw)) | (sB & c_sh) | sP | sX | (sW4 & c_jal) |
                      (sC & (tm | p_mret)) | (sW & liw);
    wire       pwe  = dstb & (pwr | prot);
    wire [3:0] pin4 = ((prot & ~(sF & tk)) | (sA & jtrap)) ? pcr[3:0] :
                      ((sA & ~c_st & ~c_jalr) | (sF & tk) | sW) ? ring[3:0] :
                      (sB | sP) ? nib_m : din4;
    always @(posedge clk) begin
        if (io_re)
            ring[15:0] <= {iob1[3:0], iob1[7:4], iob0[3:0], iob0[7:4]};
        else if (ring_in)
            ring <= {din4, ring[31:4]};
        else if (ring_rot)
            ring <= {ring[3:0], ring[31:4]};
        if (sF & dstb)
            ir <= {ir[27:0], nib_m};
        if (pwe)
            pcr <= {pin4, pcr[31:4]};
    end

    // ------------------------------------------------------------------------------------
    // Datapath flags and the edge counter (no reset)
    // ------------------------------------------------------------------------------------
    always @(posedge clk) begin
        if (gap)
            cy <= 1'b0;
        else if (dstb) begin
            if (sW) begin
                if (~ldfill)
                    cy <= tap4[3];
            end else if (fR | sF | sW4)
                cy <= cout;
        end

        if (dstb & sA) begin
            if (c_br)
                cmp <= zc;
            else if (c_slt)
                cmp <= ocmp;
            else if (c_jalr & n0)
                cmp <= ~sumj[1];                        // JALR target[1] == 0
            else if (c_sh)
                cmp <= a4[3];                           // rs1[31] at the last nibble
        end else if (dstb & sB & ~c_sh)                 // SLT, branches
            cmp <= ocmp;

        if (gap) begin
            c  <= mask ? 6'd0 : wr ? 6'd56 : 6'd52;
            mk <= mask;
        end else if (stb)
            c <= cn;
    end

    // ------------------------------------------------------------------------------------
    // Control (reset)
    // ------------------------------------------------------------------------------------
    wire tk_set = sA & nbw & taken;                     // at A's tail
    always @(posedge clk) begin
        if (!rst_n) begin
            sl    <= SL_F;
            ep    <= E_GAP;
            sck   <= 1'b0;
            first <= 1'b1;
            tk    <= 1'b0;
        end else begin
            // toggles in HDR and DATA; a masked slot holds it high through DATA (a strobe per
            // clock; SCK stays low on the pin)
            sck <= gap ? (~booting & mask) : ((ep[0] ^ ep[1]) & (~sck | mk) & ~dend);
            case (ep)
                E_GAP:  if (~booting) ep <= mask ? E_DATA : E_HDR;
                E_HDR:  if (stb & (c == 6'd63)) ep <= E_DATA;
                E_DATA: if (dend) ep <= E_TAIL;
                default: begin
                    sl <= nret ? (take ? SL_WC : SL_F) : nsl;
                    ep <= E_GAP;
                    tk <= tk_set;
                    if (nret)
                        first <= 1'b0;
                end
            endcase
        end
    end

    // ------------------------------------------------------------------------------------
    // Submodules
    // ------------------------------------------------------------------------------------
    wire       scr     = sWS | sRS;                     // the scratch word 0x80
    wire       regarea = ~scr & ~csr_sl;                // x1..x15 at 0xC0 + 4r
    wire [3:0] rsel    = sA ? rs1_f : (sB | sBS) ? rs2_f : (sW | sW4) ? rd_f :
                         csr_sl ? {2'b01, csr_idx} : 4'd0;

    r32v_mem u_mem (
        .clk(clk), .rst_n(rst_n),
        .cap_a(dstb & sA), .kn(n), .din4(din4),
        .io(io), .ps(ps), .booting(booting),
        .active(active), .prog(fM), .fetch(sF),
        .pa({(tk ? ring[24] : pcr[24]), ((sM ? c_ld : tk) ? ring[23:0] : pcr[23:0])}),
        .fz(sF & first),
        .sck(sck), .gap(gap),
        .hdr_ld((gap & ~booting & ~mask) | (stb & hdr & cn[5])),
        .hidx(gap ? 3'd0 : (cn[2:0] ^ {~wr, 2'b00})),
        .h_wr(wr), .h_reg({regarea, rsel}),
        .dat_ld(dstb & wact), .wd4(wdo4),
        .oe_off(stb & hdr & ~wr & (cn[3:0] == 4'b1100)),
        .uio_out(uio_out), .uio_oe(uio_oe)
    );

    r32v_csr u_csr (
        .clk(clk), .rst_n(rst_n),
        .csr_num(insn[31:20]), .p_mret(p_mret), .s_wc(sWC), .kn(n),
        .region(csr_region), .idx(csr_idx), .mask_lo2(mask_lo2), .rd_die(rd_die),
        .trap_go(e_end & tgo), .cause(cause), .retire(e_end & nret),
        .wc_end(e_end & sWC & ~tm),
        .wb({ring[31], ring[11], ring[7], ring[3:0]}),
        .seq_end(e_end & sC & tm), .irq(irq),
        .tm(tm), .intr(intr), .take(take)
    );

    r32v_periph u_periph (
        .clk(clk), .rst_n(rst_n), .ui_in(ui_in), .uo_out(uo_out),
        .slot(mar_lo[6:2]), .off(off), .size(f3[1:0]), .we(io_we), .re(io_re),
        .wdata(ring[23:8]), .rdata(io_rdata), .irq(irq)
    );

    // ------------------------------------------------------------------------------------
    // RVFI (formal and cosim builds only; observes, never drives the pins)
    // ------------------------------------------------------------------------------------
`ifdef RISCV_FORMAL
    reg [63:0]  r_order;
    reg         r_v, r_tr, r_intr, r_ipend;
    reg [31:0]  r_rs1, r_rs2, r_rd, r_pc, r_npc, r_mrd;
    integer     mi;
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
            // retirement comes at this same tail and still reports the old value.
            if (e_end & sC & tm)
                r_ipend <= 1'b1;
            else if (e_end & nret)
                r_ipend <= 1'b0;
            if (e_end & nret)
                r_intr <= r_ipend;
        end
        if (e_end & sF)
            r_pc <= {pcr[31:2], 2'b00} & {32{~first}};
        if (dstb & pwr & ~(sA & c_st) & ~sB)            // every pc write covers nibbles 0..7
            r_npc <= {pin4, r_npc[31:4]};
        if (e_end & tk_set)                             // a taken branch on x0: the ring's target
            r_npc <= ring;
        if (dstb & sA)
            r_rs1 <= {nib_m, r_rs1[31:4]};
        else if (e_end & sF)                            // LI runs no A: rs1 = x0 reads 0
            r_rs1 <= 32'd0;
        if (dstb & (sB | sBS))
            r_rs2 <= {nib_m, r_rs2[31:4]};
        if (dstb & (sW | sW4))
            r_rd <= {wdo4, r_rd[31:4]};
        // M: byte n/2 of the load, high nibble first
        for (mi = 0; mi < 32; mi = mi + 1)
            if (dstb & sM & c_ld & ({n[2:1], ~n[0]} == mi[4:2]))
                r_mrd[mi] <= nib_m[mi[1:0]];
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
    wire [31:0] r_rs2v = rs2z ? 32'd0 : r_rs2;          // a branch on x0 runs no B slot

    assign rvfi_valid     = r_v;
    assign rvfi_order     = r_order;
    assign rvfi_insn      = insn;
    assign rvfi_trap      = r_tr;
    assign rvfi_halt      = 1'b0;
    assign rvfi_intr      = r_intr;
    assign rvfi_mode      = 2'd3;
    assign rvfi_ixl       = 2'd1;
    assign rvfi_rs1_addr  = r_rs1e ? {1'b0, rs1_f} : 5'd0;
    assign rvfi_rs2_addr  = r_rs2e ? {1'b0, rs2_f} : 5'd0;
    assign rvfi_rs1_rdata = r_rs1e ? r_rs1 : 32'd0;
    assign rvfi_rs2_rdata = r_rs2e ? r_rs2v : 32'd0;
    assign rvfi_rd_addr   = r_rde ? {1'b0, rd_f} : 5'd0;
    assign rvfi_rd_wdata  = r_rde ? r_rd : 32'd0;
    assign rvfi_pc_rdata  = r_pc;
    assign rvfi_pc_wdata  = r_npc;
    assign rvfi_mem_addr  = r_mem ? maddr : 32'd0;
    assign rvfi_mem_rmask = (r_mem & c_ld) ? r_mask : 4'd0;
    assign rvfi_mem_wmask = (r_mem & c_st) ? r_mask : 4'd0;
    assign rvfi_mem_rdata = (r_mem & c_ld) ? (r_mrd & lmask) : 32'd0;
    assign rvfi_mem_wdata = (r_mem & c_st) ? (r_rs2v & lmask) : 32'd0;
`endif

endmodule

`default_nettype wire
