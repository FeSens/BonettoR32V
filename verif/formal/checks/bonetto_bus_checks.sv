// BonettoR32V bus checks: RVFI against the SPI pins. These replace riscv-formal's
// checks/rvfi_bus_{imem,dmem}_check.sv (Copyright (C) 2017 Claire Xenia Wolf, ISC
// license, see third_party/riscv-formal/COPYING), which shadow aligned words at full
// 32-bit bus addresses, update the shadow only when rvfi_mem_addr equals the
// shadowed address (wrong for misaligned accesses), and know nothing about GPIO.
//
// Model: one symbolic RAM byte address A (free constant) with a free initial value
// (contract section 2: the contents are whatever the test loaded). The monitor's
// decoded transactions update it (WRITE bytes) and are constrained by it (each READ
// data bit at A equals the memory's current bit). Bytes are compared on the 16-bit
// pin address, so the 64 KB alias and the misaligned wrap are exact; GPIO accesses
// (addr[31] = 1) are not RAM accesses and must never appear on the pins.
//
// Per instruction (contract section 6), the SPI traffic between the previous
// retirement and this one is this instruction's. With pc16 = pc_rdata[15:0] and
// ma = mem_addr[15:0]:
//   fetch byte  k < 4:              pc16 + k = A (mod 2^16)  -> insn byte k
//   load byte   k, rmask[k], RAM:   ma + k = A               -> mem_rdata byte k
//   store byte  k, wmask[k], RAM:   ma + k = A               -> mem_wdata byte k
// bus_imem: a fetch byte at A was read in full on the pins in this window, and it
//   equals A's contents at the start of the window (the instruction executes the
//   memory as it was before it ran).
// bus_dmem: a load byte at A was read in full in the window and equals A's contents
//   at the start of the window; a store byte at A was written exactly once in the
//   window with that value; every WRITE byte at A in the window is one of the
//   instruction's store bytes; every READ bit at A in the window belongs to one of
//   its fetch or load bytes (a fetch reads only its instruction's bytes, a load or
//   store moves only its own data).

