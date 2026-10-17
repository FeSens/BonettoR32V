// BonettoR32V reference ISS for contract v3 (docs/contract-v3.md sections 3, 4, 5 and 8): RV32E
// plus the Zicsr subset, traps to mtvec, the external interrupt, the flash / PSRAM / IO memory
// map and a register-level model of the peripherals. Header-only: the v3 cosim harness and
// iss_main share one model. The RV32E decode and execute are copied from v2's iss.h, with traps
// to mtvec instead of halts.
//
// Memory map (contract 3), for an address a:
//   - a[31] = 0, a[24] = 0: flash byte a[23:0] (16 MiB, read-only: stores change nothing);
//   - a[31] = 0, a[24] = 1: PSRAM byte a[22:0] (8 MiB; bits 30:25 and 23 ignored);
//   - a[31] = 1: IO slot a[6:2] (32-bit slots; byte a[1:0] of the slot).
//   An n-byte access touches a + k for k < n. Misaligned flash and PSRAM accesses are bytewise.
//
// Unspecified behavior (step() sets Retire::reserved, changes no state and retires nothing; the
// checkers treat it as a test-program error and stop comparing):
//   - a reserved encoding (anything not listed in contract 4, x16-x31 fields included);
//   - an unlisted CSR number, or a write to the read-only CSRs 0xF11-0xF14;
//   - a fetch from IO, a misaligned IO access (addr % n != 0), an access spanning two regions;
//   - a fetch or data access touching the core region, PSRAM bytes 0x7F_FF80-0x7F_FFFF (CPU
//     0x017F_FF80 and its aliases): Retire::region is set too, and touched_region sticks.
//
// Traps (contract 4): ECALL (mcause 11), EBREAK (3), JAL / JALR / taken branch with target[1] = 1
// (0), the words 0x00000000 and 0xFFFFFFFF (2). The trapping instruction retires with trap = 1,
// rd = 0, no memory access and npc = mtvec; mepc = pc, MPIE = MIE, MIE = 0. The next step() is
// the handler's first instruction and reports intr = 1 (rvfi_intr). Nothing halts.
//
// Interrupts: the DUT decides when one is taken (cosim), so the ISS only models the entry:
// take_interrupt() sets mepc = the next pc, MPIE = MIE, MIE = 0, mcause = 0x8000_000B,
// pc = mtvec, and the next step() reports intr = 1. irq_allowed() is MIE && MEIE.
//
// Peripherals (contract 5) are modeled at register level only, with no cycle timing. A bit the
// hardware changes on its own (a counter, a received byte, a busy or pending flag) is volatile:
// its value comes from IoHooks::volatile_read, which supplies the DUT's value. The model adopts
// that value, then applies the read's side effects. Without a hook the model's own value is used.
#pragma once

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

