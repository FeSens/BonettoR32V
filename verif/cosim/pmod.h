// Models of the TT QSPI Pmod (contract v3 section 2): a W25Q128JV flash (16 MiB, QE = 1) on
// uio[0] and an APS6404L PSRAM (8 MiB) on uio[6], sharing SCK (uio[3]) and SD0-SD3
// (uio[1], [2], [4], [5]). Only the commands of the contract tables (2.2, 2.3) exist; anything else
// is a protocol violation.
//
// Call PmodBus::edge(t, uio_out, uio_oe) once per rising clk edge t with the design's outputs
// after that edge, then feed uio_in() to the design until the next edge. The first violation of
// contract 2.1-2.4 stops the model: edge() returns false and error() says why.
//
// Cycle-level abstraction (2.1.5): at a rising SCK edge t (S(t-1) = 0, S(t) = 1, a CS_n low) the
// selected chip samples the lines it reads, as uio_out(t) where uio_oe(t) = 1 and noise elsewhere.
// Its output for that clock is in uio_in() from edge t until SCK falls. Every line nobody drives
// carries fresh noise each cycle; lines the core drives read back their uio_out value.
//
// Interpretations of the contract text, so a checker never needs to guess:
//   - "Stable across a rising SCK edge" (2.1.4): an SD line with uio_oe(t) = 1 must also have
//     uio_oe(t-1) = 1 and uio_out(t) = uio_out(t-1). Releasing a line at a rising edge is fine.
//   - The data-out phase (2.1.6) runs from the first data-out rising edge until CS_n rises,
//     SCK-low cycles included; the core must not drive the chip's output lines in any of them.
//   - The per-command SCK limit (2.1.3) is checked once the command is known: (shortest high phase
//     + shortest low phase) x clk_ns >= 1000 / max_MHz, over the phases of the transaction. A low
//     phase counts only between two rising edges (not before the first or after the last).
//   - A command whose table row says "none" takes no further edges. A write must end with its
//     address complete and its data on a byte boundary; a read may end at any edge.
//   - The PSRAM needs 0x66 then 0x99 (consecutive PSRAM transactions; ignored partial transactions
//     in between don't count) before any read or write, whatever its power-up mode.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

enum class FlashMode { Standard, Continuous };
// SpiUninit and Qpi are the contract's power-up states (2.4); both need the reset before an
// access. Spi means SPI mode, already reset (the state after 0x66, 0x99).
enum class PsramMode { SpiUninit, Spi, Qpi };

struct PmodInit {
  FlashMode flash;
  PsramMode psram;
};

// Power-up state for a cosim seed: (seed - 1) % 4 covers every combination of contract 2.4.
inline PmodInit pmod_init_for_seed(uint32_t seed) {
  static const PmodInit m[4] = {{FlashMode::Standard, PsramMode::SpiUninit},
                                {FlashMode::Continuous, PsramMode::SpiUninit},
                                {FlashMode::Standard, PsramMode::Qpi},
                                {FlashMode::Continuous, PsramMode::Qpi}};
  return m[(seed - 1u) % 4u];
}

struct PsramWrite {
  uint64_t t;     // clk edge of the byte's last data edge
  uint32_t addr;  // 23-bit byte address
  uint8_t data;
};

