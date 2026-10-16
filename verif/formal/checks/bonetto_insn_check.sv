// BonettoR32V fork of riscv-formal checks/rvfi_insn_check.sv
// (Copyright (C) 2017 Claire Xenia Wolf, ISC license, see third_party/riscv-formal/COPYING).
//
// One instruction's semantics at every retirement of that instruction (free check).
// RISCV_FORMAL_INSN_MODEL is an E-filtered model from isa_rv32e.v, so encodings
// whose used register fields name x16-x31 are not checked here (they are reserved).
//
// Differences from the stock check (contract sections 4 and 6):
//   - rvfi_halt must equal the spec's trap (fatal traps halt; nothing else halts).
//   - A trapping retirement (contract section 6) is compared only on rvfi_trap,
//     rvfi_halt and the absence of side effects: rd_addr = 0 and wmask = 0. Its
//     register-read fields, rd_wdata, rmask and pc_wdata are not compared (insn and
//     pc_rdata are checked by the pc and bus checks).
//   - Register fields of a non-trapping retirement: rs1_addr / rs2_addr equal the
//     spec's (unused fields report 0).
//   - Memory masks are exact (LSB-aligned 1/3/15 for the access size, 0 otherwise);
//     stock tolerates extra mask bits.
//   - No PMA, fault or address-validity logic (every address is valid; misaligned
//     accesses are architecture, no RISCV_FORMAL_ALIGNED_MEM).

`include "bonetto_macros.vh"

module bonetto_insn_check (
	`BONETTO_CHECKER_PORTS
);
	wire valid = !reset && rvfi_valid;

	wire                                spec_valid;
	wire                                spec_trap;
	wire [                       4 : 0] spec_rs1_addr;
	wire [                       4 : 0] spec_rs2_addr;
	wire [                       4 : 0] spec_rd_addr;
	wire [`RISCV_FORMAL_XLEN   - 1 : 0] spec_rd_wdata;
	wire [`RISCV_FORMAL_XLEN   - 1 : 0] spec_pc_wdata;
	wire [`RISCV_FORMAL_XLEN   - 1 : 0] spec_mem_addr;
	wire [`RISCV_FORMAL_XLEN/8 - 1 : 0] spec_mem_rmask;
	wire [`RISCV_FORMAL_XLEN/8 - 1 : 0] spec_mem_wmask;
	wire [`RISCV_FORMAL_XLEN   - 1 : 0] spec_mem_wdata;

	wire [`RISCV_FORMAL_XLEN - 1 : 0] rs1_rdata_or_zero = spec_rs1_addr != 0 ? rvfi_rs1_rdata : 0;
	wire [`RISCV_FORMAL_XLEN - 1 : 0] rs2_rdata_or_zero = spec_rs2_addr != 0 ? rvfi_rs2_rdata : 0;

	`RISCV_FORMAL_INSN_MODEL insn_spec (
		.rvfi_valid     (valid            ),
		.rvfi_insn      (rvfi_insn        ),
		.rvfi_pc_rdata  (rvfi_pc_rdata    ),
		.rvfi_rs1_rdata (rs1_rdata_or_zero),
		.rvfi_rs2_rdata (rs2_rdata_or_zero),
		.rvfi_mem_rdata (rvfi_mem_rdata   ),
		.spec_valid     (spec_valid       ),
		.spec_trap      (spec_trap        ),
		.spec_rs1_addr  (spec_rs1_addr    ),
		.spec_rs2_addr  (spec_rs2_addr    ),
		.spec_rd_addr   (spec_rd_addr     ),
		.spec_rd_wdata  (spec_rd_wdata    ),
		.spec_pc_wdata  (spec_pc_wdata    ),
		.spec_mem_addr  (spec_mem_addr    ),
		.spec_mem_rmask (spec_mem_rmask   ),
		.spec_mem_wmask (spec_mem_wmask   ),
		.spec_mem_wdata (spec_mem_wdata   )
	);

	integer i;
	always @* begin
		// Triggers: a retirement this check applies to, and (required for the
		// instructions whose spec can trap) a trapping one.
		if (!reset) begin
			`BONETTO_REACH(reach_insn, spec_valid)
			`BONETTO_REACH(reach_insn_trap, spec_valid && spec_trap)
		end
		if (!reset && check) begin
			assume (spec_valid);

			insn_trap: assert (rvfi_trap == spec_trap);
			insn_halt: assert (rvfi_halt == spec_trap);

			if (spec_trap) begin
				insn_trap_rd_addr: assert (rvfi_rd_addr == 0);
				insn_trap_wmask: assert (rvfi_mem_wmask == 0);
			end else begin
				if (rvfi_rs1_addr == 0) insn_rs1_zero: assert (rvfi_rs1_rdata == 0);
				if (rvfi_rs2_addr == 0) insn_rs2_zero: assert (rvfi_rs2_rdata == 0);
				insn_rs1_addr: assert (rvfi_rs1_addr == spec_rs1_addr);
				insn_rs2_addr: assert (rvfi_rs2_addr == spec_rs2_addr);
				insn_rd_addr: assert (rvfi_rd_addr == spec_rd_addr);
				insn_rd_wdata: assert (rvfi_rd_wdata == spec_rd_wdata);
				insn_pc_wdata: assert (rvfi_pc_wdata == spec_pc_wdata);
				insn_rmask: assert (rvfi_mem_rmask == spec_mem_rmask);
				insn_wmask: assert (rvfi_mem_wmask == spec_mem_wmask);
				if (spec_mem_rmask || spec_mem_wmask)
					insn_mem_addr: assert (rvfi_mem_addr == spec_mem_addr);
				for (i = 0; i < `RISCV_FORMAL_XLEN/8; i = i + 1)
					if (spec_mem_wmask[i])
						assert (rvfi_mem_wdata[i*8 +: 8] == spec_mem_wdata[i*8 +: 8]);
			end
		end
	end
endmodule