namespace v3 {

// ---- memory map (contract 3) -------------------------------------------------------------------
constexpr uint32_t FLASH_SIZE = 1u << 24;
constexpr uint32_t PSRAM_SIZE = 1u << 23;
constexpr uint32_t REGION_LO = 0x7FFF80;    // core region: PSRAM bytes 0x7F_FF80-0x7F_FFFF
constexpr uint32_t STACK_TOP = 0x017FFF80;  // programs place the stack top here

enum class Area { Flash, Psram, Io };
inline Area area_of(uint32_t a) {
  return (a >> 31) ? Area::Io : ((a >> 24) & 1) ? Area::Psram : Area::Flash;
}
inline bool in_core_region(uint32_t a) {
  return area_of(a) == Area::Psram && (a & (PSRAM_SIZE - 1)) >= REGION_LO;
}

// The initial PSRAM content: arbitrary per contract 3 (crt0 copies .data and clears .bss), so a
// fixed hash of the address that is neither 0 nor 0xFF everywhere.
inline uint8_t psram_seed_byte(uint32_t a) {
  uint32_t x = (a & (PSRAM_SIZE - 1)) * 0x9E3779B1u + 0x7F4A7C15u;
  x ^= x >> 16;
  x *= 0x7FEB352Du;
  x ^= x >> 15;
  x *= 0x846CA68Bu;
  x ^= x >> 16;
  return (uint8_t)x;
}

// ---- IO (contract 5.1) -------------------------------------------------------------------------
enum IoSlot : uint32_t {
  SLOT_GPIO_OUT = 0, SLOT_GPIO_IN = 1, SLOT_ALTFN = 2, SLOT_TIMER_CNT = 3, SLOT_TIMER_CMP = 4,
  SLOT_TIMER_CAP = 5, SLOT_TIMER_CTRL = 6, SLOT_PWM_CTRL = 7, SLOT_PWM_DUTY0 = 8,
  SLOT_PWM_DUTY1 = 9, SLOT_PWM_DUTY2 = 10, SLOT_UART_DATA = 11, SLOT_UART_CTRL = 12,
  SLOT_SPI_DATA = 13, SLOT_SPI_CTRL = 14, SLOT_IRQ_EN = 15, SLOT_IRQ_PEND = 16,
  SLOT_GPIO_EDGE = 17, SLOT_GPIO_EPEND = 18,
};
constexpr uint32_t IO_SLOTS = 32;
// Pseudo-slot for the one volatile CSR bit, mip.MEIP: a CSR read of mip with rd != x0 calls
// volatile_read(VSLOT_MIP, mip value) and keeps bit 11 of the result. In rvfi mode the harness
// answers with rvfi_rd_wdata.
constexpr uint32_t VSLOT_MIP = IO_SLOTS;

constexpr uint32_t UART_TX_BUSY = 1u << 13, UART_RX_FULL = 1u << 14, UART_RX_OVR = 1u << 15;
constexpr uint32_t SPI_BUSY = 1u << 4;
constexpr uint32_t MIP_MEIP = 1u << 11;

// Volatile bits per slot, in slot bit positions (bit i = register bit i), for loads. Index
// VSLOT_MIP is mip. Unimplemented bits read 0 and are never volatile.
inline constexpr uint32_t IO_VOLATILE_MASK[IO_SLOTS + 1] = {
    0,                // 0  GPIO_OUT
    0xFF,             // 1  GPIO_IN: all
    0,                // 2  ALTFN
    0xFFFF,           // 3  TIMER_CNT: all
    0,                // 4  TIMER_CMP
    0xFFFF,           // 5  TIMER_CAP: all
    0, 0, 0, 0, 0,    // 6-10 TIMER_CTRL, PWM_CTRL, PWM_DUTY0-2
    0xFF,             // 11 UART_DATA: the received byte
    0xE000,           // 12 UART_CTRL[15:13]: TX_BUSY, RX_FULL, RX_OVR
    0xFF,             // 13 SPI_DATA: the received byte
    0x10,             // 14 SPI_CTRL[4]: BUSY
    0,                // 15 IRQ_EN
    0x1F,             // 16 IRQ_PEND: all
    0,                // 17 GPIO_EDGE
    0xFF,             // 18 GPIO_EPEND: all
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,  // 19-31: not implemented
    MIP_MEIP,         // VSLOT_MIP
};

// Plain read/write bits per slot. UART_DATA, SPI_DATA, IRQ_PEND and GPIO_EPEND writes have their
// own rules (IoRegs::write); read-only and unimplemented bits ignore writes.
inline constexpr uint32_t IO_WRITE_MASK[IO_SLOTS] = {
    0xFF, 0, 0xFF, 0xFFFF, 0xFFFF, 0, 0x3FF, 0x7FFF, 0x3FF, 0x3FF, 0x3FF,
    0, 0x1FFF, 0, 0xF, 0x1F, 0, 0xFFFF, 0,
};

// Byte-lane set (bit k = byte k of the slot) to a bit mask.
inline uint32_t lane_bits(uint32_t lanes) {
  uint32_t m = 0;
  for (int k = 0; k < 4; k++)
    if ((lanes >> k) & 1) m |= 0xFFu << (8 * k);
  return m;
}

// The ISS model of the slot values (contract 5.1). Values are in slot bit positions.
struct IoRegs {
  // Stored bits. IRQ_PEND stores CMP and CAP only; its RX, TXIDLE and GPIO bits are derived
  // (RX = RX_FULL, TXIDLE = !TX_BUSY, GPIO = OR of GPIO_EPEND).
  uint32_t r[IO_SLOTS];

