// Equivalence-gate MUTANT: in the RISCV_FORMAL build, an RVFI-only counter clears the GPIO
// register (state both builds share) when it reaches 0xBEEF. tools/equiv.py must FAIL.
`default_nettype none

module r32v_fx_core (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] din,
    input  wire       miso,
    output reg  [7:0] gpio,
    output reg        sck,
    output wire       retire,
    output wire [7:0] acc
);
    reg [7:0] rf [0:3];
    reg [1:0] ptr;
`ifdef RISCV_FORMAL
    reg [15:0] dbg_q;
    always @(posedge clk or negedge rst_n) if (!rst_n) dbg_q <= 16'd0; else dbg_q <= dbg_q + 16'd1;
`endif

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ptr  <= 2'd0;
            gpio <= 8'd0;
            sck  <= 1'b0;
        end else begin
            ptr <= ptr + 2'd1;
            sck <= ~sck;
            if (ptr == 2'd3) gpio <= rf[0] ^ rf[2];
`ifdef RISCV_FORMAL
            // THE MUTATION: RVFI-only state changes the next value of a shared register.
            if (dbg_q == 16'hBEEF) gpio <= 8'h00;
`endif
        end
    end

    always @(posedge clk) rf[ptr] <= rf[ptr] + din + {7'd0, miso};

    assign retire = (ptr == 2'd3);
    assign acc    = rf[1];
endmodule
