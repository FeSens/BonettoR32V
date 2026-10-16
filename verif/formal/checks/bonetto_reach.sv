// BonettoR32V reach task: every check's trigger as a cover statement, in one design.
//
// The task instantiates all checkers side by side. It is built with
// BONETTO_REACH_AS_ASSERT, so each reach_* statement is assert(!trigger); every
// other assertion is removed (`chformal -assert -remove` on cells not named
// reach_*), and every assumption stays. BMC with --keep-going over the whole depth
// then reports each reach_* assertion it can falsify: that trigger is reachable.
// A trace of this combined design is also a trace of each single check's design
// (which has a subset of these assumptions), so a trigger reached here is
// reachable within the depth in that check's own task: the check is not vacuous.
// run_formal.py maps the reach_* labels to checks; one it cannot falsify is
// UNREACHED, which fails the suite.

`include "bonetto_macros.vh"

module bonetto_reach (
	`BONETTO_CHECKER_PORTS
);
	bonetto_isa_cover isa_cover (
		.clock(clock), .reset(reset),
		.rvfi_valid(rvfi_valid), .rvfi_trap(rvfi_trap), .rvfi_insn(rvfi_insn),
		.rvfi_pc_rdata(rvfi_pc_rdata), .rvfi_rs1_rdata(rvfi_rs1_rdata),
		.rvfi_rs2_rdata(rvfi_rs2_rdata), .rvfi_mem_rdata(rvfi_mem_rdata)
	);
	bonetto_reg_check      c_reg      (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_pc_fwd_check   c_pc_fwd   (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_pc_bwd_check   c_pc_bwd   (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_causal_check   c_causal   (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_unique_check   c_unique   (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_liveness_check c_liveness (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_ill_check      c_ill      (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_silent_check   c_silent   (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_rvfi_check     c_rvfi     (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_cover_check    c_cover    (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_bus_imem_check c_bus_imem (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_bus_dmem_check c_bus_dmem (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_spi_check      c_spi      (`BONETTO_CHECKER_CONN(clock, reset, check));
	bonetto_gpio_check     c_gpio     (`BONETTO_CHECKER_CONN(clock, reset, check));
endmodule
