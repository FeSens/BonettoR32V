// BonettoR32V formal (contract v3): the pin, QSPI-monitor and stop signals that the wrapper
// exports and every checker receives. All checkers share one port list so that one testbench
// (checks/rvfi_testbench.sv) can host any of them.
//
// Timing convention (contract notation): a signal's value in BMC step t is X(t), its value
// after rising clock edge t. Step 0 is the initial state, where the testbench holds rst_n low;
// step 1 is the first state after a reset edge.
//
// Pins and bus (computed in wrapper.sv):
//   pin_*            the TT pins this step; pin_puio_out / pin_puio_oe the previous step's
//   reset_q          rst_n was low in the previous step (step t is a reset state)
//   mon_en           step >= 1 (the pins are covered by the contract)
//   mon_pair         step >= 2 (rules that compare X(t) with X(t-1))
//   mon_rise         a rising SCK edge: S(t-1) = 0, S(t) = 1 (contract 2.1.4)
//   mon_sd           {SD3, SD2, SD1, SD0} as the lines carry them this step (uio_in)
// Per chip, f_* for the flash (C_f, uio[0]) and r_* for the PSRAM (C_r, uio[6]), from
// bonetto_qspi_chip (wrapper.sv), which follows the command tables of contract 2.2 and 2.3:
//   x_dout           the chip is in its data-out phase this step (from its first data-out
//                    rising edge until CS_n rises); x_dquad: quad (else single, on SD1)
//   x_bad_cmd        a command field completed this step with a command the table does not
//                    list for the chip's state, a 0x66/0x99 order error, or a PSRAM access
//                    before its reset (contract 2.1.10, 2.3, 2.4)
//   x_bad_extra      a rising edge after a command that takes no further fields
//   x_bad_wend       a write ended inside its address field or mid-byte (contract 2.1.9)
//   x_rd             a data-out rising edge: the chip drives byte x_rd_addr, bit x_rd_idx
//                    (single, 0 = MSB) or nibble x_rd_idx (quad, 0 = high) for this clock
//   x_fin            CS_n rose after a complete command field; x_fin_rd: a read with at least
//                    one data edge; x_fin_wr: a write with at least one data byte
//   r_wb             a PSRAM write byte completed: r_wb_addr (23 bits, A[23] dropped),
//                    r_wb_data
//   r_bad_trst       PSRAM CS_n fell less than tRST after a 0x99 ended (contract 2.3)
//   r_bad_cem        PSRAM CS_n has been low for more than tCEM cycles (contract 2.1.7)
//   x_bad_cph        the chip's CS_n fell less than its minimum high time after it rose
//                    (contract 2.1.2: BONETTO_TCPH_FLASH / BONETTO_TCPH_PSRAM cycles)
//   f_bad_cont       the flash's CS_n rose after a first M nibble with M[5:4] = 10, before
//                    the 4 dummy edges and one data byte (contract 2.2)
//
// UNSPECIFIED stop (contract 3 and 4, computed by rvfi_wrapper):
//   bon_unspec_ret   this retirement's fetch or data access is UNSPECIFIED: it touches the core
//                    region (PSRAM 0x7F_FF80-0x7F_FFFF, any alias), fetches from IO, spans two
//                    areas (flash, PSRAM, IO) or is a misaligned IO access. From the spec's
//                    address for a specified instruction, from the claim for a reserved one
//   bon_fetch_unspec ... through its fetch (pc_rdata + k, k < 4)
//   bon_stopped      an UNSPECIFIED retirement has happened: from the step after it, only
//                    liveness and the bus rules (qspi) apply
//   bon_brj          rvfi_insn is a branch or JALR (valid funct3, E register fields): on a trap
//                    its rs fields are truthful and checked (contract 8)
//   bon_spec         rvfi_insn is a specified instruction (one of the 48 models, E filter
//                    included, or a trap word): everything else is a reserved encoding

`ifndef BONETTO_MACROS_VH
`define BONETTO_MACROS_VH

