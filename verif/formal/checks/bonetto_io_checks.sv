// BonettoR32V IO checks (contract v3 sections 5 and 6): the GPIO pin timing and a register-level
// check of every peripheral slot. Bit-exact timer, PWM, UART and SPI waveforms are the cosim's
// (verif/cosim/periph_models.h). Both are compared up to the first UNSPECIFIED retirement,
// exclusive (a misaligned IO access is one).

`include "bonetto_macros.vh"

// The IO store or load a compared, non-trapping retirement makes (at a compared retirement an IO
// access is naturally aligned: a misaligned one is the UNSPECIFIED stop).
`define BON_IO_DECODE \
	wire full = !reset && rvfi_valid && `BONETTO_FULL; \
	wire io_acc = full && !rvfi_trap && rvfi_mem_addr[31]; \
	wire [4:0] slot = rvfi_mem_addr[6:2]; \
	wire [1:0] off = rvfi_mem_addr[1:0]; \
	wire [3:0] wl = rvfi_mem_wmask << off;   /* slot bytes the store writes */ \
	wire [3:0] rl = rvfi_mem_rmask << off;   /* slot bytes the load reads */ \
	wire [31:0] wd = rvfi_mem_wdata << {off, 3'b000};   /* store data in slot positions */ \
	wire [31:0] rdd = rvfi_mem_rdata << {off, 3'b000};  /* load data in slot positions */ \
	wire io_st = io_acc && rvfi_mem_wmask != 4'd0; \
	wire io_ld = io_acc && rvfi_mem_rmask != 4'd0;

// GPIO pins (contract 6): a uo pin whose ALTFN bit is 0 shows GPIO_OUT's bit. It takes a stored
// value no later than one cycle after the store retires, and changes only because of such a
// store or an ALTFN write. g and a are the architectural GPIO_OUT and ALTFN, updated one step
// after the store's rvfi_valid (both 0 after reset). In the step after each compared retirement
// (and after reset) every GPIO-mode pin equals g (gpio_uo_hold). Within an instruction's window
// (from the step after that up to its own rvfi_valid) a pin in GPIO mode before and after the
// instruction may move only to its new GPIO_OUT value, if the instruction changes that bit, and
// only once (it may move early, inside the store's own window): checked at the retirement
// (gpio_uo_change, gpio_uo_once). A pin whose ALTFN bit changes is compared from the step after
// the ALTFN store on; pins with ALTFN = 1 carry their peripheral and are not compared.
module bonetto_gpio_check (
	`BONETTO_CHECKER_PORTS
);
	`BON_IO_DECODE
	wire st_g = io_st && slot == 5'd0 && wl[0];
	wire st_a = io_st && slot == 5'd2 && wl[0];
	reg [7:0] g, a;
	wire [7:0] g_new = st_g ? wd[7:0] : g;
	wire [7:0] a_new = st_a ? wd[7:0] : a;
	wire [7:0] devn = pin_uo_out ^ g;      // pins off GPIO_OUT this step
	reg [7:0] dev;     // left GPIO_OUT in this window
	reg [7:0] twice;   // ... and then came back
	reg strict;        // this step is the step after a compared retirement (or after reset)
	always @(posedge clock) begin
		if (reset) begin
			g <= 8'h00;
			a <= 8'h00;
			dev <= 8'h00;
			twice <= 8'h00;
			strict <= 1'b1;
		end else begin
			strict <= full;
			if (rvfi_valid) begin
				dev <= 8'h00;
				twice <= 8'h00;
				if (full) begin
					g <= g_new;
					a <= a_new;
				end
			end else if (!strict) begin
				dev <= dev | devn;
				twice <= twice | (dev & ~devn);
			end
		end
	end
	wire live = !reset && !bon_stopped;
	wire [7:0] m = ~a & ~a_new;            // GPIO mode before and after this instruction
	wire [7:0] moved = dev | devn;
	always @* begin
		if (live && strict) gpio_uo_hold: assert (((pin_uo_out ^ g) & ~a) == 8'h00);
		if (full && check) begin
			gpio_uo_change: assert ((moved & m & ~(g_new ^ g)) == 8'h00);
			gpio_uo_once: assert ((twice & m) == 8'h00 && (dev & m & ~devn) == 8'h00);
		end
		if (!reset) begin
			`BONETTO_REACH(reach_gpio_out, st_g && ((g_new ^ g) & ~a) != 8'h00)
			`BONETTO_REACH(reach_gpio_altfn, st_a && a_new != a)
		end
	end
endmodule

// Peripheral registers (contract 5.1), per slot: reset values, read/write masks, byte lanes (a
// store writes only the bytes it covers), read-only and unimplemented bits read 0, W1C bits and
// side effects only for the byte named. A model of every slot, updated by compared IO stores,
// is compared with every compared IO load, bit by bit where the model knows the bit:
//   - read/write bits (contract 5.1 masks, IO_WRITE_MASK in verif/cosim/iss3.h): always known;
//   - GPIO_IN: the formal environment holds ui_in constant, so it reads ui_in;
//   - bits the hardware changes on its own are known only while their source is provably off:
//       TIMER_CNT: per byte, from reset or from a write made while the timer is disabled, as
//         long as it stays disabled (no ticks; contract 5.3);
//       TIMER_CAP and IRQ_PEND.CAP: 0 while the capture edge select has been off since reset
//         (CAP: or since a W1C clear made with it off);
//       IRQ_PEND.CMP: 0 from reset, or from a W1C clear made while the timer is disabled, as
//         long as it stays disabled;
//       GPIO_EPEND bit i: 0 from reset, or from a W1C clear made while GPIO_EDGE enable bit i is
//         0, as long as it stays 0; IRQ_PEND.GPIO (their OR) where all eight are known;
//       TX_BUSY 0 and IRQ_PEND.TXIDLE 1 until the first write of UART_DATA byte 0; SPI BUSY and
//         SPI_DATA's received byte 0 until the first write of SPI_DATA byte 0 (contract 5:
//         writing byte 0 starts a transfer, a write that misses byte 0 must not);
//   - the UART receiver's bits (UART_DATA's byte, RX_FULL, RX_OVR, IRQ_PEND.RX) are not
//     compared: they depend on the RX pin's synchronizer history.
module bonetto_periph_check (
	`BONETTO_CHECKER_PORTS
);
	`BON_IO_DECODE
	wire [31:0] wbm = {{8{wl[3]}}, {8{wl[2]}}, {8{wl[1]}}, {8{wl[0]}}};   // written bits

	// Read/write registers (slot bit positions).
	reg [7:0] gpo, alt, een, epol;
	reg [15:0] tcnt, tcmp;
	reg [9:0] tctl, duty0, duty1, duty2;
	reg [14:0] pctl;
	reg [12:0] div;
	reg [3:0] sctl;
	reg [4:0] ien;
	// Knowledge of the volatile bits.
	reg [1:0] cnt_k;      // TIMER_CNT bytes known
	reg cap_off;          // capture select off since reset
	reg cap_k0;           // IRQ_PEND.CAP known 0
	reg cmp_k0;           // IRQ_PEND.CMP known 0
	reg [7:0] ep_k0;      // GPIO_EPEND bits known 0
	reg tx_go, spi_go;    // a UART_DATA / SPI_DATA byte-0 write has happened

	wire t_en = tctl[4];
	wire [1:0] t_cap = tctl[9:8];

	// The model's value of a slot and which of its bits are known.
	reg [31:0] mv, mk;
	always @* begin
		mv = 32'd0;
		mk = 32'hffffffff;
		case (slot)
			5'd0: mv[7:0] = gpo;
			5'd1: mv[7:0] = pin_ui_in;
			5'd2: mv[7:0] = alt;
			5'd3: begin
				mv[15:0] = tcnt;
				mk[15:0] = {{8{cnt_k[1]}}, {8{cnt_k[0]}}};
			end
			5'd4: mv[15:0] = tcmp;
			5'd5: mk[15:0] = {16{cap_off}};
			5'd6: mv[9:0] = tctl;
			5'd7: mv[14:0] = pctl;
			5'd8: mv[9:0] = duty0;
			5'd9: mv[9:0] = duty1;
			5'd10: mv[9:0] = duty2;
			5'd11: mk[7:0] = 8'h00;
			5'd12: begin
				mv[12:0] = div;
				mk[15:13] = {2'b00, !tx_go};
			end
			5'd13: mk[7:0] = {8{!spi_go}};
			5'd14: begin
				mv[3:0] = sctl;
				mk[4] = !spi_go;
			end
			5'd15: mv[4:0] = ien;
			5'd16: begin
				mv[3] = 1'b1;
				mk[4:0] = {ep_k0 == 8'hff, !tx_go, 1'b0, cap_k0, cmp_k0};
			end
			5'd17: mv[15:0] = {epol, een};
			5'd18: mk[7:0] = ep_k0;
			default: ;
		endcase
	end

	// Stores (contract 5: only the bytes covered; W1C and transfer starts on byte 0 only).
	wire s0 = io_st && wl[0];
	wire [31:0] wv = wd;
	wire en_after = (io_st && slot == 5'd6 && wl[0]) ? wv[4] : t_en;
	wire [1:0] cap_after = (io_st && slot == 5'd6 && wl[1]) ? wv[9:8] : t_cap;
	wire [7:0] een_after = (io_st && slot == 5'd17 && wl[0]) ? wv[7:0] : een;
	always @(posedge clock) begin
		if (reset) begin
			gpo <= 8'h00; alt <= 8'h00; een <= 8'h00; epol <= 8'h00;
			tcnt <= 16'h0000; tcmp <= 16'h0000;
			tctl <= 10'h000; duty0 <= 10'h000; duty1 <= 10'h000; duty2 <= 10'h000;
			pctl <= 15'h0000; div <= 13'h0000; sctl <= 4'h1; ien <= 5'h00;
			cnt_k <= 2'b11; cap_off <= 1'b1; cap_k0 <= 1'b1; cmp_k0 <= 1'b1; ep_k0 <= 8'hff;
			tx_go <= 1'b0; spi_go <= 1'b0;
		end else if (io_st) begin
			case (slot)
				5'd0: if (wl[0]) gpo <= wv[7:0];
				5'd2: if (wl[0]) alt <= wv[7:0];
				5'd3: begin
					tcnt <= (tcnt & ~wbm[15:0]) | (wv[15:0] & wbm[15:0]);
					// a byte written while the timer is disabled is known
					if (!t_en) cnt_k <= cnt_k | wl[1:0];
				end
				5'd4: tcmp <= (tcmp & ~wbm[15:0]) | (wv[15:0] & wbm[15:0]);
				5'd6: tctl <= (tctl & ~wbm[9:0]) | (wv[9:0] & wbm[9:0]);
				5'd7: pctl <= (pctl & ~wbm[14:0]) | (wv[14:0] & wbm[14:0]);
				5'd8: duty0 <= (duty0 & ~wbm[9:0]) | (wv[9:0] & wbm[9:0]);
				5'd9: duty1 <= (duty1 & ~wbm[9:0]) | (wv[9:0] & wbm[9:0]);
				5'd10: duty2 <= (duty2 & ~wbm[9:0]) | (wv[9:0] & wbm[9:0]);
				5'd11: if (wl[0]) tx_go <= 1'b1;
				5'd12: div <= (div & ~wbm[12:0]) | (wv[12:0] & wbm[12:0]);
				5'd13: if (wl[0]) spi_go <= 1'b1;
				5'd14: if (wl[0]) sctl <= wv[3:0];
				5'd15: if (wl[0]) ien <= wv[4:0];
				5'd16: if (wl[0]) begin
					if (wv[0] && !t_en) cmp_k0 <= 1'b1;
					if (wv[1] && t_cap == 2'b00) cap_k0 <= 1'b1;
				end
				5'd17: begin
					if (wl[0]) een <= wv[7:0];
					if (wl[1]) epol <= wv[15:8];
				end
				default: ;
			endcase
			// The sources turning on (after the store, so a clear in the same store counts
			// first: different slots anyway).
			if (en_after) begin
				cnt_k <= 2'b00;
				cmp_k0 <= 1'b0;
			end
			if (cap_after != 2'b00) begin
				cap_off <= 1'b0;
				cap_k0 <= 1'b0;
			end
			// GPIO_EPEND: a W1C clear of a disabled pin's flag makes it known 0; enabling a pin
			// makes its flag unknown.
			ep_k0 <= ((slot == 5'd18 && wl[0]) ? (ep_k0 | (wv[7:0] & ~een)) : ep_k0) & ~een_after;
		end
	end

	wire [31:0] rbm = {{8{rl[3]}}, {8{rl[2]}}, {8{rl[1]}}, {8{rl[0]}}};   // read bits
	always @* begin
		if (io_ld && check) periph_read: assert (((rdd ^ mv) & mk & rbm) == 32'd0);
		if (!reset) begin
			`BONETTO_REACH(reach_periph_rw, io_ld && ((slot == 5'd4 && tcmp != 16'd0) ||
			                                          (slot == 5'd7 && pctl != 15'd0) ||
			                                          (slot == 5'd17 && {epol, een} != 16'd0)))
			`BONETTO_REACH(reach_periph_pend, io_ld && slot == 5'd16 && rl[0])
			`BONETTO_REACH(reach_periph_gpio_in, io_ld && slot == 5'd1 && rl[0])
		end
	end
endmodule
