// Seed unit-test ISS for contract v3 (docs/contract-v3.md sections 3-5). Not a contract gate:
// the gates' own Iss3 (verif/cosim/iss3.h, Task 2) replaces it. Header-only.
//
// - RV32E + Zicsr subset + MRET + WFI (no-op), machine mode only.
// - Memory map: addr[31] = 1 IO (slot addr[6:2]); else addr[24] = 0 flash (16 MB, read-only,
//   stores ignored), addr[24] = 1 PSRAM (8 MB, A[22:0]).
// - Traps (contract 4): ECALL 11, EBREAK 3, misaligned JAL/JALR/branch target 0, the words
//   0x00000000 and 0xFFFFFFFF 2. take_interrupt() enters the external interrupt (0x8000000B).
// - IO: a register-level model. Bits a test can't predict (counters, flags, inputs) are taken
//   from the DUT through the volatile hook; every other bit is checked by the harness.
#pragma once

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

namespace v3 {

constexpr uint32_t FLASH_SIZE = 1u << 24;
constexpr uint32_t PSRAM_SIZE = 1u << 23;
constexpr uint32_t REGION_LO = 0x7FFF80;  // PSRAM bytes 0x7F_FF80..0x7F_FFFF

struct Retire {
  uint32_t pc, npc, insn;
  bool trap, intr;
  uint8_t rs1, rs2, rd;
  uint32_t rs1v, rs2v, rdv;
  uint32_t maddr;
  uint8_t rmask, wmask;
  uint32_t mrdata, mwdata;
  bool io;            // the data access went to IO
  uint32_t io_vmask;  // volatile bits of mrdata (byte lanes as mrdata)
  bool region;        // touched the core region (unspecified: stop comparing)
  bool reserved;      // reserved encoding (unspecified)
};

// Volatile bits per IO slot (plan Task 2 IoRegs): their read value comes from the DUT.
static inline uint32_t io_volatile_mask(unsigned slot) {
  switch (slot) {
    case 1: case 3: case 5: case 11: case 13: case 16: case 18: return 0xFFFFFFFFu;
    case 12: return 0x0000E000u;
    case 14: return 0x00000010u;
    default: return 0;
  }
}

class Iss3 {
 public:
  // io_read(slot, iss_value) returns the DUT's value for the slot (only volatile bits are used)
  std::function<uint32_t(unsigned, uint32_t)> io_read;
  // mip_read() returns the DUT's mip value (MEIP is the live interrupt line)
  std::function<uint32_t()> mip_read;

  Iss3() : flash_(FLASH_SIZE, 0), psram_(PSRAM_SIZE, 0) { reset(); }

  void reset() {
    std::memset(x_, 0, sizeof(x_));
    pc_ = 0;
    mie_ = mpie_ = meie_ = false;
    mtvec_ = mscratch_ = mepc_ = mcause_ = 0;
    std::memset(io_, 0, sizeof(io_));
    io_[14] = 1;  // SPI_CTRL CS_n
    retired_ = 0;
  }
  void load_flash(const uint8_t* img, size_t n) {
    std::memcpy(flash_.data(), img, n < FLASH_SIZE ? n : FLASH_SIZE);
  }
  void set_psram(const uint8_t* img) { std::memcpy(psram_.data(), img, PSRAM_SIZE); }

  bool irq_allowed() const { return mie_ && meie_; }
  uint32_t pc() const { return pc_; }
  uint32_t reg(int i) const { return x_[i & 15]; }
  uint8_t psram(uint32_t a) const { return psram_[a & (PSRAM_SIZE - 1)]; }
  uint8_t gpio_out() const { return (uint8_t)io_[0]; }
  uint8_t altfn() const { return (uint8_t)io_[2]; }
  uint64_t retired() const { return retired_; }

  Retire take_interrupt() {
    Retire r;
    std::memset(&r, 0, sizeof(r));
    r.intr = true;
    mepc_ = pc_;
    mcause_ = 0x8000000Bu;
    mpie_ = mie_;
    mie_ = false;
    pc_ = mtvec_ & ~3u;
    return r;
  }

