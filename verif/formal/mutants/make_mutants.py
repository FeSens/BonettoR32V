#!/usr/bin/env python3
"""Regenerate verif/formal/mutants/<name>/ from the champion (src/, contract v3; ported to
champion 112, the pair fetch, 4c82e3a; the measured mutant results in docs/formal.md are on
champion 111, 346c4a3).

Each mutant is a copy of the champion's design files that the mutation touches (test_formal.py
lays them over a copy of src/), each with exactly one change, plus expect.json naming the formal
check(s) that must FAIL on it ("expect": "fail") and the assertion the change targets
("assertion", for the record; the gate only needs one of "checks" to fail with a
counterexample). test_formal.py runs those checks on each mutant at the champion's depth
(the cosim timing of src/). "deep" mutants run in the deep config (liveness lives there).

The set: the contract-v2 mutants re-derived on the champion where their rule still exists
(miso_low is sd_low, mosi_rise is sd_rise, and so on; the v2 transaction-attribution and halt
mutants have no v3 rule: caches, prefetch and repeated accesses are legal, and traps go to
mtvec), and one mutant per contract-v3 rule: each QSPI rule, PSRAM write attribution,
coherence (fetch and load), each CSR check, the trap, MRET and interrupt targets, liveness, the
GPIO timing and the peripheral registers.

SHARED lists the mutants whose RTL is the cosim's (verif/cosim/mutants/<name>/), so both gates
test the same designs: their expect.json has "rtl" and no copy of the RTL.

  python3 verif/formal/mutants/make_mutants.py [src_dir]

Each substitution must match exactly once, so a changed champion fails loudly here instead of
producing a mutant that is not the intended bug.
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TOP = "tt_um_fesens_r32v.v"
MEM = "r32v_mem.v"
CSR = "r32v_csr.v"
PER = "r32v_periph.v"

# name: (summary, checks, assertion, description, [(file, old, new), ...], {options})
MUTANTS = {
    # ---- QSPI rules (contract 1 and 2), check qspi -----------------------------------------
    "oe_sck_reset": (
        "SCK's output enable is off during the reset sequence", ["qspi"], "qspi_oe_fixed",
        "uio_oe[3] follows the reset sequence's done flag (contract 1: always 1).",
        [(MEM, "    assign uio_oe  = {2'b11, oe, oe, 1'b1, oe, oe, 1'b1};",
          "    assign uio_oe  = {2'b11, oe, oe, done, oe, oe, 1'b1};")],
    ),
    "reset_cs_low": (
        "flash CS_n low while rst_n is low", ["reset"], "qspi_reset_cs",
        "The flash CS_n is forced low while rst_n is low (contract 1: both CS_n high). Only the "
        "reset check sees it: with the one-edge reset of the other checks, rst_n is already "
        "high in step 1, the first step the pins are compared.",
        [(MEM, "    wire       csf = done ? ~(active & flash) : bcsf;",
          "    wire       csf = done ? ~(active & flash) : (bcsf & rst_n);")],
    ),
    "cs_both": (
        "both CS_n low", ["qspi"], "qspi_rule1",
        "The PSRAM CS_n falls with every transaction, the flash's too (contract 2.1.1).",
        [(MEM, "    wire       csr = done ? ~(active & ~flash) : bcsr;",
          "    wire       csr = done ? ~active : bcsr;")],
    ),
    "cs_sck_high": (
        "CS_n rises while SCK is high", ["qspi"], "qspi_rule2",
        "The memory controller sees the slot end one clock early (at its tail), so CS_n rises "
        "at the clock where SCK falls after the last data edge (contract 2.1.2).",
        [(TOP, "        .active(active), .prog(fM), .fetch(sF),",
          "        .active(active & ~e_end), .prog(fM), .fetch(sF),")],
    ),
    "sck_masked": (
        "SCK toggles with both CS_n high", ["qspi"], "qspi_rule3",
        "A masked slot's strobes reach the SCK pin (contract 2.1.3: SCK 0 while both CS_n "
        "are high).",
        [(MEM, "    wire       sk  = done ? (sck & active) : bsck;",
          "    wire       sk  = done ? sck : bsck;")],
    ),
    "sd_rise": (
        "SD0 changes at a rising SCK edge", ["qspi"], "qspi_rule4",
        "SD0 is XORed with SCK, so it changes at every rising edge it is driven at "
        "(contract 2.1.4).",
        [(MEM, "    wire [3:0] sd  = done ? sdo : bsd;",
          "    wire [3:0] sd  = done ? (sdo ^ {3'b000, sk}) : bsd;")],
    ),
    "contention": (
        "drives SD0-SD3 during a read's data phase", ["qspi"], "qspi_rule6_psram",
        "SD0-SD3 are never released after a read's header (contract 2.1.6).",
        [(MEM, "        else if (oe_off)\n", "        else if (oe_off & 1'b0)\n")],
    ),
    "flash_no_exit": (
        "the reset sequence skips the flash's continuous-read exit", ["qspi"],
        "qspi_rule6_flash",
        "Window w0 (8 edges with SD0-SD3 high) is gone: a flash that powers up in continuous "
        "read takes the 0xEB command as an address and M byte and drives data while the core "
        "still drives SD0-SD3 (contract 2.1.6, 2.4).",
        [(MEM, "    wire       blo  = ~done & (fe ? lo16 : (w == 3'd1) ? lo2 : (w != 3'd7) & lo8);",
          "    wire       blo  = ~done & (fe ? lo16 : (w == 3'd1) ? lo2 : (w != 3'd7) & (w != 3'd0)"
          " & lo8);")],
    ),
    "flash_m_byte": (
        "M = 0x30 in the flash header", ["qspi"], "qspi_cmd_flash",
        "The fetch header's M byte is 0x30, so the flash leaves continuous read and takes the "
        "next header as a command (contract 2.2).",
        [(MEM, "            4'b1110: hn = 4'h2;                         // M = 0x20: stay in "
               "continuous read",
          "            4'b1110: hn = 4'h3;                         // M = 0x20: stay in "
          "continuous read")],
    ),
    "psram_cmd": (
        "PSRAM read command 0x0C", ["qspi"], "qspi_cmd_psram",
        "A PSRAM read sends 0x0C, not 0x0B (contract 2.3).",
        [(MEM, "            4'b0001: hn = h_wr ? 4'h2 : 4'hB;",
          "            4'b0001: hn = h_wr ? 4'h2 : 4'hC;")],
    ),
    "psram_no_reset": (
        "no PSRAM reset", ["qspi"], "qspi_cmd_psram",
        "Windows w2 and w3 (0x66, 0x99) keep the PSRAM CS_n high: the first access comes "
        "before the PSRAM's reset (contract 2.3, 2.4).",
        [(MEM, "    wire       blo  = ~done & (fe ? lo16 : (w == 3'd1) ? lo2 : (w != 3'd7) & lo8);",
          "    wire       blo  = ~done & (fe ? lo16 : (w == 3'd1) ? lo2 : (w != 3'd7) & "
          "(w[2:1] != 2'b01) & lo8);")],
    ),
    "trst_short": (
        "CS_n high for one cycle after 0x99", ["qspi"], "qspi_trst",
        "The PSRAM CS_n pulses low again two cycles after 0x99 ends (contract 2.3: high for "
        "tRST = 3 cycles at 20 ns).",
        [(MEM, "    wire       bcsr = ~(blo & ~fl);",
          "    wire       bcsr = ~((blo & ~fl) | ((w == 3'd3) & (p == 5'd18)));")],
    ),
    "cmd_extra_edge": (
        "a ninth edge after 0x66", ["qspi"], "qspi_extra",
        "Window w2 clocks one more edge after 0x66, which takes no fields (contract 2.1.10).",
        [(MEM, "    wire       lo8  = ~p[4] | (p[3:0] == 4'd0);",
          "    wire       lo8  = ~p[4] | (p[3:0] == 4'd0) | ((w == 3'd2) & ((p == 5'd17) | "
          "(p == 5'd18)));")],
    ),
    "write_midbyte": (
        "a store ends mid-byte", ["qspi"], "qspi_wend",
        "A store's data phase ends one nibble early (contract 2.1.9).",
        [(TOP, "    wire [2:0] nlast = (sM & c_st) ? {nm1, 1'b1} : 3'd7;",
          "    wire [2:0] nlast = (sM & c_st) ? {nm1, 1'b0} : 3'd7;")],
    ),
    # ---- PSRAM write attribution (contract 8), check psram_write ---------------------------
    "store_extra_byte": (
        "SB writes two bytes", ["psram_write"], "psram_write_attr",
        "A byte store writes the next byte too (rs2's second byte).",
        [(TOP, "    wire [2:0] nlast = (sM & c_st) ? {nm1, 1'b1} : 3'd7;",
          "    wire [2:0] nlast = (sM & c_st) ? {nm1 | {1'b0, ~nm1[0]}, 1'b1} : 3'd7;")],
    ),
    "store_data_flip": (
        "a store writes a wrong bit", ["psram_write"], "psram_write_attr",
        "Bit 0 of a store's data nibble 1 is flipped on the bus (RVFI reports rs2).",
        [(TOP, "    wire [3:0] wdo4   = sW ? wd4 : fM ? tap4 : (sWC & tm & ~tk) ? pk4 :",
          "    wire [3:0] wdo4   = sW ? wd4 : fM ? (tap4 ^ {3'b000, n == 3'd1}) : "
          "(sWC & tm & ~tk) ? pk4 :")],
    ),
    "region_wrap": (
        "core-region words at a 2-byte offset", ["psram_write"], "psram_write_attr",
        "Core-region word addresses get bit 1 set: x15's word (0x7FFFFC) is written at "
        "0x7FFFFE and wraps to PSRAM 0x000000-0x000001, outside the core region.",
        [(MEM, "            4'b0111: hn = prog ? an : {h_reg[1:0], 2'b00};",
          "            4'b0111: hn = prog ? an : {h_reg[1:0], 2'b10};")],
    ),
    # ---- coherence (contract 3), check coherence -------------------------------------------
    "sd_low": (
        "samples SD0-SD3 while SCK is low", ["coherence"], "coherence_fetch",
        "The data nibble goes through an extra flip-flop, so a strobe reads the value from the "
        "clock before the rising edge (noise). The v2 miso_low.",
        [(TOP, "    wire [3:0] nib   = {uio_in[5], uio_in[4], uio_in[2], uio_in[1]};   "
               "// {SD3, SD2, SD1, SD0}",
          "    reg  [3:0] nib_q;\n"
          "    always @(posedge clk) nib_q <= {uio_in[5], uio_in[4], uio_in[2], uio_in[1]};\n"
          "    wire [3:0] nib   = nib_q;")],
    ),
    "store_dropped": (
        "stores issue nothing", ["coherence"], "coherence_load",
        "Every store's M slot is masked (as a store to flash is): a later load of the byte "
        "returns the old value.",
        [(TOP, "                (sM & c_st & ~ps) |                     // a store to flash "
               "issues nothing",
          "                (sM & c_st) |                           // a store to flash "
          "issues nothing")],
    ),
    "fetch_wrong_chip": (
        "fetches always read the flash", ["coherence"], "coherence_fetch",
        "A fetch from PSRAM reads the flash byte at the same low address.",
        [(TOP, "        .pa24(tk ? ring[24] : pcr[24]),", "        .pa24(1'b0),")],
    ),
    # ---- CSRs, traps and the interrupt (contract 4), checks csr and irq ---------------------
    "trap_target": (
        "a trap jumps through the wrong core-region word", ["csr"], "csr_trap_target",
        "A trap sequence's C reads core-region CSR word 0, not mtvec.",
        [(CSR, "    assign idx      = tm ? {s_wc, 1'b1} : p_mret ? 2'b11 : {b6, b0};",
          "    assign idx      = tm ? {s_wc, s_wc} : p_mret ? 2'b11 : {b6, b0};")],
    ),
    "mret_target": (
        "MRET jumps to mtvec", ["csr"], "csr_mret_target",
        "MRET's C reads mtvec's word, not mepc's.",
        [(CSR, "    assign idx      = tm ? {s_wc, 1'b1} : p_mret ? 2'b11 : {b6, b0};",
          "    assign idx      = tm ? {s_wc, 1'b1} : p_mret ? 2'b01 : {b6, b0};")],
    ),
    "irq_target": (
        "an interrupt jumps to mepc", ["irq"], "csr_irq_target",
        "An interrupt sequence's C reads mepc's word, not mtvec's.",
        [(CSR, "    assign idx      = tm ? {s_wc, 1'b1} : p_mret ? 2'b11 : {b6, b0};",
          "    assign idx      = tm ? {s_wc, s_wc | ~intr} : p_mret ? 2'b11 : {b6, b0};")],
    ),
    "irq_masked": (
        "the interrupt ignores mstatus.MIE", ["irq"], "csr_irq_enabled",
        "An interrupt is taken with MIE = 0.",
        [(CSR, "    wire irq_take = retire & ~tm & mie_n & meie_n & irq;",
          "    wire irq_take = retire & ~tm & meie_n & irq;")],
    ),
    "csr_mtvec_lowbits": (
        "mtvec and mepc bits 1:0 read back", ["csr"], "csr_read",
        "The read mask of mtvec and mepc bits 1:0 is gone.",
        [(CSR, "    assign mask_lo2 = idx[0] & n0;", "    assign mask_lo2 = 1'b0;")],
    ),
    "csr_mpp": (
        "mstatus.MPP bit 12 reads 0", ["csr"], "csr_read",
        "mstatus bit 12 (MPP = 3, read-only) reads 0.",
        [(CSR, "    assign rd_die[0] = (c_mstatus & n3) | (mc_lo & mc[0]);",
          "    assign rd_die[0] = (mc_lo & mc[0]);")],
    ),
    "mret_mpie": (
        "MRET leaves MPIE", ["csr"], "csr_read",
        "MRET does not set mstatus.MPIE.",
        [(CSR, "            else if (retire & p_mret)\n                mpie <= 1'b1;\n",
          "            else if (retire & p_mret)\n                mpie <= mpie;\n")],
    ),
    "trap_cause": (
        "EBREAK's mcause is 11", ["csr"], "csr_read",
        "The cause decode drops insn[20]: EBREAK records cause 11 (ECALL's).",
        [(TOP, "    wire [3:0] cause = {p_trap & ~trap_ill & ~insn[20], 1'b0, trap_ill | p_trap, "
               "p_trap & ~trap_ill};",
          "    wire [3:0] cause = {p_trap & ~trap_ill, 1'b0, trap_ill | p_trap, "
          "p_trap & ~trap_ill};")],
    ),
    "trap_mepc": (
        "a trap writes the ring to mepc", ["csr"], "csr_read",
        "A trap sequence's WC writes the ring, not the pc, to mepc.",
        [(TOP, "    wire [3:0] wdo4   = sW ? wd4 : fM ? tap4 : (sWC & tm & ~tk) ? pk4 :",
          "    wire [3:0] wdo4   = sW ? wd4 : fM ? tap4 : (sWC & tm & ~tk & 1'b0) ? pk4 :")],
    ),
    # ---- liveness (contract 4), deep --------------------------------------------------------
    "hang_reserved": (
        "custom-0 encodings hang", ["liveness"], "liveness",
        "An instruction with opcode 0001011 fetches itself again forever (nothing retires).",
        [(TOP, "                    nsl = c_jal ? SL_W4 : p_mret ? SL_C : (c_fence | priv) ? SL_X :",
          "                    nsl = (insn[6:0] == 7'b0001011) ? SL_F : c_jal ? SL_W4 : p_mret ? "
          "SL_C : (c_fence | priv) ? SL_X :")],
        {"deep": True},
    ),
    # ---- GPIO and peripherals (contract 5, 6), checks gpio and periph ------------------------
    "gpio_any_byte": (
        "a store to any byte of GPIO_OUT writes it", ["gpio", "periph"], "gpio_uo_hold",
        "Byte 0 write enables ignore the offset: SB to GPIO_OUT + 1 writes GPIO_OUT (and every "
        "other slot's byte 0).",
        [(PER, "    wire       be0 = we & (off == 2'd0);", "    wire       be0 = we;")],
    ),
    "uo_glitch": (
        "uo_out[0] glitches at a GPIO_OUT store", ["gpio"], "gpio_uo_hold",
        "uo_out[0] is inverted for the clock of the store's write enable.",
        [(PER, "    assign uo_out = (alt & fn) | (~alt & gpo);",
          "    assign uo_out = ((alt & fn) | (~alt & gpo)) ^ {7'd0, we & s_gpo};")],
    ),
    "uo_late": (
        "uo_out takes GPIO_OUT two clocks late", ["gpio"], "gpio_uo_hold",
        "GPIO_OUT reaches the pins through two more flip-flops (contract 6: no later than one "
        "cycle after the store retires).",
        [(PER, "    assign uo_out = (alt & fn) | (~alt & gpo);",
          "    reg [7:0] gpo_d1, gpo_d2;\n"
          "    always @(posedge clk) begin\n"
          "        gpo_d1 <= rst_n ? gpo : 8'd0;\n"
          "        gpo_d2 <= rst_n ? gpo_d1 : 8'd0;\n"
          "    end\n"
          "    assign uo_out = (alt & fn) | (~alt & gpo_d2);")],
    ),
    "periph_wmask": (
        "TIMER_CMP's high byte is not writable", ["periph"], "periph_read",
        "TIMER_CMP[15:8] only takes the reset value.",
        [(PER, "        if (w1 & (s_tcmp | rst)) tcmp[15:8] <= z1;",
          "        if (rst) tcmp[15:8] <= z1;")],
    ),
    "uart_any_byte": (
        "a store to UART_DATA byte 1 starts a transmission", ["periph"], "periph_read",
        "TX starts on a write of either byte of UART_DATA (contract 5: byte 0 only); "
        "UART_CTRL.TX_BUSY then reads 1.",
        [(PER, "    wire        tx_go   = be0 & s_udat & ~tx_busy;",
          "    wire        tx_go   = (be0 | be1) & s_udat & ~tx_busy;")],
    ),
    # ---- RVFI conventions (contract 8), check rvfi; illegal encodings, check ill -----------
    "rvfi_halt": (
        "rvfi_halt at a trap", ["rvfi"], "conv_halt",
        "rvfi_halt is 1 at every trap retirement (contract 8: always 0; nothing halts).",
        [(TOP, "    assign rvfi_halt      = 1'b0;", "    assign rvfi_halt      = r_tr;")],
    ),
    "intr_after_trap": (
        "rvfi_intr only after an interrupt", ["rvfi"], "conv_intr_trap",
        "The handler's first instruction after a synchronous trap reports rvfi_intr = 0.",
        [(TOP, "            if (e_end & sC & tm)\n                r_ipend <= 1'b1;\n",
          "            if (e_end & sC & tm)\n                r_ipend <= intr;\n")],
    ),
    "trap_writes_rd": (
        "a trap reports rd", ["rvfi"], "conv_trap_rd_addr",
        "A trapping instruction reports its rd field and a write (contract 8: rd 0 on a trap).",
        [(TOP, "    wire        r_rde  = ~r_tr & (ri_alu | ri_u | ri_jal | ri_jalr | ri_ld | ri_csr) & "
               "(ri[10:7] != 4'd0);",
          "    wire        r_rde  = (ri_alu | ri_u | ri_jal | ri_jalr | ri_ld | ri_csr) & "
          "(ri[10:7] != 4'd0);")],
    ),
    "zero_init": (
        "the pc is not cleared", ["rvfi"], "conv_first_pc",
        "The reset sequence no longer shifts zeros into the pc register (no reset), so the "
        "first fetch is from its power-up value (contract 1, invariant 6).",
        [(TOP, "    wire       pwe  = (dstb & (pwr | prot)) | rot_hdr | booting;",
          "    wire       pwe  = (dstb & (pwr | prot)) | rot_hdr;")],
    ),
    "ill_zero": (
        "the all-zeros word executes", ["ill"], "ill_trap",
        "Only the all-ones word traps as illegal; the all-zeros word runs as LB x0, 0(x0). "
        "(Dropping the all-ones term instead is not an ill bug: 0xFFFFFFFF then decodes as a "
        "JAL with imm[1] = 1, which still traps, with mcause 0.)",
        [(TOP, "    wire trap_ill = ~insn[0] | (insn[6] & insn[4] & insn[2]);",
          "    wire trap_ill = (insn[6] & insn[4] & insn[2]);")],
    ),
    # ---- registers and instructions ---------------------------------------------------------
    "reg_wrong_index": (
        "writes go to an odd register", ["reg"], "reg_rs1",
        "W and W4 address register rd | 1 (RVFI reports rd).",
        [(TOP, "    wire [3:0] rsel = sA ? rs1_f : (sB | sBS) ? rs2_f : (sW | sW4) ? rd_f :",
          "    wire [3:0] rsel = sA ? rs1_f : (sB | sBS) ? rs2_f : (sW | sW4) ? "
          "{rd_f[3:1], 1'b1} :")],
    ),
    "sub_decode": (
        "SUB adds", ["insn_sub"], "insn_rd_wdata",
        "The subtract decode needs funct3[0] = 1 too, so SUB adds.",
        [(TOP, "(~f3[2] & (f3[1] | insn[30])))) |", "(~f3[2] & (f3[1] | (insn[30] & f3[0])))))"
          " |")],
    ),
    "rvfi_rd": (
        "a wrong RVFI rd value", ["insn_xor"], "insn_rd_wdata",
        "RVFI reports XOR results with bit 0 flipped (the register gets the right value).",
        [(TOP, "    assign rvfi_rd_wdata  = r_rde ? r_rd : 32'd0;",
          "    assign rvfi_rd_wdata  = r_rde ? (r_rd ^ {31'd0, ri_op & (ri_f3 == 3'b100)}) : "
          "32'd0;")],
    ),
    "claim_region_stop": (
        "claims a core-region address to stop the checks", ["insn_add"], "insn_rd_wdata",
        "ADD reports a wrong rd value and claims a core-region access (0x017FFF80): the "
        "UNSPECIFIED stop uses the spec's address for a specified instruction, so the claim "
        "stops nothing.",
        [(TOP, "    assign rvfi_rd_wdata  = r_rde ? r_rd : 32'd0;",
          "    assign rvfi_rd_wdata  = r_rde ? (r_rd ^ {31'd0, ri_op & (ri_f3 == 3'b000) & "
          "~ri[30]}) : 32'd0;"),
         (TOP, "    assign rvfi_mem_addr  = r_mem ? maddr : 32'd0;",
          "    assign rvfi_mem_addr  = r_mem ? maddr : ri_op ? 32'h017fff80 : 32'd0;")],
    ),
    "norvfi_diff": (
        "a behavior difference only without RVFI", ["equiv"], "equiv",
        "uo_out is inverted in the plain build only.",
        [(PER, "    assign uo_out = (alt & fn) | (~alt & gpo);",
          "`ifdef RISCV_FORMAL\n    assign uo_out = (alt & fn) | (~alt & gpo);\n`else\n"
          "    assign uo_out = ~((alt & fn) | (~alt & gpo));\n`endif")],
    ),
}

# Shared with the cosim (verif/cosim/mutants/<name>/): name: (checks, assertion, description)
# Mutants whose counterexample needs two or more retirements after the first (a store then a
# load, a write or trap then a read, a learned mtvec or mepc, an interrupt entry): the fast
# config bounds coherence and csr at the short depth and has no irq (checks.cfg), so they run
# in the deep config (cpi=2, cpi=4).
DEEP = {"store_dropped", "fetch_wrong_chip", "trap_target", "mret_target", "mret_mpie",
        "trap_cause", "trap_mepc", "irq_target", "irq_masked"}

SHARED = {
    "tcem_stall": (["qspi"], "qspi_tcem",
                   "The cosim's tCEM stub: a PSRAM read that holds CS_n low with SCK paused for "
                   "400 cycles (about 480 cycles low, tCEM = 400 cycles at 20 ns)."),
}


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "src")
    texts = {}
    for name in os.listdir(HERE):
        if os.path.isdir(os.path.join(HERE, name)) and name != "__pycache__":
            shutil.rmtree(os.path.join(HERE, name))
    for name, spec in MUTANTS.items():
        summary, checks, assertion, desc, subs = spec[:5]
        opts = dict(spec[5]) if len(spec) > 5 else {}
        if name in DEEP:
            opts["deep"] = True
        files = {}
        for fname, old, new in subs:
            if fname not in texts:
                texts[fname] = open(os.path.join(src, fname)).read()
            m = files.get(fname, texts[fname])
            n = m.count(old)
            if n != 1:
                sys.exit(f"{name}: pattern found {n} times in {fname} (expected 1): {old[:70]!r}")
            files[fname] = m.replace(old, new)
        d = os.path.join(HERE, name)
        os.makedirs(d)
        for fname, m in files.items():
            hdr = (f"// MUTANT {name}: {summary}. {desc}\n"
                   f"// Generated by verif/formal/mutants/make_mutants.py from the champion.\n")
            with open(os.path.join(d, fname), "w") as f:
                f.write(hdr + m)
        exp = {"class": summary, "checks": checks, "assertion": assertion,
               "description": desc, "expect": opts.get("expect", "fail"),
               "deep": opts.get("deep", False)}
        with open(os.path.join(d, "expect.json"), "w") as f:
            json.dump(exp, f, indent=2)
            f.write("\n")
        print(f"wrote mutants/{name} ({', '.join(sorted(files))}; expect "
              f"{exp['expect'].upper()} in {', '.join(checks)})")
    for name, (checks, assertion, desc) in SHARED.items():
        rtl = os.path.join(ROOT, "verif", "cosim", "mutants", name)
        if not os.path.isfile(os.path.join(rtl, TOP)):
            sys.exit(f"{name}: no {TOP} in verif/cosim/mutants/{name}")
        d = os.path.join(HERE, name)
        os.makedirs(d)
        exp = {"class": name, "checks": checks, "assertion": assertion, "description": desc,
               "expect": "fail", "deep": False, "rtl": f"verif/cosim/mutants/{name}"}
        with open(os.path.join(d, "expect.json"), "w") as f:
            json.dump(exp, f, indent=2)
            f.write("\n")
        print(f"wrote mutants/{name} (shared RTL, expect FAIL in {', '.join(checks)})")


if __name__ == "__main__":
    main()