  void reset() {
    std::memset(r, 0, sizeof r);
    r[SLOT_SPI_CTRL] = 1;  // CS_n level resets to 1
  }
  // The value a load of the whole slot returns, as the model sees it (no side effects).
  uint32_t read(uint32_t slot) const {
    slot &= IO_SLOTS - 1;
    if (slot != SLOT_IRQ_PEND) return r[slot];
    const uint32_t uart = r[SLOT_UART_CTRL];
    return (r[SLOT_IRQ_PEND] & 3) | ((uart & UART_RX_FULL) ? 4u : 0u) |
           ((uart & UART_TX_BUSY) ? 0u : 8u) | ((r[SLOT_GPIO_EPEND] & 0xFF) ? 16u : 0u);
  }
  // mip.MEIP as the model sees it: OR(IRQ_PEND[4:0] & IRQ_EN[4:0]).
  bool irq_line() const { return (read(SLOT_IRQ_PEND) & r[SLOT_IRQ_EN] & 0x1F) != 0; }
  // Adopt the DUT's value v for the volatile bits in mask (the bits a load observed).
  void sync(uint32_t slot, uint32_t v, uint32_t mask) {
    slot &= IO_SLOTS - 1;
    if (slot == SLOT_IRQ_PEND) mask &= 3;  // the derived bits follow their own slots
    r[slot] = (r[slot] & ~mask) | (v & mask);
  }
  // Side effects of a load covering the byte lanes `lanes`: reading byte 0 of UART_DATA clears
  // RX_FULL and RX_OVR.
  void after_read(uint32_t slot, uint32_t lanes) {
    if ((slot & (IO_SLOTS - 1)) == SLOT_UART_DATA && (lanes & 1))
      r[SLOT_UART_CTRL] &= ~(UART_RX_FULL | UART_RX_OVR);
  }
  // A store of v (slot bit positions) covering the byte lanes `lanes`.
  void write(uint32_t slot, uint32_t v, uint32_t lanes) {
    slot &= IO_SLOTS - 1;
    const uint32_t bm = lane_bits(lanes);
    switch (slot) {
      case SLOT_UART_DATA:  // writing byte 0 sends; ignored while TX_BUSY = 1
        if (lanes & 1) r[SLOT_UART_CTRL] |= UART_TX_BUSY;
        break;
      case SLOT_SPI_DATA:  // writing byte 0 transfers; ignored while BUSY = 1
        if (lanes & 1) r[SLOT_SPI_CTRL] |= SPI_BUSY;
        break;
      case SLOT_IRQ_PEND:  // CMP, CAP: W1C; RX, TXIDLE, GPIO: read-only
        r[slot] &= ~(v & bm & 3);
        break;
      case SLOT_GPIO_EPEND:  // W1C
        r[slot] &= ~(v & bm & 0xFF);
        break;
      default: {
        const uint32_t m = IO_WRITE_MASK[slot] & bm;
        r[slot] = (r[slot] & ~m) | (v & m);
      }
    }
  }
  uint8_t gpio_out() const { return (uint8_t)r[SLOT_GPIO_OUT]; }
  uint8_t altfn() const { return (uint8_t)r[SLOT_ALTFN]; }
};

// The DUT's value for volatile bits. slot is the IO slot (0-31) or VSLOT_MIP; iss_value is the
// model's value of the whole slot and the result is the DUT's, both in slot bit positions (bit i
// = register bit i). Only the volatile bits in the bytes the load covers are used. In rvfi mode
// the harness returns rvfi_mem_rdata << 8 * (rvfi_mem_addr & 3) (rvfi_rd_wdata for VSLOT_MIP).
// It may be null (then the model's value is used), e.g. in pins mode for programs without
// volatile reads.
struct IoHooks {
  std::function<uint32_t(uint32_t slot, uint32_t iss_value)> volatile_read;
};

// One step: a retired instruction (step), or an interrupt entry record (take_interrupt).
struct Retire {
  uint32_t pc, npc, insn;
  bool trap;                  // a synchronous trap: rd = 0, no memory access, npc = mtvec
  bool halt;                  // always false in v3 (rvfi_halt = 0)
  uint8_t rs1, rs2, rd;       // register indices actually read / written (0 when unused)
  uint32_t rs1v, rs2v, rdv;   // 0 when the matching index is 0
  uint32_t maddr;             // exact address of the first byte (valid when a mask is set)
  uint8_t rmask, wmask;       // LSB-aligned: 0x1, 0x3 or 0xF; 0 for CSR ops, MRET and WFI
  uint32_t mrdata, mwdata;    // byte lane k is the byte at maddr + k; lanes outside the mask 0
  bool reserved;              // unspecified behavior (see the header): nothing retired
  bool region;                // the unspecified step touched the core region
  bool intr;                  // step(): the first instruction of a trap handler (rvfi_intr);
                              // take_interrupt(): always 1 (pc = interrupted pc, npc = mtvec)
  uint32_t mcause_after;      // mcause after this step
};

class Iss3 {
 public:
  static constexpr uint32_t PAGE = 4096;

  explicit Iss3(const uint8_t* flash_image)
      : flash_(flash_image, flash_image + FLASH_SIZE), pages_(PSRAM_SIZE / PAGE) {
    reset();
  }

  // Back to the post-reset state: pc = 0, MIE = MEIE = 0, IO registers at their reset values.
  // x1..x15 = 0 by convention (the RTL's are arbitrary; programs clear them). The other CSRs
  // reset to arbitrary values in the RTL; the ISS uses 0 and flags a read before any write
  // (uninit_read). Flash and PSRAM keep their contents.
  void reset() {
    std::memset(x_, 0, sizeof(x_));
    pc_ = 0;
    mie_ = mpie_ = meie_ = 0;
    mtvec_ = mepc_ = mscratch_ = mcause_ = 0;
    io_.reset();
    touched_region = false;
    pending_intr_ = false;
    retired_ = traps_ = interrupts_ = 0;
    written_ = 1;  // x0 counts as written
    known_ = 0;
    uninit_read_ = false;
    uninit_pc_ = 0;
    selfloop_ = false;
    prev_pc_ = prev_insn_ = 0;  // no retirement yet (0 is not jal x0, 0)
  }

