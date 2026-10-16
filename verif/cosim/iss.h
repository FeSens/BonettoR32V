// BonettoR32V reference ISS: RV32E with the exact memory map and trap rules of docs/contract.md
// (sections 3 and 4). Header-only, so the cosim harness and the standalone runner share one model.
//
// Memory map (contract section 3):
//   - fetch reads RAM at pc[15:0] (the RAM aliases every 64 KB);
//   - data with addr[31] = 0: byte k of an n-byte access is RAM[(addr[15:0] + k) mod 2^16];
//   - data with addr[31] = 1: GPIO. Reading the byte at X with X[1:0] = 0 returns ui_in, every other
//     IO byte reads 0. Writing the byte at X with X[1:0] = 0 sets uo_out, other IO writes are
//     ignored. Misaligned IO accesses (n > 1, addr[1:0] != 0) are unspecified.
// Fatal traps (contract section 4): ECALL, EBREAK, JAL / JALR / taken branch with target[1] = 1,
// 0x00000000 and 0xFFFFFFFF. A trap retires with trap = halt = 1 and no register or memory write.
// Every other non-instruction encoding is reserved (unspecified): step() flags it with
// Retire::reserved and changes no state. Programs used by the checkers never contain one.
#pragma once

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

struct Retire {
  uint32_t pc, npc, insn;
  bool trap, halt;
  uint8_t rs1, rs2, rd;     // register indices actually read / written (0 when unused)
  uint32_t rs1v, rs2v, rdv;  // 0 when the matching index is 0
  uint32_t maddr;            // exact address of the first byte (valid when a mask is set)
  uint8_t rmask, wmask;      // LSB-aligned: 0x1, 0x3 or 0xF
  uint32_t mrdata, mwdata;   // byte lane k is the byte at maddr + k; lanes outside the mask are 0
  // The behavior is unspecified by the contract: a reserved encoding, or a misaligned IO access.
  // The checkers treat it as an error in the test program.
  bool reserved;
};

// Deterministic GPIO input sequence shared by the harness, the runner and the gate-level test.
// The k-th value for a given seed. Kept simple so other languages can reproduce it:
//   x = (k * 0x9E3779B1) ^ ((seed + 0x7F4A7C15) * 0x85EBCA6B)   (all mod 2^32)
//   x ^= x >> 16; x *= 0x7FEB352D; x ^= x >> 15; x *= 0x846CA68B; x ^= x >> 16; return x & 0xFF
static inline uint8_t ui_value(uint32_t seed, uint64_t k) {
  uint32_t x = (uint32_t)k * 0x9E3779B1u ^ (seed + 0x7F4A7C15u) * 0x85EBCA6Bu;
  x ^= x >> 16;
  x *= 0x7FEB352Du;
  x ^= x >> 15;
  x *= 0x846CA68Bu;
  x ^= x >> 16;
  return (uint8_t)x;
}

class Iss {
 public:
  static constexpr uint32_t RAM_SIZE = 1u << 16;

  Iss() { reset(); }

  // Back to the post-reset state: pc = 0, registers 0, uo_out = 0, not halted. RAM is kept.
  void reset() {
    std::memset(x_, 0, sizeof(x_));
    pc_ = 0;
    uo_ = 0;
    halted_ = false;
    retired_ = 0;
    written_ = 1;  // x0 counts as written
    uninit_read_ = false;
    uninit_pc_ = 0;
  }

  void load_image(const uint8_t* ram64k) { std::memcpy(ram_, ram64k, RAM_SIZE); }

  // Execute one instruction. ui_in is the GPIO input value this instruction observes.
  Retire step(uint8_t ui_in);

  uint8_t uo_out() const { return uo_; }
  const uint8_t* ram() const { return ram_; }
  bool halted() const { return halted_; }

  // Extras for the runner and tests.
  uint32_t pc() const { return pc_; }
  uint32_t reg(int i) const { return x_[i & 15]; }
  uint64_t retired() const { return retired_; }
  // Program check: some instruction read a register no earlier instruction wrote. The core's
  // registers start unknown (contract section 1), so such a program can't be checked.
  bool uninit_read() const { return uninit_read_; }
  uint32_t uninit_pc() const { return uninit_pc_; }

