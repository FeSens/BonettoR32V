// Stand-in SPI driver for the harness self-tests (not a core). With the TT top-level pins, it runs
// a fixed sequence: WRITE DE AD BE EF at 0x8000, READ the 4 bytes back, then shows them on uo_out
// one after another. SCK = clk / 2; MOSI changes on falling SCK edges; MISO is sampled at the
// edge where SCK falls (while it is still high in the sampled waveform).
//
// Each BUG_* define breaks one contract rule; test_cosim_harness.py checks that the harness
// reports each one (harness --spi-only).
`default_nettype none

module tt_um_fesens_r32v (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  localparam IDLE = 3'd0, TX = 3'd1, ENDTX = 3'd2, SHOW = 3'd3, DONE = 3'd4;

  reg [2:0] state;
  reg       which;       // 0: WRITE transaction, 1: READ transaction
  reg       cs_n, sck, mosi;
  reg [55:0] sh;
  reg [5:0] nbits, bitn;
  reg [31:0] rx;
  reg [7:0] uo;
  reg [3:0] cnt;
  reg [1:0] idx;
`ifdef BUG_NO_RESET
  reg [7:0] junk;
  always @(posedge clk) junk <= junk;
`endif

`ifdef BUG_BAD_CMD
  localparam [7:0] WCMD = 8'h05;
`else
  localparam [7:0] WCMD = 8'h02;
`endif
`ifdef BUG_WRITE_MIDBYTE
  localparam [5:0] WBITS = 6'd36;
`else
  localparam [5:0] WBITS = 6'd56;
`endif
`ifdef BUG_SHORT_TXN
  localparam [5:0] RBITS = 6'd20;
`else
  localparam [5:0] RBITS = 6'd56;
`endif

  always @(posedge clk) begin
    if (!rst_n) begin
`ifdef BUG_RESET_CS
      cs_n  <= 1'b0;
`else
      cs_n  <= 1'b1;
`endif
      sck   <= 1'b0;
      mosi  <= 1'b0;
      state <= IDLE;
      which <= 1'b0;
      cnt   <= 4'd0;
      uo    <= 8'd0;
      idx   <= 2'd0;
    end else begin
      case (state)
        IDLE: begin
          cnt <= cnt + 4'd1;
          if (cnt == 4'd5) begin
            state <= TX;
            cs_n  <= 1'b0;
            bitn  <= 6'd0;
            if (!which) begin
              sh    <= {WCMD, 16'h8000, 32'hDEADBEEF};
              nbits <= WBITS;
              mosi  <= WCMD[7];
            end else begin
              sh    <= {8'h03, 16'h8000, 32'h0};
              nbits <= RBITS;
              mosi  <= 1'b0;
            end
          end
        end
        TX: begin
          if (!sck) begin
            sck <= 1'b1;
`ifdef BUG_MISO_LOW
            rx  <= {rx[30:0], uio_in[2]};  // samples while SCK is low
`endif
`ifdef BUG_MOSI_RISE
            mosi <= sh[54];                // changes MOSI on the rising SCK edge
            sh   <= sh << 1;
`endif
`ifdef BUG_CS_SCK_HIGH
            if (bitn + 6'd1 == nbits) cs_n <= 1'b1;  // CS_n rises together with SCK
`endif
          end else begin
            sck  <= 1'b0;
`ifndef BUG_MISO_LOW
            rx   <= {rx[30:0], uio_in[2]};
`endif
`ifndef BUG_MOSI_RISE
            mosi <= sh[54];
            sh   <= sh << 1;
`endif
            bitn <= bitn + 6'd1;
            if (bitn + 6'd1 == nbits) state <= ENDTX;
          end
        end
        ENDTX: begin
          cs_n <= 1'b1;
          cnt  <= 4'd0;
          if (!which) begin
            which <= 1'b1;
            state <= IDLE;
          end else begin
            state <= SHOW;
          end
        end
        SHOW: begin
          cnt <= cnt + 4'd1;
          if (cnt == 4'd3) begin
`ifdef BUG_NO_RESET
            uo  <= rx[31:24] ^ junk;       // junk is never reset: correct only if it powers up 0
`else
            uo  <= rx[31:24];
`endif
            rx  <= rx << 8;
            cnt <= 4'd0;
            idx <= idx + 2'd1;
            if (idx == 2'd3) state <= DONE;
          end
        end
        default: ;
      endcase
    end
  end

  assign uo_out = uo;
`ifdef BUG_COMB_OUT
  assign uio_out = {4'b0, sck, 1'b0, mosi ^ (cs_n & uio_in[7]), cs_n};  // MOSI follows an input
`elsif BUG_UIO_OUT
  assign uio_out = {4'b0, sck, 1'b1, mosi, cs_n};
`else
  assign uio_out = {4'b0, sck, 1'b0, mosi, cs_n};
`endif
`ifdef BUG_OE
  assign uio_oe = 8'b0000_1111;
`else
  assign uio_oe = 8'b0000_1011;
`endif

  wire _unused = &{ena, ui_in, uio_in[7:3], uio_in[1:0], 1'b0};
endmodule
