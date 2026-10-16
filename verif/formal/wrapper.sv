// BonettoR32V formal wrapper: the real Tiny Tapeout top module at its pins.
//
// Contents:
//   rvfi_wrapper         instantiates tt_um_fesens_r32v (RISCV_FORMAL build), drives
//                        the environment (contract sections 1-3) and exports RVFI,
//                        the pins and the SPI monitor's decoded events.
//   bonetto_spi_monitor  passive decoder of the 23LC512 SPI pins (contract section 2).
//   bonetto_spi_check    the SPI waveform rules, framing, constant pins, reset state
//                        and "all traffic finishes before rvfi_valid" (sections 1, 2, 6).
//   bonetto_gpio_check   GPIO load and store semantics at the pins (section 3).
//
// Environment (assumptions):
//   - rst_n = !reset; the testbench holds reset for the first step (one rising edge
//     with rst_n low, the contract's minimum) or, in the reset task, 1..8 steps.
//   - ena = 1.
//   - ui_in is a free constant ($anyconst): formal holds it constant (section 3).
//   - uio_in[7:3], uio_in[1:0] are free every cycle (inputs the contract leaves
//     unspecified, including the read-back of output pins).
//   - MISO (uio_in[2]) is free every cycle ($anyseq). During a high SCK phase that
//     began with a rising edge clocking a READ data bit, it is held at the value it
//     had on that rising edge (contract rule 3: the memory drives bit k from the
//     rising edge that clocks it until SCK falls). Everywhere else it is noise. The
//     monitor records the bit at the rising edge; the bus checks constrain that bit
//     to the memory contents. A core that samples MISO while SCK is low reads noise
//     and fails the bus checks.