// Reach statements (each check's triggers). In a check's reach task run_formal.py defines
// BONETTO_REACH_AS_ASSERT, so each one is assert(!trigger), removes every other assertion and
// runs BMC with --keep-going: a failed reach_* assertion is a reached trigger. In the proof
// task they are covers, and removed.
`ifdef BONETTO_REACH_AS_ASSERT
`define BONETTO_REACH(_lbl, _expr) _lbl: assert (!(_expr));
`else
`define BONETTO_REACH(_lbl, _expr) _lbl: cover (_expr);
`endif

`define BONETTO_PORTS(_d) \
  _d [7:0]  pin_ui_in, \
  _d [7:0]  pin_uo_out, \
  _d [7:0]  pin_uio_in, \
  _d [7:0]  pin_uio_out, \
  _d [7:0]  pin_uio_oe, \
  _d [7:0]  pin_puio_out, \
  _d [7:0]  pin_puio_oe, \
  _d        reset_q, \
  _d        mon_en, \
  _d        mon_pair, \
  _d        mon_rise, \
  _d [3:0]  mon_sd, \
  _d        f_dout, \
  _d        f_dquad, \
  _d        f_bad_cmd, \
  _d        f_bad_extra, \
  _d        f_bad_wend, \
  _d        f_rd, \
  _d [23:0] f_rd_addr, \
  _d        f_rd_quad, \
  _d [2:0]  f_rd_idx, \
  _d        f_fin, \
  _d        f_fin_rd, \
  _d        f_fin_wr, \
  _d        r_dout, \
  _d        r_dquad, \
  _d        r_bad_cmd, \
  _d        r_bad_extra, \
  _d        r_bad_wend, \
  _d        r_rd, \
  _d [23:0] r_rd_addr, \
  _d        r_rd_quad, \
  _d [2:0]  r_rd_idx, \
  _d        r_fin, \
  _d        r_fin_rd, \
  _d        r_fin_wr, \
  _d        r_wb, \
  _d [22:0] r_wb_addr, \
  _d [7:0]  r_wb_data, \
  _d        r_bad_trst, \
  _d        r_bad_cem, \
  _d        f_bad_cph, \
  _d        r_bad_cph, \
  _d        f_bad_cont, \
  _d        bon_unspec_ret, \
  _d        bon_fetch_unspec, \
  _d        bon_stopped, \
  _d        bon_brj, \
  _d        bon_spec

`define BONETTO_INPUTS `BONETTO_PORTS(input)
`define BONETTO_OUTPUTS `BONETTO_PORTS(output)

