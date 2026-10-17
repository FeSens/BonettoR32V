// One-bit full adder slice for the bit-serial fixture; a typed parameter on purpose.
`default_nettype none
`timescale 1ns/1ps

module r32v_serial_add #(
    parameter integer INVERT = 0
) (
    input  wire a,
    input  wire b,
    input  wire cin,
    output wire s,
    output wire cout
);
    wire sum = a ^ b ^ cin;
    assign s    = (INVERT != 0) ? ~sum : sum;
    assign cout = (a & b) | (cin & (a ^ b));
endmodule
`default_nettype wire