  // Execute one instruction.
  Retire step(const IoHooks& io);
  // Interrupt entry with no instruction (contract 4). The caller decides that the interrupt is
  // taken (it checks irq_allowed() first).
  Retire take_interrupt();
  bool irq_allowed() const { return mie_ && meie_; }

  uint8_t psram(uint32_t a) const {
    a &= PSRAM_SIZE - 1;
    const std::vector<uint8_t>& p = pages_[a / PAGE];
    return p.empty() ? psram_seed_byte(a) : p[a % PAGE];
  }
  uint8_t flash(uint32_t a) const { return flash_[a & (FLASH_SIZE - 1)]; }

  // Sticky: some fetch or data access touched the core region (contract 3). The checkers stop
  // comparing at the first one.
  bool touched_region = false;

  // Extras for the harness, the runner and tests.
  uint32_t pc() const { return pc_; }
  uint32_t reg(int i) const { return x_[i & 15]; }
  uint64_t retired() const { return retired_; }
  uint64_t traps() const { return traps_; }
  uint64_t interrupts() const { return interrupts_; }
  const IoRegs& io() const { return io_; }
  // uo pins in GPIO mode: GPIO_OUT where ALTFN = 0, 0 where ALTFN = 1.
  uint8_t uo_gpio() const { return io_.gpio_out() & (uint8_t)~io_.altfn(); }
  // The program end (contract 4): the last two retirements were jal x0, 0 at the same pc (an
  // interrupt entry between them retires nothing, so it doesn't separate them).
  bool selfloop() const { return selfloop_; }
  // The value a CSR read would return, with no side effects (mip.MEIP from the model).
  uint32_t csr_peek(uint32_t csr) const { return csr_value(csr, io_.irq_line()); }
  // Program check: a register or an arbitrary-reset CSR (mtvec, mepc, mscratch, mcause,
  // mstatus.MPIE) was read before any write. The core's values are unknown there, so such a
  // program can't be checked.
  bool uninit_read() const { return uninit_read_; }
  uint32_t uninit_pc() const { return uninit_pc_; }

 private:
  enum : uint32_t { K_MTVEC = 1, K_MEPC = 2, K_MSCRATCH = 4, K_MCAUSE = 8, K_MPIE = 16 };

  std::vector<uint8_t> flash_;
  std::vector<std::vector<uint8_t>> pages_;  // PSRAM: an empty page still holds the seed pattern
  uint32_t x_[16];
  uint32_t pc_;
  uint32_t mie_, mpie_, meie_;
  uint32_t mtvec_, mepc_, mscratch_, mcause_;
  IoRegs io_;
  bool pending_intr_;  // the next retirement is the first instruction of a handler
  uint64_t retired_, traps_, interrupts_;
  uint32_t written_;
  uint32_t known_;  // K_* : CSR fields written since reset
  bool uninit_read_;
  uint32_t uninit_pc_;
  bool selfloop_;
  uint32_t prev_pc_, prev_insn_;  // the previous retirement

  void flag_uninit(uint32_t pc) {
    if (!uninit_read_) {
      uninit_read_ = true;
      uninit_pc_ = pc;
    }
  }
  uint32_t rd_reg(uint8_t r) {
    if (!((written_ >> r) & 1)) flag_uninit(pc_);
    return x_[r];
  }
  void psram_write(uint32_t a, uint8_t v) {
    a &= PSRAM_SIZE - 1;
    std::vector<uint8_t>& p = pages_[a / PAGE];
    if (p.empty()) {
      p.resize(PAGE);
      const uint32_t base = a & ~(PAGE - 1);
      for (uint32_t i = 0; i < PAGE; i++) p[i] = psram_seed_byte(base + i);
    }
    p[a % PAGE] = v;
  }
  uint8_t mem_byte(uint32_t a) const {  // flash or PSRAM (a[31] = 0)
    return area_of(a) == Area::Psram ? psram(a) : flash(a);
  }