`include "bonetto_macros.vh"

module bonetto_spimem (
	input clock,
	input reset,
	input rvfi_valid,
	input ev_rd_bit,
	input [15:0] ev_rd_addr,
	input [2:0] ev_rd_idx,
	input ev_rd_val,
	input ev_byte,
	input ev_byte_wr,
	input [15:0] ev_byte_addr,
	input [7:0] ev_byte_data,
	output [15:0] a,
	output [7:0] mstart,   // contents of A at the start of the current window
	output rd_any,         // a READ bit at A in the current window (this step included)
	output rd_full,        // a complete READ byte at A in the current window
	output [1:0] wr_cnt,   // WRITE bytes at A in the current window (saturates at 3)
	output [7:0] wr_val    // value of the last WRITE byte at A in the window
);
	`rvformal_rand_const_reg [15:0] addr;
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

	reg [7:0] ms;
	reg ra, rf;
	reg [1:0] wc;
	reg [7:0] wv;
	always @(posedge clock) begin
		if (reset || rvfi_valid) begin
			ms <= mem_next;
			ra <= 1'b0;
			rf <= 1'b0;
			wc <= 2'd0;
		end else begin
			if (rd_bit_hit) ra <= 1'b1;
			if (rd_byte_hit) rf <= 1'b1;
			if (wr_hit) begin
				wc <= (wc == 2'd3) ? 2'd3 : wc + 2'd1;
				wv <= ev_byte_data;
			end
		end
	end

	assign a = addr;
	assign mstart = ms;
	assign rd_any = ra || rd_bit_hit;
	assign rd_full = rf || rd_byte_hit;
	assign wr_cnt = wr_hit ? ((wc == 2'd3) ? 2'd3 : wc + 2'd1) : wc;
	assign wr_val = wr_hit ? ev_byte_data : wv;
endmodule

// Which bytes of the retiring instruction touch RAM byte A.
module bonetto_bus_claims (
	input [15:0] a,
	input [31:0] rvfi_insn,
	input [31:0] rvfi_pc_rdata,
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
	wire ram = !rvfi_mem_addr[31];
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

module bonetto_bus_imem_check (
	`BONETTO_CHECKER_PORTS
);
	wire [15:0] a;
	wire [7:0] mstart, wr_val;
	wire rd_any, rd_full;
	wire [1:0] wr_cnt;
	bonetto_spimem mem (
		.clock(clock), .reset(reset), .rvfi_valid(rvfi_valid),
		.ev_rd_bit(ev_rd_bit), .ev_rd_addr(ev_rd_addr), .ev_rd_idx(ev_rd_idx), .ev_rd_val(ev_rd_val),
		.ev_byte(ev_byte), .ev_byte_wr(ev_byte_wr), .ev_byte_addr(ev_byte_addr),
		.ev_byte_data(ev_byte_data),
		.a(a), .mstart(mstart), .rd_any(rd_any), .rd_full(rd_full), .wr_cnt(wr_cnt), .wr_val(wr_val)
	);

	wire fetch_hit, load_hit, store_hit;
	wire [7:0] fetch_byte, load_byte, store_byte;
	bonetto_bus_claims claims (
		.a(a), .rvfi_insn(rvfi_insn), .rvfi_pc_rdata(rvfi_pc_rdata), .rvfi_mem_addr(rvfi_mem_addr),
		.rvfi_mem_rmask(rvfi_mem_rmask), .rvfi_mem_wmask(rvfi_mem_wmask),
		.rvfi_mem_rdata(rvfi_mem_rdata), .rvfi_mem_wdata(rvfi_mem_wdata),
		.fetch_hit(fetch_hit), .fetch_byte(fetch_byte), .load_hit(load_hit), .load_byte(load_byte),
		.store_hit(store_hit), .store_byte(store_byte)
	);

	wire valid = !reset && rvfi_valid;
	always @* begin
		if (valid && check && fetch_hit) begin
			bus_imem_read: assert (rd_full);
			bus_imem_value: assert (fetch_byte == mstart);
		end
		if (!reset) `BONETTO_REACH(reach_bus_imem, valid && fetch_hit)
	end
endmodule

module bonetto_bus_dmem_check (
	`BONETTO_CHECKER_PORTS
);
	wire [15:0] a;
	wire [7:0] mstart, wr_val;
	wire rd_any, rd_full;
	wire [1:0] wr_cnt;
	bonetto_spimem mem (
		.clock(clock), .reset(reset), .rvfi_valid(rvfi_valid),
		.ev_rd_bit(ev_rd_bit), .ev_rd_addr(ev_rd_addr), .ev_rd_idx(ev_rd_idx), .ev_rd_val(ev_rd_val),
		.ev_byte(ev_byte), .ev_byte_wr(ev_byte_wr), .ev_byte_addr(ev_byte_addr),
		.ev_byte_data(ev_byte_data),
		.a(a), .mstart(mstart), .rd_any(rd_any), .rd_full(rd_full), .wr_cnt(wr_cnt), .wr_val(wr_val)
	);

	wire fetch_hit, load_hit, store_hit;
	wire [7:0] fetch_byte, load_byte, store_byte;
	bonetto_bus_claims claims (
		.a(a), .rvfi_insn(rvfi_insn), .rvfi_pc_rdata(rvfi_pc_rdata), .rvfi_mem_addr(rvfi_mem_addr),
		.rvfi_mem_rmask(rvfi_mem_rmask), .rvfi_mem_wmask(rvfi_mem_wmask),
		.rvfi_mem_rdata(rvfi_mem_rdata), .rvfi_mem_wdata(rvfi_mem_wdata),
		.fetch_hit(fetch_hit), .fetch_byte(fetch_byte), .load_hit(load_hit), .load_byte(load_byte),
		.store_hit(store_hit), .store_byte(store_byte)
	);

	wire valid = !reset && rvfi_valid;
	always @* begin
		if (valid && check) begin
			if (load_hit) begin
				bus_dmem_load_read: assert (rd_full);
				bus_dmem_load_value: assert (load_byte == mstart);
			end
			if (store_hit) begin
				bus_dmem_store_once: assert (wr_cnt == 2'd1);
				bus_dmem_store_value: assert (wr_val == store_byte);
			end
			if (wr_cnt != 2'd0) bus_dmem_write_claimed: assert (store_hit);
			if (rd_any) bus_dmem_read_claimed: assert (fetch_hit || load_hit);
		end
		if (!reset) begin
			`BONETTO_REACH(reach_bus_dmem_load, valid && load_hit)
			`BONETTO_REACH(reach_bus_dmem_store, valid && store_hit)
		end
	end
endmodule