`define BONETTO_WIRES \
  (* keep *) wire [7:0]  pin_ui_in; \
  (* keep *) wire [7:0]  pin_uo_out; \
  (* keep *) wire [7:0]  pin_uio_in; \
  (* keep *) wire [7:0]  pin_uio_out; \
  (* keep *) wire [7:0]  pin_uio_oe; \
  (* keep *) wire [7:0]  pin_puio_out; \
  (* keep *) wire [7:0]  pin_puio_oe; \
  (* keep *) wire        reset_q; \
  (* keep *) wire        mon_en; \
  (* keep *) wire        mon_pair; \
  (* keep *) wire        mon_rise; \
  (* keep *) wire [3:0]  mon_sd; \
  (* keep *) wire        f_dout; \
  (* keep *) wire        f_dquad; \
  (* keep *) wire        f_bad_cmd; \
  (* keep *) wire        f_bad_extra; \
  (* keep *) wire        f_bad_wend; \
  (* keep *) wire        f_rd; \
  (* keep *) wire [23:0] f_rd_addr; \
  (* keep *) wire        f_rd_quad; \
  (* keep *) wire [2:0]  f_rd_idx; \
  (* keep *) wire        f_fin; \
  (* keep *) wire        f_fin_rd; \
  (* keep *) wire        f_fin_wr; \
  (* keep *) wire        r_dout; \
  (* keep *) wire        r_dquad; \
  (* keep *) wire        r_bad_cmd; \
  (* keep *) wire        r_bad_extra; \
  (* keep *) wire        r_bad_wend; \
  (* keep *) wire        r_rd; \
  (* keep *) wire [23:0] r_rd_addr; \
  (* keep *) wire        r_rd_quad; \
  (* keep *) wire [2:0]  r_rd_idx; \
  (* keep *) wire        r_fin; \
  (* keep *) wire        r_fin_rd; \
  (* keep *) wire        r_fin_wr; \
  (* keep *) wire        r_wb; \
  (* keep *) wire [22:0] r_wb_addr; \
  (* keep *) wire [7:0]  r_wb_data; \
  (* keep *) wire        r_bad_trst; \
  (* keep *) wire        r_bad_cem; \
  (* keep *) wire        f_bad_cph; \
  (* keep *) wire        r_bad_cph; \
  (* keep *) wire        f_bad_cont; \
  (* keep *) wire        bon_unspec_ret; \
  (* keep *) wire        bon_fetch_unspec; \
  (* keep *) wire        bon_stopped; \
  (* keep *) wire        bon_brj; \
  (* keep *) wire        bon_spec;

`define BONETTO_CONN \
  .pin_ui_in(pin_ui_in), .pin_uo_out(pin_uo_out), .pin_uio_in(pin_uio_in), \
  .pin_uio_out(pin_uio_out), .pin_uio_oe(pin_uio_oe), .pin_puio_out(pin_puio_out), \
  .pin_puio_oe(pin_puio_oe), .reset_q(reset_q), .mon_en(mon_en), .mon_pair(mon_pair), \
  .mon_rise(mon_rise), .mon_sd(mon_sd), \
  .f_dout(f_dout), .f_dquad(f_dquad), .f_bad_cmd(f_bad_cmd), .f_bad_extra(f_bad_extra), \
  .f_bad_wend(f_bad_wend), .f_rd(f_rd), .f_rd_addr(f_rd_addr), .f_rd_quad(f_rd_quad), \
  .f_rd_idx(f_rd_idx), .f_fin(f_fin), .f_fin_rd(f_fin_rd), .f_fin_wr(f_fin_wr), \
  .r_dout(r_dout), .r_dquad(r_dquad), .r_bad_cmd(r_bad_cmd), .r_bad_extra(r_bad_extra), \
  .r_bad_wend(r_bad_wend), .r_rd(r_rd), .r_rd_addr(r_rd_addr), .r_rd_quad(r_rd_quad), \
  .r_rd_idx(r_rd_idx), .r_fin(r_fin), .r_fin_rd(r_fin_rd), .r_fin_wr(r_fin_wr), \
  .r_wb(r_wb), .r_wb_addr(r_wb_addr), .r_wb_data(r_wb_data), .r_bad_trst(r_bad_trst), \
  .r_bad_cem(r_bad_cem), .f_bad_cph(f_bad_cph), .r_bad_cph(r_bad_cph), \
  .f_bad_cont(f_bad_cont), .bon_unspec_ret(bon_unspec_ret), .bon_fetch_unspec(bon_fetch_unspec), \
  .bon_stopped(bon_stopped), .bon_brj(bon_brj), .bon_spec(bon_spec)

// The uniform checker interface: clock, checker reset, the free check input
// (RISCV_FORMAL_UNBOUNDED: the solver picks the check cycle), RVFI, pins, monitor.
`define BONETTO_CHECKER_PORTS \
  input clock, input reset, input check, \
  `RVFI_INPUTS, \
  `BONETTO_INPUTS

// A fully compared retirement: not at or after the first UNSPECIFIED retirement.
`define BONETTO_FULL (!bon_stopped && !bon_unspec_ret)

`define BONETTO_CHECKER_CONN(_clock, _reset, _check) \
  .clock(_clock), .reset(_reset), .check(_check), \
  `RVFI_CONN, \
  `BONETTO_CONN

// Memory map helpers (contract 3), for a CPU byte address b: area 0 flash (b[31] = 0,
// b[24] = 0), 1 PSRAM (b[24] = 1), 2 IO (b[31] = 1); the core region is PSRAM bytes
// 0x7F_FF80-0x7F_FFFF (b[22:7] all ones; bits 30:25 and 23 ignored).
`define BON_AREA(_b) ((_b[31]) ? 2'd2 : (_b[24]) ? 2'd1 : 2'd0)
`define BON_REGION(_b) (!_b[31] && _b[24] && (_b[22:7] == 16'hffff))

`endif
