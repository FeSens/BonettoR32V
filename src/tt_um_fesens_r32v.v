/*
 * BonettoR32V: bit-serial RV32E (hypothesis 001).
 *
 * Everything 32 bits wide is a circular shift register that rotates every clock: the 15
 * registers x1..x15 and the pc. A free-running 5-bit phase counter ph names the bit that sits
 * at the tap (bit 0 of each ring) right now, so register bit k is readable at ph == k, and a
 * write of bit k goes into the ring's input at ph == k. There are no per-bit enables in the
 * register file: a register's only extra cell is the write mux at its input.
 *
 * One instruction (see src/ARCHITECTURE.md for the timing):
 *   fetch   SPI READ of the 4 instruction bytes at pc[15:0] into the instruction register
 *   P1, P2  one or more 32-cycle passes, each aligned to ph = 0
 *   memory  SPI READ or WRITE at the exact byte address (misaligned and 64 KB wrap for free)
 *   retire  always at ph = 0, so RVFI sees every ring aligned
 *
 * The 23LC512 wants addresses and data MSB first, the datapath is LSB first. An 8-bit ring
 * (bufr) that also rotates every clock does the reversal: with SCK at a period of 7 clocks,
 * consecutive SPI bits meet the ring 7 = -1 (mod 8) positions apart, so a fixed tap reads the
 * ring backwards. Command bits and the instruction word run at SCK = clk/2.
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

    // ------------------------------------------------------------------------------------
    // States. Every state that waits for a ph value is entered so that the wait is
    // unambiguous (see ARCHITECTURE.md).
    // ------------------------------------------------------------------------------------
    // The encoding groups states so that the group decodes below are single gates:
    // 11xxx slow SPI slots, 10xxx clk/2 SPI, 0100x execute passes.
    localparam [4:0] S_IDLE = 5'b00000,  // CS_n high; at ph 6 start a transaction
                     S_ALD  = 5'b00001,  // from ph 15: bufr <- address bits 7..0 at ph 0..7
                     S_FEND = 5'b00010,  // CS_n high, wait for ph 31
                     S_SLD  = 5'b00011,  // store: bufr <- rs2 byte k
                     S_LWW  = 5'b00100,  // load: one cycle
                     S_LWR  = 5'b00101,  // load: write byte k (and the fill) into rd
                     S_RETW = 5'b00110,  // wait for ph 31
                     S_RET  = 5'b00111,  // retire (ph 0)
                     S_P1   = 5'b01000,  // first execute pass
                     S_P2   = 5'b01001,  // later execute passes
                     S_CLR  = 5'b01010,  // after reset: pc <- 0 during ph 0..31
                     S_HALT = 5'b01111,  // after a fatal trap
                     S_CMD  = 5'b10000,  // ph 7..22: 8 command bits, SCK = clk/2
                     S_FDAT = 5'b10001,  // 32 instruction bits, SCK = clk/2
                     S_AHI  = 5'b11000,  // address bits 15..8 from bufr, slow SCK
                     S_ALO  = 5'b11001,  // address bits 7..0 from bufr, slow SCK
                     S_SEM  = 5'b11010,  // store: emit byte k, slow SCK
                     S_LRX  = 5'b11011;  // load: receive byte k into bufr, slow SCK

    reg  [4:0]   st;
    reg  [4:0]   ph;      // bit index at the ring taps
    reg  [4:0]   j;       // SPI bit counter, shift amount counter
    reg          cs_n, sck, mosi;
    reg          mem;     // the current SPI transaction is a data access
    reg          cy;      // ALU carry; shift delay-1 element; load sign
    reg          cmp;     // compare result; address[1:0] == 0; JALR misalign; first rotation
    reg          c4;      // pc + 4 carry; shift fill sign
    reg  [7:0]   bufr;    // byte ring: bit reversal, shift delay-8, GPIO store byte
    reg  [31:0]  irs;     // instruction register, filled in SPI order
    reg  [31:0]  pcr;     // pc ring
    reg  [479:0] rfq;     // x1..x15 rings: x_i is rfq[32*i-32 +: 32]
    reg  [7:0]   gpo;

    wire miso = uio_in[2];

    assign uo_out  = gpo;
    assign uio_out = {4'b0000, sck, 1'b0, mosi, cs_n};
    assign uio_oe  = 8'b0000_1011;

    wire _unused = &{ena, uio_in[7:3], uio_in[1:0], 1'b0};

    // ------------------------------------------------------------------------------------
    // Decode. The SPI delivers byte 0 first and every byte MSB first, so after 32 shifts
    // irs holds the bytes in reverse order: a pure rewiring.
    // ------------------------------------------------------------------------------------
    wire [31:0] insn  = {irs[7:0], irs[15:8], irs[23:16], irs[31:24]};
    wire [2:0]  f3    = insn[14:12];
    wire [3:0]  rd_f  = insn[10:7];
    wire [3:0]  rs1_f = insn[18:15];
    wire [3:0]  rs2_f = insn[23:20];

    // Strict legality: anything that is not an RV32E instruction traps.
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
    wire trap_dec  = ~legal | (is_jal & insn[21]);   // JAL target[1] = imm[1] (pc[1:0] = 0)

    // Datapath classes: only legal instructions execute, so a few opcode bits suffice.
    wire c_br   = insn[6] & ~insn[2];
    wire c_jalr = insn[6] & insn[2] & ~insn[3];
    wire c_jal  = insn[6] & insn[3];
    wire c_mem  = ~insn[6] & ~insn[4] & ~insn[2];
    wire c_st   = c_mem & insn[5];
    wire c_ld   = c_mem & ~insn[5];
    wire c_u    = ~insn[6] & insn[4] & insn[2];
    wire c_lui  = c_u & insn[5];
    wire c_alu  = ~insn[6] & insn[4] & ~insn[2];
    wire c_op   = c_alu & insn[5];
    wire c_sh   = c_alu & (f3[1:0] == 2'b01);
    wire c_slt  = c_alu & (f3[2:1] == 2'b01);
    wire sh_left = ~f3[2];

    // Immediate bit at the current phase, from three shared selectors:
    //   m_a = insn[20 + ph] (ph 0..11), m_b = insn[7 + ph] (ph 0..4), m_c = insn[ph] (ph >= 12).
    wire [11:0] va   = insn[31:20];
    wire [4:0]  vb   = insn[11:7];
    wire        m_a  = va[ph[3:0]];
    wire        m_b  = vb[ph[2:0]];
    wire        m_c  = insn[ph];
    wire        ph_hi = ph[4] | (ph[3] & ph[2]);           // ph >= 12
    wire        ph0  = (ph == 5'd0);
    wire        ph11 = (ph == 5'd11);
    wire        ph_lo5 = ~ph[4] & ~ph[3] & ~(ph[2] & (ph[1] | ph[0]));   // ph < 5
    wire        sign = insn[31];
    wire        imm_bit =
        c_u   ? (ph_hi & m_c) :
        c_jal ? (ph_hi ? ((~ph[4] | (ph[3:2] == 2'b00)) ? m_c : sign) :
                         (ph0 ? 1'b0 : ph11 ? insn[20] : m_a)) :
        ph_hi ? sign :
        c_br  ? (ph0 ? 1'b0 : ph11 ? insn[7] : ph_lo5 ? m_b : m_a) :
        c_st  ? (ph_lo5 ? m_b : m_a) :
                m_a;

    // ------------------------------------------------------------------------------------
    // Register file taps
    // ------------------------------------------------------------------------------------
    wire [15:0] taps;
    assign taps[0] = 1'b0;
    genvar gi;
    generate
        for (gi = 1; gi < 16; gi = gi + 1) begin : g_tap
            assign taps[gi] = rfq[32*gi-32];
        end
    endgenerate

    wire [3:0] rs1_sel = (st == S_P2 && !insn[6]) ? rd_f : rs1_f;  // shifts read rd in P2
    wire rs1_t = taps[rs1_sel];
    wire rs2_t = taps[rs2_f];
    wire pc_t  = pcr[0];

    // ------------------------------------------------------------------------------------
    // Slow SPI slot grid (period 7): d = ph[2:0] + j[2:0]. d == 7 loads MOSI from the ring
    // tap, SCK is high while d == 1 (and MISO is captured into the ring then), j advances at
    // d == 5 and the next slot starts at d == 7 again.
    // ------------------------------------------------------------------------------------
    wire [2:0] d        = ph[2:0] + j[2:0];
    wire       slow     = st[4] & st[3];
    wire       emit     = slow & ~(st[1] & st[0]);
    wire       slot_end = (d == 3'd5);
    wire       byte_end = slot_end & (j[2:0] == 3'd7);
    wire       fast     = st[4] & ~st[3];
    wire       fsample  = (st == S_FDAT) & sck;
    wire [1:0] nm1      = {f3[1], f3[1] | f3[0]};   // access bytes - 1

    // ------------------------------------------------------------------------------------
    // Serial ALU and pc + 4
    // ------------------------------------------------------------------------------------
    reg  [1:0] asel;   // 0: zero, 1: rs1, 2: pc
    reg  [1:0] bsel;   // 0: zero, 1: rs2, 2: immediate
    reg        sub;
    wire a_bit = (asel == 2'd1) ? rs1_t : (asel == 2'd2) ? pc_t : 1'b0;
    wire b_raw = (bsel == 2'd1) ? rs2_t : (bsel == 2'd2) ? imm_bit : 1'b0;
    wire b_x   = b_raw ^ sub;
    wire cin   = (ph == 5'd0) ? sub : cy;
    wire sum   = a_bit ^ b_x ^ cin;
    wire cout  = (a_bit & b_x) | (cin & (a_bit ^ b_x));
    wire boolr = ((a_bit ^ b_raw) & ~f3[0]) | (f3[1] & a_bit & b_raw);
    wire sgnd  = c_br ? ~f3[1] : ~f3[0];
    wire lt    = (a_bit & sgnd) ^ ~(b_raw & sgnd) ^ cout;
    wire eqacc = ~sum & (cmp | (ph == 5'd0));
    wire ocmp  = (c_br & ~f3[2]) ? eqacc : lt;

    wire inc   = pc_t ^ (c4 & (ph != 5'd0));

    // ------------------------------------------------------------------------------------
    // Shifts: P1 copies rs1 to rd and loads j with -shamt (left) or shamt (right) through the
    // ALU; each P2 rotates rd left by 8 (or the remaining 1..7) through bufr and adds that to
    // j, until j wraps to 0: a left rotation by shamt, or by 32 - shamt for right shifts.
    // Source bits that must not wrap (index >= j for left, < j for right) are replaced with
    // the fill as they enter the delay line during the first rotation.
    // ------------------------------------------------------------------------------------
    // Pass delay: 8 while 32 - j >= 8, then the remaining 32 - j = 8 - j[2:0] in one last pass.
    // A bit written into bufr is read back 8 - t clocks later at bufr[t].
    wire       sh_last  = j[4] & j[3];                       // j >= 24: this pass finishes
    wire [2:0] sh_tap   = j[2:0] & {3{sh_last}};
    wire       masked   = (ph < j) ^ sh_left;
    wire       fill     = ~sh_left & insn[30] & c4;
    wire       mask_now = ((st == S_P1) | cmp) & masked;
    wire       msrc     = mask_now ? fill : rs1_t;
    wire       sh_w     = (st == S_P1) ? rs1_t : bufr[sh_tap];
    // the last 8 (or 1) writes of a pass feed the next pass: they take the new rd bits
    wire       sh_tail  = (sh_tap == 3'd0) & (ph[4:3] == 2'b11);

    // ------------------------------------------------------------------------------------
    // Loads
    // ------------------------------------------------------------------------------------
    wire [1:0] kb      = j[4:3] - 2'd1;          // byte being written (LWR)
    wire       ld_last = (kb == nm1);
    wire       lwin    = (ph[4:3] == kb);
    wire       lfill   = ~f3[2] & cy;            // LB/LH sign, LBU/LHU zero
    wire       io_w    = (ph[4:3] == 2'b00) ? (cmp & ui_in[ph[2:0]]) :
                         ((f3 == 3'b000) & cmp & ui_in[7]);

    // Fatal traps found after P1, kept in cmp
    wire trap = trap_dec | (c_br & (cmp ^ f3[0]) & insn[8]) | (c_jalr & cmp);

    // ------------------------------------------------------------------------------------
    // Datapath control
    // ------------------------------------------------------------------------------------
    reg rd_we, rd_w, pc_we, pc_w, buf_we, buf_in;
    always @(*) begin
        asel   = 2'd2;      // default sum = pc + 0: the carry stays 0 from ph 0
        bsel   = 2'd0;
        sub    = 1'b0;
        rd_we  = 1'b0;
        rd_w   = sum;
        pc_we  = 1'b0;
        pc_w   = inc;
        buf_we = 1'b0;
        buf_in = sum;
        case (st)
            S_CMD: buf_we = ~mem & (ph[4:3] == 2'b01);           // fetch: pc[15:8]
            S_CLR: begin
                pc_we = 1'b1;
                pc_w  = 1'b0;
            end
            S_ALD: begin
                if (mem) begin
                    asel = 2'd1;
                    bsel = 2'd2;
                end
                buf_we = (ph[4:3] == 2'b00);                    // pc[7:0] or address[7:0]
            end
            S_ALO: if (mem & c_st) begin                        // last slot: bufr <- rs2[7:0]
                asel   = 2'd0;
                bsel   = 2'd1;
                buf_we = (j == 5'd31) & (ph[4:3] == 2'b00);
            end
            S_SLD: begin
                asel   = 2'd0;
                bsel   = 2'd1;
                buf_we = (ph[4:3] == j[4:3]);                   // rs2 byte k
            end
            S_LRX: begin
                buf_we = (d == 3'd1);
                buf_in = miso;
            end
            S_LWR: begin
                rd_w  = lwin ? bufr[2] : lfill;
                rd_we = ld_last ? (ph[4:3] >= kb) : lwin;
            end
            S_P1: begin
                if (c_alu) begin
                    asel  = c_sh ? 2'd0 : 2'd1;
                    bsel  = c_op ? 2'd1 : 2'd2;
                    sub   = (c_op & insn[30] & (f3 == 3'b000)) | c_slt | (c_sh & sh_left);
                    pc_we = 1'b1;
                    if (c_sh) begin
                        rd_we  = 1'b1;
                        rd_w   = rs1_t;
                        buf_we = 1'b1;
                        buf_in = msrc;
                    end else if (!c_slt) begin
                        rd_we = 1'b1;
                        rd_w  = (f3 == 3'b000) ? sum : boolr;
                    end
                end else if (c_u) begin
                    asel  = c_lui ? 2'd0 : 2'd2;
                    bsel  = 2'd2;
                    rd_we = 1'b1;
                    pc_we = 1'b1;
                end else if (c_jal) begin
                    bsel  = 2'd2;
                    rd_we = 1'b1;
                    rd_w  = inc;
                    pc_we = 1'b1;
                    pc_w  = sum;
                end else if (c_jalr) begin
                    asel  = 2'd1;                               // target[1] check only
                    bsel  = 2'd2;
                end else if (c_br) begin
                    asel = 2'd1;
                    bsel = 2'd1;
                    sub  = 1'b1;
                end else if (c_mem) begin
                    asel   = 2'd1;
                    bsel   = 2'd2;
                    pc_we  = 1'b1;
                    buf_we = (ph[4:3] == 2'b01);               // address[15:8]
                end else begin
                    pc_we = 1'b1;                               // FENCE
                end
            end
            S_P2: begin
                if (c_sh) begin
                    rd_we  = 1'b1;
                    rd_w   = sh_w;
                    buf_we = 1'b1;
                    buf_in = sh_tail ? sh_w : msrc;
                end else if (c_slt) begin
                    rd_we = 1'b1;
                    rd_w  = (ph == 5'd0) & cmp;
                end else if (c_jalr) begin
                    asel  = 2'd1;
                    bsel  = 2'd2;
                    rd_we = ~cmp;                               // cmp: target[1] (trap)
                    rd_w  = inc;
                    pc_we = ~cmp;
                    pc_w  = sum & (ph != 5'd0);
                end else if (c_br) begin
                    bsel  = 2'd2;
                    pc_we = ~((cmp ^ f3[0]) & insn[8]);         // no write on a trap
                    pc_w  = (cmp ^ f3[0]) ? sum : inc;
                end else if (c_ld) begin
                    rd_we = ~mem;                               // GPIO load
                    rd_w  = io_w;
                end else begin
                    asel   = 2'd0;                              // GPIO store: bufr <- rs2[7:0]
                    bsel   = 2'd1;
                    buf_we = ~mem & (ph[4:3] == 2'b00);
                end
            end
            default: ;
        endcase
    end

    // ------------------------------------------------------------------------------------
    // Rings (no reset: nothing reads them before they are written)
    // ------------------------------------------------------------------------------------
    integer ri;
    always @(posedge clk) begin
        for (ri = 1; ri < 16; ri = ri + 1)
            rfq[32*ri-32 +: 32] <= {(rd_we && (rd_f == ri[3:0])) ? rd_w : rfq[32*ri-32],
                                    rfq[32*ri-31 +: 31]};
        pcr  <= {pc_we ? pc_w : pcr[0], pcr[31:1]};
        bufr <= {buf_we ? buf_in : bufr[0], bufr[7:1]};
        if (fsample)
            irs <= {irs[30:0], miso};
    end

    // ------------------------------------------------------------------------------------
    // Datapath state without reset
    // ------------------------------------------------------------------------------------
    always @(posedge clk) begin
        // ALU carry / shift delay / load sign
        if (st == S_LWR)
            cy <= (lwin && ph[2:0] == 3'd7) ? bufr[2] : cy;
        else
            cy <= cout;

        // compare / flags
        if (st == S_P1) begin
            if (c_br | c_slt)
                cmp <= ocmp;
            else if (c_mem && ph[4:1] == 4'd0)
                cmp <= eqacc;                                   // address[1:0] == 0
            else if (c_jalr && ph == 5'd1)
                cmp <= sum;                                     // target[1]
            else if (c_sh)
                cmp <= 1'b1;
        end else if (st == S_P2 && c_sh && ph == 5'd31)
            cmp <= 1'b0;

        // pc + 4 carry, shift fill
        if (st == S_P1 && c_sh && ph == 5'd31)
            c4 <= rs1_t;
        else if (!(st == S_P2 && c_sh))
            c4 <= (ph == 5'd1) | (c4 & pc_t);

        // bit counter / shift amount
        if (st == S_CMD)
            j <= 5'd16;
        else if ((slow & slot_end) | fsample)
            j <= j + 5'd1;
        else if (st == S_P1 && c_sh && ph < 5'd5)
            j <= {sum, j[4:1]};
        else if (st == S_P2 && c_sh && ph == 5'd31)
            j <= j + 5'd8;

        // MOSI: ring bits in the emitting states, else the command (0x03 / 0x02) or 0. It only
        // changes at edges where SCK stays low or falls.
        if (emit) begin
            if (d == 3'd7)
                mosi <= bufr[0];
        end else
            mosi <= (st == S_CMD) & ((ph[4:1] == 4'd9) | ((ph[4:1] == 4'd10) & ~(mem & c_st)));
    end

    // ------------------------------------------------------------------------------------
    // Control (the only flops with a reset, plus uo_out)
    // ------------------------------------------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            st   <= S_CLR;
            ph   <= 5'd0;
            cs_n <= 1'b1;
            sck  <= 1'b0;
            mem  <= 1'b0;
            gpo  <= 8'd0;
        end else begin
            ph  <= ph + 5'd1;
            sck <= fast ? ph[0] : slow ? (d == 3'd0) : 1'b0;
            case (st)
                S_IDLE: if (ph == 5'd6) begin
                    cs_n <= 1'b0;
                    st   <= S_CMD;
                end
                S_CMD:  if (ph == 5'd22) st <= S_AHI;
                S_AHI:  if (byte_end) st <= S_ALD;              // ph 15
                S_ALD:  if (ph == 5'd14) st <= S_ALO;
                // ALO ends at ph 6 with j = 0: the next cycle is already a slot start (d = 7)
                // (stores load data byte 0 into bufr during the last slot)
                S_ALO:  if (byte_end) st <= ~mem ? S_FDAT : c_st ? S_SLD : S_LRX;
                S_FDAT: if (fsample && j == 5'd31) st <= S_FEND;
                S_FEND: begin
                    cs_n <= 1'b1;
                    if (ph == 5'd31) st <= trap_dec ? S_RET : S_P1;
                end
                S_P1: if (ph == 5'd31) begin
                    // sum = address[31]: RAM accesses pass through P2 for one cycle
                    if (c_mem) mem <= ~sum;
                    st <= (c_slt | c_br | c_jalr | c_mem | (c_sh & (j != 5'd0))) ? S_P2 : S_RET;
                end
                S_P2: begin
                    if (mem)
                        st <= S_IDLE;                           // RAM access: start the SPI
                    else if (ph == 5'd31 && !(c_sh && !sh_last))
                        st <= S_RET;
                end
                S_SLD: if (ph[4:3] == j[4:3] + 2'd1 && ph[2:0] == 3'd6) st <= S_SEM;
                S_SEM: if (byte_end) begin
                    if (j[4:3] == nm1) begin
                        cs_n <= 1'b1;
                        st   <= S_RETW;
                    end else
                        st <= S_SLD;
                end
                S_LRX: if (byte_end) begin
                    if (j[4:3] == nm1) cs_n <= 1'b1;
                    st <= S_LWW;
                end
                S_LWW: st <= S_LWR;
                S_LWR: begin
                    if (ld_last) begin
                        if (ph == 5'd31) st <= S_RET;
                    end else if (ph[4:3] == j[4:3] && ph[2:0] == 3'd6)
                        st <= S_LRX;
                end
                S_RETW: if (ph == 5'd31) st <= S_RET;
                S_CLR:  if (ph == 5'd31) st <= S_IDLE;
                S_RET: begin
                    if (c_st && !mem && cmp && !trap)
                        gpo <= bufr;
                    mem <= 1'b0;
                    st  <= trap ? S_HALT : S_IDLE;
                end
                default: st <= S_HALT;
            endcase
        end
    end

    // ------------------------------------------------------------------------------------
    // RVFI (formal and cosim builds only; observes, never drives the pins). Retirement is
    // at ph = 0, where every ring holds its bits in order.
    // ------------------------------------------------------------------------------------
`ifdef RISCV_FORMAL
    wire [31:0] xr [0:15];
    assign xr[0] = 32'd0;
    generate
        for (gi = 1; gi < 16; gi = gi + 1) begin : g_xr
            assign xr[gi] = rfq[32*gi-32 +: 32];
        end
    endgenerate

    reg [63:0] r_order;
    reg [31:0] r_rs1, r_rs2, r_pc, r_mrd;
    wire [31:0] v_rs1 = xr[rs1_f];
    wire [31:0] v_rs2 = xr[rs2_f];
    always @(posedge clk) begin
        if (!rst_n)
            r_order <= 64'd0;
        else if (st == S_RET)
            r_order <= r_order + 64'd1;
        // at ph 31 every ring is rotated by one
        if (st == S_FEND && ph == 5'd31) begin
            r_rs1 <= {v_rs1[0], v_rs1[31:1]};
            r_rs2 <= {v_rs2[0], v_rs2[31:1]};
            r_pc  <= {pcr[0], pcr[31:1]};
        end
        if (st == S_LWR && lwin)
            r_mrd[{kb, ph[2:0]}] <= bufr[2];
    end

    wire [31:0] imm_i = {{20{insn[31]}}, insn[31:20]};
    wire [31:0] imm_s = {{20{insn[31]}}, insn[31:25], insn[11:7]};
    wire [31:0] imm_b = {{20{insn[31]}}, insn[7], insn[30:25], insn[11:8], 1'b0};
    wire [31:0] imm_j = {{12{insn[31]}}, insn[19:12], insn[20], insn[30:21], 1'b0};
    wire [31:0] jalr_t = (r_rs1 + imm_i) & 32'hFFFF_FFFE;
    wire [31:0] npc_t  = is_jal ? r_pc + imm_j : is_jalr ? jalr_t :
                         (is_branch & (cmp ^ f3[0])) ? r_pc + imm_b : r_pc + 32'd4;

    wire        r_rs1e = legal & uses_rs1;
    wire        r_rs2e = legal & uses_rs2;
    wire        r_rde  = legal & uses_rd & (rd_f != 4'd0) & ~trap;
    wire        r_mem  = ~trap & (is_load | is_store);
    wire [31:0] maddr  = r_rs1 + (is_store ? imm_s : imm_i);
    wire [3:0]  r_mask = (f3[1:0] == 2'b00) ? 4'b0001 : (f3[1:0] == 2'b01) ? 4'b0011 : 4'b1111;
    wire [31:0] lmask  = {{8{r_mask[3]}}, {8{r_mask[2]}}, {8{r_mask[1]}}, {8{r_mask[0]}}};
    wire [31:0] mrd    = maddr[31] ? {24'd0, cmp ? ui_in : 8'd0} : r_mrd;

    assign rvfi_valid     = (st == S_RET);
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
    assign rvfi_rd_wdata  = r_rde ? xr[rd_f] : 32'd0;
    assign rvfi_pc_rdata  = r_pc;
    assign rvfi_pc_wdata  = trap ? npc_t : pcr;
    assign rvfi_mem_addr  = r_mem ? maddr : 32'd0;
    assign rvfi_mem_rmask = (r_mem & is_load) ? r_mask : 4'd0;
    assign rvfi_mem_wmask = (r_mem & is_store) ? r_mask : 4'd0;
    assign rvfi_mem_rdata = (r_mem & is_load) ? (mrd & lmask) : 32'd0;
    assign rvfi_mem_wdata = (r_mem & is_store) ? (r_rs2 & lmask) : 32'd0;
`endif

endmodule

`default_nettype wire