namespace pmod_detail {

// uio bits (contract section 1)
constexpr uint8_t CSF = 1u << 0, SD0 = 1u << 1, SD1 = 1u << 2, SCK = 1u << 3, SD2 = 1u << 4,
                  SD3 = 1u << 5, CSR = 1u << 6, CSB = 1u << 7;
constexpr uint8_t SDALL = SD0 | SD1 | SD2 | SD3;
constexpr uint8_t OE_FIXED = CSF | SCK | CSR | CSB;

// nibble {SD3, SD2, SD1, SD0} <-> uio bits
inline uint8_t nib_to_uio(uint8_t n) {
  return (uint8_t)(((n & 1) << 1) | (((n >> 1) & 1) << 2) | (((n >> 2) & 1) << 4) |
                   (((n >> 3) & 1) << 5));
}
inline uint8_t uio_to_nib(uint8_t u) {
  return (uint8_t)(((u >> 1) & 1) | (((u >> 2) & 1) << 1) | (((u >> 4) & 1) << 2) |
                   (((u >> 5) & 1) << 3));
}

enum class Data : uint8_t { None, OutSingle, OutQuad, InSingle, InQuad };

// The fields after a command: one row of a contract table.
struct Layout {
  uint8_t addr_edges = 0;   // 0, 6 (quad) or 24 (single)
  uint8_t mode_edges = 0;   // 2: the flash's M byte (0xEB)
  uint8_t dummy_edges = 0;  // dummy (flash) or wait (PSRAM) edges
  Data data = Data::None;
  int max_mhz = 0;
};

inline std::string fmt(const char* f, ...) __attribute__((format(printf, 1, 2)));
inline std::string fmt(const char* f, ...) {
  char b[512];
  va_list ap;
  va_start(ap, f);
  std::vsnprintf(b, sizeof b, f, ap);
  va_end(ap);
  return b;
}

// One chip: a state machine fed by rising SCK edges while its CS_n is low.
class QspiChip {
 public:
  virtual ~QspiChip() = default;

  void begin(uint64_t t) {  // CS_n fell at edge t
    x_ = Txn();
    x_.active = true;
    x_.start = t;
    start_txn();
  }

  // A rising SCK edge; nib = {SD3, SD2, SD1, SD0} as the chip samples them.
  bool clock(uint64_t t, uint8_t nib) {
    uint32_t n = ++x_.edges;
    if (!x_.cmd_known) {
      x_.cmd = cmd_quad() ? (uint8_t)((x_.cmd << 4) | nib) : (uint8_t)((x_.cmd << 1) | (nib & 1));
      if (n < (cmd_quad() ? 2u : 8u)) return true;
      x_.cmd_known = true;
      x_.field0 = n;
      return decode();
    }
    uint32_t e = n - 1 - x_.field0;  // index of this edge after the command field
    const Layout& L = x_.lay;
    if (e < L.addr_edges) {
      x_.addr = L.addr_edges == 6 ? ((x_.addr << 4) | nib) : ((x_.addr << 1) | (nib & 1));
      return true;
    }
    e -= L.addr_edges;
    if (e < L.mode_edges) {
      x_.mode = (uint8_t)((x_.mode << 4) | nib);
      x_.mode_seen++;
      return true;
    }
    e -= L.mode_edges;
    if (e < L.dummy_edges) return true;
    e -= L.dummy_edges;
    x_.data_edges = e + 1;
    switch (L.data) {
      case Data::None:
        return fail(fmt("command 0x%02x takes no fields, but SCK clocked another edge "
                        "(rule 2.1.10)",
                        x_.cmd));
      case Data::OutSingle:
        if (e % 8 == 0) load(e / 8);
        x_.out = (uint8_t)(((x_.cur >> (7 - e % 8)) & 1) ? SD1 : 0);
        return true;
      case Data::OutQuad:
        if (e % 2 == 0) load(e / 2);
        x_.out = nib_to_uio((uint8_t)(e % 2 == 0 ? x_.cur >> 4 : x_.cur & 0xF));
        return true;
      case Data::InSingle:
        x_.cur = (uint8_t)((x_.cur << 1) | (nib & 1));
        if (e % 8 == 7) store(t, e / 8, x_.cur);
        return true;
      case Data::InQuad:
        x_.cur = (uint8_t)((x_.cur << 4) | nib);
        if (e % 2 == 1) store(t, e / 2, x_.cur);
        return true;
    }
    return true;
  }

  bool end(uint64_t t) {  // CS_n rose at edge t; on a violation the transaction stays open
    if (!x_.cmd_known) {  // ended inside the command field: ignored
      x_.active = false;
      return true;
    }
    const Layout& L = x_.lay;
    if (L.data == Data::InSingle || L.data == Data::InQuad) {
      if (x_.edges - x_.field0 < L.addr_edges)
        return fail(fmt("write 0x%02x ended mid-byte, inside its address field (rule 2.1.9)",
                        x_.cmd));
      uint32_t per = L.data == Data::InSingle ? 8 : 2;
      if (x_.data_edges % per)
        return fail(fmt("write 0x%02x ended mid-byte after %u data edges (rule 2.1.9: writes "
                        "end on a byte boundary)",
                        x_.cmd, x_.data_edges));
    }
    x_.active = false;
    return finish(t);
  }

