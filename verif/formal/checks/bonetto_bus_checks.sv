// BonettoR32V bus checks: RVFI against the SPI pins, with transaction identity
// (contract v2, section 6). These replace riscv-formal's
// checks/rvfi_bus_{imem,dmem}_check.sv (Copyright (C) 2017 Claire Xenia Wolf, ISC
// license, see third_party/riscv-formal/COPYING), which shadow aligned words at full
// 32-bit bus addresses, update the shadow only when rvfi_mem_addr equals the
// shadowed address (wrong for misaligned accesses), and know nothing about GPIO or
// SPI transactions.
//
// Model: one symbolic program byte address X (free constant, outside the core region
// 0xFF80-0xFFFF, which the wrapper models as a real memory in every task) with a free
// initial value (contract section 2: the contents are whatever the test loaded). The
// monitor's WRITE bytes at X update it, and each READ data bit at X equals its current
// bit. Bytes are compared on the 16-bit pin address, so the 64 KB alias and the
// misaligned wrap are exact; GPIO accesses (addr[31] = 1) never appear on the pins.
//
// Transactions (classified by the monitor when CS_n rises): a region transaction
// addresses only region bytes and is exempt; a mixed one (region and program bytes)
// is forbidden; every other one is a program transaction. Per instruction (the SPI
// traffic between the previous retirement and this one), with pc16 = the expected pc
// (0 after reset, then the previous retirement's pc_wdata[15:0]; pc_bwd ties it to
// pc_rdata) and ma = mem_addr[15:0]:
//   - the fetch: a program READ of exactly 32 data bits at A = pc16 (an "F READ").
//     There must be one. Another F READ is allowed only as a load READ (its 4 bytes are
//     load bytes): a re-fetch fails.
//   - every other program READ (split fetches, a fetch running on into more bytes, ...)
//     reads only the load's bytes (ma + k, rmask[k], RAM, non-trapping);
//   - each load byte is read in full by a READ other than the fetch (the fetch never
//     counts toward a load: a second F READ may);
//   - program WRITEs write only the store's bytes (ma + k, wmask[k], RAM,
//     non-trapping), each exactly once (a store may be split over several WRITEs);
//   - fetch and load bytes equal X's contents at the start of the window (the
//     instruction executes and loads the memory as it was before it ran); a store byte
//     is the value written.
// All of it is checked at each compared retirement (not at or after the first
// region-touching one, contract section 3), per byte X; X is free, so every byte is
// covered. The fetch itself (bus_imem_pc, _fetch, _value) is also checked at the
// region-touching retirement unless its fetch touched the region: its instruction word
// decided the stop.

