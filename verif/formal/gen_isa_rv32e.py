#!/usr/bin/env python3
"""Generate the rv32e ISA files for BonettoR32V's formal suite.

Reads third_party/riscv-formal/insns/isa_rv32i.txt (the 37 RV32I computational,
memory and control instructions) and writes:

  isa_rv32e.txt  the 48 instruction names: the 37 RV32I ones plus fence, ecall, ebreak, the six
                 Zicsr instructions, mret and wfi (contract v3 section 4).
  isa_rv32e.v    Verilog modules:
    rvfi_insn_<new>
        New models (riscv-formal has none). Contract section 4: FENCE (every
        encoding with opcode MISC-MEM and funct3 000, FENCE.TSO and PAUSE
        included) is a no-op; ECALL (0x00000073) and EBREAK (0x00100073) trap;
        the CSR instructions on the listed CSR numbers (a write to the read-only
        0xF11-0xF14 is UNSPECIFIED, so not modeled) read rs1 (register forms),
        write rd and go to pc + 4; MRET (0x30200073) and WFI (0x10500073) use no
        register and no memory. The CSR state (a CSR instruction's rd value,
        MRET's target) is not visible to a model: the csr check compares it.
    rvfi_insn_e_<name>  (one per instruction)
        The E filter: wraps the model (stock rv32i from the submodule, or new)
        and gates spec_valid off when any register field the instruction uses
        names x16-x31. Those encodings are reserved in RV32E (contract section
        4), so the instruction checks demand nothing of them; the consistency
        checks still apply.
    rvfi_isa_rv32e
        All 48 E-filtered models muxed into one spec, in riscv-formal's
        rvfi_isa_<isa> layout.

Usage: python3 verif/formal/gen_isa_rv32e.py [--check]
  --check  regenerate in memory and exit 1 if the committed files differ.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RVI_TXT = os.path.join(ROOT, "third_party", "riscv-formal", "insns", "isa_rv32i.txt")

NEW_INSNS = ["fence", "ecall", "ebreak", "csrrw", "csrrs", "csrrc", "csrrwi", "csrrsi",
             "csrrci", "mret", "wfi"]

PORTS = """\
  input                                 rvfi_valid,
  input  [`RISCV_FORMAL_ILEN   - 1 : 0] rvfi_insn,
  input  [`RISCV_FORMAL_XLEN   - 1 : 0] rvfi_pc_rdata,
  input  [`RISCV_FORMAL_XLEN   - 1 : 0] rvfi_rs1_rdata,
  input  [`RISCV_FORMAL_XLEN   - 1 : 0] rvfi_rs2_rdata,
  input  [`RISCV_FORMAL_XLEN   - 1 : 0] rvfi_mem_rdata,

  output                                spec_valid,
  output                                spec_trap,
  output [                       4 : 0] spec_rs1_addr,
  output [                       4 : 0] spec_rs2_addr,
  output [                       4 : 0] spec_rd_addr,
  output [`RISCV_FORMAL_XLEN   - 1 : 0] spec_rd_wdata,
  output [`RISCV_FORMAL_XLEN   - 1 : 0] spec_pc_wdata,
  output [`RISCV_FORMAL_XLEN   - 1 : 0] spec_mem_addr,
  output [`RISCV_FORMAL_XLEN/8 - 1 : 0] spec_mem_rmask,
  output [`RISCV_FORMAL_XLEN/8 - 1 : 0] spec_mem_wmask,
  output [`RISCV_FORMAL_XLEN   - 1 : 0] spec_mem_wdata"""

SPEC_SIGS = ["valid", "trap", "rs1_addr", "rs2_addr", "rd_addr", "rd_wdata", "pc_wdata",
             "mem_addr", "mem_rmask", "mem_wmask", "mem_wdata"]
SPEC_DECL = {
    "valid": "", "trap": "",
    "rs1_addr": "[                       4 : 0] ",
    "rs2_addr": "[                       4 : 0] ",
    "rd_addr": "[                       4 : 0] ",
    "rd_wdata": "[`RISCV_FORMAL_XLEN   - 1 : 0] ",
    "pc_wdata": "[`RISCV_FORMAL_XLEN   - 1 : 0] ",
    "mem_addr": "[`RISCV_FORMAL_XLEN   - 1 : 0] ",
    "mem_rmask": "[`RISCV_FORMAL_XLEN/8 - 1 : 0] ",
    "mem_wmask": "[`RISCV_FORMAL_XLEN/8 - 1 : 0] ",
    "mem_wdata": "[`RISCV_FORMAL_XLEN   - 1 : 0] ",
}

