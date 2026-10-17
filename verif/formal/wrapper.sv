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
//   - The core region (RAM 0xFF80-0xFFFF, contract section 3) is a real memory in
//     every task: 128 bytes with free initial contents, written by every WRITE data
//     byte at a region address and read by every READ data bit at one (the bit at
//     the rising edge equals the memory's bit). A core keeping x1..x15 or the pc there
//     reads back what it wrote. Program bytes stay data-free outside the bus checks.
//
// Core-region stop (contract section 3): checks stop comparing at the first
// retirement whose fetch or data access touches the region (bon_region_ret, from the
// spec-model address); only liveness and the SPI waveform rules apply after it
// (bon_stopped). The fields that decide the stop are still checked at that retirement:
// pc_rdata (pc_fwd, pc_bwd), the rs1 read (reg) and mem_addr (insn).

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
		.ev_end_write(ev_end_write), .ev_end_bytes(ev_end_bytes), .ev_end_bit(ev_end_bit),
		.ev_end_addr(ev_end_addr), .ev_end_nbits(ev_end_nbits), .ev_end_region(ev_end_region),
		.ev_end_mixed(ev_end_mixed)
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

	// Core region memory (see the header). No initializer: free initial contents.
	reg [7:0] rmem [0:127];
	wire [7:0] rmem_rd = rmem[ev_rd_addr[6:0]];
	always @* begin
		if (!reset && ev_rd_bit && ev_rd_addr[15:7] == 9'h1ff)
			assume (miso == rmem_rd[3'd7 - ev_rd_idx]);
	end
	always @(posedge clock) begin
		if (!reset && ev_byte && ev_byte_wr && ev_byte_addr[15:7] == 9'h1ff)
			rmem[ev_byte_addr[6:0]] <= ev_byte_data;
	end

	// Region-touching retirements (contract section 3), from the spec-model address
	// only, never from what the core claims (rvfi_mem_*), so a core cannot stop the
	// checks by misreporting. Fetch: bytes pc16 + k, k < 4, reach 0xFF80 (or wrap
	// through 0xFFFF) iff pc16 >= 0xFF7D. Data: an RV32E load/store at rs1 + imm (rs1 = x0
	// reads 0), RAM (addr[31] = 0), n bytes touch the region iff
	// addr16 >= 0xFF80 - (n - 1). Its inputs are checked at that retirement: rvfi_insn
	// by bus_imem (the fetched bytes), rs1_addr by the insn check, rs1_rdata by reg. A
	// reserved encoding (a used register field x16-x31) has no spec address and never
	// stops the checks (its rs1_rdata is unconstrained).
	wire [6:0] r_op = rvfi_insn[6:0];
	wire [2:0] r_f3 = rvfi_insn[14:12];
	wire r_ld = r_op == 7'b0000011 && (r_f3 == 3'd0 || r_f3 == 3'd1 || r_f3 == 3'd2 ||
	                                   r_f3 == 3'd4 || r_f3 == 3'd5);
	wire r_st = r_op == 7'b0100011 && (r_f3 == 3'd0 || r_f3 == 3'd1 || r_f3 == 3'd2);
	wire [15:0] r_nm1 = (r_f3[1:0] == 2'd0) ? 16'd0 : (r_f3[1:0] == 2'd1) ? 16'd1 : 16'd3;
	wire [31:0] r_rs1 = (rvfi_insn[19:15] == 5'd0) ? 32'd0 : rvfi_rs1_rdata;
	wire [31:0] r_imm = r_st ? {{20{rvfi_insn[31]}}, rvfi_insn[31:25], rvfi_insn[11:7]}
	                         : {{20{rvfi_insn[31]}}, rvfi_insn[31:20]};
	wire [31:0] r_addr = r_rs1 + r_imm;
	wire r_e = !rvfi_insn[19] && (r_ld ? !rvfi_insn[11] : !rvfi_insn[24]);   // E filter
	wire spec_touch = (r_ld || r_st) && r_e && !r_addr[31] &&
	                  r_addr[15:0] >= 16'hff80 - r_nm1;
	wire fetch_touch = rvfi_pc_rdata[15:0] >= 16'hff7d;
	assign bon_fetch_touch = fetch_touch;
	assign bon_region_ret = fetch_touch || spec_touch;
	reg stopped_r = 1'b0;
	always @(posedge clock) begin
		if (reset) stopped_r <= 1'b0;
		else if (rvfi_valid && bon_region_ret) stopped_r <= 1'b1;
	end
	assign bon_stopped = stopped_r;

	// A branch (funct3 not 010/011) or JALR (funct3 000) whose used register fields
	// are x0..x15: on a trap its rs fields are truthful and checked (section 6).
	assign bon_brj = (r_op == 7'b1100011 && r_f3 != 3'd2 && r_f3 != 3'd3 &&
	                  !rvfi_insn[19] && !rvfi_insn[24]) ||
	                 (r_op == 7'b1100111 && r_f3 == 3'd0 && !rvfi_insn[19] && !rvfi_insn[11]);
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
	output [2:0] ev_end_bit,
	output [15:0] ev_end_addr,
	output [9:0] ev_end_nbits,
	output ev_end_region,
	output ev_end_mixed
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
	reg [15:0] taddr;  // start address A
	reg [9:0]  nbits;  // data bits so far (saturates at 1023)
	reg        t_reg;  // a data bit of a region byte (0xFF80-0xFFFF)
	reg        t_prog; // a data bit of a program byte

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
	assign ev_end_addr = taddr;
	assign ev_end_nbits = nbits;
	// Section 6: a region transaction addresses only region bytes (it may not wrap out
	// of the region); one with no data byte is a region transaction iff A is in the
	// region. Bytes are the ones with at least one data bit.
	assign ev_end_region = in_data && (t_reg ? !t_prog : taddr[15:7] == 9'h1ff);
	assign ev_end_mixed = t_reg && t_prog;

	always @(posedge clock) begin
		if (reset || cs) begin
			hdr <= 5'd0;
			bidx <= 3'd0;
			wr <= 1'b0;
			anyb <= 1'b0;
			nbits <= 10'd0;
			t_reg <= 1'b0;
			t_prog <= 1'b0;
		end else if (rise) begin
			if (hdr < 5'd8) begin
				cmdsh <= {cmdsh[5:0], mosi};
				hdr <= hdr + 5'd1;
				if (hdr == 5'd7) wr <= {cmdsh, mosi} == 8'h02;
			end else if (hdr < 5'd24) begin
				addrsh <= {addrsh[13:0], mosi};
				hdr <= hdr + 5'd1;
				if (hdr == 5'd23) begin
					baddr <= {addrsh, mosi};
					taddr <= {addrsh, mosi};
				end
			end else begin
				dsh <= {dsh[5:0], bitv};
				bidx <= bidx + 3'd1;
				if (nbits != 10'h3ff) nbits <= nbits + 10'd1;
				if (baddr[15:7] == 9'h1ff) t_reg <= 1'b1;
				else t_prog <= 1'b1;
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
		// transaction belong to exactly one instruction (the next to retire). Not at
		// or after the first region-touching retirement (section 3).
		if (mon_en && !reset_q && rvfi_valid && `BONETTO_FULL) spi_cs_at_valid: assert (mon_cs);
	end

	// Triggers (reach task): a complete READ and a complete WRITE.
	always @* begin
		if (!reset) begin
			`BONETTO_REACH(reach_spi_end_read, ev_end && ev_end_hdr && !ev_end_write)
			`BONETTO_REACH(reach_spi_end_write, ev_end && ev_end_hdr && ev_end_write)
		end
	end
endmodule

// GPIO at the pins (contract section 3). ui_in is constant (formal environment).
// Compared up to the first region-touching retirement (section 3), exclusive.
module bonetto_gpio_check (
	`BONETTO_CHECKER_PORTS
);
	// Compared retirements with architectural effect (a trapping instruction has none).
	wire full = !reset && rvfi_valid && `BONETTO_FULL;
	wire valid = full && !rvfi_trap;
	wire io = rvfi_mem_addr[31];
	wire [1:0] a10 = rvfi_mem_addr[1:0];
	wire [3:0] rm = rvfi_mem_rmask;
	wire [3:0] wm = rvfi_mem_wmask;

	// Misaligned IO accesses (addr[1:0] != 0 with n > 1) are unspecified.
	wire ld_spec = valid && io && rm != 4'b0 && (rm == 4'b0001 || a10 == 2'd0);
	wire st_spec = valid && io && wm != 4'b0 && (wm == 4'b0001 || a10 == 2'd0);
	wire st_unspec = valid && io && wm != 4'b0 && !(wm == 4'b0001 || a10 == 2'd0);
	wire ld_unspec = valid && io && rm != 4'b0 && !(rm == 4'b0001 || a10 == 2'd0);
	wire io_unspec = st_unspec || ld_unspec;   // either may do anything to uo_out

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
	// store's value, and must stay there; that is checked at the retirement, so the
	// window of a region-touching retirement is not compared. After a misaligned IO
	// store or load (unspecified) uo_out is not compared again: `unspec` from the next
	// step, io_unspec at its own retirement step (it may change uo_out in or before it).
	reg [7:0] uo_ref;
	reg dev;            // uo_out left uo_ref in this window
	reg [7:0] dev_val;  // ... to this value
	reg twice;          // ... and then moved again
	reg unspec;
	reg strict;  // this step is the step after a compared retirement (or after reset)
	wire deviates = pin_uo_out != uo_ref;
	always @(posedge clock) begin
		if (reset) begin
			uo_ref <= 8'h00;
			dev <= 1'b0;
			twice <= 1'b0;
			unspec <= 1'b0;
			strict <= 1'b1;
		end else begin
			strict <= full;
			if (rvfi_valid) begin
				dev <= 1'b0;
				twice <= 1'b0;
				if (upd) uo_ref <= wr0_val;
				if (io_unspec) unspec <= 1'b1;
			end else if (!strict) begin
				if (deviates && !dev) begin
					dev <= 1'b1;
					dev_val <= pin_uo_out;
				end else if (dev && pin_uo_out != dev_val) begin
					twice <= 1'b1;
				end
			end
		end
	end
	wire live = !reset && !unspec && !bon_stopped;
	always @* begin
		if (live && strict) gpio_uo_hold: assert (!deviates);
		if (live && full && !io_unspec) begin
			gpio_uo_once: assert (!twice && (!dev || pin_uo_out == dev_val));
			if (dev || deviates) begin
				gpio_uo_cause: assert (upd);
				gpio_uo_value: assert (wr0_val == (dev ? dev_val : pin_uo_out));
			end
		end
	end

	// Triggers (reach task): an IO load and an IO store that writes byte 0.
	always @* begin
		if (!reset) begin
			`BONETTO_REACH(reach_gpio_load, ld_spec)
			`BONETTO_REACH(reach_gpio_store, upd)
		end
	end
endmodule