  bool active() const { return x_.active; }
  // In the data-out phase: from the first data-out rising edge until CS_n rises.
  bool in_data_out() const {
    return x_.active && x_.data_edges > 0 &&
           (x_.lay.data == Data::OutSingle || x_.lay.data == Data::OutQuad);
  }
  uint8_t drive_mask() const { return x_.lay.data == Data::OutSingle ? SD1 : SDALL; }
  uint8_t out_uio() const { return x_.out; }  // output for the latest data-out clock
  int max_mhz() const { return x_.cmd_known ? x_.lay.max_mhz : 0; }
  uint8_t cmd() const { return x_.cmd; }
  uint64_t reads() const { return reads_; }
  const std::string& why() const { return why_; }
  std::string describe() const {
    return fmt("%s transaction from cycle %llu: %u edges, cmd %s, addr 0x%06x", name(),
               (unsigned long long)x_.start, x_.edges,
               x_.cmd_known ? fmt("0x%02x", x_.cmd).c_str() : "incomplete", x_.addr);
  }

 protected:
  struct Txn {
    bool active = false;
    uint64_t start = 0;
    uint32_t edges = 0;  // rising SCK edges so far
    bool cmd_known = false;
    uint8_t cmd = 0;
    uint32_t field0 = 0;  // edges taken by the command field
    Layout lay;
    uint32_t addr = 0;
    uint8_t mode = 0;
    uint32_t mode_seen = 0;
    uint32_t data_edges = 0;
    uint8_t cur = 0;  // data byte being shifted out or in
    uint8_t out = 0;  // uio bits for the latest data-out clock
  };
  Txn x_;
  uint64_t reads_ = 0;
  std::string why_;

  bool fail(const std::string& w) {
    why_ = w;
    return false;
  }
  void load(uint32_t k) {  // a read byte with at least one clocked bit counts as read
    x_.cur = read_byte(k);
    reads_++;
  }

  virtual const char* name() const = 0;
  virtual void start_txn() {}
  virtual bool cmd_quad() const = 0;
  virtual bool decode() = 0;  // sets x_.lay from x_.cmd, or fails
  virtual uint8_t read_byte(uint32_t k) const = 0;
  virtual void store(uint64_t t, uint32_t k, uint8_t v) = 0;
  virtual bool finish(uint64_t t) = 0;  // state changes when CS_n rises
};

}  // namespace pmod_detail

// W25Q128JV, QE = 1 (contract 2.2). Contents never change.
class QspiFlash : public pmod_detail::QspiChip {
 public:
  static constexpr uint32_t SIZE = 1u << 24;

  QspiFlash(const uint8_t* image, FlashMode mode)
      : mem_(SIZE, 0xFF), continuous_(mode == FlashMode::Continuous) {
    if (image) std::memcpy(mem_.data(), image, SIZE);
  }
  bool continuous() const { return continuous_; }

 private:
  std::vector<uint8_t> mem_;
  bool continuous_;

  static pmod_detail::Layout row(uint8_t a, uint8_t m, uint8_t d, pmod_detail::Data k, int mhz) {
    pmod_detail::Layout l;
    l.addr_edges = a;
    l.mode_edges = m;
    l.dummy_edges = d;
    l.data = k;
    l.max_mhz = mhz;
    return l;
  }

