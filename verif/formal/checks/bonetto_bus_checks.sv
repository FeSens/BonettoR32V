// BonettoR32V bus checks (contract v3): the QSPI Pmod rules at the pins, PSRAM write attribution
// and coherence. They replace riscv-formal's checks/rvfi_bus_{imem,dmem}_check.sv (Copyright (C)
// 2017 Claire Xenia Wolf, ISC license, see third_party/riscv-formal/COPYING), which shadow
// aligned words at full bus addresses and know nothing about the pins. Contract 8 removed v2's
// per-instruction transaction attribution; in its place:
//   qspi         contract 2 on every cycle (bonetto_qspi_check);
//   psram_write  every PSRAM write writes only core-region bytes, or bytes of retired stores at
//                their addresses with their data (bonetto_psram_write_check);
//   coherence    every load and fetch returns the value most recently stored to that address,
//                or the loaded image (bonetto_coherence_check).

`include "bonetto_macros.vh"

// Contract 1 (constant pins, reset state) and 2 (waveform rules, command legality, PSRAM
// timing), on every cycle. Never stops: these hold after an UNSPECIFIED retirement too.
module bonetto_qspi_check (
	`BONETTO_CHECKER_PORTS
);
	wire cf = pin_uio_out[0], cr = pin_uio_out[6], sck = pin_uio_out[3];
	wire pcf = pin_puio_out[0], pcr = pin_puio_out[6], psck = pin_puio_out[3];
	localparam [7:0] SDALL = 8'b0011_0110, SD1 = 8'b0000_0100;

	always @* begin
		if (mon_en) begin
			// Section 1: uio_oe[0], [3], [6], [7] = 1 and uio_out[7] = 1 at all times.
			qspi_oe_fixed: assert (pin_uio_oe[0] && pin_uio_oe[3] && pin_uio_oe[6] &&
			                       pin_uio_oe[7]);
			qspi_csb: assert (pin_uio_out[7]);
			// 2.1.1: at most one CS_n is low. 2.1.3: SCK is 0 whenever both are high.
			qspi_rule1: assert (cf || cr);
			qspi_rule3: assert (!sck || !(cf && cr));
		end
		// Section 1: while rst_n is low both CS_n are 1, SCK is 0, uo_out is 0; nothing retires.
		if (mon_en && reset_q) begin
			qspi_reset_cs: assert (cf && cr);
			qspi_reset_sck: assert (!sck);
			qspi_reset_uo: assert (pin_uo_out == 8'h00);
			qspi_reset_valid: assert (!rvfi_valid);
		end
		if (mon_pair) begin
			// 2.1.2: a CS_n changes only while SCK is low at t-1 and t.
			if (cf != pcf || cr != pcr) qspi_rule2: assert (!psck && !sck);
			// 2.1.4: a line the core drives at a rising SCK edge was driven at t-1 too, with the
			// same value (releasing a line at the edge is fine).
			if (mon_rise) qspi_rule4: assert ((pin_uio_oe & SDALL &
			                                   (~pin_puio_oe | (pin_uio_out ^ pin_puio_out))) == 8'h00);
		end
		if (mon_en) begin
			// 2.1.6: no contention: during a chip's data-out phase the core drives none of its
			// output lines (SD1 for a single read, SD0-SD3 for a quad read).
			if (f_dout) qspi_rule6_flash: assert ((pin_uio_oe & (f_dquad ? SDALL : SD1)) == 8'h00);
			if (r_dout) qspi_rule6_psram: assert ((pin_uio_oe & (r_dquad ? SDALL : SD1)) == 8'h00);
			// 2.1.7: PSRAM CS_n low for at most tCEM; 2.3: CS_n high for tRST after 0x99.
			qspi_tcem: assert (!r_bad_cem);
			qspi_trst: assert (!r_bad_trst);
			// 2.1.10, 2.2, 2.3, 2.4: only the listed commands for the chip's state, 0x66 then
			// 0x99, no PSRAM access before its reset, nothing after a command that takes no
			// fields.
			qspi_cmd_flash: assert (!f_bad_cmd);
			qspi_cmd_psram: assert (!r_bad_cmd);
			qspi_extra: assert (!f_bad_extra && !r_bad_extra);
			// 2.1.9: a write ends with its address complete and on a byte boundary.
			qspi_wend: assert (!f_bad_wend && !r_bad_wend);
		end
	end

	// Triggers (reach task): a flash read, a PSRAM read and a PSRAM write, each with data.
	always @* begin
		if (!reset) begin
			`BONETTO_REACH(reach_qspi_flash_read, f_fin_rd)
			`BONETTO_REACH(reach_qspi_psram_read, r_fin_rd)
			`BONETTO_REACH(reach_qspi_psram_write, r_fin_wr)
		end
	end
endmodule

// Whether CPU byte address b (a byte of a retirement's access) is PSRAM byte a23.
`define BON_PSRAM_BYTE(_b, _a23) (!_b[31] && _b[24] && _b[22:0] == _a23)

// Contract 8: every PSRAM write outside the core region writes a byte of a retired store, at
// its address, with its data. One symbolic PSRAM byte address wa outside the region and one
// symbolic value wv (free constants, so every pair is covered). A write of wv at wa is
// explained by a compared, non-trapping retirement that stores wv at wa (rvfi_mem_wmask lane k,
// byte address rvfi_mem_addr + k). The write may land before its store retires (a core without
// a write buffer writes during the store's own execution), but no later than the store's
// retirement: every write since the previous retirement must be explained by the stores retired
// so far, this retirement included (the cosim's rule, verif/cosim/harness.cpp
// check_psram_writes). Writes after a store retires (write buffers) are explained by it.
// Not covered here: that every store byte is written at all (coherence and the cosim's final
// memory compare), and the order of two writes of different values (coherence).
module bonetto_psram_write_check (
	`BONETTO_CHECKER_PORTS
);
	`rvformal_rand_const_reg [22:0] wa;
	`rvformal_rand_const_reg [7:0] wv;
	always @* assume (wa[22:7] != 16'hffff);   // the core region is exempt

	wire full = !reset && rvfi_valid && `BONETTO_FULL;
	wire [31:0] b0 = rvfi_mem_addr, b1 = rvfi_mem_addr + 32'd1, b2 = rvfi_mem_addr + 32'd2,
	            b3 = rvfi_mem_addr + 32'd3;
	wire hit = (rvfi_mem_wmask[0] && `BON_PSRAM_BYTE(b0, wa) && rvfi_mem_wdata[7:0] == wv) ||
	           (rvfi_mem_wmask[1] && `BON_PSRAM_BYTE(b1, wa) && rvfi_mem_wdata[15:8] == wv) ||
	           (rvfi_mem_wmask[2] && `BON_PSRAM_BYTE(b2, wa) && rvfi_mem_wdata[23:16] == wv) ||
	           (rvfi_mem_wmask[3] && `BON_PSRAM_BYTE(b3, wa) && rvfi_mem_wdata[31:24] == wv);
	wire st_now = full && !rvfi_trap && hit;
	wire wr_now = !reset && r_wb && r_wb_addr == wa && r_wb_data == wv;
	reg st_seen;   // a retired store wrote wv at wa
	reg pend;      // a write of wv at wa since the previous retirement
	always @(posedge clock) begin
		if (reset) begin
			st_seen <= 1'b0;
			pend <= 1'b0;
		end else begin
			if (st_now) st_seen <= 1'b1;
			if (rvfi_valid) pend <= 1'b0;
			else if (wr_now) pend <= 1'b1;
		end
	end
	wire wrote = pend || wr_now;

	always @* begin
		if (full && check && wrote) psram_write_attr: assert (st_seen || st_now);
		if (!reset) `BONETTO_REACH(reach_psram_write, full && wrote && (st_seen || st_now))
	end
endmodule

// Contract 3, coherence: every load and fetch returns the value most recently stored to that
// address by the program, or the loaded image if nothing was stored. One symbolic byte X: the
// flash byte ax (cx = 0) or the PSRAM byte ax[22:0] (cx = 1, outside the core region).
//   - pmem: the chip's byte at X, with a free initial value. The flash never changes (its 0x02
//     is ignored, contract 2.2); a PSRAM write byte at X updates it. Every data-out edge at X
//     drives pmem's bits (the bit or nibble the edge clocks). This, and the core region in the
//     wrapper, are the only memory bytes that are not noise.
//   - amem: the architectural byte at X: pmem's initial value (the loaded image, or the
//     PSRAM's arbitrary content), then the bytes of compared, non-trapping retirements that
//     store at X. A store to flash changes nothing (contract 3).
//   - At each compared retirement, every fetched byte (rvfi_insn, pc_rdata + k) and every
//     loaded byte (rvfi_mem_rdata lane k, rvfi_mem_addr + k) at X equals amem before this
//     retirement. The fetch is checked at the UNSPECIFIED retirement too, unless the fetch
//     itself is UNSPECIFIED: its instruction word decided the stop.
// Caches, prefetch and write buffers are free in between: only what retires is compared.
module bonetto_coherence_check (
	`BONETTO_CHECKER_PORTS
);
	`rvformal_rand_const_reg cx;
	`rvformal_rand_const_reg [23:0] ax;
	always @* if (cx) assume (!ax[23] && ax[22:7] != 16'hffff);

	reg [7:0] pmem;
	reg [7:0] amem;
	always @* if (reset) assume (amem == pmem);

	// The environment drives X's bits at each data-out edge at X.
	wire f_hit = !cx && f_rd && f_rd_addr == ax;
	wire r_hit = cx && r_rd && r_rd_addr[22:0] == ax[22:0];
	wire rquad = f_hit ? f_rd_quad : r_rd_quad;
	wire [2:0] ridx = f_hit ? f_rd_idx : r_rd_idx;
	always @* begin
		if (!reset && (f_hit || r_hit)) begin
			if (rquad) assume (mon_sd == (ridx[0] ? pmem[3:0] : pmem[7:4]));
			else assume (mon_sd[1] == pmem[3'd7 - ridx]);
		end
	end

	// CPU byte address b is X.
	function is_x(input [31:0] b);
		is_x = !b[31] && b[24] == cx && (cx ? b[22:0] == ax[22:0] : b[23:0] == ax);
	endfunction

	wire full = !reset && rvfi_valid && `BONETTO_FULL;
	wire fvalid = !reset && rvfi_valid && !bon_stopped && !bon_fetch_unspec;
	wire [31:0] pc = rvfi_pc_rdata, ma = rvfi_mem_addr;
	reg fetch_hit, fetch_ok, load_hit, load_ok, store_hit;
	reg [7:0] store_byte;
	integer k;
	always @* begin
		fetch_hit = 1'b0;
		fetch_ok = 1'b1;
		load_hit = 1'b0;
		load_ok = 1'b1;
		store_hit = 1'b0;
		store_byte = 8'h00;
		for (k = 0; k < 4; k = k + 1) begin
			if (is_x(pc + k)) begin
				fetch_hit = 1'b1;
				if (rvfi_insn[8*k +: 8] != amem) fetch_ok = 1'b0;
			end
			if (!rvfi_trap && rvfi_mem_rmask[k] && is_x(ma + k)) begin
				load_hit = 1'b1;
				if (rvfi_mem_rdata[8*k +: 8] != amem) load_ok = 1'b0;
			end
			if (!rvfi_trap && rvfi_mem_wmask[k] && is_x(ma + k)) begin
				store_hit = 1'b1;
				store_byte = rvfi_mem_wdata[8*k +: 8];
			end
		end
	end

	reg stored;   // a compared store wrote X
	always @(posedge clock) begin
		if (reset) begin
			stored <= 1'b0;
		end else begin
			if (cx && r_wb && r_wb_addr == ax[22:0]) pmem <= r_wb_data;
			if (full && store_hit && cx) begin
				amem <= store_byte;
				stored <= 1'b1;
			end
		end
	end

	always @* begin
		if (fvalid && check && fetch_hit) coherence_fetch: assert (fetch_ok);
		if (full && check && load_hit) coherence_load: assert (load_ok);
		if (!reset) begin
			`BONETTO_REACH(reach_coherence_fetch, full && fetch_hit)
			`BONETTO_REACH(reach_coherence_load, full && load_hit)
			`BONETTO_REACH(reach_coherence_store_load, full && load_hit && stored)
		end
	end
endmodule