`include "bonetto_macros.vh"

module rvfi_wrapper (
	input clock,
	input reset,
	`RVFI_OUTPUTS,
	`BONETTO_OUTPUTS
);
	`rvformal_rand_const_reg [7:0] ui_in_c;
	`rvformal_rand_reg [7:0] uio_in_free;

	wire [7:0] uo_out, uio_out, uio_oe;
	wire miso = uio_in_free[2];
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
		// The contract's RVFI port set (section 6), connected by name. No riscv-formal
		// feature define (CSRs, MEM_FAULT, ...) is enabled, so the testbench's RVFI
		// wires carry exactly these signals.
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

	reg reset_q_r = 1'b0;
	always @(posedge clock) reset_q_r <= reset;
	assign reset_q = reset_q_r;

	assign pin_ui_in = ui_in_c;
	assign pin_uo_out = uo_out;
	assign pin_uio_in = uio_in;
	assign pin_uio_out = uio_out;
	assign pin_uio_oe = uio_oe;

	bonetto_spi_monitor monitor (
		.clock (clock),
		.reset (reset),
		.cs    (uio_out[0]),
		.mosi  (uio_out[1]),
		.sck   (uio_out[3]),
		.miso  (miso),
		.mon_en(mon_en), .mon_pair(mon_pair), .mon_cs(mon_cs), .mon_sck(mon_sck),
		.mon_mosi(mon_mosi), .mon_miso(mon_miso), .mon_pcs(mon_pcs), .mon_psck(mon_psck),
		.mon_pmosi(mon_pmosi), .mon_rise(mon_rise), .ev_cmd_done(ev_cmd_done), .ev_cmd(ev_cmd),
		.ev_rd_bit(ev_rd_bit), .ev_rd_addr(ev_rd_addr), .ev_rd_idx(ev_rd_idx), .ev_rd_val(ev_rd_val),
		.ev_byte(ev_byte), .ev_byte_wr(ev_byte_wr), .ev_byte_addr(ev_byte_addr),
		.ev_byte_data(ev_byte_data), .ev_end(ev_end), .ev_end_hdr(ev_end_hdr),
		.ev_end_write(ev_end_write), .ev_end_bytes(ev_end_bytes), .ev_end_bit(ev_end_bit)
	);

	// MISO hold (contract rule 3): from the rising edge that clocks a READ data bit
	// until SCK falls, MISO keeps the value it had on that rising edge.
	reg miso_hold = 1'b0;
	reg miso_hold_val;
	always @(posedge clock) begin
		if (reset) begin
			miso_hold <= 1'b0;
		end else if (ev_rd_bit) begin
			miso_hold <= 1'b1;
			miso_hold_val <= miso;
		end else if (!uio_out[3] || uio_out[0]) begin
			miso_hold <= 1'b0;
		end
	end
	always @* begin
		if (!reset && miso_hold && uio_out[3] && !uio_out[0])
			assume (miso == miso_hold_val);
	end
endmodule

// Passive 23LC512 decoder (contract section 2, mode 0, READ 0x03 / WRITE 0x02,
// 16-bit address, sequential with a 64 KB wrap).
module bonetto_spi_monitor (
	input clock,
	input reset,
	input cs, mosi, sck, miso,
	output mon_en, mon_pair, mon_cs, mon_sck, mon_mosi, mon_miso, mon_pcs, mon_psck, mon_pmosi,
	output mon_rise,
	output ev_cmd_done,
	output [7:0] ev_cmd,
	output ev_rd_bit,
	output [15:0] ev_rd_addr,
	output [2:0] ev_rd_idx,
	output ev_rd_val,
	output ev_byte, ev_byte_wr,
	output [15:0] ev_byte_addr,
	output [7:0] ev_byte_data,
	output ev_end, ev_end_hdr, ev_end_write, ev_end_bytes,
	output [2:0] ev_end_bit
);
	// Steps since the start of the trace, saturating at 2. Step 0 is the initial
	// state (before any edge); the contract covers the pins from step 1.
	reg [1:0] age = 2'd0;
	always @(posedge clock) if (age != 2'd2) age <= age + 2'd1;
	assign mon_en = age != 2'd0;
	assign mon_pair = age == 2'd2;

	reg pcs, psck, pmosi;
	always @(posedge clock) begin
		pcs <= cs;
		psck <= sck;
		pmosi <= mosi;
	end
	assign mon_cs = cs;
	assign mon_sck = sck;
	assign mon_mosi = mosi;
	assign mon_miso = miso;
	assign mon_pcs = pcs;
	assign mon_psck = psck;
	assign mon_pmosi = pmosi;

	// A rising SCK edge while CS_n = 0: the memory samples MOSI and starts driving
	// the next MISO bit.
	wire rise = mon_en && !psck && sck && !cs;
	assign mon_rise = rise;

	reg [4:0]  hdr;    // header bits received, 0..24
	reg [6:0]  cmdsh;
	reg [14:0] addrsh;
	reg [15:0] baddr;  // address of the current data byte
	reg [2:0]  bidx;   // data bit index within the byte (0 = MSB)
	reg [6:0]  dsh;
	reg        wr;     // command was WRITE
	reg        anyb;   // at least one full data byte

	wire in_data = hdr == 5'd24;
	wire bitv = (in_data && !wr) ? miso : mosi;

	assign ev_cmd_done = rise && hdr == 5'd7;
	assign ev_cmd = {cmdsh, mosi};
	assign ev_rd_bit = rise && in_data && !wr;
	assign ev_rd_addr = baddr;
	assign ev_rd_idx = bidx;
	assign ev_rd_val = miso;
	assign ev_byte = rise && in_data && bidx == 3'd7;
	assign ev_byte_wr = wr;
	assign ev_byte_addr = baddr;
	assign ev_byte_data = {dsh, bitv};
	assign ev_end = mon_pair && !pcs && cs;
	assign ev_end_hdr = in_data;
	assign ev_end_write = wr;
	assign ev_end_bytes = anyb;
	assign ev_end_bit = bidx;

	always @(posedge clock) begin
		if (reset || cs) begin
			hdr <= 5'd0;
			bidx <= 3'd0;
			wr <= 1'b0;
			anyb <= 1'b0;
		end else if (rise) begin
			if (hdr < 5'd8) begin
				cmdsh <= {cmdsh[5:0], mosi};
				hdr <= hdr + 5'd1;
				if (hdr == 5'd7) wr <= {cmdsh, mosi} == 8'h02;
			end else if (hdr < 5'd24) begin
				addrsh <= {addrsh[13:0], mosi};
				hdr <= hdr + 5'd1;
				if (hdr == 5'd23) baddr <= {addrsh, mosi};
			end else begin
				dsh <= {dsh[5:0], bitv};
				bidx <= bidx + 3'd1;
				if (bidx == 3'd7) begin
					baddr <= baddr + 16'd1;
					anyb <= 1'b1;
				end
			end
		end
	end
endmodule

// SPI protocol, constant pins, reset state and transaction placement.
module bonetto_spi_check (
	`BONETTO_CHECKER_PORTS
);
	always @* begin
		// Section 1: uio_oe is constant 8'b0000_1011 at all times; uio_out[7:4]
		// and uio_out[2] are constant 0.
		spi_uio_oe: assert (pin_uio_oe == 8'b0000_1011);
		spi_uio_out_hi: assert (pin_uio_out[7:4] == 4'b0000);
		spi_uio_out_2: assert (pin_uio_out[2] == 1'b0);

		// Section 1: while rst_n is low (from the first rising edge at which it is
		// low), CS_n = 1, SCK = 0 and uo_out = 0. Nothing retires in reset.
		if (mon_en && reset_q) begin
			spi_reset_cs: assert (mon_cs == 1'b1);
			spi_reset_sck: assert (mon_sck == 1'b0);
			spi_reset_uo: assert (pin_uo_out == 8'h00);
			spi_reset_valid: assert (!rvfi_valid);
		end

		if (mon_pair) begin
			// Rule 1: C(t) != C(t-1) requires S(t-1) = 0 and S(t) = 0.
			if (mon_cs != mon_pcs) begin
				spi_rule1_prev: assert (!mon_psck);
				spi_rule1_now: assert (!mon_sck);
			end
			// Rule 2: MOSI does not change on a rising SCK edge.
			if (mon_rise) spi_rule2: assert (mon_mosi == mon_pmosi);
		end

		// Rule 4: the command is 0x03 READ or 0x02 WRITE.
		if (ev_cmd_done) spi_rule4_cmd: assert (ev_cmd == 8'h03 || ev_cmd == 8'h02);

		if (ev_end) begin
			// Rule 4: a transaction carries a full 8-bit command and 16-bit address.
			spi_rule4_header: assert (ev_end_hdr);
			// Rule 5: a WRITE ends on a byte boundary with at least one data byte.
			if (ev_end_write) spi_rule5_write: assert (ev_end_bytes && ev_end_bit == 3'd0);
		end

		// Section 6: all of an instruction's SPI traffic finishes before its
		// rvfi_valid cycle, so CS_n is high in that cycle. This also makes every
		// transaction belong to exactly one instruction (the next to retire).
		if (mon_en && !reset_q && rvfi_valid) spi_cs_at_valid: assert (mon_cs);
	end

	// Reach covers (bonetto_reach): a complete READ and a complete WRITE.
	always @* begin
		if (!reset) begin
			`BONETTO_REACH(reach_spi_end_read, ev_end && ev_end_hdr && !ev_end_write)
			`BONETTO_REACH(reach_spi_end_write, ev_end && ev_end_hdr && ev_end_write)
		end
	end
endmodule

// GPIO at the pins (contract section 3). ui_in is constant (formal environment).
module bonetto_gpio_check (
	`BONETTO_CHECKER_PORTS
);
	// Retirements with architectural effect (a trapping instruction has none).
	wire valid = !reset && rvfi_valid && !rvfi_trap;
	wire io = rvfi_mem_addr[31];
	wire [1:0] a10 = rvfi_mem_addr[1:0];
	wire [3:0] rm = rvfi_mem_rmask;
	wire [3:0] wm = rvfi_mem_wmask;

	// Misaligned IO accesses (addr[1:0] != 0 with n > 1) are unspecified.
	wire ld_spec = valid && io && rm != 4'b0 && (rm == 4'b0001 || a10 == 2'd0);
	wire st_spec = valid && io && wm != 4'b0 && (wm == 4'b0001 || a10 == 2'd0);
	wire st_unspec = valid && io && wm != 4'b0 && !(wm == 4'b0001 || a10 == 2'd0);

	// The store lane that writes the IO byte at an address X with X[1:0] = 0.
	reg wr0;
	reg [7:0] wr0_val;
	integer k;
	always @* begin
		wr0 = 1'b0;
		wr0_val = 8'h00;
		for (k = 0; k < 4; k = k + 1) begin
			if (wm[k] && ((a10 + k) & 3) == 0) begin
				wr0 = 1'b1;
				wr0_val = rvfi_mem_wdata[8*k +: 8];
			end
		end
	end
	wire upd = st_spec && wr0;

	// Loads: byte k (address X = addr + k) reads ui_in if X[1:0] = 0, else 0.
	always @* begin
		if (check && ld_spec) begin
			for (k = 0; k < 4; k = k + 1) begin
				if (rm[k]) begin
					if (((a10 + k) & 3) == 0)
						assert (rvfi_mem_rdata[8*k +: 8] == pin_ui_in);
					else
						assert (rvfi_mem_rdata[8*k +: 8] == 8'h00);
				end
			end
		end
	end

	// uo_out: 0 after reset; changes only as the result of an IO store that writes
	// byte 0, and holds the new value no later than one cycle after that store
	// retires. uo_ref is the architectural value, updated one step after the store's
	// rvfi_valid. Within an instruction's window (after the previous retirement's
	// "+1" step, up to its own rvfi_valid) uo_out may move once, from uo_ref to the
	// store's value, and must stay there.
	reg [7:0] uo_ref;
	reg dev;
	reg [7:0] dev_val;
	reg unspec;
	reg strict;  // this step is the step after a retirement (or the first after reset)
	wire deviates = pin_uo_out != uo_ref;
	always @(posedge clock) begin
		if (reset) begin
			uo_ref <= 8'h00;
			dev <= 1'b0;
			unspec <= 1'b0;
			strict <= 1'b1;
		end else begin
			strict <= rvfi_valid;
			if (rvfi_valid) begin
				dev <= 1'b0;
				if (upd) uo_ref <= wr0_val;
				if (st_unspec) unspec <= 1'b1;
			end else if (!strict && deviates && !dev) begin
				dev <= 1'b1;
				dev_val <= pin_uo_out;
			end
		end
	end
	always @* begin
		if (!reset && !unspec) begin
			if (strict) gpio_uo_hold: assert (!deviates);
			if (dev) gpio_uo_once: assert (pin_uo_out == dev_val);
			if (rvfi_valid && (dev || deviates)) begin
				gpio_uo_cause: assert (upd);
				gpio_uo_value: assert (wr0_val == (dev ? dev_val : pin_uo_out));
			end
		end
	end

	// Reach covers: an IO load and an IO store that writes byte 0.
	always @* begin
		if (!reset) begin
			`BONETTO_REACH(reach_gpio_load, ld_spec)
			`BONETTO_REACH(reach_gpio_store, upd)
		end
	end
endmodule
