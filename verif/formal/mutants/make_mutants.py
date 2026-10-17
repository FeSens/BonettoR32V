#!/usr/bin/env python3
"""Regenerate verif/formal/mutants/<name>/ from the seed (src/tt_um_fesens_r32v.v).

Each mutant is a full copy of the seed's top file with exactly one change, plus
expect.json naming the formal check(s) that must FAIL on it ("expect": "fail"), or that
must PASS ("expect": "pass": the legal variants; "checks": "all" is the whole fast
suite). test_formal.py runs those checks on each mutant at the seed's depth. The
liveness boundary pairs (stall_*) are not bugs on one side: a core whose worst-case
retirement comes exactly 1,024 edges after reset or after the previous retirement must
PASS, one edge later must FAIL (contract section 4, latency cap). They run in the deep
config, where liveness lives.

SHARED lists the mutants whose RTL is the cosim's (verif/cosim/mutants/<name>/), so
both gates test the same designs: their expect.json has "rtl" and no copy of the RTL.
"timing" names a design whose cosim timing sets the depth (for mutants of the hyp/002
cores), "slow" marks the runs of hours (skipped unless BONETTO_FORMAL_SLOW=1).

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
        ["insn_sub", "insn_add"],   # a two-member group: the counterexample is SUB's
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
    "refetch": (
        "fetches every instruction twice",
        ["bus_imem"],
        "After the fetch READ the core sends the same 4-byte READ at pc again before it "
        "executes (contract section 6: exactly one fetch; a re-fetch fails).",
        [("    reg  [31:0] regs [1:15]; // x1..x15; x0 reads as zero\n",
          "    reg  [31:0] regs [1:15]; // x1..x15; x0 reads as zero\n"
          "    reg         refetched;\n"),
         ("            gpio_out <= 8'd0;\n        end else begin\n",
          "            gpio_out <= 8'd0;\n            refetched <= 1'b0;\n        end else begin\n"),
         ("                S_FEND: begin\n"
          "                    cs_n  <= 1'b1;\n"
          "                    state <= S_EXEC;\n",
          "                S_FEND: begin\n"
          "                    cs_n      <= 1'b1;\n"
          "                    refetched <= ~refetched;\n"
          "                    state     <= refetched ? S_EXEC : S_FETCH;\n")],
    ),
}



def _trap_txn(cmd, addr, bits):
    """A trapping instruction makes one SPI transaction before it retires: the given
    command and address, `bits` + 1 bits in all (24 header bits + data)."""
    return [("                    if (trap) begin\n                        state <= S_WB;",
             "                    if (trap) begin\n                        cs_n  <= 1'b0;\n"
             "                        state <= S_MSPI;   // MUTANT: a transaction on a trap"),
            ("            S_EXEC: begin\n                if (is_load | is_store) begin",
             "            S_EXEC: begin\n                if (trap) begin\n"
             f"                    sr     <= {{8'h{cmd:02x}, 16'h{addr:04x}, 32'd0}};\n"
             f"                    bitcnt <= 6'd{bits};\n"
             "                end else if (is_load | is_store) begin")]


def _junk_trap_fields(cond):
    return [("    assign rvfi_rs1_addr  = r_rs1 ? rs1 : 5'd0;",
             f"    assign rvfi_rs1_addr  = ({cond}) ? 5'd7 : r_rs1 ? rs1 : 5'd0;"),
            ("    assign rvfi_rs2_addr  = r_rs2 ? rs2 : 5'd0;",
             f"    assign rvfi_rs2_addr  = ({cond}) ? 5'd9 : r_rs2 ? rs2 : 5'd0;"),
            ("    assign rvfi_rs1_rdata = r_rs1 ? rs1_val : 32'd0;",
             f"    assign rvfi_rs1_rdata = ({cond}) ? 32'hdeadbeef : r_rs1 ? rs1_val : 32'd0;"),
            ("    assign rvfi_rs2_rdata = r_rs2 ? rs2_val : 32'd0;",
             f"    assign rvfi_rs2_rdata = ({cond}) ? 32'h0badf00d : r_rs2 ? rs2_val : 32'd0;"),
            ("    assign rvfi_rd_wdata  = rd_we ? rd_wdata : 32'd0;",
             "    assign rvfi_rd_wdata  = trap ? 32'h00001234 : rd_we ? rd_wdata : 32'd0;"),
            ("    assign rvfi_pc_wdata  = npc;",
             "    assign rvfi_pc_wdata  = trap ? 32'h00000002 : npc;")]


MUTANTS.update({
    "trap_fields_brj": (
        "junk register fields on a trapping branch or JALR",
        ["insn_jalr"],
        "Every trapping retirement reports arbitrary rs1/rs2 fields (the cosim's v1 "
        "legal_trap_fields). Contract v2 section 6: a trapping branch or JALR must report its "
        "operands truthfully.",
        _junk_trap_fields("trap"),
    ),
    "legal_trap_fields_nb": (
        "legal: junk fields on non-branch traps",
        "all",
        "Trapping retirements other than a branch or JALR report arbitrary rs1/rs2 fields, "
        "rd_wdata and pc_wdata; contract section 6 compares none of them. Expect PASS.",
        _junk_trap_fields("trap & ~(is_branch | is_jalr)"),
        {"expect": "pass"},
    ),
    "legal_uo_early": (
        "legal: uo_out changes in the retirement cycle",
        "all",
        "An IO store sets uo_out on the edge that enters its retirement cycle (allowed: "
        "section 3 only bounds it by one cycle after the retirement), misaligned IO stores "
        "included (unspecified). The review's variant. Expect PASS.",
        [("                    end else begin\n                        state <= S_WB;\n"
          "                    end\n                end\n                S_MEND: begin",
          "                    end else begin\n                        if (io_write)\n"
          "                            gpio_out <= io_wbyte;   // MUTANT: in the retirement cycle\n"
          "                        state <= S_WB;\n"
          "                    end\n                end\n                S_MEND: begin"),
         ("                    pc <= npc;\n                    if (io_write)\n"
          "                        gpio_out <= io_wbyte;\n",
          "                    pc <= npc;\n")],
        {"expect": "pass"},
    ),
    "trap_program_write": (
        "a program WRITE on a trapping instruction",
        ["bus_dmem"],
        "Before a trapping instruction retires, the core WRITEs one byte at 0x0100 (contract "
        "section 4: no program-memory write on a trap).",
        _trap_txn(0x02, 0x0100, 31),
    ),
    "trap_region_wrap": (
        "a region READ that wraps out of the region",
        ["bus_dmem"],
        "Before a trapping instruction retires, the core READs 4 bytes at 0xFFFE: 0xFFFE, "
        "0xFFFF, 0x0000, 0x0001 (contract section 6: a region transaction may not wrap out of "
        "the region).",
        _trap_txn(0x03, 0xfffe, 55),
    ),
    "trap_region_straddle": (
        "a READ that straddles into the region",
        ["bus_dmem"],
        "Before a trapping instruction retires, the core READs 4 bytes at 0xFF7E: two program "
        "bytes and two region bytes in one transaction.",
        _trap_txn(0x03, 0xff7e, 55),
    ),
    "legal_trap_region": (
        "legal: a region READ on a trap",
        "all",
        "Before a trapping instruction retires, the core READs 4 bytes at 0xFF80 (contract "
        "section 4: a trapping instruction may make core-region transactions). Expect PASS.",
        _trap_txn(0x03, 0xff80, 55),
        {"expect": "pass"},
    ),
    "legal_null_read": (
        "legal: a null READ",
        "all",
        "Before a trapping instruction retires, the core sends a READ header for 0x1234 and "
        "raises CS_n with no data bit (contract section 6: null READs are allowed anywhere). "
        "Expect PASS.",
        _trap_txn(0x03, 0x1234, 23),
        {"expect": "pass"},
    ),
    "region_write_after_halt": (
        "a region WRITE after the halt",
        ["silent"],
        "After the halting retirement the core WRITEs one byte at 0xFF80 (contract section 4: "
        "never another SPI WRITE after the halt, core region included).",
        [("                     S_HALT  = 4'd8;   // after a fatal trap: never leaves (until reset)",
          "                     S_HALT  = 4'd8,   // after a fatal trap: never leaves (until reset)\n"
          "                     S_HW = 4'd9, S_HSPI = 4'd10, S_HEND = 4'd11;   // MUTANT"),
         ("                S_FSPI, S_MSPI: begin\n                    sck <= ~sck;\n"
          "                    if (sck && bitcnt == 6'd0)\n"
          "                        state <= (state == S_FSPI) ? S_FEND : S_MEND;",
          "                S_FSPI, S_MSPI, S_HSPI: begin\n                    sck <= ~sck;\n"
          "                    if (sck && bitcnt == 6'd0)\n"
          "                        state <= (state == S_FSPI) ? S_FEND :\n"
          "                                 (state == S_MSPI) ? S_MEND : S_HEND;"),
         ("state <= trap ? S_HALT : S_FETCH;", "state <= trap ? S_HW : S_FETCH;"),
         ("                default: state <= state;   // S_HALT",
          "                S_HW: begin\n                    cs_n  <= 1'b0;\n"
          "                    state <= S_HSPI;\n                end\n"
          "                S_HEND: begin\n                    cs_n  <= 1'b1;\n"
          "                    state <= S_HALT;\n                end\n"
          "                default: state <= state;   // S_HALT"),
         ("            S_FSPI, S_MSPI: begin\n                if (sck) begin",
          "            S_FSPI, S_MSPI, S_HSPI: begin\n                if (sck) begin"),
         ("            S_FEND: ir <= fetch_word;",
          "            S_HW: begin\n                sr     <= {8'h02, 16'hff80, 32'd0};\n"
          "                bitcnt <= 6'd31;\n            end\n"
          "            S_FEND: ir <= fetch_word;")],
    ),
})

# The cosim's mutants (verif/cosim/mutants/<name>/tt_um_fesens_r32v.v), shared:
# name: (expected checks, options)
SHARED = {
    "cs_open_at_retire": (["spi"], {}),
    "fetch_extra_byte": (["bus_imem", "bus_dmem"], {}),
    "load_own_fetch": (["bus_dmem"], {}),
    "zero_init": (["rvfi", "bus_imem"], {}),
    "trap_writes_rd": (["insn_jal", "rvfi"], {}),
    "uo_glitch": (["gpio"], {}),
    "uo_late": (["gpio"], {}),
    "no_halt": (["silent"], {}),
    "store_twice": (["bus_dmem"], {}),
    "store_extra_byte": (["bus_dmem"], {}),
    "load_skip_byte": (["bus_dmem"], {}),
    "legal_mosi_echo": ("all", {"expect": "pass"}),
    "legal_split_store": ("all", {"expect": "pass"}),
    "region_wrap": (["bus_dmem"], {"timing": "legal_h002_rf", "slow": True}),
    "region_straddle": (["bus_dmem"], {"timing": "legal_h002_rf", "slow": True}),
    "region_zero_init": (["rvfi", "bus_imem"], {"timing": "legal_h002_pcram", "slow": True}),
    "legal_h002_rf": ("all", {"expect": "pass", "timing": "legal_h002_rf", "slow": True}),
    "legal_h002_pcram": ("all", {"expect": "pass", "timing": "legal_h002_pcram", "slow": True}),
}

# Liveness boundary (contract section 4 edge count, as the cosim counts it). Measured
# on the seed (cosim --timing-json: first retirement at edge 115, a 4-byte RAM access
# takes 230 edges, everything else 116): the latest first retirement is a 4-byte RAM
# access at edge 229, and the longest gap between retirements is 230 edges. A fetch
# stall of N cycles moves the latest first retirement to edge 229 + N (stall_first;
# N = 795 gives 1,024, N = 796 gives 1,025) or the longest gap after the first
# retirement to 230 + N (stall_later; N = 794 gives 1,024, N = 795 gives 1,025). Each
# pair also pins formal's count: the PASS/FAIL flip is at exactly one cycle.
_DECL = ("    reg  [31:0] regs [1:15]; // x1..x15; x0 reads as zero\n",
         "    reg  [31:0] regs [1:15]; // x1..x15; x0 reads as zero\n"
         "    reg  [10:0] stall_n;     // MUTANT: fetch stall counter\n"
         "    reg         armed;\n")
_FETCH = ("                S_FETCH: begin\n"
          "                    cs_n  <= 1'b0;\n"
          "                    state <= S_FSPI;\n"
          "                end\n",
          "                S_FETCH: begin\n"
          "                    if (stall_n != 11'd0) begin\n"
          "                        stall_n <= stall_n - 11'd1;\n"
          "                    end else begin\n"
          "                        cs_n  <= 1'b0;\n"
          "                        state <= S_FSPI;\n"
          "                    end\n"
          "                end\n")


def _reset(first_n):
    return ("            gpio_out <= 8'd0;\n        end else begin\n",
            "            gpio_out <= 8'd0;\n"
            f"            stall_n  <= 11'd{first_n};\n"
            "            armed    <= 1'b1;\n"
            "        end else begin\n")


def _wb(later_n):
    return ("                S_WB: begin\n                    pc <= npc;\n",
            "                S_WB: begin\n                    pc <= npc;\n"
            "                    if (armed) begin\n"
            f"                        stall_n <= 11'd{later_n};\n"
            "                        armed   <= 1'b0;\n"
            "                    end\n")


for _n, _exp in ((795, "pass"), (796, "fail")):
    MUTANTS[f"stall_first_{229 + _n}"] = (
        f"first retirement at edge {229 + _n} at the latest",
        ["liveness"],
        f"The first fetch waits {_n} extra cycles after reset: the latest first retirement "
        f"(a 4-byte RAM access) is at edge {229 + _n}. Expect {_exp.upper()}.",
        [_DECL, _reset(_n), _FETCH],
        {"expect": _exp, "deep": True},
    )
for _n, _exp in ((794, "pass"), (795, "fail")):
    MUTANTS[f"stall_later_{230 + _n}"] = (
        f"second retirement {230 + _n} edges after the first at the latest",
        ["liveness"],
        f"The second fetch waits {_n} extra cycles: the longest gap (a 4-byte RAM access "
        f"second) is {230 + _n} edges. Expect {_exp.upper()}.",
        [_DECL, _reset(0), _FETCH, _wb(_n)],
        {"expect": _exp, "deep": True},
    )


def main():
    seed = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "src", TOPFILE)
    text = open(seed).read()
    for name, spec in MUTANTS.items():
        cls, checks, desc, subs = spec[:4]
        opts = spec[4] if len(spec) > 4 else {}
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
            json.dump({"class": cls, "checks": checks, "description": desc,
                       "expect": opts.get("expect", "fail"), "deep": opts.get("deep", False)},
                      f, indent=2)
            f.write("\n")
        print(f"wrote mutants/{name} (expect {opts.get('expect', 'fail').upper()} in "
              f"{checks if isinstance(checks, str) else ', '.join(checks)})")
    cosim = os.path.join(ROOT, "verif", "cosim", "mutants")
    with open(os.path.join(cosim, "MANIFEST.json")) as f:
        manifest = json.load(f)
    for name, (checks, opts) in SHARED.items():
        rtl = os.path.join(cosim, name)
        if not os.path.isfile(os.path.join(rtl, TOPFILE)):
            sys.exit(f"{name}: no {TOPFILE} in verif/cosim/mutants/{name}")
        d = os.path.join(HERE, name)
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            if f.endswith((".v", ".sv")):
                os.remove(os.path.join(d, f))   # the RTL is the cosim's
        exp = {"class": name, "checks": checks,
               "description": manifest.get(name, {}).get("desc", ""),
               "expect": opts.get("expect", "fail"), "deep": opts.get("deep", False),
               "rtl": f"verif/cosim/mutants/{name}"}
        for k in ("timing", "slow"):
            if k in opts:
                exp[k] = opts[k]
        with open(os.path.join(d, "expect.json"), "w") as f:
            json.dump(exp, f, indent=2)
            f.write("\n")
        print(f"wrote mutants/{name} (shared RTL, expect {exp['expect'].upper()})")


if __name__ == "__main__":
    main()
