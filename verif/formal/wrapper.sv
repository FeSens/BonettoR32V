// BonettoR32V formal wrapper (contract v3): the real Tiny Tapeout top module at its pins, on
// TT's QSPI Pmod.
//
// Contents:
//   rvfi_wrapper       instantiates tt_um_fesens_r32v (RISCV_FORMAL build), drives the
//                      environment (contract sections 1-3) and exports RVFI, the pins, the two
//                      chip monitors' events and the UNSPECIFIED-stop signals.
//   bonetto_qspi_chip  passive model of one Pmod chip's command state machine (contract 2.2
//                      flash W25Q128JV, 2.3 PSRAM APS6404L), a port of verif/cosim/pmod.h's
//                      QspiFlash / Psram tables: it decodes each transaction, flags illegal
//                      commands and sequences, and reports data-out edges and write bytes.
//
// Environment (assumptions):
//   - rst_n = !reset; the testbench holds reset for the first step (one rising edge with rst_n
//     low, the contract's minimum) or, in the reset task, 1..8 steps.
//   - ena = 1. ui_in is a free constant ($anyconst).
//   - uio_in is free every cycle ($anyseq): every line the core does not drive carries noise
//     (contract 2.1.5), and the read-back of a driven line is free too (no check relies on it).
//     The chips sample (uio_out & uio_oe) | (uio_in & ~uio_oe) at each rising SCK edge.
//   - Memory outputs (contract 2.1.5): from a data-out rising edge until SCK falls, the lines
//     the selected chip drives (SD1 for a single read, SD0-SD3 for a quad read) hold the value
//     they had at that edge. Outside such a high phase they are noise. What the chip drives at
//     the edge is free, except where a memory model below constrains it.
//   - Power-up modes (contract 2.4): one free constant per chip: flash standard or continuous
//     read; PSRAM in SPI mode or QPI mode, not yet reset (so all four combinations).
//   - The core region (PSRAM 0x7F_FF80-0x7F_FFFF, A[23] ignored) is a real memory in every task:
//     128 bytes with free initial contents (contract 3: arbitrary at reset). Each data-out edge
//     at a region byte drives that byte's bits; each completed write byte at a region byte
//     updates it. A core keeping x1..x15, the pc or CSRs there reads back what it wrote, so the
//     consistency checks cover the core plus that memory. Every other memory byte is noise,
//     which the coherence check constrains one symbolic byte at a time.
//
// UNSPECIFIED stop (contracts 3 and 4): the checks stop comparing at the first retirement whose
// fetch or data access is UNSPECIFIED (bon_unspec_ret, then bon_stopped): it touches the core
// region, fetches from IO, spans two areas or is a misaligned IO access. Only liveness and the
// bus rules apply after it. A specified instruction's data access comes from its spec address
// (rs1 + imm), never from what the core claims; a reserved encoding has no spec access, so its
// claimed access decides. Formal picks the instruction stream, so a core cannot use either to
// end the checks.

