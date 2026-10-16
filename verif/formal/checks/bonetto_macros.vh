// BonettoR32V formal: the pin and SPI-monitor signals that the wrapper exports
// and every checker receives. All checkers share one port list so that one
// testbench (checks/rvfi_testbench.sv) can host any of them.
//
// Timing convention (contract notation): a signal's value in BMC step t is X(t),
// its value after rising clock edge t. Step 0 is the initial state, where the
// testbench holds rst_n low; step 1 is the first state after a reset edge.
//
// Monitor signals (computed by bonetto_spi_monitor in wrapper.sv):
//   reset_q       rst_n was low in the previous step (step t is a reset state)
//   mon_en        step >= 1 (pins are covered by the contract)
//   mon_pair      step >= 2 (rules that compare X(t) with X(t-1))
//   mon_cs/sck/mosi/miso, mon_pcs/psck/pmosi   current and previous pin values
//   mon_rise      rising SCK edge while CS_n = 0 (S(t-1) = 0, S(t) = 1, C(t) = 0)
//   ev_cmd_done   the 8th command bit arrived this step; ev_cmd is the command
//   ev_rd_bit     a READ data bit was clocked this step: byte address ev_rd_addr,
//                 bit index ev_rd_idx (0 = MSB), value ev_rd_val = MISO(t)
//   ev_byte       a data byte completed this step (8th bit): ev_byte_wr = WRITE,
//                 ev_byte_addr, ev_byte_data (MOSI bits for WRITE, MISO bits for READ)
//   ev_end        CS_n rose this step (end of a transaction); ev_end_hdr = the
//                 24 header bits were complete, ev_end_write = command was WRITE,
//                 ev_end_bytes = at least one full data byte, ev_end_bit = data
//                 bits mod 8

`ifndef BONETTO_MACROS_VH
`define BONETTO_MACROS_VH

// Reach statements (each check's triggers). In a check's reach task run_formal.py
// defines BONETTO_REACH_AS_ASSERT, so each one is assert(!trigger), removes every
// other assertion and runs BMC with --keep-going: a failed reach_* assertion is a
// reached trigger. This is much faster than SBY's cover mode, which must solve step
// by step. In the proof task they are covers, and removed.
`ifdef BONETTO_REACH_AS_ASSERT
`define BONETTO_REACH(_lbl, _expr) _lbl: assert (!(_expr));
`else
`define BONETTO_REACH(_lbl, _expr) _lbl: cover (_expr);
`endif

`define BONETTO_INPUTS \
  input [7:0]  pin_ui_in, \
  input [7:0]  pin_uo_out, \
  input [7:0]  pin_uio_in, \
  input [7:0]  pin_uio_out, \
  input [7:0]  pin_uio_oe, \
  input        reset_q, \
  input        mon_en, \
  input        mon_pair, \
  input        mon_cs, \
  input        mon_sck, \
  input        mon_mosi, \
  input        mon_miso, \
  input        mon_pcs, \
  input        mon_psck, \
  input        mon_pmosi, \
  input        mon_rise, \
  input        ev_cmd_done, \
  input [7:0]  ev_cmd, \
  input        ev_rd_bit, \
  input [15:0] ev_rd_addr, \
  input [2:0]  ev_rd_idx, \
  input        ev_rd_val, \
  input        ev_byte, \
  input        ev_byte_wr, \
  input [15:0] ev_byte_addr, \
  input [7:0]  ev_byte_data, \
  input        ev_end, \
  input        ev_end_hdr, \
  input        ev_end_write, \
  input        ev_end_bytes, \
  input [2:0]  ev_end_bit

