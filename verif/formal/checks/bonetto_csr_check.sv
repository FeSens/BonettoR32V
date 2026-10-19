// BonettoR32V CSR, trap and interrupt check (contract v3 sections 4 and 8). Contract v3 has no
// rvfi_csr_* ports (and the lead ruled against adding them), so riscv-formal's csrw/csrc checks
// cannot run. Instead this check keeps a shadow of the contract-4 CSR state, built from the
// retirement stream alone, and compares every observation with it:
//
//   state           mstatus.MIE, mstatus.MPIE, mie.MEIE, mtvec[31:2], mscratch, mepc[31:2],
//                   mcause {31, 3:0}; each bit has a known flag. After reset MIE, MPIE and MEIE
//                   are known 0 (contract 4); mtvec, mscratch, mepc and mcause are unknown
//                   (arbitrary reset values).
//   learning        an unknown bit takes the first value observed for it (a CSR read's rd_wdata,
//                   a trap's pc_wdata for mtvec, an MRET's pc_wdata for mepc, an interrupt
//                   entry's pc_rdata for mtvec); every later observation must agree. So CSR
//                   state may start arbitrary but never changes by itself.
//   updates         CSR instructions (contract-4 masks; CSRRS/CSRRC with rs1 = x0 or zimm = 0
//                   do not write; a set or clear of an unknown bit stays unknown unless the
//                   source bit decides it), trap entry (mepc = pc, mcause = the cause, MPIE =
//                   MIE, MIE = 0), interrupt entry (mepc = the interrupted pc, i.e. the previous
//                   retirement's pc_wdata, mcause = 0x8000_000B, MPIE = MIE, MIE = 0) and MRET
//                   (MIE = MPIE, MPIE = 1).
//   havoc           a reserved encoding (bon_spec = 0: not one of the specified instructions,
//                   which includes CSR instructions on unlisted numbers and writes to the
//                   read-only id CSRs) may have any consistent effect (contract 4): every shadow
//                   bit becomes unknown after it.
//
// Assertions, at each compared retirement (before the UNSPECIFIED stop):
//   csr_read          a specified CSR instruction with rd != x0: rd_wdata is the old value:
//                     known shadow bits, MPP = 3, every other bit 0 (misa, mtval, the id CSRs
//                     and mip read 0, except mip.MEIP, the interrupt line, which is free here)
//   csr_trap_target   a trapping retirement's pc_wdata = mtvec (bits 1:0 = 0)
//   csr_mret_target   an MRET's pc_wdata = mepc (bits 1:0 = 0)
//   csr_irq_enabled   an interrupt entry (rvfi_intr = 1 not right after a trapping retirement,
//                     contract 8) happens only with MIE = MEIE = 1 (known bits)
//   csr_irq_target    ... and its pc_rdata = mtvec (bits 1:0 = 0)
// Not checked here: mip.MEIP against the interrupt line (the line is internal); that an
// interrupt is taken while MIE, MEIE and MEIP hold (it needs the line); WFI's stall (liveness).

