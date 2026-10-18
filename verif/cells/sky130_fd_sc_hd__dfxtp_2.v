// Behavioral model of sky130_fd_sc_hd__dfxtp_2, the D flip-flop at drive 2: at each rising CLK
// edge Q takes D.
// One of the cells src/ may instantiate (contract section 1; the list is CELL_WHITELIST in
// tools/src_check.py). Every gate that compiles RTL reads it (lint, cosim and arch, equiv, the RTL
// test); synthesis never does, so area, harden and the gate-level test use the PDK's own cell.
// tools/test_cells.py proves it matches the PDK's functional model, X values included.
// No initial value: the flop powers up unknown (X in Icarus, random in the cosim). The state is
// the internal reg q, not the output port: Verilator zero-initializes an inlined module's output
// reg when the parent connects it to a bit-select, which would hide a design relying on the
// power-up value (verif/cosim/test_cosim_harness.py, test_cell_stub_power_up_reliance_fails).
`default_nettype none
module sky130_fd_sc_hd__dfxtp_2 (
    output wire Q,
    input  wire CLK,
    input  wire D
);
    reg q;
    always @(posedge CLK) q <= D;
    assign Q = q;
endmodule
`default_nettype wire