  // Test-only hooks for the spike cross-check (spike has no GPIO and needs code away from 0).
  void set_pc(uint32_t pc) { pc_ = pc; }
  // IO accesses go to a plain 4 KB RAM page at 0x80000000 instead of the GPIO model.
  void set_io_ram_compat(bool on) {
    io_compat_ = on;
    std::memset(io_ram_, 0, sizeof(io_ram_));
  }

 private:
  uint32_t x_[16];
  uint32_t pc_;
  uint8_t uo_;
  bool halted_;
  uint64_t retired_;
  uint32_t written_;
  bool uninit_read_;
  uint32_t uninit_pc_;
  uint8_t ram_[RAM_SIZE];
  bool io_compat_ = false;
  uint8_t io_ram_[4096];

  uint32_t rd_reg(uint8_t r) {
    if (!((written_ >> r) & 1) && !uninit_read_) {
      uninit_read_ = true;
      uninit_pc_ = pc_;
    }
    return x_[r];
  }

  // Byte k of an access at effective address ea. The access goes to IO or RAM as a whole,
  // decided by ea[31] (contract section 3): LW at 0x7FFFFFFE reads RAM 0xFFFE..0x0001.
  uint8_t data_read_byte(uint32_t ea, unsigned k, uint8_t ui_in) const {
    uint32_t a = ea + k;
    if (ea >> 31) {
      if (io_compat_) return io_ram_[a & 0xFFF];
      return (a & 3) == 0 ? ui_in : 0;
    }
    return ram_[a & 0xFFFF];
  }
  void data_write_byte(uint32_t ea, unsigned k, uint8_t v) {
    uint32_t a = ea + k;
    if (ea >> 31) {
      if (io_compat_) {
        io_ram_[a & 0xFFF] = v;
        return;
      }
      if ((a & 3) == 0) uo_ = v;
      return;
    }
    ram_[a & 0xFFFF] = v;
  }
};

