// Behavioral model of sky130_fd_sc_hd__mux2_1, the 2-input mux: X = S ? A1 : A0.
// One of the cells src/ may instantiate (contract section 1; the list is CELL_WHITELIST in
// tools/src_check.py). Every gate that compiles RTL reads it (lint, cosim and arch, equiv, the RTL
// test); synthesis never does, so area, harden and the gate-level test use the PDK's own cell.
// tools/test_cells.py proves it matches the PDK's functional model, X values included.
`default_nettype none
module sky130_fd_sc_hd__mux2_1 (
    output wire X,
    input  wire A0,
    input  wire A1,
    input  wire S
);
    assign X = S ? A1 : A0;
endmodule
`default_nettype wire