`define BONETTO_OUTPUTS \
  output [7:0]  pin_ui_in, \
  output [7:0]  pin_uo_out, \
  output [7:0]  pin_uio_in, \
  output [7:0]  pin_uio_out, \
  output [7:0]  pin_uio_oe, \
  output        reset_q, \
  output        mon_en, \
  output        mon_pair, \
  output        mon_cs, \
  output        mon_sck, \
  output        mon_mosi, \
  output        mon_miso, \
  output        mon_pcs, \
  output        mon_psck, \
  output        mon_pmosi, \
  output        mon_rise, \
  output        ev_cmd_done, \
  output [7:0]  ev_cmd, \
  output        ev_rd_bit, \
  output [15:0] ev_rd_addr, \
  output [2:0]  ev_rd_idx, \
  output        ev_rd_val, \
  output        ev_byte, \
  output        ev_byte_wr, \
  output [15:0] ev_byte_addr, \
  output [7:0]  ev_byte_data, \
  output        ev_end, \
  output        ev_end_hdr, \
  output        ev_end_write, \
  output        ev_end_bytes, \
  output [2:0]  ev_end_bit

`define BONETTO_WIRES \
  (* keep *) wire [7:0]  pin_ui_in; \
  (* keep *) wire [7:0]  pin_uo_out; \
  (* keep *) wire [7:0]  pin_uio_in; \
  (* keep *) wire [7:0]  pin_uio_out; \
  (* keep *) wire [7:0]  pin_uio_oe; \
  (* keep *) wire        reset_q; \
  (* keep *) wire        mon_en; \
  (* keep *) wire        mon_pair; \
  (* keep *) wire        mon_cs; \
  (* keep *) wire        mon_sck; \
  (* keep *) wire        mon_mosi; \
  (* keep *) wire        mon_miso; \
  (* keep *) wire        mon_pcs; \
  (* keep *) wire        mon_psck; \
  (* keep *) wire        mon_pmosi; \
  (* keep *) wire        mon_rise; \
  (* keep *) wire        ev_cmd_done; \
  (* keep *) wire [7:0]  ev_cmd; \
  (* keep *) wire        ev_rd_bit; \
  (* keep *) wire [15:0] ev_rd_addr; \
  (* keep *) wire [2:0]  ev_rd_idx; \
  (* keep *) wire        ev_rd_val; \
  (* keep *) wire        ev_byte; \
  (* keep *) wire        ev_byte_wr; \
  (* keep *) wire [15:0] ev_byte_addr; \
  (* keep *) wire [7:0]  ev_byte_data; \
  (* keep *) wire        ev_end; \
  (* keep *) wire        ev_end_hdr; \
  (* keep *) wire        ev_end_write; \
  (* keep *) wire        ev_end_bytes; \
  (* keep *) wire [2:0]  ev_end_bit;

`define BONETTO_CONN \
  .pin_ui_in(pin_ui_in), .pin_uo_out(pin_uo_out), .pin_uio_in(pin_uio_in), \
  .pin_uio_out(pin_uio_out), .pin_uio_oe(pin_uio_oe), .reset_q(reset_q), \
  .mon_en(mon_en), .mon_pair(mon_pair), .mon_cs(mon_cs), .mon_sck(mon_sck), \
  .mon_mosi(mon_mosi), .mon_miso(mon_miso), .mon_pcs(mon_pcs), .mon_psck(mon_psck), \
  .mon_pmosi(mon_pmosi), .mon_rise(mon_rise), .ev_cmd_done(ev_cmd_done), .ev_cmd(ev_cmd), \
  .ev_rd_bit(ev_rd_bit), .ev_rd_addr(ev_rd_addr), .ev_rd_idx(ev_rd_idx), .ev_rd_val(ev_rd_val), \
  .ev_byte(ev_byte), .ev_byte_wr(ev_byte_wr), .ev_byte_addr(ev_byte_addr), \
  .ev_byte_data(ev_byte_data), .ev_end(ev_end), .ev_end_hdr(ev_end_hdr), \
  .ev_end_write(ev_end_write), .ev_end_bytes(ev_end_bytes), .ev_end_bit(ev_end_bit)

// The uniform checker interface: clock, checker reset, the free check input
// (RISCV_FORMAL_UNBOUNDED: the solver picks the check cycle), RVFI, pins, monitor.
`define BONETTO_CHECKER_PORTS \
  input clock, input reset, input check, \
  `RVFI_INPUTS, \
  `BONETTO_INPUTS

`define BONETTO_CHECKER_CONN(_clock, _reset, _check) \
  .clock(_clock), .reset(_reset), .check(_check), \
  `RVFI_CONN, \
  `BONETTO_CONN

`endif