  const char* name() const override { return "flash"; }
  bool cmd_quad() const override { return false; }
  void start_txn() override {
    if (!continuous_) return;
    // continuous read: no command, the transaction starts with the quad address
    x_.cmd_known = true;
    x_.cmd = 0xEB;
    x_.field0 = 0;
    x_.lay = row(6, 2, 4, pmod_detail::Data::OutQuad, 133);
  }
  bool decode() override {
    using pmod_detail::Data;
    switch (x_.cmd) {
      case 0x03: x_.lay = row(24, 0, 0, Data::OutSingle, 50); return true;
      case 0x0B: x_.lay = row(24, 0, 8, Data::OutSingle, 133); return true;
      case 0x6B: x_.lay = row(24, 0, 8, Data::OutQuad, 133); return true;
      case 0xEB: x_.lay = row(6, 2, 4, Data::OutQuad, 133); return true;
      case 0xFF: x_.lay = row(0, 0, 0, Data::None, 133); return true;
      case 0x02: x_.lay = row(24, 0, 0, Data::InSingle, 133); return true;  // ignored (no WEL)
    }
    return fail(pmod_detail::fmt("illegal command 0x%02x for the flash (rule 2.1.10, table 2.2)",
                                 x_.cmd));
  }
  uint8_t read_byte(uint32_t k) const override { return mem_[(x_.addr + k) & (SIZE - 1)]; }
  void store(uint64_t, uint32_t, uint8_t) override {}  // Page Program without write enable
  bool finish(uint64_t) override {
    // M was clocked completely: M[5:4] = 10 enters (or stays in) continuous read, else leaves it
    if (x_.mode_seen >= 2) continuous_ = ((x_.mode >> 4) & 3) == 2;
    return true;
  }
};

// APS6404L (contract 2.3). A[23] is ignored; bursts are linear mod 2^23.
class Psram : public pmod_detail::QspiChip {
 public:
  static constexpr uint32_t SIZE = 1u << 23;

  Psram(uint64_t fill_seed, PsramMode mode)
      : mem_(SIZE), qpi_(mode == PsramMode::Qpi), init_(mode == PsramMode::Spi) {
    // power-up content is arbitrary (contract 3)
    uint64_t s = fill_seed;
    for (uint32_t i = 0; i < SIZE; i += 8) {
      s += 0x9E3779B97F4A7C15ull;
      uint64_t z = s;
      z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
      z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
      z ^= z >> 31;
      std::memcpy(&mem_[i], &z, 8);
    }
  }

  uint8_t byte(uint32_t a) const { return mem_[a & (SIZE - 1)]; }
  bool initialized() const { return init_; }
  bool qpi() const { return qpi_; }
  const std::vector<PsramWrite>& writes() const { return writes_; }
  // tRST bookkeeping: the edge where the 0x99 transaction ended, until the next PSRAM command.
  bool reset_pending() const { return reset_pending_; }
  uint64_t reset_t() const { return reset_t_; }

 private:
  std::vector<uint8_t> mem_;
  bool qpi_, init_;
  bool rsten_ = false;  // the previous PSRAM transaction was 0x66
  bool reset_pending_ = false;
  uint64_t reset_t_ = 0;
  std::vector<PsramWrite> writes_;

  static pmod_detail::Layout row(uint8_t a, uint8_t d, pmod_detail::Data k, int mhz) {
    pmod_detail::Layout l;
    l.addr_edges = a;
    l.dummy_edges = d;
    l.data = k;
    l.max_mhz = mhz;
    return l;
  }

  const char* name() const override { return "PSRAM"; }
  bool cmd_quad() const override { return qpi_; }
  void start_txn() override { reset_pending_ = false; }
  bool decode() override {
    using pmod_detail::Data;
    using pmod_detail::fmt;
    const uint8_t c = x_.cmd;
    bool known = true, access = true;
    if (!qpi_) {
      switch (c) {
        case 0x03: x_.lay = row(24, 0, Data::OutSingle, 33); break;
        case 0x0B: x_.lay = row(24, 8, Data::OutSingle, 84); break;
        case 0xEB: x_.lay = row(6, 6, Data::OutQuad, 84); break;
        case 0x02: x_.lay = row(24, 0, Data::InSingle, 84); break;
        case 0x38: x_.lay = row(6, 0, Data::InQuad, 84); break;
        case 0x35: case 0x66: case 0x99: x_.lay = row(0, 0, Data::None, 84); access = false; break;
        default: known = false;
      }
    } else {
      switch (c) {
        case 0x0B: x_.lay = row(6, 4, Data::OutQuad, 66); break;
        case 0xEB: x_.lay = row(6, 6, Data::OutQuad, 84); break;
        case 0x02: case 0x38: x_.lay = row(6, 0, Data::InQuad, 84); break;
        case 0xF5: case 0x66: case 0x99: x_.lay = row(0, 0, Data::None, 84); access = false; break;
        default: known = false;
      }
    }
    if (!known)
      return fail(fmt("illegal command 0x%02x for the PSRAM in %s mode (rule 2.1.10, table 2.3)",
                      c, qpi_ ? "QPI" : "SPI"));
    if (rsten_ && c != 0x99)
      return fail(fmt("0x66 (reset enable) must be followed by 0x99, got 0x%02x (table 2.3)", c));
    if (!rsten_ && c == 0x99)
      return fail("0x99 (reset) without 0x66 (reset enable) in the previous PSRAM transaction "
                  "(table 2.3)");
    if (access && !init_)
      return fail(fmt("PSRAM access before reset: 0x%02x before 0x66, 0x99 (rule 2.1.10, 2.4)", c));
    return true;
  }
  uint8_t read_byte(uint32_t k) const override { return mem_[(x_.addr + k) & (SIZE - 1)]; }
  void store(uint64_t t, uint32_t k, uint8_t v) override {
    uint32_t a = (x_.addr + k) & (SIZE - 1);
    mem_[a] = v;
    writes_.push_back(PsramWrite{t, a, v});
  }
  bool finish(uint64_t t) override {
    const uint8_t c = x_.cmd;
    rsten_ = c == 0x66;
    if (c == 0x35) qpi_ = true;
    if (c == 0xF5) qpi_ = false;
    if (c == 0x99) {
      qpi_ = false;
      init_ = true;
      reset_pending_ = true;
      reset_t_ = t;
    }
    return true;
  }
};