`include "bonetto_macros.vh"

module bonetto_bus_track (
	input clock,
	input reset,
	input rvfi_valid,
	input [31:0] rvfi_pc_wdata,
	input ev_rd_bit,
	input [15:0] ev_rd_addr,
	input [2:0] ev_rd_idx,
	input ev_rd_val,
	input ev_byte,
	input ev_byte_wr,
	input [15:0] ev_byte_addr,
	input [7:0] ev_byte_data,
	input ev_end,
	input ev_end_hdr,
	input ev_end_write,
	input [15:0] ev_end_addr,
	input [9:0] ev_end_nbits,
	input ev_end_region,
	input ev_end_mixed,
	output [15:0] a,
	output [15:0] pc16,    // the expected fetch address of the current window
	output [7:0] mstart,   // contents of X at the start of the current window
	output [1:0] n_fetch,  // F READs in the window (saturates at 3), this step included
	output rd_nonf,        // a data bit of X read by a non-F program READ in the window
	output full_nonf,      // X read in full (8 bits) by a non-F program READ
	output [1:0] wr_cnt,   // program WRITE bytes at X in the window (saturates at 3)
	output [7:0] wr_val,   // value of the last WRITE byte at X in the window
	output mixed           // a mixed transaction in the window
);
	`rvformal_rand_const_reg [15:0] addr;
	always @* assume (addr[15:7] != 9'h1ff);   // the region is the wrapper's memory
	reg [7:0] mem;  // current contents of RAM byte `addr`; free initial value

	wire rd_bit_hit = !reset && ev_rd_bit && ev_rd_addr == addr;
	wire rd_byte_hit = !reset && ev_byte && !ev_byte_wr && ev_byte_addr == addr;
	wire wr_hit = !reset && ev_byte && ev_byte_wr && ev_byte_addr == addr;
	wire [7:0] mem_next = wr_hit ? ev_byte_data : mem;

	// The memory drives each READ data bit from its current contents.
	always @* begin
		if (rd_bit_hit) assume (ev_rd_val == mem[3'd7 - ev_rd_idx]);
	end
	always @(posedge clock) mem <= mem_next;

	// The current transaction's accesses to X (cleared when it ends).
	reg c_rd, c_full;
	reg [1:0] c_wr;
	reg [7:0] c_wv;
	always @(posedge clock) begin
		if (reset || ev_end) begin
			c_rd <= 1'b0;
			c_full <= 1'b0;
			c_wr <= 2'd0;
		end else begin
			if (rd_bit_hit) c_rd <= 1'b1;
			if (rd_byte_hit) c_full <= 1'b1;
			if (wr_hit) begin
				c_wr <= (c_wr == 2'd3) ? 2'd3 : c_wr + 2'd1;
				c_wv <= ev_byte_data;
			end
		end
	end

	// This step's transaction end, classified.
	wire e_prog = !reset && ev_end && !ev_end_region && !ev_end_mixed;
	wire e_f = e_prog && !ev_end_write && ev_end_hdr && ev_end_addr == pc16 &&
	           ev_end_nbits == 10'd32;
	wire e_rd = e_prog && !ev_end_write && !e_f;   // a non-F program READ
	wire e_wr = e_prog && ev_end_write;

	// Window accumulators (cleared at each retirement; the values below include this
	// step, so a transaction ending in the retirement step counts for it).
	reg [15:0] epc;
	reg [7:0] ms;
	reg [1:0] nf;
	reg rn, fn, mx;
	reg [1:0] wc;
	reg [7:0] wv;
	wire [1:0] nf_now = (e_f && nf != 2'd3) ? nf + 2'd1 : nf;
	wire rn_now = rn || (e_rd && c_rd);
	wire fn_now = fn || (e_rd && c_full);
	wire [2:0] wc_sum = {1'b0, wc} + {1'b0, (e_wr ? c_wr : 2'd0)};
	wire [1:0] wc_now = wc_sum[2] ? 2'd3 : wc_sum[1:0];
	wire mx_now = mx || (!reset && ev_end && ev_end_mixed);
	always @(posedge clock) begin
		if (reset) begin
			epc <= 16'd0;
		end else if (rvfi_valid) begin
			epc <= rvfi_pc_wdata[15:0];
		end
		if (reset || rvfi_valid) begin
			ms <= mem_next;
			nf <= 2'd0;
			rn <= 1'b0;
			fn <= 1'b0;
			wc <= 2'd0;
			mx <= 1'b0;
		end else begin
			nf <= nf_now;
			rn <= rn_now;
			fn <= fn_now;
			wc <= wc_now;
			if (e_wr && c_wr != 2'd0) wv <= c_wv;
			mx <= mx_now;
		end
	end

	assign a = addr;
	assign pc16 = epc;
	assign mstart = ms;
	assign n_fetch = nf_now;
	assign rd_nonf = rn_now;
	assign full_nonf = fn_now;
	assign wr_cnt = wc_now;
	assign wr_val = (e_wr && c_wr != 2'd0) ? c_wv : wv;
	assign mixed = mx_now;
endmodule

// Which bytes of the retiring instruction are byte X. A trapping retirement makes no
// load or store (contract section 4: no effect), whatever it claims.
module bonetto_bus_claims (
	input [15:0] a,
	input [31:0] rvfi_insn,
	input [31:0] rvfi_pc_rdata,
	input rvfi_trap,
	input [31:0] rvfi_mem_addr,
	input [3:0] rvfi_mem_rmask,
	input [3:0] rvfi_mem_wmask,
	input [31:0] rvfi_mem_rdata,
	input [31:0] rvfi_mem_wdata,
	output reg fetch_hit,
	output reg [7:0] fetch_byte,
	output reg load_hit,
	output reg [7:0] load_byte,
	output reg store_hit,
	output reg [7:0] store_byte
);
	wire ram = !rvfi_mem_addr[31] && !rvfi_trap;
	wire [15:0] pc16 = rvfi_pc_rdata[15:0];
	wire [15:0] ma = rvfi_mem_addr[15:0];
	wire [15:0] fa [0:3];
	wire [15:0] da [0:3];
	assign fa[0] = pc16;
	assign fa[1] = pc16 + 16'd1;
	assign fa[2] = pc16 + 16'd2;
	assign fa[3] = pc16 + 16'd3;
	assign da[0] = ma;
	assign da[1] = ma + 16'd1;
	assign da[2] = ma + 16'd2;
	assign da[3] = ma + 16'd3;

	integer k;
	always @* begin
		fetch_hit = 1'b0;
		fetch_byte = 8'h00;
		load_hit = 1'b0;
		load_byte = 8'h00;
		store_hit = 1'b0;
		store_byte = 8'h00;
		for (k = 0; k < 4; k = k + 1) begin
			if (fa[k] == a) begin
				fetch_hit = 1'b1;
				fetch_byte = rvfi_insn[8*k +: 8];
			end
			if (ram && rvfi_mem_rmask[k] && da[k] == a) begin
				load_hit = 1'b1;
				load_byte = rvfi_mem_rdata[8*k +: 8];
			end
			if (ram && rvfi_mem_wmask[k] && da[k] == a) begin
				store_hit = 1'b1;
				store_byte = rvfi_mem_wdata[8*k +: 8];
			end
		end
	end
endmodule

`define BONETTO_BUS_COMMON \
	wire [15:0] a, pc16; \
	wire [7:0] mstart, wr_val; \
	wire [1:0] n_fetch, wr_cnt; \
	wire rd_nonf, full_nonf, mixed; \
	bonetto_bus_track track ( \
		.clock(clock), .reset(reset), .rvfi_valid(rvfi_valid), .rvfi_pc_wdata(rvfi_pc_wdata), \
		.ev_rd_bit(ev_rd_bit), .ev_rd_addr(ev_rd_addr), .ev_rd_idx(ev_rd_idx), .ev_rd_val(ev_rd_val), \
		.ev_byte(ev_byte), .ev_byte_wr(ev_byte_wr), .ev_byte_addr(ev_byte_addr), \
		.ev_byte_data(ev_byte_data), .ev_end(ev_end), .ev_end_hdr(ev_end_hdr), \
		.ev_end_write(ev_end_write), .ev_end_addr(ev_end_addr), .ev_end_nbits(ev_end_nbits), \
		.ev_end_region(ev_end_region), .ev_end_mixed(ev_end_mixed), \
		.a(a), .pc16(pc16), .mstart(mstart), .n_fetch(n_fetch), .rd_nonf(rd_nonf), \
		.full_nonf(full_nonf), .wr_cnt(wr_cnt), .wr_val(wr_val), .mixed(mixed) \
	); \
	wire fetch_hit, load_hit, store_hit; \
	wire [7:0] fetch_byte, load_byte, store_byte; \
	bonetto_bus_claims claims ( \
		.a(a), .rvfi_insn(rvfi_insn), .rvfi_pc_rdata(rvfi_pc_rdata), .rvfi_trap(rvfi_trap), \
		.rvfi_mem_addr(rvfi_mem_addr), .rvfi_mem_rmask(rvfi_mem_rmask), \
		.rvfi_mem_wmask(rvfi_mem_wmask), .rvfi_mem_rdata(rvfi_mem_rdata), \
		.rvfi_mem_wdata(rvfi_mem_wdata), .fetch_hit(fetch_hit), .fetch_byte(fetch_byte), \
		.load_hit(load_hit), .load_byte(load_byte), .store_hit(store_hit), \
		.store_byte(store_byte) \
	); \
	wire valid = !reset && rvfi_valid && `BONETTO_FULL;
	// The fetch is checked at the stop retirement too, unless the fetch itself touched
	// the region: its insn decided the stop.
	wire fvalid = !reset && rvfi_valid && !bon_stopped && !bon_fetch_touch;

module bonetto_bus_imem_check (
	`BONETTO_CHECKER_PORTS
);
	`BONETTO_BUS_COMMON
	always @* begin
		if (fvalid && check) begin
			// A fetch: a program READ of exactly the 4 instruction bytes, at the pc.
			bus_imem_pc: assert (pc16 == rvfi_pc_rdata[15:0]);
			bus_imem_fetch: assert (n_fetch != 2'd0);
			if (fetch_hit) bus_imem_value: assert (fetch_byte == mstart);
		end
		if (valid && check && fetch_hit && n_fetch[1]) begin
			// Exactly one: a second fetch-shaped READ is only allowed as a load READ.
			bus_imem_refetch: assert (load_hit);
		end
		if (!reset) `BONETTO_REACH(reach_bus_imem, valid && fetch_hit)
	end
endmodule

module bonetto_bus_dmem_check (
	`BONETTO_CHECKER_PORTS
);
	`BONETTO_BUS_COMMON
	always @* begin
		if (valid && check) begin
			if (load_hit) begin
				// Read in full by a READ that is not the fetch (a second F READ is a
				// load READ).
				bus_dmem_load_read: assert (full_nonf || (fetch_hit && n_fetch[1]));
				bus_dmem_load_value: assert (load_byte == mstart);
			end
			if (store_hit) begin
				bus_dmem_store_once: assert (wr_cnt == 2'd1);
				bus_dmem_store_value: assert (wr_val == store_byte);
			end
			if (wr_cnt != 2'd0) bus_dmem_write_claimed: assert (store_hit);
			if (rd_nonf) bus_dmem_read_claimed: assert (load_hit);
			bus_region_mixed: assert (!mixed);
		end
		if (!reset) begin
			`BONETTO_REACH(reach_bus_dmem_load, valid && load_hit)
			`BONETTO_REACH(reach_bus_dmem_store, valid && store_hit)
		end
	end
endmodule
