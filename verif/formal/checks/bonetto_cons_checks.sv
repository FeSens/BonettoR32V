// BonettoR32V consistency checks. reg, pc_fwd, causal and unique follow riscv-formal's
// checks/rvfi_{reg,pc_fwd,causal,unique}_check.sv (Copyright (C) 2017 Claire Xenia
// Wolf, ISC license, see third_party/riscv-formal/COPYING), specialised to NRET = 1
// and rewritten for the uniform checker interface. The free `check` input selects
// the retirement to check (RISCV_FORMAL_UNBOUNDED style), so BMC checks every
// retirement within the depth. These checks apply to every retirement, reserved
// encodings included (contract section 4: the RVFI trace stays consistent), up to
// the first region-touching retirement (contract section 3: bon_region_ret, then
// bon_stopped). At that retirement only the fields that decide the stop are checked:
// its pc_rdata (pc_fwd, pc_bwd, order) and, for a data access, its rs1 read (reg).

`include "bonetto_macros.vh"

// reg: a register read returns the last value written to it (writes of trapping
// instructions excluded, as in stock). Reads of a trapping retirement are compared
// only for a branch or JALR (contract section 6: its operands decide the trap). Fork:
// a register that has not been written yet must read the same value every time (the
// value seen on its first read): the register file (on the die or in the core region)
// may start unknown but does not change by itself. Stock only checks reads after a
// write. At a region-touching retirement only the rs1 read of a data access is
// compared (its spec address decides the stop), and its write is not recorded.
module bonetto_reg_check (
	`BONETTO_CHECKER_PORTS
);
	`rvformal_rand_const_reg [4:0] register_index;
	reg [31:0] shadow;
	reg known;

	wire valid = !reset && rvfi_valid && !bon_stopped;
	wire full = !bon_region_ret;
	wire rds = !rvfi_trap || bon_brj;
	wire rd1 = rds && rvfi_rs1_addr == register_index && (full || !bon_fetch_touch);
	wire rd2 = rds && rvfi_rs2_addr == register_index && full;
	wire wr = full && !rvfi_trap && rvfi_rd_addr == register_index;

	always @(posedge clock) begin
		if (reset) begin
			known <= 1'b0;
		end else if (valid) begin
			if (wr) begin
				shadow <= rvfi_rd_wdata;
				known <= 1'b1;
			end else if (!known && rd1) begin
				shadow <= rvfi_rs1_rdata;
				known <= 1'b1;
			end else if (!known && rd2) begin
				shadow <= rvfi_rs2_rdata;
				known <= 1'b1;
			end
		end
	end

	always @* begin
		if (valid && check) begin
			if (known && rd1) reg_rs1: assert (rvfi_rs1_rdata == shadow);
			if (known && rd2) reg_rs2: assert (rvfi_rs2_rdata == shadow);
			if (rd1 && rd2) reg_rs1_rs2: assert (rvfi_rs1_rdata == rvfi_rs2_rdata);
		end
		if (!reset) begin
			`BONETTO_REACH(reach_reg, valid && full && known && (rd1 || rd2))
		end
	end
endmodule