inline Retire Iss::step(uint8_t ui_in) {
  Retire r;
  std::memset(&r, 0, sizeof(r));
  if (halted_) {  // caller bug: a halted hart never retires again
    r.pc = r.npc = pc_;
    r.trap = r.halt = true;
    return r;
  }
  const uint32_t pc = pc_;
  uint32_t insn = 0;
  for (int k = 0; k < 4; k++) insn |= (uint32_t)ram_[(pc + k) & 0xFFFF] << (8 * k);
  r.pc = pc;
  r.insn = insn;
  r.npc = pc + 4;

  const uint32_t opcode = insn & 0x7F;
  const uint32_t rd = (insn >> 7) & 0x1F;
  const uint32_t f3 = (insn >> 12) & 7;
  const uint32_t rs1 = (insn >> 15) & 0x1F;
  const uint32_t rs2 = (insn >> 20) & 0x1F;
  const uint32_t f7 = insn >> 25;
  const uint32_t imm_i = (uint32_t)((int32_t)insn >> 20);
  const uint32_t imm_s = (uint32_t)(((int32_t)insn >> 25) << 5) | ((insn >> 7) & 0x1F);
  const uint32_t imm_b = (uint32_t)(((int32_t)insn >> 31) << 12) | ((insn & 0x80) << 4) |
                         ((insn >> 20) & 0x7E0) | ((insn >> 7) & 0x1E);
  const uint32_t imm_u = insn & 0xFFFFF000u;
  const uint32_t imm_j = (uint32_t)(((int32_t)insn >> 31) << 20) | (insn & 0xFF000) |
                         ((insn >> 9) & 0x800) | ((insn >> 20) & 0x7FE);

  auto reserved = [&]() {
    // No state change. npc stays pc + 4, but nothing may rely on it.
    r.reserved = true;
    return r;
  };
  auto trap = [&]() {
    r.trap = r.halt = true;
    r.rd = 0;
    r.rdv = 0;
    halted_ = true;
    retired_++;
    return r;
  };
  auto use_rs1 = [&]() {
    r.rs1 = (uint8_t)rs1;
    r.rs1v = rs1 ? rd_reg((uint8_t)rs1) : 0;
  };
  auto use_rs2 = [&]() {
    r.rs2 = (uint8_t)rs2;
    r.rs2v = rs2 ? rd_reg((uint8_t)rs2) : 0;
  };
  auto finish = [&](bool writes_rd, uint32_t value) {
    if (writes_rd && rd != 0) {
      r.rd = (uint8_t)rd;
      r.rdv = value;
      x_[rd] = value;
      written_ |= 1u << rd;
    }
    pc_ = r.npc;
    retired_++;
    return r;
  };

  if (insn == 0x00000000u || insn == 0xFFFFFFFFu) return trap();
  if ((insn & 3) != 3) return reserved();

  switch (opcode) {
    case 0x37:  // LUI
      if (rd > 15) return reserved();
      return finish(true, imm_u);
    case 0x17:  // AUIPC
      if (rd > 15) return reserved();
      return finish(true, pc + imm_u);
    case 0x6F: {  // JAL
      if (rd > 15) return reserved();
      uint32_t t = pc + imm_j;
      r.npc = t;
      if (t & 2) return trap();
      return finish(true, pc + 4);
    }
    case 0x67: {  // JALR
      if (f3 != 0 || rd > 15 || rs1 > 15) return reserved();
      use_rs1();
      uint32_t t = (r.rs1v + imm_i) & ~1u;
      r.npc = t;
      if (t & 2) return trap();
      return finish(true, pc + 4);
    }
    case 0x63: {  // BRANCH
      if (f3 == 2 || f3 == 3 || rs1 > 15 || rs2 > 15) return reserved();
      use_rs1();
      use_rs2();
      uint32_t a = r.rs1v, b = r.rs2v;
      bool take = false;
      switch (f3) {
        case 0: take = a == b; break;
        case 1: take = a != b; break;
        case 4: take = (int32_t)a < (int32_t)b; break;
        case 5: take = (int32_t)a >= (int32_t)b; break;
        case 6: take = a < b; break;
        case 7: take = a >= b; break;
      }
      if (take) {
        uint32_t t = pc + imm_b;
        r.npc = t;
        if (t & 2) return trap();
      }
      return finish(false, 0);
    }
    case 0x03: {  // LOAD
      if (f3 == 3 || f3 > 5 || rd > 15 || rs1 > 15) return reserved();
      use_rs1();
      uint32_t ea = r.rs1v + imm_i;
      unsigned n = (f3 & 3) == 0 ? 1 : (f3 & 3) == 1 ? 2 : 4;
      if ((ea >> 31) && n > 1 && (ea & 3) && !io_compat_) return reserved();
      uint32_t v = 0;
      for (unsigned k = 0; k < n; k++) v |= (uint32_t)data_read_byte(ea, k, ui_in) << (8 * k);
      r.maddr = ea;
      r.rmask = (uint8_t)((1u << n) - 1);
      r.mrdata = v;
      uint32_t res;
      switch (f3) {
        case 0: res = (uint32_t)(int32_t)(int8_t)v; break;
        case 1: res = (uint32_t)(int32_t)(int16_t)v; break;
        default: res = v; break;  // LW, LBU, LHU
      }
      return finish(true, res);
    }
    case 0x23: {  // STORE
      if (f3 > 2 || rs1 > 15 || rs2 > 15) return reserved();
      use_rs1();
      use_rs2();
      uint32_t ea = r.rs1v + imm_s;
      unsigned n = 1u << f3;
      if ((ea >> 31) && n > 1 && (ea & 3) && !io_compat_) return reserved();
      uint32_t v = n == 4 ? r.rs2v : r.rs2v & ((1u << (8 * n)) - 1);
      for (unsigned k = 0; k < n; k++) data_write_byte(ea, k, (uint8_t)(v >> (8 * k)));
      r.maddr = ea;
      r.wmask = (uint8_t)((1u << n) - 1);
      r.mwdata = v;
      return finish(false, 0);
    }
    case 0x13: {  // OP-IMM
      if (rd > 15 || rs1 > 15) return reserved();
      if (f3 == 1 && f7 != 0) return reserved();
      if (f3 == 5 && f7 != 0 && f7 != 0x20) return reserved();
      use_rs1();
      uint32_t a = r.rs1v, res = 0, sh = rs2;  // shamt = insn[24:20]
      switch (f3) {
        case 0: res = a + imm_i; break;
        case 2: res = (int32_t)a < (int32_t)imm_i; break;
        case 3: res = a < imm_i; break;
        case 4: res = a ^ imm_i; break;
        case 6: res = a | imm_i; break;
        case 7: res = a & imm_i; break;
        case 1: res = a << sh; break;
        case 5: res = f7 ? (uint32_t)((int32_t)a >> sh) : a >> sh; break;
      }
      return finish(true, res);
    }
    case 0x33: {  // OP
      if (rd > 15 || rs1 > 15 || rs2 > 15) return reserved();
      if (f7 != 0 && !(f7 == 0x20 && (f3 == 0 || f3 == 5))) return reserved();
      use_rs1();
      use_rs2();
      uint32_t a = r.rs1v, b = r.rs2v, res = 0;
      switch (f3) {
        case 0: res = f7 ? a - b : a + b; break;
        case 1: res = a << (b & 31); break;
        case 2: res = (int32_t)a < (int32_t)b; break;
        case 3: res = a < b; break;
        case 4: res = a ^ b; break;
        case 5: res = f7 ? (uint32_t)((int32_t)a >> (b & 31)) : a >> (b & 31); break;
        case 6: res = a | b; break;
        case 7: res = a & b; break;
      }
      return finish(true, res);
    }
    case 0x0F:  // MISC-MEM: every FENCE encoding (funct3 = 0) is a no-op; FENCE.I is not RV32E
      if (f3 != 0) return reserved();
      return finish(false, 0);
    case 0x73:  // SYSTEM: only ECALL and EBREAK exist
      if (insn == 0x00000073u || insn == 0x00100073u) return trap();
      return reserved();
    default:
      return reserved();
  }
}