NEW_MODELS = {
    "fence": """\
  // FENCE: contract section 4, every FENCE encoding (FENCE.TSO and PAUSE included)
  // is a no-op that retires normally. Its rd, rs1, fm, pred and succ fields are
  // not register uses, so no field is reported and nothing is filtered.
  assign spec_valid = rvfi_valid && rvfi_insn[6:0] == 7'b 0001111 && rvfi_insn[14:12] == 3'b 000;
  assign spec_trap = 0;
  assign spec_rs1_addr = 0;
  assign spec_rs2_addr = 0;
  assign spec_rd_addr = 0;
  assign spec_rd_wdata = 0;
  assign spec_pc_wdata = rvfi_pc_rdata + 4;
  assign spec_mem_addr = 0;
  assign spec_mem_rmask = 0;
  assign spec_mem_wmask = 0;
  assign spec_mem_wdata = 0;
""",
    "ecall": """\
  // ECALL: fatal trap (contract section 4). Exactly 0x00000073; any other
  // SYSTEM encoding is reserved.
  assign spec_valid = rvfi_valid && rvfi_insn == 32'h 00000073;
  assign spec_trap = 1;
  assign spec_rs1_addr = 0;
  assign spec_rs2_addr = 0;
  assign spec_rd_addr = 0;
  assign spec_rd_wdata = 0;
  assign spec_pc_wdata = 0;
  assign spec_mem_addr = 0;
  assign spec_mem_rmask = 0;
  assign spec_mem_wmask = 0;
  assign spec_mem_wdata = 0;
""",
    "ebreak": """\
  // EBREAK: fatal trap (contract section 4). Exactly 0x00100073.
  assign spec_valid = rvfi_valid && rvfi_insn == 32'h 00100073;
  assign spec_trap = 1;
  assign spec_rs1_addr = 0;
  assign spec_rs2_addr = 0;
  assign spec_rd_addr = 0;
  assign spec_rd_wdata = 0;
  assign spec_pc_wdata = 0;
  assign spec_mem_addr = 0;
  assign spec_mem_rmask = 0;
  assign spec_mem_wmask = 0;
  assign spec_mem_wdata = 0;
""",
    "csrrw": """\
  // CSRRW (contract v3 section 4): funct3 001, a listed CSR number (mstatus, misa, mie,
  // mtvec, mscratch, mepc, mcause, mtval, mip, mvendorid..mhartid); a write to the read-only
  // 0xF11-0xF14 (always a write) is UNSPECIFIED. rd gets the old CSR value: spec_rd_wdata is not
  // compared by the insn check (the csr check compares it with its CSR shadow).
  wire [11:0] csr = rvfi_insn[31:20];
  wire listed = csr == 12'h 300 || csr == 12'h 301 || csr == 12'h 304 || csr == 12'h 305 ||
                csr == 12'h 340 || csr == 12'h 341 || csr == 12'h 342 || csr == 12'h 343 ||
                csr == 12'h 344 || csr == 12'h f11 || csr == 12'h f12 || csr == 12'h f13 ||
                csr == 12'h f14;
  wire writes = 1;
  assign spec_valid = rvfi_valid && rvfi_insn[6:0] == 7'b 1110011 && rvfi_insn[14:12] == 3'b 001 &&
                      listed && !(writes && csr[11:10] == 2'b 11);
  assign spec_trap = 0;
  assign spec_rs1_addr = rvfi_insn[19:15];
  assign spec_rs2_addr = 0;
  assign spec_rd_addr = rvfi_insn[11:7];
  assign spec_rd_wdata = 0;
  assign spec_pc_wdata = rvfi_pc_rdata + 4;
  assign spec_mem_addr = 0;
  assign spec_mem_rmask = 0;
  assign spec_mem_wmask = 0;
  assign spec_mem_wdata = 0;
""",
    "csrrs": """\
  // CSRRS (contract v3 section 4): funct3 010, a listed CSR number (mstatus, misa, mie,
  // mtvec, mscratch, mepc, mcause, mtval, mip, mvendorid..mhartid); a write to the read-only
  // 0xF11-0xF14 (rs1 not x0) is UNSPECIFIED. rd gets the old CSR value: spec_rd_wdata is not
  // compared by the insn check (the csr check compares it with its CSR shadow).
  wire [11:0] csr = rvfi_insn[31:20];
  wire listed = csr == 12'h 300 || csr == 12'h 301 || csr == 12'h 304 || csr == 12'h 305 ||
                csr == 12'h 340 || csr == 12'h 341 || csr == 12'h 342 || csr == 12'h 343 ||
                csr == 12'h 344 || csr == 12'h f11 || csr == 12'h f12 || csr == 12'h f13 ||
                csr == 12'h f14;
  wire writes = rvfi_insn[19:15] != 0;
  assign spec_valid = rvfi_valid && rvfi_insn[6:0] == 7'b 1110011 && rvfi_insn[14:12] == 3'b 010 &&
                      listed && !(writes && csr[11:10] == 2'b 11);
  assign spec_trap = 0;
  assign spec_rs1_addr = rvfi_insn[19:15];
  assign spec_rs2_addr = 0;
  assign spec_rd_addr = rvfi_insn[11:7];
  assign spec_rd_wdata = 0;
  assign spec_pc_wdata = rvfi_pc_rdata + 4;
  assign spec_mem_addr = 0;
  assign spec_mem_rmask = 0;
  assign spec_mem_wmask = 0;
  assign spec_mem_wdata = 0;
""",
    "csrrc": """\
  // CSRRC (contract v3 section 4): funct3 011, a listed CSR number (mstatus, misa, mie,
  // mtvec, mscratch, mepc, mcause, mtval, mip, mvendorid..mhartid); a write to the read-only
  // 0xF11-0xF14 (rs1 not x0) is UNSPECIFIED. rd gets the old CSR value: spec_rd_wdata is not
  // compared by the insn check (the csr check compares it with its CSR shadow).
  wire [11:0] csr = rvfi_insn[31:20];
  wire listed = csr == 12'h 300 || csr == 12'h 301 || csr == 12'h 304 || csr == 12'h 305 ||
                csr == 12'h 340 || csr == 12'h 341 || csr == 12'h 342 || csr == 12'h 343 ||
                csr == 12'h 344 || csr == 12'h f11 || csr == 12'h f12 || csr == 12'h f13 ||
                csr == 12'h f14;
  wire writes = rvfi_insn[19:15] != 0;
  assign spec_valid = rvfi_valid && rvfi_insn[6:0] == 7'b 1110011 && rvfi_insn[14:12] == 3'b 011 &&
                      listed && !(writes && csr[11:10] == 2'b 11);
  assign spec_trap = 0;
  assign spec_rs1_addr = rvfi_insn[19:15];
  assign spec_rs2_addr = 0;
  assign spec_rd_addr = rvfi_insn[11:7];
  assign spec_rd_wdata = 0;
  assign spec_pc_wdata = rvfi_pc_rdata + 4;
  assign spec_mem_addr = 0;
  assign spec_mem_rmask = 0;
  assign spec_mem_wmask = 0;
  assign spec_mem_wdata = 0;
""",
    "csrrwi": """\
  // CSRRWI (contract v3 section 4): funct3 101, a listed CSR number (mstatus, misa, mie,
  // mtvec, mscratch, mepc, mcause, mtval, mip, mvendorid..mhartid); a write to the read-only
  // 0xF11-0xF14 (always a write) is UNSPECIFIED. rd gets the old CSR value: spec_rd_wdata is not
  // compared by the insn check (the csr check compares it with its CSR shadow).
  wire [11:0] csr = rvfi_insn[31:20];
  wire listed = csr == 12'h 300 || csr == 12'h 301 || csr == 12'h 304 || csr == 12'h 305 ||
                csr == 12'h 340 || csr == 12'h 341 || csr == 12'h 342 || csr == 12'h 343 ||
                csr == 12'h 344 || csr == 12'h f11 || csr == 12'h f12 || csr == 12'h f13 ||
                csr == 12'h f14;
  wire writes = 1;
  assign spec_valid = rvfi_valid && rvfi_insn[6:0] == 7'b 1110011 && rvfi_insn[14:12] == 3'b 101 &&
                      listed && !(writes && csr[11:10] == 2'b 11);
  assign spec_trap = 0;
  assign spec_rs1_addr = 0;
  assign spec_rs2_addr = 0;
  assign spec_rd_addr = rvfi_insn[11:7];
  assign spec_rd_wdata = 0;
  assign spec_pc_wdata = rvfi_pc_rdata + 4;
  assign spec_mem_addr = 0;
  assign spec_mem_rmask = 0;
  assign spec_mem_wmask = 0;
  assign spec_mem_wdata = 0;
""",
    "csrrsi": """\
  // CSRRSI (contract v3 section 4): funct3 110, a listed CSR number (mstatus, misa, mie,
  // mtvec, mscratch, mepc, mcause, mtval, mip, mvendorid..mhartid); a write to the read-only
  // 0xF11-0xF14 (a nonzero zimm) is UNSPECIFIED. rd gets the old CSR value: spec_rd_wdata is not
  // compared by the insn check (the csr check compares it with its CSR shadow).
  wire [11:0] csr = rvfi_insn[31:20];
  wire listed = csr == 12'h 300 || csr == 12'h 301 || csr == 12'h 304 || csr == 12'h 305 ||
                csr == 12'h 340 || csr == 12'h 341 || csr == 12'h 342 || csr == 12'h 343 ||
                csr == 12'h 344 || csr == 12'h f11 || csr == 12'h f12 || csr == 12'h f13 ||
                csr == 12'h f14;
  wire writes = rvfi_insn[19:15] != 0;
  assign spec_valid = rvfi_valid && rvfi_insn[6:0] == 7'b 1110011 && rvfi_insn[14:12] == 3'b 110 &&
                      listed && !(writes && csr[11:10] == 2'b 11);
  assign spec_trap = 0;
  assign spec_rs1_addr = 0;
  assign spec_rs2_addr = 0;
  assign spec_rd_addr = rvfi_insn[11:7];
  assign spec_rd_wdata = 0;
  assign spec_pc_wdata = rvfi_pc_rdata + 4;
  assign spec_mem_addr = 0;
  assign spec_mem_rmask = 0;
  assign spec_mem_wmask = 0;
  assign spec_mem_wdata = 0;
""",
    "csrrci": """\
  // CSRRCI (contract v3 section 4): funct3 111, a listed CSR number (mstatus, misa, mie,
  // mtvec, mscratch, mepc, mcause, mtval, mip, mvendorid..mhartid); a write to the read-only
  // 0xF11-0xF14 (a nonzero zimm) is UNSPECIFIED. rd gets the old CSR value: spec_rd_wdata is not
  // compared by the insn check (the csr check compares it with its CSR shadow).
  wire [11:0] csr = rvfi_insn[31:20];
  wire listed = csr == 12'h 300 || csr == 12'h 301 || csr == 12'h 304 || csr == 12'h 305 ||
                csr == 12'h 340 || csr == 12'h 341 || csr == 12'h 342 || csr == 12'h 343 ||
                csr == 12'h 344 || csr == 12'h f11 || csr == 12'h f12 || csr == 12'h f13 ||
                csr == 12'h f14;
  wire writes = rvfi_insn[19:15] != 0;
  assign spec_valid = rvfi_valid && rvfi_insn[6:0] == 7'b 1110011 && rvfi_insn[14:12] == 3'b 111 &&
                      listed && !(writes && csr[11:10] == 2'b 11);
  assign spec_trap = 0;
  assign spec_rs1_addr = 0;
  assign spec_rs2_addr = 0;
  assign spec_rd_addr = rvfi_insn[11:7];
  assign spec_rd_wdata = 0;
  assign spec_pc_wdata = rvfi_pc_rdata + 4;
  assign spec_mem_addr = 0;
  assign spec_mem_rmask = 0;
  assign spec_mem_wmask = 0;
  assign spec_mem_wdata = 0;
""",
    "mret": """\
  // MRET (contract v3 section 4): exactly 0x30200073; pc = mepc, MIE = MPIE, MPIE = 1. No
  // register or memory use. spec_pc_wdata is not compared by the insn check (the csr check
  // compares pc_wdata with its mepc shadow).
  assign spec_valid = rvfi_valid && rvfi_insn == 32'h 30200073;
  assign spec_trap = 0;
  assign spec_rs1_addr = 0;
  assign spec_rs2_addr = 0;
  assign spec_rd_addr = 0;
  assign spec_rd_wdata = 0;
  assign spec_pc_wdata = 0;
  assign spec_mem_addr = 0;
  assign spec_mem_rmask = 0;
  assign spec_mem_wmask = 0;
  assign spec_mem_wdata = 0;
""",
    "wfi": """\
  // WFI (contract v3 section 4): exactly 0x10500073; retires (it may stall before the next
  // retirement, see liveness) and goes to pc + 4. No register or memory use.
  assign spec_valid = rvfi_valid && rvfi_insn == 32'h 10500073;
  assign spec_trap = 0;
  assign spec_rs1_addr = 0;
  assign spec_rs2_addr = 0;
  assign spec_rd_addr = 0;
  assign spec_rd_wdata = 0;
  assign spec_pc_wdata = rvfi_pc_rdata + 4;
  assign spec_mem_addr = 0;
  assign spec_mem_rmask = 0;
  assign spec_mem_wmask = 0;
  assign spec_mem_wdata = 0;
""",
}