// pc_fwd (stock semantics): the pc_rdata of the retirement with order N equals the
// pc_wdata of the retirement with order N-1.
module bonetto_pc_fwd_check (
	`BONETTO_CHECKER_PORTS
);
	`rvformal_rand_const_reg [63:0] insn_order;
	reg [31:0] expect_pc;
	reg expect_valid;
	wire valid = !reset && rvfi_valid && !bon_stopped;

	always @(posedge clock) begin
		if (reset) begin
			expect_valid <= 1'b0;
		end else if (valid && !bon_region_ret && rvfi_order == insn_order - 64'd1) begin
			expect_pc <= rvfi_pc_wdata;
			expect_valid <= 1'b1;
		end
	end

	always @* begin
		if (valid && check && rvfi_order == insn_order && expect_valid)
			pc_fwd: assert (rvfi_pc_rdata == expect_pc);
		if (!reset)
			`BONETTO_REACH(reach_pc_fwd, valid && rvfi_order == insn_order && expect_valid)
	end
endmodule

// pc_bwd (fork). Stock pc_bwd only fires when the retirement with order N+1 comes
// before the one with order N, which never happens with NRET = 1 and in-order
// retirement, so its assertion would be unreachable. The fork checks the
// time-adjacent pair instead: the previous retirement in time has order N-1 and
// its pc_wdata is this retirement's pc_rdata. Together with the order-from-0 rule in
// bonetto_rvfi_check this makes rvfi_order the retirement count.
module bonetto_pc_bwd_check (
	`BONETTO_CHECKER_PORTS
);
	reg [31:0] prev_pc_wdata;
	reg [63:0] prev_order;
	reg prev_valid;
	wire valid = !reset && rvfi_valid && !bon_stopped;

	always @(posedge clock) begin
		if (reset) begin
			prev_valid <= 1'b0;
		end else if (valid) begin
			prev_pc_wdata <= rvfi_pc_wdata;
			prev_order <= rvfi_order;
			prev_valid <= 1'b1;
		end
	end

	always @* begin
		if (valid && check && prev_valid) begin
			pc_bwd_order: assert (rvfi_order == prev_order + 64'd1);
			pc_bwd_pc: assert (rvfi_pc_rdata == prev_pc_wdata);
		end
		if (!reset)
			`BONETTO_REACH(reach_pc_bwd, valid && prev_valid)
	end
endmodule

// causal (stock semantics): a register written by the retirement with order N is
// not read by any retirement with a larger order that retired before it. Trapping
// retirements write nothing, and only a trapping branch or JALR has its reads
// compared (contract section 6).
module bonetto_causal_check (
	`BONETTO_CHECKER_PORTS
);
	`rvformal_rand_const_reg [63:0] insn_order;
	`rvformal_rand_const_reg [4:0] register_index;
	reg found_non_causal;
	wire valid = !reset && rvfi_valid && `BONETTO_FULL;

	always @(posedge clock) begin
		if (reset) begin
			found_non_causal <= 1'b0;
		end else if (valid && (!rvfi_trap || bon_brj) && rvfi_order > insn_order &&
				(rvfi_rs1_addr == register_index || rvfi_rs2_addr == register_index)) begin
			found_non_causal <= 1'b1;
		end
	end

	wire trigger = valid && !rvfi_trap && register_index != 0 && rvfi_rd_addr == register_index &&
			rvfi_order == insn_order;
	always @* begin
		if (check && trigger) causal: assert (!found_non_causal);
		if (!reset) `BONETTO_REACH(reach_causal, trigger)
	end
endmodule

// unique (fork): no two retirements share an rvfi_order. Stock unique relies on a
// trig that fires once before check; with a free trig (unbounded mode) its first
// non-trig retirement sets found_other_insn, so it fails on any core. The fork
// counts retirements with order N directly.
module bonetto_unique_check (
	`BONETTO_CHECKER_PORTS
);
	`rvformal_rand_const_reg [63:0] insn_order;
	reg seen;
	reg any;
	wire valid = !reset && rvfi_valid && !bon_stopped;

	always @(posedge clock) begin
		if (reset) begin
			seen <= 1'b0;
			any <= 1'b0;
		end else if (valid) begin
			any <= 1'b1;
			if (rvfi_order == insn_order) seen <= 1'b1;
		end
	end

	always @* begin
		if (valid && check && rvfi_order == insn_order) unique_order: assert (!seen);
		if (!reset) `BONETTO_REACH(reach_unique, valid && rvfi_order == insn_order && any)
	end
endmodule

// liveness (fork). Contract section 4 latency cap, with its edge count: edge 0 is
// the last rising edge at which rst_n is sampled low; the first retirement comes at an
// edge <= 1,024 and each later one at most 1,024 edges after the previous; a halted
// core is exempt (the halting retirement starts no window). A retirement "at edge e"
// is rvfi_valid high after edge e (X(e) = 1 in the contract notation), as the cosim
// harness counts it (it reads the outputs right after evaluating edge e).
// Step/edge map: BMC step s holds X(s-1). Reset is step 0, so edge 0 is the transition
// out of step 0, and a retirement at edge e is rvfi_valid = 1 in step e + 1. gap is 0
// in step 1 and counts up by one per step without a retirement; after a retirement in
// step r it is 1 in step r + 1. So in step s, gap = (s - 1) - (edge of the last
// retirement, or 0) = the edge count since edge 0 or since the last retirement. The
// assertion fires when gap reaches 1,024 without rvfi_valid: a first retirement at
// edge 1,024 (step 1,025), or one 1,024 edges after the previous, passes; one edge
// later fails. The stall_* mutant pairs pin both boundaries.
// This is a safety property; BMC covers only windows that close within the depth
// (see docs/formal.md). Stock liveness fires `check` right after `trig` in unbounded
// mode.
module bonetto_liveness_check (
	`BONETTO_CHECKER_PORTS
);
	reg [10:0] gap;
	reg halted;

	always @(posedge clock) begin
		if (reset) begin
			gap <= 11'd0;
			halted <= 1'b0;
		end else begin
			if (rvfi_valid) gap <= 11'd1;
			else if (gap != 11'h7ff) gap <= gap + 11'd1;
			if (rvfi_valid && rvfi_halt) halted <= 1'b1;
		end
	end

	always @* begin
		if (!reset && !halted && gap >= 11'd1024) liveness: assert (rvfi_valid);
		if (!reset) `BONETTO_REACH(reach_liveness, rvfi_valid && !rvfi_halt)
	end
endmodule

// ill (fork): stock covers only 0x00000000. Contract section 4: the all-zeros and
// all-ones words trap and halt, with no register write and no memory write (the
// fields compared on a trapping retirement, contract section 6).
module bonetto_ill_check (
	`BONETTO_CHECKER_PORTS
);
	wire valid = !reset && rvfi_valid && `BONETTO_FULL;
	wire ill = rvfi_insn == 32'h00000000 || rvfi_insn == 32'hffffffff;

	always @* begin
		if (valid && check && ill) begin
			ill_trap: assert (rvfi_trap);
			ill_halt: assert (rvfi_halt);
			ill_rd_addr: assert (rvfi_rd_addr == 0);
			ill_wmask: assert (rvfi_mem_wmask == 0);
		end
		if (!reset) begin
			`BONETTO_REACH(reach_ill_zero, valid && rvfi_insn == 32'h00000000)
			`BONETTO_REACH(reach_ill_ones, valid && rvfi_insn == 32'hffffffff)
		end
	end
endmodule

// silent (new): after a compared retirement with rvfi_halt the core never retires
// again, never starts another SPI WRITE, core region included (contract section 4:
// nothing after the halt), and uo_out never changes (no IO store can retire).
module bonetto_silent_check (
	`BONETTO_CHECKER_PORTS
);
	reg halted;
	reg [7:0] prev_uo;

	always @(posedge clock) begin
		prev_uo <= pin_uo_out;
		if (reset) halted <= 1'b0;
		else if (rvfi_valid && rvfi_halt && `BONETTO_FULL) halted <= 1'b1;
	end

	always @* begin
		if (!reset && halted) begin
			silent_retire: assert (!rvfi_valid);
			silent_write: assert (!(ev_cmd_done && ev_cmd == 8'h02));
			silent_uo: assert (pin_uo_out == prev_uo);
		end
		if (!reset) `BONETTO_REACH(reach_silent, halted)
	end
endmodule

// rvfi (new): the RVFI conventions of contract section 6 and the trap rules of
// section 4 that hold for every retirement, reserved encodings included.
module bonetto_rvfi_check (
	`BONETTO_CHECKER_PORTS
);
	reg seen;
	reg [63:0] last_order;
	wire valid = !reset && rvfi_valid && `BONETTO_FULL;

	function mask_ok(input [3:0] m);
		mask_ok = m == 4'b0000 || m == 4'b0001 || m == 4'b0011 || m == 4'b1111;
	endfunction

	always @(posedge clock) begin
		if (reset) begin
			seen <= 1'b0;
		end else if (valid) begin
			seen <= 1'b1;
			last_order <= rvfi_order;
		end
	end

	always @* begin
		if (valid && check) begin
			conv_intr: assert (rvfi_intr == 1'b0);
			conv_mode: assert (rvfi_mode == 2'd3);
			conv_ixl: assert (rvfi_ixl == 2'd1);
			// Register and read-mask fields of a trapping retirement are not
			// compared (contract section 6), except the register reads of a
			// trapping branch or JALR.
			if (!rvfi_trap || bon_brj) begin
				if (rvfi_rs1_addr == 0) conv_rs1_x0: assert (rvfi_rs1_rdata == 0);
				if (rvfi_rs2_addr == 0) conv_rs2_x0: assert (rvfi_rs2_rdata == 0);
			end
			if (!rvfi_trap) begin
				if (rvfi_rd_addr == 0) conv_rd_x0: assert (rvfi_rd_wdata == 0);
				conv_rmask: assert (mask_ok(rvfi_mem_rmask));
			end
			// Fatal traps retire with trap = halt = 1; reserved encodings either
			// retire or trap and halt; nothing halts without trapping.
			conv_trap_halt: assert (rvfi_trap == rvfi_halt);
			if (rvfi_trap) begin
				conv_trap_rd_addr: assert (rvfi_rd_addr == 0);
				conv_trap_wmask: assert (rvfi_mem_wmask == 0);
			end
			conv_wmask: assert (mask_ok(rvfi_mem_wmask));
			// rvfi_order counts retirements from 0; execution starts at pc = 0.
			if (!seen) begin
				conv_first_order: assert (rvfi_order == 64'd0);
				conv_first_pc: assert (rvfi_pc_rdata == 32'd0);
			end else begin
				conv_order_next: assert (rvfi_order == last_order + 64'd1);
			end
		end
		if (!reset) begin
			`BONETTO_REACH(reach_rvfi, valid && seen)
		end
	end
endmodule

// cover (riscv-formal's cover check): sanity covers that the core does anything
// at all within the depth. A pure reach task (no assertions of its own).
module bonetto_cover_check (
	`BONETTO_CHECKER_PORTS
);
	reg [1:0] n_retired;
	always @(posedge clock) begin
		if (reset) n_retired <= 2'd0;
		else if (rvfi_valid && n_retired != 2'd3) n_retired <= n_retired + 2'd1;
	end
	always @* begin
		if (!reset) begin
			`BONETTO_REACH(reach_cover_nontrap, rvfi_valid && !rvfi_trap)
			`BONETTO_REACH(reach_cover_trap, rvfi_valid && rvfi_trap && rvfi_halt)
			`BONETTO_REACH(reach_cover_second, rvfi_valid && n_retired == 2'd1)
		end
	end
endmodule

// reset: run under BONETTO_RESET_FREE (rst_n low for 1..8 edges). The SPI check
// covers the reset-state pins for every reset step; the rvfi check covers
// "execution starts at pc = 0" and order 0 after a long reset.
module bonetto_reset_check (
	`BONETTO_CHECKER_PORTS
);
	bonetto_spi_check spi (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_rvfi_check rvfi (`BONETTO_CHECKER_CONN(clock, reset, check));

	reg [3:0] reset_len = 4'd0;
	always @(posedge clock) if (reset && reset_len != 4'hf) reset_len <= reset_len + 4'd1;
	always @* begin
		if (!reset) `BONETTO_REACH(reach_reset, rvfi_valid && reset_len >= 4'd3)
	end
endmodule