// Instruction classes for the timing report (run_cosim.py --timing-json).
static inline const char* iss_class(const Retire& r) {
  if (r.trap) return "trap";
  uint32_t op = r.insn & 0x7F, f3 = (r.insn >> 12) & 7;
  switch (op) {
    case 0x37:
    case 0x17: return "lui_auipc";
    case 0x6F: return "jal";
    case 0x67: return "jalr";
    case 0x63: {
      uint32_t a = r.rs1v, b = r.rs2v;
      bool take = false;
      switch (f3) {
        case 0: take = a == b; break;
        case 1: take = a != b; break;
        case 4: take = (int32_t)a < (int32_t)b; break;
        case 5: take = (int32_t)a >= (int32_t)b; break;
        case 6: take = a < b; break;
        case 7: take = a >= b; break;
      }
      return take ? "branch_taken" : "branch_not_taken";
    }
    case 0x03: return (r.maddr >> 31) ? "io_load" : "load";
    case 0x23: return (r.maddr >> 31) ? "io_store" : "store";
    case 0x13: return (f3 == 1 || f3 == 5) ? "shift" : "alu_imm";
    case 0x33: return (f3 == 1 || f3 == 5) ? "shift" : "alu";
    case 0x0F: return "fence";
  }
  return "reserved";
}

