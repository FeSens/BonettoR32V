// Fixture submodule: a 4-entry register file (no reset), a pointer, a GPIO register and a
// toggling clock-like output (asynchronous reset). Copied into every equiv fixture.
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

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ptr  <= 2'd0;
            gpio <= 8'd0;
            sck  <= 1'b0;
        end else begin
            ptr <= ptr + 2'd1;
            sck <= ~sck;
            if (ptr == 2'd3) gpio <= rf[0] ^ rf[2];
        end
    end

    always @(posedge clk) rf[ptr] <= rf[ptr] + din + {7'd0, miso};

    assign retire = (ptr == 2'd3);
    assign acc    = rf[1];
`ifdef RISCV_FORMAL
    // RVFI-only aliases of shared state (unused here; they only exist to test naming).
    wire [7:0] a_rf1  = rf[1];
    wire [7:0] a_gpio = gpio;
    wire [1:0] a_ptr  = ptr;
`endif
endmodule