HEADER = ("// DO NOT EDIT -- generated by verif/formal/gen_isa_rv32e.py from\n"
          "// third_party/riscv-formal/insns/isa_rv32i.txt plus the fence, ecall, ebreak, Zicsr,\n"
          "// mret and wfi models.\n")


def rv32i_insns():
    with open(RVI_TXT) as f:
        return [l.strip() for l in f if l.strip()]


def all_insns():
    return rv32i_insns() + NEW_INSNS


def gen_txt():
    return "".join(f"{i}\n" for i in all_insns())


def conn(prefix):
    lines = [
        "    .rvfi_valid(rvfi_valid),",
        "    .rvfi_insn(rvfi_insn),",
        "    .rvfi_pc_rdata(rvfi_pc_rdata),",
        "    .rvfi_rs1_rdata(rvfi_rs1_rdata),",
        "    .rvfi_rs2_rdata(rvfi_rs2_rdata),",
        "    .rvfi_mem_rdata(rvfi_mem_rdata),",
    ]
    outs = [f"    .spec_{s}({prefix}{s})" for s in SPEC_SIGS]
    return "\n".join(lines + [",\n".join(outs)])


def gen_v():
    out = [HEADER]
    # New models.
    for insn in NEW_INSNS:
        out.append(f"\nmodule rvfi_insn_{insn} (\n{PORTS}\n);\n{NEW_MODELS[insn]}endmodule\n")
    # E-filtered wrappers.
    for insn in all_insns():
        body = [f"\nmodule rvfi_insn_e_{insn} (\n{PORTS}\n);"]
        body.append("  wire raw_valid;")
        body.append(f"  rvfi_insn_{insn} model (")
        body.append(conn("spec_").replace(".spec_valid(spec_valid)", ".spec_valid(raw_valid)"))
        body.append("  );")
        body.append("  // E filter: a used register field naming x16-x31 makes the encoding reserved.")
        body.append("  assign spec_valid = raw_valid && !spec_rs1_addr[4] && !spec_rs2_addr[4] && !spec_rd_addr[4];")
        body.append("endmodule\n")
        out.append("\n".join(body))
    # Combined ISA module.
    out.append(f"\nmodule rvfi_isa_rv32e (\n{PORTS}\n);")
    for insn in all_insns():
        for s in SPEC_SIGS:
            out.append(f"  wire {SPEC_DECL[s]}spec_insn_{insn}_{s};")
        out.append(f"  rvfi_insn_e_{insn} insn_{insn} (")
        out.append(conn(f"spec_insn_{insn}_"))
        out.append("  );")
    for s in SPEC_SIGS:
        terms = [f"spec_insn_{i}_valid ? spec_insn_{i}_{s}" for i in all_insns()]
        out.append(f"  assign spec_{s} =\n\t\t" + " :\n\t\t".join(terms) + " : 0;")
    out.append("endmodule\n")
    return "\n".join(out)


def main():
    check = "--check" in sys.argv[1:]
    files = {os.path.join(HERE, "isa_rv32e.txt"): gen_txt(),
             os.path.join(HERE, "isa_rv32e.v"): gen_v()}
    bad = False
    for path, text in files.items():
        if check:
            try:
                with open(path) as f:
                    cur = f.read()
            except FileNotFoundError:
                cur = None
            if cur != text:
                print(f"STALE: {os.path.relpath(path, ROOT)}")
                bad = True
        else:
            with open(path, "w") as f:
                f.write(text)
            print(f"wrote {os.path.relpath(path, ROOT)}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