  Retire step();

 private:
  uint32_t x_[16];
  uint32_t pc_;
  bool mie_, mpie_, meie_;
  uint32_t mtvec_, mscratch_, mepc_, mcause_;
  uint32_t io_[32];
  uint64_t retired_;
  std::vector<uint8_t> flash_, psram_;

  static bool region_byte(uint32_t a) {
    return !(a >> 31) && ((a >> 24) & 1) && (a & (PSRAM_SIZE - 1)) >= REGION_LO;
  }
  uint8_t mem_byte(uint32_t a) const {
    if ((a >> 24) & 1) return psram_[a & (PSRAM_SIZE - 1)];
    return flash_[a & (FLASH_SIZE - 1)];
  }
  void mem_write(uint32_t a, uint8_t v) {
    if ((a >> 24) & 1) psram_[a & (PSRAM_SIZE - 1)] = v;  // stores to flash: no effect
  }

  // Register-level IO model: the value a load of slot s returns (non-volatile bits).
  uint32_t io_value(unsigned s) const {
    switch (s) {
      case 0: case 2: return io_[s] & 0xFF;
      case 4: return io_[4] & 0xFFFF;
      case 6: return io_[6] & 0x3FF;
      case 7: return io_[7] & 0x7FFF;
      case 8: case 9: case 10: return io_[s] & 0x3FF;
      case 12: return io_[12] & 0x1FFF;
      case 14: return io_[14] & 0xF;
      case 15: return io_[15] & 0x1F;
      case 17: return io_[17] & 0xFFFF;
      default: return 0;  // volatile slots come from the DUT; unimplemented slots read 0
    }
  }
  void io_write_byte(unsigned s, unsigned b, uint8_t v) {
    uint32_t m = 0xFFu << (8 * b);
    uint32_t nv = (io_[s] & ~m) | ((uint32_t)v << (8 * b));
    switch (s) {
      case 0: case 2: case 4: case 6: case 7: case 8: case 9: case 10: case 12: case 14:
      case 15: case 17:
        io_[s] = nv;
        break;
      default: break;  // read-only, W1C or side-effect-only slots: volatile on read
    }
  }