class PmodBus {
 public:
  PmodBus(uint32_t noise_seed, const uint8_t* flash_image /* 16 MiB */, PmodInit init,
          double clk_ns)
      : flash_(flash_image, init.flash),
        psram_(noise_seed * 0xD1B54A32D192ED03ull + 0x5851F42D4C957F2Dull, init.psram),
        clk_ns_(clk_ns),
        tcem_((uint64_t)std::floor(8000.0 / clk_ns + 1e-9)),
        trst_((uint64_t)std::ceil(50.0 / clk_ns - 1e-9)),
        rng_(noise_seed * 0x9E3779B97F4A7C15ull + 1) {
    in_ = noise();
  }

  bool edge(uint64_t t, uint8_t out, uint8_t oe) {
    using namespace pmod_detail;
    if (!err_.empty()) return false;
    const bool s = out & SCK, cf = out & CSF, cr = out & CSR;
    const uint8_t nz = noise();
    if ((oe & OE_FIXED) != OE_FIXED)
      return fail(t, fmt("uio_oe = 0x%02x: uio_oe[0], [3], [6] and [7] must be 1 (section 1)",
                         oe));
    if (!(out & CSB)) return fail(t, "uio_out[7] (PSRAM B CS_n) must be 1 (section 1)");
    if (!cf && !cr) return fail(t, "both CS_n low (rule 2.1.1)");
    if ((cf != pcf_ || cr != pcr_) && (ps_ || s))
      return fail(t, "CS_n changed while SCK was high (rule 2.1.2: S(t-1) = S(t) = 0)");
    if (s && cf && cr) return fail(t, "SCK high while both CS_n are high (rule 2.1.3)");
    const bool rising = !ps_ && s, falling = ps_ && !s;
    if (rising) {
      uint8_t bad = (uint8_t)(oe & SDALL & (~poe_ | (out ^ pout_)));
      if (bad)
        return fail(t, fmt("core data changed across a rising SCK edge on uio bits 0x%02x "
                           "(rule 2.1.4: driven lines are stable across it)",
                           bad));
    }

    if (pcf_ && !cf) {
      flash_.begin(t);
      new_txn();
    }
    if (pcr_ && !cr) {
      if (psram_.reset_pending() && t - psram_.reset_t() < trst_)
        return fail(t, fmt("tRST: PSRAM CS_n high for %llu cycles after 0x99, needs 50 ns = %llu "
                           "cycles (table 2.3)",
                           (unsigned long long)(t - psram_.reset_t()),
                           (unsigned long long)trst_));
      psram_.begin(t);
      psram_fall_ = t;
      new_txn();
    }
    QspiChip* chip = !cf ? (QspiChip*)&flash_ : !cr ? (QspiChip*)&psram_ : nullptr;
    if (!cr && t - psram_fall_ + 1 > tcem_)
      return fail(t, fmt("tCEM: PSRAM CS_n low for more than %llu cycles = floor(8000 ns / "
                         "%g ns) (rule 2.1.7)",
                         (unsigned long long)tcem_, clk_ns_));

    const uint8_t lines = (uint8_t)((out & oe) | (nz & ~oe));
    if (!s) valid_ = false;
    if (chip) {
      if (rising) {
        if (seen_rise_) min_low_ = std::min(min_low_, t - t_fall_);
        seen_rise_ = true;
        t_rise_ = t;
        if (!chip->clock(t, uio_to_nib(lines))) return fail(t, chip->why());
        valid_ = chip->in_data_out();
      }
      if (falling) {
        min_high_ = std::min(min_high_, t - t_rise_);
        t_fall_ = t;
      }
      int mhz = chip->max_mhz();
      if ((rising || falling) && mhz && min_high_ != NONE && min_low_ != NONE &&
          (double)(min_high_ + min_low_) * clk_ns_ < 1000.0 / mhz - 1e-9)
        return fail(t, fmt("SCK too fast for 0x%02x (%s, max %d MHz): shortest high + low phase "
                           "%llu + %llu cycles x %g ns < %.3g ns (rule 2.1.3)",
                           chip->cmd(), chip == &flash_ ? "flash" : "PSRAM", mhz,
                           (unsigned long long)min_high_, (unsigned long long)min_low_, clk_ns_,
                           1000.0 / mhz));
      if (chip->in_data_out() && (oe & chip->drive_mask()))
        return fail(t, fmt("contention: the core drives uio bits 0x%02x during the memory's "
                           "data-out phase (rule 2.1.6)",
                           (unsigned)(oe & chip->drive_mask())));
    }

    if (!pcf_ && cf && !flash_.end(t)) return fail(t, flash_.why());
    if (!pcr_ && cr && !psram_.end(t)) return fail(t, psram_.why());

    uint8_t in = lines;
    if (valid_ && chip) in = (uint8_t)((in & ~chip->drive_mask()) | chip->out_uio());
    in_ = in;
    pcf_ = cf;
    pcr_ = cr;
    ps_ = s;
    pout_ = out;
    poe_ = oe;
    return true;
  }