`include "bonetto_macros.vh"

module rvfi_wrapper (
	input clock,
	input reset,
	`RVFI_OUTPUTS,
	`BONETTO_OUTPUTS
);
	`rvformal_rand_const_reg [7:0] ui_in_c;
	`rvformal_rand_reg [7:0] uio_in_free;
	`rvformal_rand_const_reg flash_cont0;   // flash power-up: continuous read
	`rvformal_rand_const_reg psram_qpi0;    // PSRAM power-up: QPI mode (else SPI), not reset

	wire [7:0] uo_out, uio_out, uio_oe;
	wire [7:0] uio_in = uio_in_free;

	tt_um_fesens_r32v uut (
		.ui_in   (ui_in_c),
		.uo_out  (uo_out),
		.uio_in  (uio_in),
		.uio_out (uio_out),
		.uio_oe  (uio_oe),
		.ena     (1'b1),
		.clk     (clock),
		.rst_n   (!reset),
		// The contract's RVFI port set (section 8), connected by name. No riscv-formal feature
		// define (CSRs, MEM_FAULT, ...) is enabled, so the testbench's RVFI wires carry exactly
		// these signals.
		.rvfi_valid     (rvfi_valid),
		.rvfi_order     (rvfi_order),
		.rvfi_insn      (rvfi_insn),
		.rvfi_trap      (rvfi_trap),
		.rvfi_halt      (rvfi_halt),
		.rvfi_intr      (rvfi_intr),
		.rvfi_mode      (rvfi_mode),
		.rvfi_ixl       (rvfi_ixl),
		.rvfi_rs1_addr  (rvfi_rs1_addr),
		.rvfi_rs2_addr  (rvfi_rs2_addr),
		.rvfi_rs1_rdata (rvfi_rs1_rdata),
		.rvfi_rs2_rdata (rvfi_rs2_rdata),
		.rvfi_rd_addr   (rvfi_rd_addr),
		.rvfi_rd_wdata  (rvfi_rd_wdata),
		.rvfi_pc_rdata  (rvfi_pc_rdata),
		.rvfi_pc_wdata  (rvfi_pc_wdata),
		.rvfi_mem_addr  (rvfi_mem_addr),
		.rvfi_mem_rmask (rvfi_mem_rmask),
		.rvfi_mem_wmask (rvfi_mem_wmask),
		.rvfi_mem_rdata (rvfi_mem_rdata),
		.rvfi_mem_wdata (rvfi_mem_wdata)
	);

	// Steps since the start of the trace, saturating at 2. Step 0 is the initial state (before
	// any edge); the contract covers the pins from step 1.
	reg [1:0] age = 2'd0;
	always @(posedge clock) if (age != 2'd2) age <= age + 2'd1;
	assign mon_en = age != 2'd0;
	assign mon_pair = age == 2'd2;

	reg reset_q_r = 1'b0;
	always @(posedge clock) reset_q_r <= reset;
	assign reset_q = reset_q_r;

	reg [7:0] puio_out, puio_oe;
	always @(posedge clock) begin
		puio_out <= uio_out;
		puio_oe <= uio_oe;
	end

	assign pin_ui_in = ui_in_c;
	assign pin_uo_out = uo_out;
	assign pin_uio_in = uio_in;
	assign pin_uio_out = uio_out;
	assign pin_uio_oe = uio_oe;
	assign pin_puio_out = puio_out;
	assign pin_puio_oe = puio_oe;

	wire sck = uio_out[3], psck = puio_out[3];
	wire cf = uio_out[0], cr = uio_out[6];
	// A rising SCK edge (contract 2.1.4); a chip clocks it only while its CS_n is low.
	assign mon_rise = mon_pair && !psck && sck;
	// What the chips sample: driven lines carry uio_out, the others noise.
	wire [7:0] smp = (uio_out & uio_oe) | (uio_in & ~uio_oe);
	wire [3:0] nib = {smp[5], smp[4], smp[2], smp[1]};
	assign mon_sd = {uio_in[5], uio_in[4], uio_in[2], uio_in[1]};

	bonetto_qspi_chip #(.PSRAM(0)) flash (
		.clock(clock), .reset(reset), .en(mon_pair), .cs(cf), .rise(mon_rise), .nib(nib),
		.mode0(flash_cont0),
		.dout(f_dout), .dquad(f_dquad), .bad_cmd(f_bad_cmd), .bad_extra(f_bad_extra),
		.bad_wend(f_bad_wend), .rd(f_rd), .rd_addr(f_rd_addr), .rd_quad(f_rd_quad),
		.rd_idx(f_rd_idx), .wb(), .wb_addr(), .wb_data(), .fin(f_fin), .fin_rd(f_fin_rd),
		.fin_wr(f_fin_wr), .bad_trst(), .bad_cem()
	);
	wire [23:0] r_wb_addr24;
	bonetto_qspi_chip #(.PSRAM(1)) psram (
		.clock(clock), .reset(reset), .en(mon_pair), .cs(cr), .rise(mon_rise), .nib(nib),
		.mode0(psram_qpi0),
		.dout(r_dout), .dquad(r_dquad), .bad_cmd(r_bad_cmd), .bad_extra(r_bad_extra),
		.bad_wend(r_bad_wend), .rd(r_rd), .rd_addr(r_rd_addr), .rd_quad(r_rd_quad),
		.rd_idx(r_rd_idx), .wb(r_wb), .wb_addr(r_wb_addr24), .wb_data(r_wb_data), .fin(r_fin),
		.fin_rd(r_fin_rd), .fin_wr(r_fin_wr), .bad_trst(r_bad_trst), .bad_cem(r_bad_cem)
	);
	assign r_wb_addr = r_wb_addr24[22:0];

	// Memory outputs (contract 2.1.5): from the data-out rising edge until SCK falls, the lines
	// the chip drives keep the value they had at that edge.
	reg hold = 1'b0;
	reg hold_q;
	reg [3:0] hold_v;
	always @(posedge clock) begin
		if (reset) begin
			hold <= 1'b0;
		end else if ((f_rd && !cf) || (r_rd && !cr)) begin
			hold <= 1'b1;
			hold_q <= (f_rd && !cf) ? f_rd_quad : r_rd_quad;
			hold_v <= mon_sd;
		end else if (!sck || (cf && cr)) begin
			hold <= 1'b0;
		end
	end
	always @* begin
		if (!reset && hold && sck && !(cf && cr)) begin
			if (hold_q) assume (mon_sd == hold_v);
			else assume (mon_sd[1] == hold_v[1]);
		end
	end

	// Core region memory (see the header). No initializer: free initial contents.
	reg [7:0] rmem [0:127];
	wire [7:0] rmem_rd = rmem[r_rd_addr[6:0]];
	wire r_rd_region = r_rd_addr[22:7] == 16'hffff;
	always @* begin
		if (!reset && r_rd && !cr && r_rd_region) begin
			if (r_rd_quad) assume (mon_sd == (r_rd_idx[0] ? rmem_rd[3:0] : rmem_rd[7:4]));
			else assume (mon_sd[1] == rmem_rd[3'd7 - r_rd_idx]);
		end
	end
	always @(posedge clock) begin
		if (!reset && r_wb && r_wb_addr[22:7] == 16'hffff)
			rmem[r_wb_addr[6:0]] <= r_wb_data;
	end

	// ---------------------------------------------------------------------------------------
	// The UNSPECIFIED stop (see the header) and decode helpers for the checkers.
	// ---------------------------------------------------------------------------------------
	wire isa_valid;
	rvfi_isa_rv32e isa_decode (
		.rvfi_valid(1'b1), .rvfi_insn(rvfi_insn), .rvfi_pc_rdata(rvfi_pc_rdata),
		.rvfi_rs1_rdata(rvfi_rs1_rdata), .rvfi_rs2_rdata(rvfi_rs2_rdata),
		.rvfi_mem_rdata(rvfi_mem_rdata), .spec_valid(isa_valid),
		.spec_trap(), .spec_rs1_addr(), .spec_rs2_addr(), .spec_rd_addr(), .spec_rd_wdata(),
		.spec_pc_wdata(), .spec_mem_addr(), .spec_mem_rmask(), .spec_mem_wmask(),
		.spec_mem_wdata()
	);
	wire spec_insn = isa_valid || rvfi_insn == 32'h00000000 || rvfi_insn == 32'hffffffff;
	assign bon_spec = spec_insn;
	wire [6:0] r_op = rvfi_insn[6:0];
	wire [2:0] r_f3 = rvfi_insn[14:12];
	wire r_ld = r_op == 7'b0000011 && (r_f3 == 3'd0 || r_f3 == 3'd1 || r_f3 == 3'd2 ||
	                                   r_f3 == 3'd4 || r_f3 == 3'd5);
	wire r_st = r_op == 7'b0100011 && (r_f3 == 3'd0 || r_f3 == 3'd1 || r_f3 == 3'd2);
	wire [1:0] r_nm1 = (r_f3[1:0] == 2'd0) ? 2'd0 : (r_f3[1:0] == 2'd1) ? 2'd1 : 2'd3;
	wire [31:0] r_rs1 = (rvfi_insn[19:15] == 5'd0) ? 32'd0 : rvfi_rs1_rdata;
	wire [31:0] r_imm = r_st ? {{20{rvfi_insn[31]}}, rvfi_insn[31:25], rvfi_insn[11:7]}
	                         : {{20{rvfi_insn[31]}}, rvfi_insn[31:20]};
	wire [31:0] r_addr = r_rs1 + r_imm;
	wire [3:0] c_mask = rvfi_mem_rmask | rvfi_mem_wmask;
	wire [1:0] c_nm1 = (c_mask == 4'b0001) ? 2'd0 : (c_mask == 4'b0011) ? 2'd1 : 2'd3;

	// An n-byte access at a (n - 1 = nm1): UNSPECIFIED iff a byte is in the core region, the
	// bytes span two areas, or it is an IO access that is not naturally aligned.
	function acc_unspec(input [31:0] a, input [1:0] nm1);
		reg [31:0] b1, b2, b3;
		reg r, s;
		begin
			b1 = a + 32'd1;
			b2 = a + 32'd2;
			b3 = a + 32'd3;
			r = `BON_REGION(a);
			s = 1'b0;
			if (nm1 != 2'd0) begin
				r = r || `BON_REGION(b1);
				s = s || `BON_AREA(b1) != `BON_AREA(a);
			end
			if (nm1 == 2'd3) begin
				r = r || `BON_REGION(b2) || `BON_REGION(b3);
				s = s || `BON_AREA(b2) != `BON_AREA(a) || `BON_AREA(b3) != `BON_AREA(a);
			end
			acc_unspec = r || s ||
			             (a[31] && ((nm1 == 2'd1 && a[0]) || (nm1 == 2'd3 && a[1:0] != 2'd0)));
		end
	endfunction

	wire spec_unspec = isa_valid && (r_ld || r_st) && acc_unspec(r_addr, r_nm1);
	wire claim_unspec = !spec_insn && !rvfi_trap && c_mask != 4'b0 &&
	                    acc_unspec(rvfi_mem_addr, c_nm1);
	wire fetch_unspec = rvfi_pc_rdata[31] || acc_unspec(rvfi_pc_rdata, 2'd3);
	assign bon_fetch_unspec = fetch_unspec;
	assign bon_unspec_ret = fetch_unspec || spec_unspec || claim_unspec;
	reg stopped_r = 1'b0;
	always @(posedge clock) begin
		if (reset) stopped_r <= 1'b0;
		else if (rvfi_valid && bon_unspec_ret) stopped_r <= 1'b1;
	end
	assign bon_stopped = stopped_r;

	// A branch (funct3 not 010/011) or JALR (funct3 000) whose used register fields are x0..x15:
	// on a trap its rs fields are truthful and checked (contract 8).
	assign bon_brj = (r_op == 7'b1100011 && r_f3 != 3'd2 && r_f3 != 3'd3 &&
	                  !rvfi_insn[19] && !rvfi_insn[24]) ||
	                 (r_op == 7'b1100111 && r_f3 == 3'd0 && !rvfi_insn[19] && !rvfi_insn[11]);
endmodule

// One chip of the QSPI Pmod (contract 2.2 for PSRAM = 0, 2.3 for PSRAM = 1): the command state
// machine of verif/cosim/pmod.h (QspiChip, QspiFlash, Psram), clocked by the rising SCK edges
// while its CS_n is low. Passive: it only reports what the chip does and flags what the
// contract forbids.
//   - Flash: standard mode (8-bit single command) or continuous read (no command: the
//     transaction starts with 0xEB's quad address). At CS_n rise, a transaction that clocked
//     both mode nibbles enters or stays in continuous read iff M[5:4] = 10, else leaves it.
//   - PSRAM: SPI mode (8-bit single command) or QPI mode (2-edge quad command); 0x35 enters
//     QPI, 0xF5 leaves it, 0x66 then 0x99 (consecutive PSRAM transactions with a complete
//     command field) resets it to SPI mode, initialized. Any read or write before that reset is
//     a violation. tRST: CS_n high for at least TRST cycles after 0x99; tCEM: CS_n low for at
//     most TCEM cycles (both from run_formal.py, at the signoff period).
//   - A transaction that ends inside its command field is ignored.
module bonetto_qspi_chip #(
	parameter PSRAM = 0
) (
	input clock,
	input reset,
	input en,              // step >= 2
	input cs,              // this chip's CS_n
	input rise,            // a rising SCK edge
	input [3:0] nib,       // {SD3, SD2, SD1, SD0} as the chip samples them
	input mode0,           // power-up mode: flash continuous read / PSRAM QPI
	output dout,           // in the data-out phase this step
	output dquad,
	output bad_cmd,
	output bad_extra,
	output bad_wend,
	output rd,
	output [23:0] rd_addr,
	output rd_quad,
	output [2:0] rd_idx,
	output wb,
	output [23:0] wb_addr,
	output [7:0] wb_data,
	output fin,
	output fin_rd,
	output fin_wr,
	output bad_trst,
	output bad_cem
);
	`ifndef BONETTO_TCEM
	`define BONETTO_TCEM 400
	`endif
	`ifndef BONETTO_TRST
	`define BONETTO_TRST 3
	`endif

	localparam P_CMD = 3'd0, P_ADDR = 3'd1, P_MODE = 3'd2, P_DUMMY = 3'd3, P_DATA = 3'd4,
	           P_NONE = 3'd5;
	// data kinds
	localparam K_NONE = 3'd0, K_OS = 3'd1, K_OQ = 3'd2, K_IS = 3'd3, K_IQ = 3'd4;

	// Chip state (power-up mode from mode0 at reset).
	reg cont;    // flash: continuous read
	reg qpi;     // PSRAM: QPI mode
	reg init;    // PSRAM: reset done (0x66, 0x99)
	reg rsten;   // PSRAM: the previous transaction with a command was 0x66
	reg rpend;   // PSRAM: a 0x99 ended and no transaction has started since
	reg [3:0] rcnt;   // steps since that 0x99 ended (saturating)
	reg [9:0] lowcnt; // PSRAM: consecutive steps with CS_n low (saturating)

	// Transaction state.
	reg pcs;
	reg [2:0] ph;
	reg [7:0] cmd;
	reg [4:0] cnt;     // edges in the current field
	reg aq;            // address field is quad (6 edges), else single (24)
	reg lm;            // a 2-edge mode field (flash 0xEB)
	reg [3:0] ld;      // dummy/wait edges
	reg [2:0] lk;      // data kind
	reg [23:0] addr;
	reg [7:0] mode;
	reg [1:0] mcnt;
	reg [2:0] didx;    // bit (single) or nibble (quad) index within the data byte
	reg [23:0] baddr;  // address of the current data byte
	reg [7:0] wsh;
	reg dseen;         // a data edge has happened (data-out phase started)
	reg wone;          // a write data byte has completed

	wire sel = en && rise && !cs;
	wire quadcmd = PSRAM && qpi;
	wire [7:0] cmd_n = quadcmd ? {cmd[3:0], nib} : {cmd[6:0], nib[0]};
	wire cmd_done = (ph == P_CMD) && (quadcmd ? cnt == 5'd1 : cnt == 5'd7);

	// Decode (one row of table 2.2 or 2.3) for the command completing this edge.
	reg dv;           // listed
	reg acc;          // an access (has data)
	reg d_aq, d_lm;
	reg [3:0] d_ld;
	reg [2:0] d_lk;
	reg d_none;       // no fields after the command
	always @* begin
		dv = 1'b1; acc = 1'b1; d_aq = 1'b0; d_lm = 1'b0; d_ld = 4'd0; d_lk = K_NONE;
		d_none = 1'b0;
		if (!PSRAM) begin
			case (cmd_n)
				8'h03: begin d_lk = K_OS; end
				8'h0b: begin d_ld = 4'd8; d_lk = K_OS; end
				8'h6b: begin d_ld = 4'd8; d_lk = K_OQ; end
				8'heb: begin d_aq = 1'b1; d_lm = 1'b1; d_ld = 4'd4; d_lk = K_OQ; end
				8'hff: begin d_none = 1'b1; acc = 1'b0; end
				8'h02: begin d_lk = K_IS; end
				default: dv = 1'b0;
			endcase
		end else if (!qpi) begin
			case (cmd_n)
				8'h03: begin d_lk = K_OS; end
				8'h0b: begin d_ld = 4'd8; d_lk = K_OS; end
				8'heb: begin d_aq = 1'b1; d_ld = 4'd6; d_lk = K_OQ; end
				8'h02: begin d_lk = K_IS; end
				8'h38: begin d_aq = 1'b1; d_lk = K_IQ; end
				8'h35, 8'h66, 8'h99: begin d_none = 1'b1; acc = 1'b0; end
				default: dv = 1'b0;
			endcase
		end else begin
			case (cmd_n)
				8'h0b: begin d_aq = 1'b1; d_ld = 4'd4; d_lk = K_OQ; end
				8'heb: begin d_aq = 1'b1; d_ld = 4'd6; d_lk = K_OQ; end
				8'h02, 8'h38: begin d_aq = 1'b1; d_lk = K_IQ; end
				8'hf5, 8'h66, 8'h99: begin d_none = 1'b1; acc = 1'b0; end
				default: dv = 1'b0;
			endcase
		end
	end
	wire legal = dv && (!PSRAM || ((rsten ? cmd_n == 8'h99 : cmd_n != 8'h99) && (!acc || init)));
	assign bad_cmd = sel && cmd_done && !legal;
	assign bad_extra = sel && ph == P_NONE;

	// The field after the address / mode / dummy fields.
	function [2:0] after_addr(input m, input [3:0] d);
		after_addr = m ? P_MODE : (d != 4'd0) ? P_DUMMY : P_DATA;
	endfunction

	wire out_k = lk == K_OS || lk == K_OQ;
	wire in_k = lk == K_IS || lk == K_IQ;
	wire dq = lk == K_OQ || lk == K_IQ;
	wire [23:0] addr_n = aq ? {addr[19:0], nib} : {addr[22:0], nib[0]};
	wire addr_last = aq ? cnt == 5'd5 : cnt == 5'd23;
	wire byte_last = dq ? didx == 3'd1 : didx == 3'd7;
	wire [7:0] wsh_n = dq ? {wsh[3:0], nib} : {wsh[6:0], nib[0]};
	wire [23:0] binc = PSRAM ? {1'b0, baddr[22:0] + 23'd1} : baddr + 24'd1;

	assign rd = sel && ph == P_DATA && out_k;
	assign rd_addr = baddr;
	assign rd_quad = dq;
	assign rd_idx = didx;
	assign wb = sel && ph == P_DATA && in_k && byte_last;
	assign wb_addr = PSRAM ? {1'b0, baddr[22:0]} : baddr;
	assign wb_data = wsh_n;
	assign dout = en && !cs && out_k && (dseen || rd);
	assign dquad = dq;

	// CS_n rose this step.
	wire endt = en && !pcs && cs;
	wire known = ph != P_CMD;
	assign fin = endt && known;
	assign fin_rd = fin && out_k && dseen;
	assign fin_wr = fin && in_k && wone;
	assign bad_wend = endt && known && in_k && (ph == P_ADDR || (ph == P_DATA && didx != 3'd0));

	// PSRAM timing: tRST at the CS_n fall, tCEM while it is low.
	wire fall = en && pcs && !cs;
	assign bad_trst = PSRAM && fall && rpend && rcnt < `BONETTO_TRST;
	wire [9:0] low_now = pcs ? 10'd1 : lowcnt + 10'd1;
	assign bad_cem = PSRAM && en && !cs && low_now > `BONETTO_TCEM;

	// Mode updates when CS_n rises after a complete command field.
	wire f_cont_n = (fin && !PSRAM && mcnt == 2'd2) ? (mode[5:4] == 2'b10) : cont;
	wire p_qpi_n = (fin && PSRAM) ? ((cmd == 8'h35) ? 1'b1 : (cmd == 8'hf5 || cmd == 8'h99) ? 1'b0
	                                                         : qpi) : qpi;

	always @(posedge clock) begin
		pcs <= cs;
		if (reset) begin
			cont <= PSRAM ? 1'b0 : mode0;
			qpi <= PSRAM ? mode0 : 1'b0;
			init <= 1'b0;
			rsten <= 1'b0;
			rpend <= 1'b0;
			rcnt <= 4'd0;
			lowcnt <= 10'd0;
			pcs <= 1'b1;
		end else begin
			cont <= f_cont_n;
			qpi <= p_qpi_n;
			if (fin && PSRAM) begin
				rsten <= cmd == 8'h66;
				if (cmd == 8'h99) init <= 1'b1;
			end
			if (fin && PSRAM && cmd == 8'h99) begin
				rpend <= 1'b1;
				rcnt <= 4'd1;
			end else begin
				if (fall) rpend <= 1'b0;
				if (rcnt != 4'hf) rcnt <= rcnt + 4'd1;
			end
			if (!cs) lowcnt <= (low_now == 10'h3ff) ? 10'h3ff : low_now;
		end

		if (reset || cs) begin
			// Idle: the next transaction starts here. A flash in continuous read takes no
			// command (0xEB's fields from the quad address on).
			cnt <= 5'd0;
			mcnt <= 2'd0;
			didx <= 3'd0;
			dseen <= 1'b0;
			wone <= 1'b0;
			if (!PSRAM && (reset ? mode0 : f_cont_n)) begin
				ph <= P_ADDR;
				cmd <= 8'heb;
				aq <= 1'b1; lm <= 1'b1; ld <= 4'd4; lk <= K_OQ;
			end else begin
				ph <= P_CMD;
				cmd <= 8'h00;
				aq <= 1'b0; lm <= 1'b0; ld <= 4'd0; lk <= K_NONE;
			end
		end else if (sel) begin
			case (ph)
				P_CMD: begin
					cmd <= cmd_n;
					cnt <= cnt + 5'd1;
					if (cmd_done) begin
						cnt <= 5'd0;
						aq <= d_aq; lm <= d_lm; ld <= d_ld; lk <= d_lk;
						ph <= d_none ? P_NONE : P_ADDR;
					end
				end
				P_ADDR: begin
					addr <= addr_n;
					cnt <= cnt + 5'd1;
					if (addr_last) begin
						cnt <= 5'd0;
						baddr <= addr_n;
						ph <= after_addr(lm, ld);
					end
				end
				P_MODE: begin
					mode <= {mode[3:0], nib};
					mcnt <= mcnt + 2'd1;
					if (mcnt == 2'd1) ph <= (ld != 4'd0) ? P_DUMMY : P_DATA;
				end
				P_DUMMY: begin
					cnt <= cnt + 5'd1;
					if (cnt == {1'b0, ld} - 5'd1) begin
						cnt <= 5'd0;
						ph <= P_DATA;
					end
				end
				P_DATA: begin
					dseen <= 1'b1;
					wsh <= wsh_n;
					didx <= byte_last ? 3'd0 : didx + 3'd1;
					if (byte_last) baddr <= binc;
					if (byte_last && in_k) wone <= 1'b1;
				end
				default: ;
			endcase
		end
	end
endmodule