  uint32_t csr_read(uint32_t c) const {
    switch (c) {
      case 0x300: return (mie_ ? 8u : 0u) | (mpie_ ? 0x80u : 0u) | 0x1800u;
      case 0x304: return meie_ ? 0x800u : 0u;
      case 0x305: return mtvec_ & ~3u;
      case 0x340: return mscratch_;
      case 0x341: return mepc_ & ~3u;
      case 0x342: return mcause_ & 0x8000000Fu;
      case 0x344: return mip_read ? (mip_read() & 0x800u) : 0u;
      default: return 0;
    }
  }
  void csr_write(uint32_t c, uint32_t v) {
    switch (c) {
      case 0x300: mie_ = (v >> 3) & 1; mpie_ = (v >> 7) & 1; break;
      case 0x304: meie_ = (v >> 11) & 1; break;
      case 0x305: mtvec_ = v & ~3u; break;
      case 0x340: mscratch_ = v; break;
      case 0x341: mepc_ = v & ~3u; break;
      case 0x342: mcause_ = v & 0x8000000Fu; break;
      default: break;
    }
  }
};

inline Retire Iss3::step() {
  Retire r;
  std::memset(&r, 0, sizeof(r));
  const uint32_t pc = pc_;
  uint32_t insn = 0;
  for (int k = 0; k < 4; k++) {
    if (region_byte(pc + k)) r.region = true;
    insn |= (uint32_t)mem_byte(pc + k) << (8 * k);
  }
  r.pc = pc;
  r.insn = insn;
  r.npc = pc + 4;
  if (r.region) return r;

  const uint32_t opcode = insn & 0x7F, rd = (insn >> 7) & 0x1F, f3 = (insn >> 12) & 7,
                 rs1 = (insn >> 15) & 0x1F, rs2 = (insn >> 20) & 0x1F, f7 = insn >> 25;
  const uint32_t imm_i = (uint32_t)((int32_t)insn >> 20);
  const uint32_t imm_s = (uint32_t)(((int32_t)insn >> 25) << 5) | ((insn >> 7) & 0x1F);
  const uint32_t imm_b = (uint32_t)(((int32_t)insn >> 31) << 12) | ((insn & 0x80) << 4) |
                         ((insn >> 20) & 0x7E0) | ((insn >> 7) & 0x1E);
  const uint32_t imm_u = insn & 0xFFFFF000u;
  const uint32_t imm_j = (uint32_t)(((int32_t)insn >> 31) << 20) | (insn & 0xFF000) |
                         ((insn >> 9) & 0x800) | ((insn >> 20) & 0x7FE);

  auto trap = [&](uint32_t cause) {
    r.trap = true;
    r.rd = 0;
    r.rdv = 0;
    mepc_ = pc;
    mcause_ = cause;
    mpie_ = mie_;
    mie_ = false;
    r.npc = pc_ = mtvec_ & ~3u;
    retired_++;
    return r;
  };
  auto reserved = [&]() {
    r.reserved = true;
    return r;
  };
  auto use_rs1 = [&]() {
    r.rs1 = (uint8_t)rs1;
    r.rs1v = rs1 ? x_[rs1 & 15] : 0;
  };
  auto use_rs2 = [&]() {
    r.rs2 = (uint8_t)rs2;
    r.rs2v = rs2 ? x_[rs2 & 15] : 0;
  };
  auto finish = [&](bool wr, uint32_t v) {
    if (wr && rd) {
      r.rd = (uint8_t)rd;
      r.rdv = v;
      x_[rd & 15] = v;
    }
    pc_ = r.npc;
    retired_++;
    return r;
  };

  if (insn == 0 || insn == 0xFFFFFFFFu) return trap(2);
  if ((insn & 3) != 3) return reserved();
  switch (opcode) {
    case 0x37: if (rd > 15) return reserved(); return finish(true, imm_u);
    case 0x17: if (rd > 15) return reserved(); return finish(true, pc + imm_u);
    case 0x6F: {
      if (rd > 15) return reserved();
      uint32_t t = pc + imm_j;
      if (t & 2) return trap(0);
      r.npc = t;
      return finish(true, pc + 4);
    }
    case 0x67: {
      if (f3 || rd > 15 || rs1 > 15) return reserved();
      use_rs1();
      uint32_t t = (r.rs1v + imm_i) & ~1u;
      if (t & 2) return trap(0);
      r.npc = t;
      return finish(true, pc + 4);
    }
    case 0x63: {
      if (f3 == 2 || f3 == 3 || rs1 > 15 || rs2 > 15) return reserved();
      use_rs1();
      use_rs2();
      uint32_t a = r.rs1v, b = r.rs2v;
      bool t = false;
      switch (f3) {
        case 0: t = a == b; break;
        case 1: t = a != b; break;
        case 4: t = (int32_t)a < (int32_t)b; break;
        case 5: t = (int32_t)a >= (int32_t)b; break;
        case 6: t = a < b; break;
        case 7: t = a >= b; break;
      }
      if (t) {
        uint32_t tg = pc + imm_b;
        if (tg & 2) return trap(0);
        r.npc = tg;
      }
      return finish(false, 0);
    }
    case 0x03: {
      if (f3 == 3 || f3 > 5 || rd > 15 || rs1 > 15) return reserved();
      use_rs1();
      uint32_t ea = r.rs1v + imm_i;
      unsigned n = (f3 & 3) == 0 ? 1 : (f3 & 3) == 1 ? 2 : 4;
      uint32_t v = 0;
      r.maddr = ea;
      r.rmask = (uint8_t)((1u << n) - 1);
      if (ea >> 31) {
        if (n > 1 && (ea & 3)) return reserved();
        unsigned s = (ea >> 2) & 31, b = ea & 3;
        uint32_t vm = io_volatile_mask(s);
        uint32_t w = io_value(s);
        if (vm && io_read) w = (w & ~vm) | (io_read(s, w) & vm);
        r.io = true;
        uint32_t lane = n == 4 ? 0xFFFFFFFFu : ((1u << (8 * n)) - 1);
        v = (w >> (8 * b)) & lane;
        r.io_vmask = (vm >> (8 * b)) & lane;
      } else {
        for (unsigned k = 0; k < n; k++) {
          if (region_byte(ea + k)) r.region = true;
          v |= (uint32_t)mem_byte(ea + k) << (8 * k);
        }
        if (r.region) return r;
      }
      r.mrdata = v;
      uint32_t res = f3 == 0 ? (uint32_t)(int32_t)(int8_t)v
                   : f3 == 1 ? (uint32_t)(int32_t)(int16_t)v : v;
      return finish(true, res);
    }
    case 0x23: {
      if (f3 > 2 || rs1 > 15 || rs2 > 15) return reserved();
      use_rs1();
      use_rs2();
      uint32_t ea = r.rs1v + imm_s;
      unsigned n = 1u << f3;
      uint32_t v = n == 4 ? r.rs2v : r.rs2v & ((1u << (8 * n)) - 1);
      r.maddr = ea;
      r.wmask = (uint8_t)((1u << n) - 1);
      r.mwdata = v;
      if (ea >> 31) {
        if (n > 1 && (ea & 3)) return reserved();
        r.io = true;
        unsigned s = (ea >> 2) & 31, b = ea & 3;
        for (unsigned k = 0; k < n && b + k < 4; k++) io_write_byte(s, b + k, (uint8_t)(v >> (8 * k)));
      } else {
        for (unsigned k = 0; k < n; k++)
          if (region_byte(ea + k)) r.region = true;
        if (r.region) return r;
        for (unsigned k = 0; k < n; k++) mem_write(ea + k, (uint8_t)(v >> (8 * k)));
      }
      return finish(false, 0);
    }
    case 0x13: {
      if (rd > 15 || rs1 > 15) return reserved();
      if (f3 == 1 && f7 != 0) return reserved();
      if (f3 == 5 && f7 != 0 && f7 != 0x20) return reserved();
      use_rs1();
      uint32_t a = r.rs1v, res = 0;
      switch (f3) {
        case 0: res = a + imm_i; break;
        case 2: res = (int32_t)a < (int32_t)imm_i; break;
        case 3: res = a < imm_i; break;
        case 4: res = a ^ imm_i; break;
        case 6: res = a | imm_i; break;
        case 7: res = a & imm_i; break;
        case 1: res = a << rs2; break;
        case 5: res = f7 ? (uint32_t)((int32_t)a >> rs2) : a >> rs2; break;
      }
      return finish(true, res);
    }
    case 0x33: {
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
    case 0x0F:
      if (f3) return reserved();
      return finish(false, 0);
    case 0x73: {
      if (f3 == 0) {
        if (insn == 0x00000073u) return trap(11);
        if (insn == 0x00100073u) return trap(3);
        if (insn == 0x30200073u) {  // MRET
          r.npc = mepc_ & ~3u;
          mie_ = mpie_;
          mpie_ = true;
          return finish(false, 0);
        }
        if (insn == 0x10500073u) return finish(false, 0);  // WFI: no-op
        return reserved();
      }
      if (f3 == 4 || rd > 15) return reserved();
      uint32_t c = insn >> 20;
      bool imm = f3 & 4;
      if (!imm && rs1 > 15) return reserved();
      uint32_t src = imm ? rs1 : (rs1 ? x_[rs1 & 15] : 0);
      if (!imm) {
        r.rs1 = (uint8_t)rs1;
        r.rs1v = src;
      }
      uint32_t old = csr_read(c), nv = old;
      switch (f3 & 3) {
        case 1: nv = src; break;
        case 2: nv = old | src; break;
        case 3: nv = old & ~src; break;
      }
      bool wr = (f3 & 3) == 1 || rs1 != 0;
      if (wr) csr_write(c, nv);
      return finish(true, old);
    }
    default:
      return reserved();
  }
}

}  // namespace v3