// Small disassembler for error messages.
static inline std::string iss_disasm(uint32_t insn) {
  static const char* rn[32] = {"zero", "ra", "sp",  "gp",  "tp", "t0", "t1", "t2",
                               "s0",   "s1", "a0",  "a1",  "a2", "a3", "a4", "a5",
                               "x16",  "x17", "x18", "x19", "x20", "x21", "x22", "x23",
                               "x24",  "x25", "x26", "x27", "x28", "x29", "x30", "x31"};
  char b[96];
  uint32_t op = insn & 0x7F, rd = (insn >> 7) & 31, f3 = (insn >> 12) & 7, rs1 = (insn >> 15) & 31,
           rs2 = (insn >> 20) & 31, f7 = insn >> 25;
  int32_t ii = (int32_t)insn >> 20;
  int32_t is = (int32_t)((((int32_t)insn >> 25) << 5) | ((insn >> 7) & 0x1F));
  int32_t ib = (int32_t)((((int32_t)insn >> 31) << 12) | ((insn & 0x80) << 4) |
                         ((insn >> 20) & 0x7E0) | ((insn >> 7) & 0x1E));
  int32_t ij = (int32_t)((((int32_t)insn >> 31) << 20) | (insn & 0xFF000) | ((insn >> 9) & 0x800) |
                         ((insn >> 20) & 0x7FE));
  static const char* br[8] = {"beq", "bne", "?", "?", "blt", "bge", "bltu", "bgeu"};
  static const char* ld[8] = {"lb", "lh", "lw", "?", "lbu", "lhu", "?", "?"};
  static const char* st[8] = {"sb", "sh", "sw", "?", "?", "?", "?", "?"};
  static const char* oi[8] = {"addi", "slli", "slti", "sltiu", "xori", "srli", "ori", "andi"};
  static const char* oo[8] = {"add", "sll", "slt", "sltu", "xor", "srl", "or", "and"};
  if (insn == 0) return "illegal(0x00000000)";
  if (insn == 0xFFFFFFFFu) return "illegal(0xffffffff)";
  switch (op) {
    case 0x37: snprintf(b, sizeof b, "lui %s, 0x%x", rn[rd], insn >> 12); break;
    case 0x17: snprintf(b, sizeof b, "auipc %s, 0x%x", rn[rd], insn >> 12); break;
    case 0x6F: snprintf(b, sizeof b, "jal %s, pc%+d", rn[rd], ij); break;
    case 0x67: snprintf(b, sizeof b, "jalr %s, %d(%s)", rn[rd], ii, rn[rs1]); break;
    case 0x63: snprintf(b, sizeof b, "%s %s, %s, pc%+d", br[f3], rn[rs1], rn[rs2], ib); break;
    case 0x03: snprintf(b, sizeof b, "%s %s, %d(%s)", ld[f3], rn[rd], ii, rn[rs1]); break;
    case 0x23: snprintf(b, sizeof b, "%s %s, %d(%s)", st[f3], rn[rs2], is, rn[rs1]); break;
    case 0x13:
      if (f3 == 1 || f3 == 5)
        snprintf(b, sizeof b, "%s %s, %s, %u", f3 == 5 && f7 ? "srai" : oi[f3], rn[rd], rn[rs1], rs2);
      else
        snprintf(b, sizeof b, "%s %s, %s, %d", oi[f3], rn[rd], rn[rs1], ii);
      break;
    case 0x33:
      snprintf(b, sizeof b, "%s %s, %s, %s",
               f7 == 0x20 ? (f3 == 0 ? "sub" : f3 == 5 ? "sra" : "?") : oo[f3], rn[rd], rn[rs1],
               rn[rs2]);
      break;
    case 0x0F: snprintf(b, sizeof b, "fence(0x%08x)", insn); break;
    case 0x73:
      snprintf(b, sizeof b, "%s", insn == 0x73 ? "ecall" : insn == 0x00100073 ? "ebreak" : "system?");
      break;
    default: snprintf(b, sizeof b, "unknown(0x%08x)", insn); break;
  }
  return b;
}

// Load a $readmemh image: one hex byte per line, 65,536 lines. Returns false on a format error.
static inline bool load_hex_image(const char* path, uint8_t* out, std::string* err) {
  FILE* f = std::fopen(path, "r");
  if (!f) {
    if (err) *err = std::string("cannot open ") + path;
    return false;
  }
  std::memset(out, 0, Iss::RAM_SIZE);
  char line[64];
  uint32_t n = 0;
  while (std::fgets(line, sizeof line, f)) {
    char* p = line;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '\n' || *p == '\r' || *p == 0 || (p[0] == '/' && p[1] == '/')) continue;
    if (*p == '@') {
      n = (uint32_t)std::strtoul(p + 1, nullptr, 16);
      continue;
    }
    char* end = nullptr;
    unsigned long v = std::strtoul(p, &end, 16);
    if (end == p || v > 0xFF || n >= Iss::RAM_SIZE) {
      if (err) *err = std::string("bad hex image line in ") + path;
      std::fclose(f);
      return false;
    }
    out[n++] = (uint8_t)v;
  }
  std::fclose(f);
  if (n != Iss::RAM_SIZE) {
    if (err) *err = std::string("hex image is not 65536 bytes: ") + path;
    return false;
  }
  return true;
}