  uint8_t uio_in() const { return in_; }
  const std::string& error() const { return err_; }
  const std::vector<PsramWrite>& writes() const { return psram_.writes(); }
  uint8_t psram_byte(uint32_t a) const { return psram_.byte(a); }
  bool psram_initialized() const { return psram_.initialized(); }
  uint64_t flash_reads() const { return flash_.reads(); }
  uint64_t psram_reads() const { return psram_.reads(); }

 private:
  static constexpr uint64_t NONE = ~0ull;
  QspiFlash flash_;
  Psram psram_;
  double clk_ns_;
  uint64_t tcem_, trst_;
  uint64_t rng_;
  // pins after the previous edge; before the first edge the bus is idle (reset state, section 1)
  bool pcf_ = true, pcr_ = true, ps_ = false;
  uint8_t pout_ = pmod_detail::CSF | pmod_detail::CSR | pmod_detail::CSB;
  uint8_t poe_ = pmod_detail::OE_FIXED;
  uint64_t psram_fall_ = 0;
  // SCK phases of the current transaction
  bool seen_rise_ = false;
  uint64_t t_rise_ = 0, t_fall_ = 0, min_high_ = NONE, min_low_ = NONE;
  bool valid_ = false;  // the memory output is valid: SCK high after a data-out edge
  uint8_t in_ = 0;
  std::string err_;

  uint8_t noise() {
    rng_ ^= rng_ << 13;
    rng_ ^= rng_ >> 7;
    rng_ ^= rng_ << 17;
    return (uint8_t)(rng_ >> 24);
  }
  void new_txn() {
    seen_rise_ = false;
    min_high_ = min_low_ = NONE;
  }
  bool fail(uint64_t t, const std::string& why) {
    err_ = pmod_detail::fmt("Pmod protocol violation at cycle %llu: ", (unsigned long long)t) + why;
    if (flash_.active()) err_ += " [" + flash_.describe() + "]";
    if (psram_.active()) err_ += " [" + psram_.describe() + "]";
    return false;
  }
};
