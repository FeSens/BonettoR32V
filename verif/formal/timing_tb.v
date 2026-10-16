// Fallback timing measurement for run_formal.py's depth policy, used only when the
// cosim's `run_cosim.py --timing-json` is not available. Runs the RISCV_FORMAL build
// of the TT top against a behavioural 23LC512 (contract section 2: MISO driven from
// the rising SCK edge while SCK is high, random noise while it is low) and prints
// one line per retirement: "RETIRE <cycle> <insn hex> <trap>". Cycle 0 is the first
// rising edge with rst_n high. It ends on a halt or after +max_cycles.
//
// iverilog -g2012 -DRISCV_FORMAL -o tb timing_tb.v src/*.v   (RISCV_FORMAL is the only
// define src/ sees)
// vvp tb +hex=<64 KB byte image, one byte per line> [+max_cycles=N]
`timescale 1ns/1ps

module timing_tb;
	reg clk = 0;
	reg rst_n = 0;
	wire [7:0] uo_out, uio_out, uio_oe;
	reg [7:0] ui_in = 8'h5a;
	reg miso;
	wire [7:0] uio_in = {4'b0, uio_out[3], miso, uio_out[1:0]};

	// The RVFI ports this testbench reads, connected by name (contract section 6);
	// the others are left open.
	wire rvfi_valid, rvfi_trap, rvfi_halt;
	wire [31:0] rvfi_insn;

	tt_um_fesens_r32v uut (
		.ui_in(ui_in), .uo_out(uo_out), .uio_in(uio_in), .uio_out(uio_out), .uio_oe(uio_oe),
		.ena(1'b1), .clk(clk), .rst_n(rst_n),
		.rvfi_valid(rvfi_valid), .rvfi_insn(rvfi_insn), .rvfi_trap(rvfi_trap),
		.rvfi_halt(rvfi_halt)
	);

	reg [7:0] mem [0:65535];
	wire cs = uio_out[0], mosi = uio_out[1], sck = uio_out[3];
	reg psck = 0;
	reg [5:0] hdr;
	reg [7:0] cmd;
	reg [15:0] addr;
	reg [2:0] bidx;
	reg [7:0] wbyte;
	reg drive_bit;
	integer cycle = 0, max_cycles = 200000;
	reg [1023:0] hexfile;

	initial begin
		if (!$value$plusargs("hex=%s", hexfile)) begin
			$display("ERROR no +hex");
			$finish;
		end
		if ($value$plusargs("max_cycles=%d", max_cycles)) ;
		$readmemh(hexfile, mem);
	end

	always #5 clk = ~clk;

	// The memory, evaluated after every rising clock edge on the new pin values.
	always @(posedge clk) begin
		#1;
		if (cs) begin
			hdr = 0; bidx = 0;
		end else if (!psck && sck) begin
			if (hdr < 8) begin
				cmd = {cmd[6:0], mosi}; hdr = hdr + 1;
			end else if (hdr < 24) begin
				addr = {addr[14:0], mosi}; hdr = hdr + 1;
			end else begin
				if (cmd == 8'h03) begin
					drive_bit = mem[addr][7 - bidx];
				end else begin
					wbyte = {wbyte[6:0], mosi};
					if (bidx == 7) mem[addr] = {wbyte[6:0], mosi};
				end
				bidx = bidx + 1;
				if (bidx == 0) addr = addr + 1;
			end
		end
		miso = (sck && !cs) ? drive_bit : $random;
		psck = sck;
	end

	initial begin
		miso = 0;
		repeat (2) @(posedge clk);
		#2 rst_n = 1;
	end

	always @(posedge clk) begin
		#2;
		if (rst_n) begin
			if (rvfi_valid) begin
				$display("RETIRE %0d %08x %0d", cycle, rvfi_insn, rvfi_trap);
				if (rvfi_halt) begin
					$display("HALT %0d", cycle);
					$finish;
				end
			end
			cycle = cycle + 1;
			if (cycle > max_cycles) begin
				$display("TIMEOUT %0d", cycle);
				$finish;
			end
		end
	end
endmodule