  static bool csr_exists(uint32_t c) {
    switch (c) {
      case 0x300: case 0x301: case 0x304: case 0x305: case 0x340: case 0x341: case 0x342:
      case 0x343: case 0x344: case 0xF11: case 0xF12: case 0xF13: case 0xF14:
        return true;
    }
    return false;
  }
  uint32_t csr_value(uint32_t c, bool meip) const {
    switch (c) {
      case 0x300: return (mie_ << 3) | (mpie_ << 7) | (3u << 11);
      case 0x304: return meie_ << 11;
      case 0x305: return mtvec_;
      case 0x340: return mscratch_;
      case 0x341: return mepc_;
      case 0x342: return mcause_;
      case 0x344: return meip ? MIP_MEIP : 0;
      default: return 0;  // misa, mtval, mvendorid, marchid, mimpid, mhartid
    }
  }
  // Trap or interrupt entry: mepc = epc, MPIE = MIE, MIE = 0, mcause, pc = mtvec.
  void enter_trap(uint32_t epc, uint32_t cause) {
    if (!(known_ & K_MTVEC)) flag_uninit(epc);
    mepc_ = epc & ~3u;
    mpie_ = mie_;
    mie_ = 0;
    mcause_ = cause & 0x8000000Fu;
    pc_ = mtvec_;
    known_ |= K_MEPC | K_MCAUSE | K_MPIE;
    pending_intr_ = true;
  }
};

inline Retire Iss3::take_interrupt() {
  Retire r;
  std::memset(&r, 0, sizeof(r));
  r.pc = pc_;
  r.intr = true;
  enter_trap(pc_, 0x8000000Bu);
  r.npc = pc_;
  r.mcause_after = mcause_;
  interrupts_++;
  return r;
}

inline Retire Iss3::step(const IoHooks& io) {
  Retire r;
  std::memset(&r, 0, sizeof(r));
  const uint32_t pc = pc_;
  r.pc = pc;
  r.npc = pc + 4;
  r.mcause_after = mcause_;

  auto unspecified = [&]() {  // no state change, nothing retired
    r.reserved = true;
    return r;
  };
  auto region = [&]() {
    touched_region = true;
    r.region = true;
    return unspecified();
  };

  // Fetch (pc is always 4-aligned: targets with bit 1 set trap, mtvec and mepc drop bits 1:0).
  if (area_of(pc) == Area::Io) return unspecified();  // a fetch from IO
  for (uint32_t k = 0; k < 4; k++)
    if (in_core_region(pc + k)) return region();
  uint32_t insn = 0;
  for (uint32_t k = 0; k < 4; k++) insn |= (uint32_t)mem_byte(pc + k) << (8 * k);
  r.insn = insn;

  const uint32_t opcode = insn & 0x7F;
  const uint32_t rd = (insn >> 7) & 0x1F;
  const uint32_t f3 = (insn >> 12) & 7;
  const uint32_t rs1 = (insn >> 15) & 0x1F;
  const uint32_t rs2 = (insn >> 20) & 0x1F;
  const uint32_t f7 = insn >> 25;
  const uint32_t imm_i = (uint32_t)((int32_t)insn >> 20);
  // Sign bits are shifted as uint32_t: a left shift of a negative int32_t is UB before C++20.
  const uint32_t imm_s = ((uint32_t)((int32_t)insn >> 25) << 5) | ((insn >> 7) & 0x1F);
  const uint32_t imm_b = ((uint32_t)((int32_t)insn >> 31) << 12) | ((insn & 0x80) << 4) |
                         ((insn >> 20) & 0x7E0) | ((insn >> 7) & 0x1E);
  const uint32_t imm_u = insn & 0xFFFFF000u;
  const uint32_t imm_j = ((uint32_t)((int32_t)insn >> 31) << 20) | (insn & 0xFF000) |
                         ((insn >> 9) & 0x800) | ((insn >> 20) & 0x7FE);

  const bool first_of_handler = pending_intr_;
  auto retire = [&]() {
    r.intr = first_of_handler;
    pending_intr_ = false;
    retired_++;
    // Two retirements in a row; an interrupt entry between them retires nothing.
    selfloop_ = insn == 0x0000006Fu && prev_insn_ == insn && prev_pc_ == pc;
    prev_pc_ = pc;
    prev_insn_ = insn;
  };
  auto trap = [&](uint32_t cause) {
    r.trap = true;
    r.rd = 0;
    r.rdv = 0;
    retire();
    enter_trap(pc, cause);
    r.npc = pc_;
    r.mcause_after = mcause_;
    traps_++;
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
    retire();
    r.mcause_after = mcause_;
    return r;
  };
  // Classify an n-byte data access: 0 = fine, 1 = unspecified, 2 = touches the core region.
  auto check_data = [&](uint32_t ea, unsigned n) {
    const Area a0 = area_of(ea);
    bool span = false;
    for (unsigned k = 0; k < n; k++) {
      if (in_core_region(ea + k)) return 2;
      if (area_of(ea + k) != a0) span = true;
    }
    if (span) return 1;
    if (a0 == Area::Io && (ea % n) != 0) return 1;  // misaligned IO
    return 0;
  };

  if (insn == 0x00000000u || insn == 0xFFFFFFFFu) return trap(2);
  if ((insn & 3) != 3) return unspecified();

  switch (opcode) {
    case 0x37:  // LUI
      if (rd > 15) return unspecified();
      return finish(true, imm_u);
    case 0x17:  // AUIPC
      if (rd > 15) return unspecified();
      return finish(true, pc + imm_u);
    case 0x6F: {  // JAL
      if (rd > 15) return unspecified();
      uint32_t t = pc + imm_j;
      if (t & 2) return trap(0);
      r.npc = t;
      return finish(true, pc + 4);
    }
    case 0x67: {  // JALR
      if (f3 != 0 || rd > 15 || rs1 > 15) return unspecified();
      use_rs1();
      uint32_t t = (r.rs1v + imm_i) & ~1u;
      if (t & 2) return trap(0);
      r.npc = t;
      return finish(true, pc + 4);
    }
    case 0x63: {  // BRANCH
      if (f3 == 2 || f3 == 3 || rs1 > 15 || rs2 > 15) return unspecified();
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
        if (t & 2) return trap(0);
        r.npc = t;
      }
      return finish(false, 0);
    }
    case 0x03: {  // LOAD
      if (f3 == 3 || f3 > 5 || rd > 15 || rs1 > 15) return unspecified();
      use_rs1();
      const uint32_t ea = r.rs1v + imm_i;
      const unsigned n = (f3 & 3) == 0 ? 1 : (f3 & 3) == 1 ? 2 : 4;
      const int c = check_data(ea, n);
      if (c == 2) return region();
      if (c == 1) return unspecified();
      const uint32_t nmask = n == 4 ? 0xFFFFFFFFu : (1u << (8 * n)) - 1;
      uint32_t v = 0;
      if (area_of(ea) == Area::Io) {
        const uint32_t slot = (ea >> 2) & (IO_SLOTS - 1), off = ea & 3;
        const uint32_t lanes = ((1u << n) - 1) << off;
        const uint32_t model = io_.read(slot);
        const uint32_t vm = IO_VOLATILE_MASK[slot] & lane_bits(lanes);
        uint32_t val = model;
        if (vm) {
          const uint32_t dut = io.volatile_read ? io.volatile_read(slot, model) : model;
          val = (model & ~vm) | (dut & vm);
          io_.sync(slot, val, vm);
        }
        io_.after_read(slot, lanes);
        v = (val >> (8 * off)) & nmask;
      } else {
        for (unsigned k = 0; k < n; k++) v |= (uint32_t)mem_byte(ea + k) << (8 * k);
      }
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
      if (f3 > 2 || rs1 > 15 || rs2 > 15) return unspecified();
      use_rs1();
      use_rs2();
      const uint32_t ea = r.rs1v + imm_s;
      const unsigned n = 1u << f3;
      const int c = check_data(ea, n);
      if (c == 2) return region();
      if (c == 1) return unspecified();
      const uint32_t v = n == 4 ? r.rs2v : r.rs2v & ((1u << (8 * n)) - 1);
      switch (area_of(ea)) {
        case Area::Io: {
          const uint32_t off = ea & 3;
          io_.write((ea >> 2) & (IO_SLOTS - 1), v << (8 * off), ((1u << n) - 1) << off);
          break;
        }
        case Area::Psram:
          for (unsigned k = 0; k < n; k++) psram_write(ea + k, (uint8_t)(v >> (8 * k)));
          break;
        case Area::Flash:  // no effect on memory (contract 3)
          break;
      }
      r.maddr = ea;
      r.wmask = (uint8_t)((1u << n) - 1);
      r.mwdata = v;
      return finish(false, 0);
    }
    case 0x13: {  // OP-IMM
      if (rd > 15 || rs1 > 15) return unspecified();
      if (f3 == 1 && f7 != 0) return unspecified();
      if (f3 == 5 && f7 != 0 && f7 != 0x20) return unspecified();
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
      if (rd > 15 || rs1 > 15 || rs2 > 15) return unspecified();
      if (f7 != 0 && !(f7 == 0x20 && (f3 == 0 || f3 == 5))) return unspecified();
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
    case 0x0F:  // MISC-MEM: every FENCE encoding (funct3 = 0) is a no-op; FENCE.I is reserved
      if (f3 != 0) return unspecified();
      return finish(false, 0);
    case 0x73: {  // SYSTEM
      if (f3 == 0) {
        if (insn == 0x00000073u) return trap(11);  // ECALL
        if (insn == 0x00100073u) return trap(3);   // EBREAK
        if (insn == 0x30200073u) {                 // MRET: pc = mepc, MIE = MPIE, MPIE = 1
          if ((known_ & (K_MEPC | K_MPIE)) != (K_MEPC | K_MPIE)) flag_uninit(pc);
          r.npc = mepc_;
          mie_ = mpie_;
          mpie_ = 1;
          known_ |= K_MPIE;
          return finish(false, 0);
        }
        if (insn == 0x10500073u) return finish(false, 0);  // WFI: a no-op (contract 4)
        return unspecified();
      }
      // Zicsr: CSRRW 1, CSRRS 2, CSRRC 3, and the immediate forms 5-7 (zimm = the rs1 field)
      if (f3 == 4 || rd > 15) return unspecified();
      const bool imm = f3 >= 5;
      if (!imm && rs1 > 15) return unspecified();
      const uint32_t csr = insn >> 20;
      if (!csr_exists(csr)) return unspecified();
      const uint32_t op = f3 & 3;
      const bool writes = op == 1 || rs1 != 0;  // CSRRS/C with rs1 = x0 or zimm = 0 don't write
      if (writes && (csr >> 10) == 3) return unspecified();  // 0xF11-0xF14 are read-only
      uint32_t src = rs1;  // zimm
      if (!imm) {
        use_rs1();
        src = r.rs1v;
      }
      uint32_t old = 0;
      if (op != 1 || rd != 0) {  // CSRRW with rd = x0 does not read
        bool meip = io_.irq_line();
        if (csr == 0x344 && rd != 0 && io.volatile_read) {
          const uint32_t dut = io.volatile_read(VSLOT_MIP, csr_value(csr, meip));
          meip = (dut & MIP_MEIP) != 0;
        }
        old = csr_value(csr, meip);
        if (rd != 0) {
          const uint32_t need = csr == 0x305 ? K_MTVEC : csr == 0x340 ? K_MSCRATCH
                                : csr == 0x341 ? K_MEPC : csr == 0x342 ? K_MCAUSE
                                : csr == 0x300 ? K_MPIE : 0;
          if ((known_ & need) != need) flag_uninit(pc);
        }
      }
      if (writes) {
        const uint32_t nv = op == 1 ? src : op == 2 ? (old | src) : (old & ~src);
        switch (csr) {
          case 0x300:
            mie_ = (nv >> 3) & 1;
            mpie_ = (nv >> 7) & 1;
            if (op == 1 || (src & 0x80)) known_ |= K_MPIE;
            break;
          case 0x304: meie_ = (nv >> 11) & 1; break;
          case 0x305:
            mtvec_ = nv & ~3u;
            if (op == 1) known_ |= K_MTVEC;
            break;
          case 0x340:
            mscratch_ = nv;
            if (op == 1) known_ |= K_MSCRATCH;
            break;
          case 0x341:
            mepc_ = nv & ~3u;
            if (op == 1) known_ |= K_MEPC;
            break;
          case 0x342:
            mcause_ = nv & 0x8000000Fu;
            if (op == 1) known_ |= K_MCAUSE;
            break;
          default: break;  // misa, mtval, mip: writes ignored
        }
      }
      return finish(true, old);
    }
    default:
      return unspecified();
  }
}

// Instruction classes for timing reports.
inline const char* iss_class(const Retire& r) {
  if (r.trap) return "trap";
  uint32_t op = r.insn & 0x7F, f3 = (r.insn >> 12) & 7;
  switch (op) {
    case 0x37:
    case 0x17: return "lui_auipc";
    case 0x6F: return "jal";
    case 0x67: return "jalr";
    case 0x63: return r.npc != r.pc + 4 ? "branch_taken" : "branch_not_taken";
    case 0x03: return (r.maddr >> 31) ? "io_load" : "load";
    case 0x23: return (r.maddr >> 31) ? "io_store" : "store";
    case 0x13: return (f3 == 1 || f3 == 5) ? "shift" : "alu_imm";
    case 0x33: return (f3 == 1 || f3 == 5) ? "shift" : "alu";
    case 0x0F: return "fence";
    case 0x73:
      if (f3) return "csr";
      return r.insn == 0x30200073u ? "mret" : r.insn == 0x10500073u ? "wfi" : "system";
  }
  return "reserved";
}

// Small disassembler for error messages.
inline std::string iss_disasm(uint32_t insn) {
  static const char* rn[32] = {"zero", "ra",  "sp",  "gp",  "tp",  "t0",  "t1",  "t2",
                               "s0",   "s1",  "a0",  "a1",  "a2",  "a3",  "a4",  "a5",
                               "x16",  "x17", "x18", "x19", "x20", "x21", "x22", "x23",
                               "x24",  "x25", "x26", "x27", "x28", "x29", "x30", "x31"};
  char b[96];
  uint32_t op = insn & 0x7F, rd = (insn >> 7) & 31, f3 = (insn >> 12) & 7, rs1 = (insn >> 15) & 31,
           rs2 = (insn >> 20) & 31, f7 = insn >> 25;
  int32_t ii = (int32_t)insn >> 20;
  int32_t is = (int32_t)(((uint32_t)((int32_t)insn >> 25) << 5) | ((insn >> 7) & 0x1F));
  int32_t ib = (int32_t)(((uint32_t)((int32_t)insn >> 31) << 12) | ((insn & 0x80) << 4) |
                         ((insn >> 20) & 0x7E0) | ((insn >> 7) & 0x1E));
  int32_t ij = (int32_t)(((uint32_t)((int32_t)insn >> 31) << 20) | (insn & 0xFF000) |
                         ((insn >> 9) & 0x800) | ((insn >> 20) & 0x7FE));
  static const char* br[8] = {"beq", "bne", "?", "?", "blt", "bge", "bltu", "bgeu"};
  static const char* ld[8] = {"lb", "lh", "lw", "?", "lbu", "lhu", "?", "?"};
  static const char* st[8] = {"sb", "sh", "sw", "?", "?", "?", "?", "?"};
  static const char* oi[8] = {"addi", "slli", "slti", "sltiu", "xori", "srli", "ori", "andi"};
  static const char* oo[8] = {"add", "sll", "slt", "sltu", "xor", "srl", "or", "and"};
  static const char* cs[8] = {"?", "csrrw", "csrrs", "csrrc", "?", "csrrwi", "csrrsi", "csrrci"};
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
      if (f3 == 0)
        snprintf(b, sizeof b, "%s", insn == 0x73 ? "ecall" : insn == 0x00100073 ? "ebreak"
                                    : insn == 0x30200073 ? "mret" : insn == 0x10500073 ? "wfi"
                                    : "system?");
      else if (f3 >= 5)
        snprintf(b, sizeof b, "%s %s, 0x%03x, %u", cs[f3], rn[rd], insn >> 20, rs1);
      else
        snprintf(b, sizeof b, "%s %s, 0x%03x, %s", cs[f3], rn[rd], insn >> 20, rn[rs1]);
      break;
    default: snprintf(b, sizeof b, "unknown(0x%08x)", insn); break;
  }
  return b;
}

