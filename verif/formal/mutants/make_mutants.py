#!/usr/bin/env python3
"""Regenerate verif/formal/mutants/<name>/ from the seed (src/tt_um_fesens_r32v.v).

Each mutant is a full copy of the seed's top file with exactly one bug, plus
expect.json naming the formal check(s) that must FAIL on it. test_formal.py runs
those checks on each mutant at the seed's depth and expects a FAIL.

  python3 verif/formal/mutants/make_mutants.py [seed.v]

Each substitution must match exactly once, so a changed seed fails loudly here
instead of producing a mutant that is not the intended bug.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TOPFILE = "tt_um_fesens_r32v.v"

# name: (class, expected failing checks, description, [(old, new), ...])
MUTANTS = {
    "miso_low": (
        "samples MISO while SCK is low",
        ["bus_imem"],
        "MISO goes through an extra flip-flop, so the falling-edge capture reads the value "
        "from the cycle where SCK was low (noise).",
        [("    reg  [31:0] regs [1:15]; // x1..x15; x0 reads as zero\n",
          "    reg  [31:0] regs [1:15]; // x1..x15; x0 reads as zero\n"
          "    reg         miso_q;\n    always @(posedge clk) miso_q <= uio_in[2];\n"),
         ("sr     <= {sr[54:0], miso & ~bitcnt[5]};", "sr     <= {sr[54:0], miso_q & ~bitcnt[5]};")],
    ),
    "mosi_rise": (
        "changes MOSI on the rising SCK edge",
        ["spi"],
        "MOSI is XORed with the next bit while SCK is high.",
        [("wire mosi = sr[55] & ~cs_n;", "wire mosi = (sr[55] ^ (sck & sr[54])) & ~cs_n;")],
    ),
    "cs_sck_high": (
        "CS_n toggles while SCK is high",
        ["spi"],
        "CS_n rises on the edge where SCK falls after the last bit.",
        [("                    if (sck && bitcnt == 6'd0)\n"
          "                        state <= (state == S_FSPI) ? S_FEND : S_MEND;",
          "                    if (sck && bitcnt == 6'd0) begin\n"
          "                        state <= (state == S_FSPI) ? S_FEND : S_MEND;\n"
          "                        cs_n  <= 1'b1;\n"
          "                    end")],
    ),
    "write_midbyte": (
        "WRITE ends mid-byte",
        ["spi", "bus_dmem"],
        "Store transactions send one data bit fewer than the access size.",
        [("wire [5:0] mem_bits = (msize == 2'b00) ? 6'd31 : (msize == 2'b01) ? 6'd39 : 6'd55;",
          "wire [5:0] mem_bits = ((msize == 2'b00) ? 6'd31 : (msize == 2'b01) ? 6'd39 : 6'd55) "
          "- {5'd0, is_store};")],
    ),
    "rvfi_rd": (
        "a wrong RVFI rd value",
        ["insn_xor"],
        "RVFI reports XOR results with bit 0 flipped (the register file gets the right value).",
        [("assign rvfi_rd_wdata  = rd_we ? rd_wdata : 32'd0;",
          "assign rvfi_rd_wdata  = rd_we ? (rd_wdata ^ {31'd0, is_op & (funct3 == 3'b100)}) : 32'd0;")],
    ),
    "norvfi_diff": (
        "a behavior difference only without RVFI",
        ["equiv"],
        "uo_out is inverted in the plain build only.",
        [("    assign uo_out  = gpio_out;\n",
          "`ifdef RISCV_FORMAL\n    assign uo_out  = gpio_out;\n`else\n    assign uo_out  = ~gpio_out;\n`endif\n")],
    ),
    "sub_decode": (
        "a decoder bug in SUB",
        ["insn_sub"],
        "SUB (funct3 000) is decoded as ADD.",
        [("wire        alu_sub = is_op & ir[30];", "wire        alu_sub = is_op & ir[30] & funct3[0];")],
    ),
    "gpio_any_byte": (
        "GPIO store ignores the byte address",
        ["gpio"],
        "Any IO store sets uo_out, even when it writes no byte with X[1:0] = 0.",
        [("wire        io_write = ~trap & is_store & is_io & io_hit;",
          "wire        io_write = ~trap & is_store & is_io;")],
    ),
    "trap_continues": (
        "keeps executing after a fatal trap",
        ["silent"],
        "A misaligned jump or branch target retires with trap = halt = 1, then the core fetches again.",
        [("state <= trap ? S_HALT : S_FETCH;", "state <= (trap & ~misaligned) ? S_HALT : S_FETCH;")],
    ),
    "hang_reserved": (
        "hangs on a reserved encoding",
        ["liveness"],
        "The custom-0 opcode (0001011) stays in EXEC forever instead of trapping.",
        [("                    if (trap) begin\n                        state <= S_WB;",
          "                    if (trap) begin\n"
          "                        state <= (opcode == 7'b0001011) ? S_EXEC : S_WB;")],
    ),
    "reg_wrong_index": (
        "writes the wrong register",
        ["reg"],
        "Even destination registers are written one register up (x2 writes x3); RVFI reports x2.",
        [("regs[rd[3:0]] <= rd_wdata;", "regs[{rd[3:1], 1'b1}] <= rd_wdata;")],
    ),
    "load_half_swap": (
        "halfword load bytes swapped",
        ["bus_dmem"],
        "LH/LHU from RAM return their two bytes swapped, in the register and in RVFI alike.",
        [("                2'b01:   swap_n = {16'd0, x[7:0], x[15:8]};",
          "                2'b01:   swap_n = {16'd0, x[15:8], x[7:0]};")],
    ),
}


def main():
    seed = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "src", TOPFILE)
    text = open(seed).read()
    for name, (cls, checks, desc, subs) in MUTANTS.items():
        m = text
        for old, new in subs:
            n = m.count(old)
            if n != 1:
                sys.exit(f"{name}: pattern found {n} times (expected 1): {old[:60]!r}")
            m = m.replace(old, new)
        d = os.path.join(HERE, name)
        os.makedirs(d, exist_ok=True)
        hdr = f"// MUTANT {name}: {cls}. {desc}\n// Generated by verif/formal/mutants/make_mutants.py from the seed.\n"
        with open(os.path.join(d, TOPFILE), "w") as f:
            f.write(hdr + m)
        with open(os.path.join(d, "expect.json"), "w") as f:
            json.dump({"class": cls, "checks": checks, "description": desc}, f, indent=2)
            f.write("\n")
        print(f"wrote mutants/{name} (expect FAIL in {', '.join(checks)})")


if __name__ == "__main__":
    main()