`include "bonetto_macros.vh"

module bonetto_csr_check (
	`BONETTO_CHECKER_PORTS
);
	wire full = !reset && rvfi_valid && `BONETTO_FULL;

	// Decode.
	wire [31:0] insn = rvfi_insn;
	wire sys = insn[6:0] == 7'b1110011;
	wire [2:0] f3 = insn[14:12];
	wire [11:0] num = insn[31:20];
	wire [4:0] rs1f = insn[19:15];
	wire [4:0] rdf = insn[11:7];
	wire [1:0] op = f3[1:0];
	wire csri = sys && f3 != 3'd0 && f3 != 3'd4;
	wire writes = op == 2'd1 || rs1f != 5'd0;
	// A specified CSR instruction (the global decode: listed number, E fields, no write to the
	// read-only id CSRs).
	wire spec_csr = csri && bon_spec;
	wire is_mret = insn == 32'h30200073;
	wire [31:0] src = f3[2] ? {27'd0, rs1f} : (rs1f == 5'd0 ? 32'd0 : rvfi_rs1_rdata);
	wire c_mstatus = num == 12'h300, c_mie = num == 12'h304, c_mtvec = num == 12'h305,
	     c_mscratch = num == 12'h340, c_mepc = num == 12'h341, c_mcause = num == 12'h342,
	     c_mip = num == 12'h344;
	// mcause of a synchronous trap (contract 4), for a specified trapping instruction.
	wire t_ill = insn == 32'h00000000 || insn == 32'hffffffff;
	wire t_ecall = insn == 32'h00000073, t_ebreak = insn == 32'h00100073;
	wire t_jump = insn[6:0] == 7'b1101111 || insn[6:0] == 7'b1100111 || insn[6:0] == 7'b1100011;
	wire t_known = t_ill || t_ecall || t_ebreak || t_jump;
	wire [3:0] t_cause = t_ill ? 4'd2 : t_ecall ? 4'd11 : t_ebreak ? 4'd3 : 4'd0;

	// Shadow state.
	reg mie, mie_k, mpie, mpie_k, meie, meie_k;
	reg [31:2] tvec, tvec_k, epc, epc_k;
	reg [31:0] scr, scr_k;
	reg [4:0] cause, cause_k;            // {mcause[31], mcause[3:0]}
	reg prev_v, prev_trap;
	reg [31:0] prev_pcw;
	// Which CSRs have been written by a CSR instruction (w_*) or set by a trap entry (t_*),
	// for the triggers only.
	reg [5:0] w_csr;   // mstatus, mie, mtvec, mscratch, mepc, mcause
	reg t_set;

	// Stage A: an interrupt entry before this retirement (contract 8: the handler's first
	// instruction carries rvfi_intr = 1; after a synchronous trap it carries it too, but the
	// trap's own retirement already applied the entry).
	wire ient = rvfi_intr && !(prev_v && prev_trap);
	wire a_mie = ient ? 1'b0 : mie;
	wire a_mie_k = ient ? 1'b1 : mie_k;
	wire a_mpie = ient ? mie : mpie;
	wire a_mpie_k = ient ? mie_k : mpie_k;
	wire [31:2] a_tvec = ient ? ((tvec & tvec_k) | (rvfi_pc_rdata[31:2] & ~tvec_k)) : tvec;
	wire [31:2] a_tvec_k = ient ? {30{1'b1}} : tvec_k;
	wire [31:2] a_epc = ient ? prev_pcw[31:2] : epc;
	wire [31:2] a_epc_k = ient ? {30{prev_v}} : epc_k;
	wire [4:0] a_cause = ient ? 5'b11011 : cause;
	wire [4:0] a_cause_k = ient ? 5'b11111 : cause_k;

	// The read value (rdv) and which bits of it are known (rdk; fixed bits count as known).
	reg [31:0] rdv, rdk;
	always @* begin
		rdv = 32'd0;
		rdk = 32'hffffffff;
		if (c_mstatus) begin
			rdv = {19'd0, 2'b11, 3'd0, a_mpie, 3'd0, a_mie, 3'd0};
			rdk = ~32'h00000088 | {24'd0, a_mpie_k, 3'd0, a_mie_k, 3'd0};
		end else if (c_mie) begin
			rdv = {20'd0, meie, 11'd0};
			rdk = ~32'h00000800 | {20'd0, meie_k, 11'd0};
		end else if (c_mtvec) begin
			rdv = {a_tvec, 2'b00};
			rdk = {a_tvec_k, 2'b11};
		end else if (c_mscratch) begin
			rdv = scr;
			rdk = scr_k;
		end else if (c_mepc) begin
			rdv = {a_epc, 2'b00};
			rdk = {a_epc_k, 2'b11};
		end else if (c_mcause) begin
			rdv = {a_cause[4], 27'd0, a_cause[3:0]};
			rdk = {a_cause_k[4], 27'h7ffffff, a_cause_k[3:0]};
		end else if (c_mip) begin
			rdk = ~32'h00000800;   // MEIP is the interrupt line: not compared
		end
	end
	wire rd_read = spec_csr && !rvfi_trap && rdf != 5'd0;
	// The old value after learning from the read (only the stored bits matter below).
	wire [31:0] old_v = rd_read ? ((rdv & rdk) | (rvfi_rd_wdata & ~rdk)) : rdv;
	wire [31:0] old_k = rd_read ? 32'hffffffff : rdk;
	// The new value of a write, and which bits of it are known.
	wire [31:0] nv = (op == 2'd1) ? src : (op == 2'd2) ? (old_v | src) : (old_v & ~src);
	wire [31:0] nk = (op == 2'd1) ? 32'hffffffff : (old_k | src);

	always @(posedge clock) begin
		if (reset) begin
			mie <= 1'b0; mie_k <= 1'b1;
			mpie <= 1'b0; mpie_k <= 1'b1;
			meie <= 1'b0; meie_k <= 1'b1;
			tvec_k <= 30'd0;
			epc_k <= 30'd0;
			scr_k <= 32'd0;
			cause_k <= 5'd0;
			prev_v <= 1'b0;
			prev_trap <= 1'b0;
			w_csr <= 6'd0;
			t_set <= 1'b0;
		end else if (full) begin
			prev_v <= 1'b1;
			prev_trap <= rvfi_trap;
			prev_pcw <= rvfi_pc_wdata;
			// Start from stage A (the interrupt entry, if any).
			mie <= a_mie; mie_k <= a_mie_k;
			mpie <= a_mpie; mpie_k <= a_mpie_k;
			tvec <= a_tvec; tvec_k <= a_tvec_k;
			epc <= a_epc; epc_k <= a_epc_k;
			cause <= a_cause; cause_k <= a_cause_k;
			if (ient) t_set <= 1'b1;
			if (!bon_spec) begin
				// A reserved encoding: any consistent effect (contract 4).
				mie_k <= 1'b0; mpie_k <= 1'b0; meie_k <= 1'b0;
				tvec_k <= 30'd0; epc_k <= 30'd0; scr_k <= 32'd0; cause_k <= 5'd0;
				w_csr <= 6'd0;
				t_set <= 1'b0;
			end else if (rvfi_trap) begin
				// Synchronous trap entry; mtvec learns from the target.
				tvec <= (a_tvec & a_tvec_k) | (rvfi_pc_wdata[31:2] & ~a_tvec_k);
				tvec_k <= {30{1'b1}};
				epc <= rvfi_pc_rdata[31:2];
				epc_k <= {30{1'b1}};
				cause <= {1'b0, t_cause};
				cause_k <= {5{t_known}};
				mpie <= a_mie; mpie_k <= a_mie_k;
				mie <= 1'b0; mie_k <= 1'b1;
				t_set <= 1'b1;
			end else if (is_mret) begin
				epc <= (a_epc & a_epc_k) | (rvfi_pc_wdata[31:2] & ~a_epc_k);
				epc_k <= {30{1'b1}};
				mie <= a_mpie; mie_k <= a_mpie_k;
				mpie <= 1'b1; mpie_k <= 1'b1;
			end else if (spec_csr) begin
				// Learn from the read, then write (contract-4 masks).
				if (rd_read) begin
					if (c_mstatus) begin
						mie <= old_v[3]; mie_k <= 1'b1;
						mpie <= old_v[7]; mpie_k <= 1'b1;
					end
					if (c_mie) begin meie <= old_v[11]; meie_k <= 1'b1; end
					if (c_mtvec) begin tvec <= old_v[31:2]; tvec_k <= {30{1'b1}}; end
					if (c_mscratch) begin scr <= old_v; scr_k <= 32'hffffffff; end
					if (c_mepc) begin epc <= old_v[31:2]; epc_k <= {30{1'b1}}; end
					if (c_mcause) begin
						cause <= {old_v[31], old_v[3:0]};
						cause_k <= 5'b11111;
					end
				end
				if (writes) begin
					if (c_mstatus) begin
						mie <= nv[3]; mie_k <= nk[3];
						mpie <= nv[7]; mpie_k <= nk[7];
						w_csr[0] <= 1'b1;
					end
					if (c_mie) begin meie <= nv[11]; meie_k <= nk[11]; w_csr[1] <= 1'b1; end
					if (c_mtvec) begin tvec <= nv[31:2]; tvec_k <= nk[31:2]; w_csr[2] <= 1'b1; end
					if (c_mscratch) begin scr <= nv; scr_k <= nk; w_csr[3] <= 1'b1; end
					if (c_mepc) begin epc <= nv[31:2]; epc_k <= nk[31:2]; w_csr[4] <= 1'b1; end
					if (c_mcause) begin
						cause <= {nv[31], nv[3:0]};
						cause_k <= {nk[31], nk[3:0]};
						w_csr[5] <= 1'b1;
					end
				end
			end
		end
	end

	wire [5:0] which = {c_mcause, c_mepc, c_mscratch, c_mtvec, c_mie, c_mstatus};
	wire stored_known = |(rdk & (c_mstatus ? 32'h88 : c_mie ? 32'h800 : c_mtvec ? 32'hfffffffc :
	                             c_mscratch ? 32'hffffffff : c_mepc ? 32'hfffffffc :
	                             c_mcause ? 32'h8000000f : 32'h0));
	always @* begin
		if (full && check) begin
			if (ient) begin
				csr_irq_enabled: assert ((!mie_k || mie) && (!meie_k || meie));
				csr_irq_target: assert (((rvfi_pc_rdata[31:2] ^ tvec) & tvec_k) == 30'd0 &&
				                        rvfi_pc_rdata[1:0] == 2'b00);
			end
			if (rvfi_trap)
				csr_trap_target: assert (((rvfi_pc_wdata[31:2] ^ a_tvec) & a_tvec_k) == 30'd0 &&
				                         rvfi_pc_wdata[1:0] == 2'b00);
			if (is_mret && !rvfi_trap)
				csr_mret_target: assert (((rvfi_pc_wdata[31:2] ^ a_epc) & a_epc_k) == 30'd0 &&
				                         rvfi_pc_wdata[1:0] == 2'b00);
			if (rd_read) csr_read: assert (((rvfi_rd_wdata ^ rdv) & rdk) == 32'd0);
		end
		if (!reset) begin
			`BONETTO_REACH(reach_csr_read, full && rd_read && stored_known)
			`BONETTO_REACH(reach_csr_write_read, full && rd_read && stored_known &&
			                                     (w_csr & which) != 6'd0)
			`BONETTO_REACH(reach_csr_trap_read, full && rd_read && t_set &&
			                                    (c_mepc || c_mcause || c_mstatus))
			`BONETTO_REACH(reach_csr_trap, full && rvfi_trap && a_tvec_k != 30'd0)
			`BONETTO_REACH(reach_csr_mret, full && is_mret && !rvfi_trap && a_epc_k != 30'd0)
			`BONETTO_REACH(reach_csr_irq, full && ient)
		end
	end
endmodule