// Load a flash image in $readmemh form into out (FLASH_SIZE bytes). Tokens are hex bytes
// separated by white space; "@hex" sets the byte address of the next token; "//" starts a
// comment. Bytes the file doesn't set read 0xFF (erased flash). Returns false on a format error
// or an address outside the 16 MiB flash.
inline bool load_flash_hex(const char* path, uint8_t* out, std::string* err) {
  FILE* f = std::fopen(path, "r");
  if (!f) {
    if (err) *err = std::string("cannot open ") + path;
    return false;
  }
  std::memset(out, 0xFF, FLASH_SIZE);
  char line[512];
  uint64_t n = 0;
  unsigned lineno = 0;
  auto fail = [&](const char* why) {
    if (err) *err = std::string(why) + " at " + path + ":" + std::to_string(lineno);
    std::fclose(f);
    return false;
  };
  while (std::fgets(line, sizeof line, f)) {
    lineno++;
    if (!std::strchr(line, '\n') && !std::feof(f)) return fail("line too long");
    if (char* c = std::strstr(line, "//")) *c = 0;
    char* p = line;
    for (;;) {
      while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
      if (!*p) break;
      const bool addr = *p == '@';
      if (addr) p++;
      char* end = nullptr;
      unsigned long long v = std::strtoull(p, &end, 16);
      if (end == p || (*end && *end != ' ' && *end != '\t' && *end != '\r' && *end != '\n'))
        return fail("bad token");
      p = end;
      if (addr) {
        if (v >= FLASH_SIZE) return fail("address outside the 16 MiB flash");
        n = v;
        continue;
      }
      if (v > 0xFF) return fail("token is not a byte");
      if (n >= FLASH_SIZE) return fail("image runs past the 16 MiB flash");
      out[n++] = (uint8_t)v;
    }
  }
  std::fclose(f);
  return true;
}

}  // namespace v3

// Global names for the v3 harness. A translation unit that also includes v2's iss.h (which has
// its own Retire, iss_class and iss_disasm) defines ISS3_NAMESPACED and writes v3:: instead.
#ifndef ISS3_NAMESPACED
using v3::IoHooks;
using v3::IoRegs;
using v3::Iss3;
using v3::Retire;
using v3::iss_class;
using v3::iss_disasm;
using v3::load_flash_hex;
using v3::IO_VOLATILE_MASK;
using v3::VSLOT_MIP;
#endif
