// BonettoR32V fork of riscv-formal checks/rvfi_testbench.sv
// (Copyright (C) 2017 Claire Xenia Wolf, ISC license, see third_party/riscv-formal/COPYING).
//
// Differences from the stock testbench:
//   - A 16-bit cycle counter (stock is 8 bits and saturates at 255; our cores need
//     hundreds of cycles per instruction).
//   - Always RISCV_FORMAL_UNBOUNDED style: `check` is a free input, so the solver
//     picks the check cycle and BMC covers every retirement up to the depth.
//   - The checker gets the pins and the SPI monitor's events from the wrapper
//     (uniform port list, see bonetto_macros.vh).
//   - BONETTO_RESET_FREE: rst_n low for 1..`BONETTO_RESET_MAX rising edges, then high
//     for good (the reset task). Default: low for exactly the first edge, the
//     contract's minimum.

`include "bonetto_macros.vh"

module rvfi_testbench (
	input clock,
	input reset,
	input check
);
	`RVFI_WIRES
	`BONETTO_WIRES

	reg [15:0] step = 16'd0;  // steps since the start of the trace (step 0 = initial state)
	always @(posedge clock) if (step != 16'hffff) step <= step + 16'd1;

`ifdef BONETTO_RESET_FREE
	reg released = 1'b0;
	always @(posedge clock) if (!reset) released <= 1'b1;
	always @* begin
		if (step == 16'd0) assume (reset);
		if (released) assume (!reset);
		if (step >= `BONETTO_RESET_MAX) assume (!reset);
	end
`else
	always @* assume (reset == (step == 16'd0));
`endif

	// Cycles since the last reset step (0 during reset), saturating at 65535.
	reg [15:0] cycle_reg = 16'd0;
	(* keep *) wire [15:0] cycle = reset ? 16'd0 : cycle_reg;
	always @(posedge clock) cycle_reg <= reset ? 16'd1 : cycle_reg + (cycle_reg != 16'hffff);

	`RISCV_FORMAL_CHECKER checker_inst (
		`BONETTO_CHECKER_CONN(clock, reset, check)
	);

	rvfi_wrapper wrapper (
		.clock (clock),
		.reset (reset),
		`RVFI_CONN,
		`BONETTO_CONN
	);
endmodule
