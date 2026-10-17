// Unit tests for the QSPI Pmod chip models (pmod.h), contract v3 section 2.
//
// Each test drives a hand-built pin waveform through PmodBus::edge, one call per clk edge, with
// SCK = clk/2 unless it says otherwise, and prints "PASS name" or "FAIL name: why". The exit code is
// the number of failures. Run through verif/cosim/test_pmod.py.
#include <cstdio>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "pmod.h"

namespace {

using Bytes = std::vector<uint8_t>;

// uio bits (contract section 1)
constexpr uint8_t CSF = 1u << 0, SD0 = 1u << 1, SD1 = 1u << 2, SCK = 1u << 3, SD2 = 1u << 4,
                  SD3 = 1u << 5, CSR = 1u << 6, CSB = 1u << 7;
constexpr uint8_t SDALL = SD0 | SD1 | SD2 | SD3;
constexpr uint8_t OE_FIXED = CSF | SCK | CSR | CSB;

// nibble {SD3, SD2, SD1, SD0} <-> uio bits
uint8_t to_uio(uint8_t n) {
  return (uint8_t)(((n & 1) << 1) | (((n >> 1) & 1) << 2) | (((n >> 2) & 1) << 4) |
                   (((n >> 3) & 1) << 5));
}
uint8_t to_nib(uint8_t u) {
  return (uint8_t)(((u >> 1) & 1) | (((u >> 2) & 1) << 1) | (((u >> 4) & 1) << 2) |
                   (((u >> 5) & 1) << 3));
}

const Bytes& image() {
  static Bytes img = [] {
    Bytes b(1u << 24);
    uint32_t x = 0x2545F491u;
    for (auto& v : b) {
      x ^= x << 13;
      x ^= x >> 17;
      x ^= x << 5;
      v = (uint8_t)(x >> 11);
    }
    return b;
  }();
  return img;
}
Bytes img_at(uint32_t a, size_t n) {
  Bytes b;
  for (size_t k = 0; k < n; k++) b.push_back(image()[(a + k) & 0xFFFFFF]);
  return b;
}

std::string hex(const Bytes& b) {
  std::string s;
  char h[4];
  for (uint8_t v : b) {
    std::snprintf(h, sizeof h, "%02x ", v);
    s += h;
  }
  return s;
}

const PmodInit STD_UNINIT{FlashMode::Standard, PsramMode::SpiUninit};

// Pin-level driver: holds the core's uio_out / uio_oe and feeds one PmodBus edge per step().
struct Drv {
  std::unique_ptr<PmodBus> bus;
  uint64_t t = 0;
  uint8_t out = CSF | CSR | CSB, oe = OE_FIXED;  // idle: both CS_n high, SCK low
  bool ok = true;
  uint8_t in = 0;      // uio_in after the latest edge
  uint8_t in_low = 0;  // uio_in after the low edge of the latest clk()
  int half;            // clk cycles per SCK phase (1: SCK = clk/2)

  explicit Drv(PmodInit init = STD_UNINIT, double clk_ns = 25.0, int half_ = 1, uint32_t seed = 7)
      : bus(new PmodBus(seed, image().data(), init, clk_ns)), half(half_) {}

  bool step() {
    if (!ok) return false;
    ok = bus->edge(++t, out, oe);
    in = bus->uio_in();
    return ok;
  }
  const std::string& err() const { return bus->error(); }

  void select(uint8_t cs) {  // CS_n falls with SCK low
    out = (uint8_t)(out & ~cs & ~SCK);
    step();
  }
  void deselect() {  // CS_n rises (SCK already low)
    out |= CSF | CSR;
    step();
  }
  void idle(int n) {
    for (int i = 0; i < n; i++) step();
  }
  void end() {  // the last falling SCK edge, then CS_n high
    out &= (uint8_t)~SCK;
    step();
    deselect();
  }
  // One SCK clock: SD lines set while SCK is low, then the rising edge. Returns the nibble the core
  // sees after the rising edge (the memory output for this clock, if any).
  uint8_t clk(uint8_t nib, uint8_t sd_oe) {
    out = (uint8_t)((out & ~SDALL & ~SCK) | to_uio(nib));
    oe = (uint8_t)((oe & ~SDALL) | sd_oe);
    for (int i = 0; i < half; i++) step();
    in_low = in;
    out |= SCK;
    for (int i = 0; i < half; i++) step();
    return to_nib(in);
  }
  void send_single(uint32_t v, int bits) {
    for (int i = bits - 1; i >= 0; i--) clk((uint8_t)((v >> i) & 1), SD0);
  }
  void send_quad(uint32_t v, int nibbles) {
    for (int i = nibbles - 1; i >= 0; i--) clk((uint8_t)((v >> (4 * i)) & 0xF), SDALL);
  }
  void dummy(int n) {
    for (int i = 0; i < n; i++) clk(0, 0);
  }
  // Single-output data on SD1; the core keeps driving SD0 (MOSI) low, which is legal.
  Bytes read_single_bits(int nbits) {
    Bytes b;
    for (int i = 0; i < nbits; i++) {
      if (i % 8 == 0) b.push_back(0);
      uint8_t bit = (clk(0, SD0) >> 1) & 1;
      b.back() = (uint8_t)(b.back() | (bit << (7 - i % 8)));
    }
    return b;
  }
  Bytes read_single(int nbytes) { return read_single_bits(8 * nbytes); }
  Bytes read_quad(int nbytes) {
    Bytes b;
    for (int i = 0; i < nbytes; i++) {
      uint8_t hi = clk(0, 0);
      uint8_t lo = clk(0, 0);
      b.push_back((uint8_t)((hi << 4) | lo));
    }
    return b;
  }
  void write_single(const Bytes& b) {
    for (uint8_t v : b) send_single(v, 8);
  }
  void write_quad(const Bytes& b) {
    for (uint8_t v : b) send_quad(v, 2);
  }

  Bytes flash_read03(uint32_t a, int n) {
    select(CSF);
    send_single(0x03, 8);
    send_single(a, 24);
    Bytes b = read_single(n);
    end();
    return b;
  }
  // 0x66 then 0x99 in SPI form, then CS_n high long enough for tRST at any clk_ns >= 10.
  void psram_reset_spi() {
    select(CSR);
    send_single(0x66, 8);
    end();
    select(CSR);
    send_single(0x99, 8);
    end();
    idle(5);
  }
  void psram_write02(uint32_t a, const Bytes& b) {
    select(CSR);
    send_single(0x02, 8);
    send_single(a, 24);
    write_single(b);
    end();
  }
  Bytes psram_read03(uint32_t a, int n) {
    select(CSR);
    send_single(0x03, 8);
    send_single(a, 24);
    Bytes b = read_single(n);
    end();
    return b;
  }
};

#define REQUIRE(cond, ...)                         \
  do {                                             \
    if (!(cond)) {                                 \
      char m_[768];                                \
      std::snprintf(m_, sizeof m_, __VA_ARGS__);   \
      return std::string(m_);                      \
    }                                              \
  } while (0)
#define REQUIRE_OK(d) REQUIRE((d).ok, "unexpected violation: %s", (d).err().c_str())
#define REQUIRE_ERR(d, s)                                                          \
  REQUIRE(!(d).ok && (d).err().find(s) != std::string::npos,                       \
          "expected a violation containing \"%s\", got: %s", s,                    \
          (d).ok ? "(none)" : (d).err().c_str())
#define REQUIRE_BYTES(got, want, what) \
  REQUIRE((got) == (want), "%s: got %s, want %s", what, hex(got).c_str(), hex(want).c_str())

// ------------------------------------------------------------------------------------------------

std::string test_flash_read03() {
  Drv d;
  d.select(CSF);
  d.send_single(0x03, 8);
  d.send_single(0x000100, 24);
  Bytes want = img_at(0x100, 8);
  Bytes got;
  int noise_mismatch = 0;  // SD1 while SCK is low must be noise, not the data bit
  int prev_bit = -1;
  for (int i = 0; i < 64; i++) {
    if (i % 8 == 0) got.push_back(0);
    uint8_t bit = (d.clk(0, SD0) >> 1) & 1;
    if (prev_bit >= 0 && (int)((d.in_low >> 2) & 1) != prev_bit) noise_mismatch++;
    prev_bit = bit;
    got.back() = (uint8_t)(got.back() | (bit << (7 - i % 8)));
  }
  d.end();
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, want, "0x03 at 0x000100");
  REQUIRE(noise_mismatch > 0, "SD1 held the data bit while SCK was low (expected noise)");
  REQUIRE(d.bus->flash_reads() == 8, "flash_reads = %llu, want 8",
          (unsigned long long)d.bus->flash_reads());
  return "";
}

std::string test_flash_fastread_0b_6b_eb() {
  Drv d;
  // 0x0B: address single, 8 dummy edges, data single
  d.select(CSF);
  d.send_single(0x0B, 8);
  d.send_single(0x123456, 24);
  d.dummy(8);
  Bytes got = d.read_single(4);
  d.end();
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, img_at(0x123456, 4), "0x0B");
  // 0x6B: address single, 8 dummy edges, data quad
  d.select(CSF);
  d.send_single(0x6B, 8);
  d.send_single(0xABCDEF, 24);
  d.dummy(8);
  got = d.read_quad(4);
  d.end();
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, img_at(0xABCDEF, 4), "0x6B");
  // 0xEB: address quad (6 edges), M quad (2 edges), 4 dummy edges, data quad
  d.select(CSF);
  d.send_single(0xEB, 8);
  d.send_quad(0xFFFFFE, 6);
  d.send_quad(0x00, 2);
  d.dummy(4);
  got = d.read_quad(4);  // wraps at 2^24
  d.end();
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, img_at(0xFFFFFE, 4), "0xEB (wrapping at 16 MiB)");
  // the flash stays in standard mode after M = 0x00
  got = d.flash_read03(0x000040, 2);
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, img_at(0x40, 2), "0x03 after 0xEB with M = 0x00");
  return "";
}

std::string test_flash_continuous() {
  Drv d;
  d.select(CSF);
  d.send_single(0xEB, 8);
  d.send_quad(0x001000, 6);
  d.send_quad(0xA0, 2);  // M[5:4] = 10: continuous read
  d.dummy(4);
  Bytes got = d.read_quad(3);
  d.end();
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, img_at(0x001000, 3), "0xEB with M = 0xA0");
  // no command: the transaction starts with the quad address
  d.select(CSF);
  d.send_quad(0x765432, 6);
  d.send_quad(0xA0, 2);
  d.dummy(4);
  got = d.read_quad(3);
  d.end();
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, img_at(0x765432, 3), "continuous read (no command)");
  // M = 0xFF: this transaction is still continuous, the next one is not
  d.select(CSF);
  d.send_quad(0x0A0B0C, 6);
  d.send_quad(0xFF, 2);
  d.dummy(4);
  got = d.read_quad(3);
  d.end();
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, img_at(0x0A0B0C, 3), "continuous read with M = 0xFF");
  got = d.flash_read03(0x00BEEF, 3);
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, img_at(0x00BEEF, 3), "0x03 after leaving continuous read");
  // starting in continuous read (power-up state)
  Drv c(PmodInit{FlashMode::Continuous, PsramMode::SpiUninit});
  c.select(CSF);
  c.send_quad(0x000777, 6);
  c.send_quad(0x20, 2);  // also M[5:4] = 10
  c.dummy(4);
  got = c.read_quad(2);
  c.end();
  REQUIRE_OK(c);
  REQUIRE_BYTES(got, img_at(0x000777, 2), "continuous read from power-up");
  return "";
}

std::string test_flash_program_ignored() {
  Drv d;
  const uint32_t a = 0x004321;
  Bytes orig = img_at(a, 2);
  d.select(CSF);
  d.send_single(0x02, 8);
  d.send_single(a, 24);
  d.write_single(Bytes{(uint8_t)~orig[0], (uint8_t)~orig[1]});
  d.end();
  REQUIRE_OK(d);
  Bytes got = d.flash_read03(a, 2);
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, orig, "flash after 0x02");
  d.select(CSF);
  d.send_single(0x06, 8);
  REQUIRE_ERR(d, "illegal command");
  return "";
}

std::string test_psram_requires_reset() {
  {
    Drv d;
    REQUIRE(!d.bus->psram_initialized(), "PSRAM initialized at power-up");
    d.select(CSR);
    d.send_single(0x03, 8);
    d.send_single(0, 24);
    REQUIRE_ERR(d, "PSRAM access before reset");
  }
  {  // SpiUninit: a write is an access too
    Drv d;
    d.select(CSR);
    d.send_single(0x02, 8);
    REQUIRE_ERR(d, "PSRAM access before reset");
  }
  {  // power-up in QPI mode also needs the reset (contract 2.4)
    Drv d(PmodInit{FlashMode::Standard, PsramMode::Qpi});
    REQUIRE(!d.bus->psram_initialized(), "PSRAM initialized at power-up (QPI)");
    d.select(CSR);
    d.send_quad(0xEB, 2);
    REQUIRE_ERR(d, "PSRAM access before reset");
  }
  {  // after 0x66, 0x99 the read is legal
    Drv d;
    d.psram_reset_spi();
    REQUIRE_OK(d);
    REQUIRE(d.bus->psram_initialized(), "PSRAM not initialized after 0x66, 0x99");
    d.psram_read03(0, 2);
    REQUIRE_OK(d);
  }
  return "";
}

std::string test_psram_spi_rw() {
  Drv d;
  d.psram_reset_spi();
  Bytes w{0x11, 0x22, 0x33, 0x44};
  d.psram_write02(0x7FFFFE, w);
  REQUIRE_OK(d);
  const uint32_t at[4] = {0x7FFFFE, 0x7FFFFF, 0x000000, 0x000001};
  for (int k = 0; k < 4; k++)
    REQUIRE(d.bus->psram_byte(at[k]) == w[k], "psram_byte(0x%06x) = 0x%02x, want 0x%02x", at[k],
            d.bus->psram_byte(at[k]), w[k]);
  const auto& ws = d.bus->writes();
  REQUIRE(ws.size() == 4, "writes() has %zu entries, want 4", ws.size());
  for (int k = 0; k < 4; k++)
    REQUIRE(ws[k].addr == at[k] && ws[k].data == w[k],
            "writes()[%d] = {addr 0x%06x, data 0x%02x}, want {0x%06x, 0x%02x}", k, ws[k].addr,
            ws[k].data, at[k], w[k]);
  REQUIRE(ws[0].t < ws[1].t && ws[1].t < ws[2].t && ws[2].t < ws[3].t,
          "writes() times not increasing");
  Bytes got = d.psram_read03(0x7FFFFE, 4);
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, w, "0x03 read back");
  got = d.psram_read03(0xFFFFFE, 4);  // A[23] is ignored
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, w, "0x03 read back with A[23] = 1");
  REQUIRE(d.bus->psram_reads() == 8, "psram_reads = %llu, want 8",
          (unsigned long long)d.bus->psram_reads());
  return "";
}

std::string test_psram_quad_and_qpi() {
  Drv d;
  d.psram_reset_spi();
  Bytes w1{0xA1, 0xB2, 0xC3, 0xD4}, w2{0x01, 0x23, 0x45, 0x67}, w3{0xFE, 0xDC, 0xBA, 0x98};
  // SPI 0x38: address quad, data quad
  d.select(CSR);
  d.send_single(0x38, 8);
  d.send_quad(0x100000, 6);
  d.write_quad(w1);
  d.end();
  REQUIRE_OK(d);
  // SPI 0xEB: address quad, 6 wait edges, data quad
  d.select(CSR);
  d.send_single(0xEB, 8);
  d.send_quad(0x100000, 6);
  d.dummy(6);
  Bytes got = d.read_quad(4);
  d.end();
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, w1, "SPI 0x38 then 0xEB");
  // 0x35: QPI mode
  d.select(CSR);
  d.send_single(0x35, 8);
  d.end();
  REQUIRE_OK(d);
  // QPI 0x02 write
  d.select(CSR);
  d.send_quad(0x02, 2);
  d.send_quad(0x200000, 6);
  d.write_quad(w2);
  d.end();
  REQUIRE_OK(d);
  // QPI 0x0B: 4 wait edges
  d.select(CSR);
  d.send_quad(0x0B, 2);
  d.send_quad(0x200000, 6);
  d.dummy(4);
  got = d.read_quad(4);
  d.end();
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, w2, "QPI 0x02 then 0x0B");
  // QPI 0xEB: 6 wait edges
  d.select(CSR);
  d.send_quad(0xEB, 2);
  d.send_quad(0x200000, 6);
  d.dummy(6);
  got = d.read_quad(4);
  d.end();
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, w2, "QPI 0xEB");
  // QPI 0x38 write
  d.select(CSR);
  d.send_quad(0x38, 2);
  d.send_quad(0x300000, 6);
  d.write_quad(w3);
  d.end();
  REQUIRE_OK(d);
  REQUIRE(d.bus->psram_byte(0x300003) == 0x98, "QPI 0x38 did not write");
  // QPI 0xF5: back to SPI mode
  d.select(CSR);
  d.send_quad(0xF5, 2);
  d.end();
  REQUIRE_OK(d);
  got = d.psram_read03(0x200000, 4);
  REQUIRE_OK(d);
  REQUIRE_BYTES(got, w2, "SPI 0x03 after 0xF5");
  return "";
}

std::string test_reset_sequence_from_every_state() {
  const PmodInit want[4] = {{FlashMode::Standard, PsramMode::SpiUninit},
                            {FlashMode::Continuous, PsramMode::SpiUninit},
                            {FlashMode::Standard, PsramMode::Qpi},
                            {FlashMode::Continuous, PsramMode::Qpi}};
  for (uint32_t seed = 1; seed <= 8; seed++) {
    PmodInit init = pmod_init_for_seed(seed);
    const PmodInit& w = want[(seed - 1) % 4];
    REQUIRE(init.flash == w.flash && init.psram == w.psram, "pmod_init_for_seed(%u) is wrong",
            seed);
  }
  for (uint32_t seed = 1; seed <= 4; seed++) {
    Drv d(pmod_init_for_seed(seed), 25.0, 1, seed);
    // 1. flash: 8 edges with SD0-SD3 all driven high
    d.select(CSF);
    d.send_quad(0xFFFFFFFF, 8);
    d.end();
    // 2. PSRAM: 0xF5 in QPI form, then 0x66 and 0x99 in SPI form
    d.select(CSR);
    d.send_quad(0xF5, 2);
    d.end();
    d.psram_reset_spi();
    REQUIRE(d.ok, "seed %u: reset sequence: %s", seed, d.err().c_str());
    REQUIRE(d.bus->psram_initialized(), "seed %u: PSRAM not initialized", seed);
    Bytes got = d.flash_read03(0x0003F0, 4);
    REQUIRE(d.ok, "seed %u: flash 0x03: %s", seed, d.err().c_str());
    REQUIRE(got == img_at(0x3F0, 4), "seed %u: flash 0x03 got %s, want %s", seed, hex(got).c_str(),
            hex(img_at(0x3F0, 4)).c_str());
    Bytes wd{0x5A, (uint8_t)seed, 0xC3};
    d.psram_write02(0x7FFF00, wd);
    got = d.psram_read03(0x7FFF00, 3);
    REQUIRE(d.ok, "seed %u: PSRAM 0x02/0x03: %s", seed, d.err().c_str());
    REQUIRE(got == wd, "seed %u: PSRAM read back %s, want %s", seed, hex(got).c_str(),
            hex(wd).c_str());
  }
  return "";
}

std::string test_trst() {
  // tRST: after 0x99, CS_n stays high >= 50 ns before the next command: 2 cycles at 25 ns
  for (int extra = 0; extra <= 1; extra++) {
    Drv d;
    d.select(CSR);
    d.send_single(0x66, 8);
    d.end();
    d.select(CSR);
    d.send_single(0x99, 8);
    d.end();  // CS_n rises here
    d.idle(extra);
    d.select(CSR);  // CS_n high for 1 + extra cycles
    if (extra == 0) {
      REQUIRE_ERR(d, "tRST");
    } else {
      REQUIRE_OK(d);
    }
  }
  return "";
}

std::string test_tcem_limit() {
  // tCEM = floor(8000 / 25) = 320 cycles. CS_n low cycles = select (1) + 2 per clock + falling (1).
  for (int extra = 0; extra <= 1; extra++) {
    Drv d;
    d.psram_reset_spi();
    uint64_t reads0 = d.bus->psram_reads();
    d.select(CSR);
    uint64_t t_first_low = d.t;
    d.idle(extra);
    d.send_single(0x03, 8);
    d.send_single(0x000010, 24);
    d.read_single_bits(127);  // 15 bytes and 7 bits: the partial byte counts as read
    d.out &= (uint8_t)~SCK;
    d.step();  // last cycle with CS_n low
    uint64_t low = d.t - t_first_low + 1;
    REQUIRE(extra || low == 320, "test bug: CS_n low for %llu cycles", (unsigned long long)low);
    d.deselect();
    if (extra == 0) {
      REQUIRE_OK(d);
      REQUIRE(d.bus->psram_reads() - reads0 == 16, "psram_reads grew by %llu, want 16",
              (unsigned long long)(d.bus->psram_reads() - reads0));
    } else {
      REQUIRE_ERR(d, "tCEM");
    }
  }
  return "";
}

std::string test_contention() {
  {  // core drives SD1 during a flash 0x03 data phase
    Drv d;
    d.select(CSF);
    d.send_single(0x03, 8);
    d.send_single(0x000100, 24);
    d.read_single(1);
    REQUIRE_OK(d);
    d.clk(0, SD0 | SD1);
    REQUIRE_ERR(d, "contention");
  }
  {  // core drives SD3 during a PSRAM quad data phase
    Drv d;
    d.psram_reset_spi();
    d.select(CSR);
    d.send_single(0xEB, 8);
    d.send_quad(0, 6);
    d.dummy(6);
    d.read_quad(1);
    REQUIRE_OK(d);
    d.clk(0, SD3);
    REQUIRE_ERR(d, "contention");
  }
  {  // driving SD1 during the address phase is fine (the memory doesn't drive yet)
    Drv d;
    d.select(CSF);
    d.send_single(0x03, 8);
    for (int i = 0; i < 24; i++) d.clk(0, SD0 | SD1);
    d.read_single(1);
    d.end();
    REQUIRE_OK(d);
  }
  return "";
}

std::string test_waveform_rules() {
  {  // CS_n rises at the edge where SCK falls (SCK was high in the previous cycle)
    Drv d;
    d.select(CSF);
    d.send_single(0x03, 3);
    d.out = (uint8_t)((d.out & ~SCK) | CSF);
    d.step();
    REQUIRE_ERR(d, "CS_n changed while SCK");
  }
  {  // CS_n falls at the edge where SCK rises
    Drv d;
    d.out = (uint8_t)((d.out & ~CSF) | SCK);
    d.step();
    REQUIRE_ERR(d, "CS_n changed while SCK");
  }
  {  // both CS_n low
    Drv d;
    d.out &= (uint8_t)~(CSF | CSR);
    d.step();
    REQUIRE_ERR(d, "both CS_n low");
  }
  {  // SCK high with both CS_n high
    Drv d;
    d.out |= SCK;
    d.step();
    REQUIRE_ERR(d, "SCK high while both CS_n");
  }
  {  // core data changes across a rising SCK edge
    Drv d;
    d.select(CSF);
    d.clk(0, SD0);  // SD0 = 0 ...
    d.out &= (uint8_t)~SCK;
    d.step();
    d.out |= SCK | SD0;  // ... changes to 1 at the rising edge
    d.step();
    REQUIRE_ERR(d, "changed across a rising SCK edge");
  }
  {  // a line starts being driven at a rising SCK edge
    Drv d;
    d.select(CSF);
    d.clk(0, 0);
    d.out &= (uint8_t)~SCK;
    d.step();
    d.out |= SCK;
    d.oe |= SD0;
    d.step();
    REQUIRE_ERR(d, "changed across a rising SCK edge");
  }
  const uint8_t fixed[4] = {CSF, SCK, CSR, CSB};
  for (uint8_t b : fixed) {
    Drv d;
    d.oe &= (uint8_t)~b;
    d.step();
    REQUIRE_ERR(d, "uio_oe");
  }
  {
    Drv d;
    d.out &= (uint8_t)~CSB;
    d.step();
    REQUIRE_ERR(d, "uio_out[7]");
  }
  return "";
}

std::string test_partial_command_ignored() {
  {  // QPI-form 0xF5 in SPI mode, before and after the reset
    Drv d;
    d.select(CSR);
    d.send_quad(0xF5, 2);
    d.end();
    REQUIRE_OK(d);
    d.psram_reset_spi();
    d.select(CSR);
    d.send_quad(0xF5, 2);
    d.end();
    REQUIRE_OK(d);
    d.psram_write02(0x40, Bytes{0x77});
    Bytes got = d.psram_read03(0x40, 1);
    REQUIRE_OK(d);
    REQUIRE_BYTES(got, Bytes{0x77}, "SPI read after the ignored 0xF5");
  }
  {  // the flash ignores a transaction that ends inside its command field, even an illegal one
    Drv d;
    d.select(CSF);
    d.send_single(0x06 >> 1, 7);
    d.end();
    REQUIRE_OK(d);
    Bytes got = d.flash_read03(0x55, 2);
    REQUIRE_OK(d);
    REQUIRE_BYTES(got, img_at(0x55, 2), "flash read after the ignored partial command");
  }
  {  // CS_n low with no SCK edge at all
    Drv d;
    d.select(CSR);
    d.end();
    REQUIRE_OK(d);
  }
  return "";
}

std::string test_max_sck() {
  {  // clk 10 ns, SCK = clk/2 = 50 MHz: legal for flash 0x03 (50 MHz), not PSRAM 0x03 (33 MHz)
    Drv d(STD_UNINIT, 10.0);
    d.psram_reset_spi();  // 0x66 / 0x99: 84 MHz
    REQUIRE_OK(d);
    Bytes got = d.flash_read03(0x000200, 2);
    REQUIRE_OK(d);
    REQUIRE_BYTES(got, img_at(0x200, 2), "flash 0x03 at 50 MHz");
    d.select(CSR);
    d.send_single(0x03, 8);
    REQUIRE_ERR(d, "SCK too fast for 0x03");
  }
  {  // SCK = clk/4 = 25 MHz: PSRAM 0x03 is legal
    Drv d(STD_UNINIT, 10.0, 2);
    d.psram_reset_spi();
    d.psram_write02(0x10, Bytes{0x9C});
    Bytes got = d.psram_read03(0x10, 1);
    REQUIRE_OK(d);
    REQUIRE_BYTES(got, Bytes{0x9C}, "PSRAM 0x03 at 25 MHz");
  }
  {  // one short phase is enough to break the limit: high 2 + low 1 cycles = 30 ns < 30.3 ns
    Drv d(STD_UNINIT, 10.0, 2);
    d.psram_reset_spi();
    d.select(CSR);
    d.send_single(0x03, 8);
    d.send_single(0, 20);
    REQUIRE_OK(d);
    d.out = (uint8_t)(d.out & ~SCK);
    d.step();  // a 1-cycle low phase
    d.out |= SCK;
    d.step();
    d.step();
    d.out &= (uint8_t)~SCK;
    d.step();
    REQUIRE_ERR(d, "SCK too fast for 0x03");
  }
  return "";
}

std::string test_write_byte_boundary() {
  {  // SPI 0x02 ending 4 bits into its second byte
    Drv d;
    d.psram_reset_spi();
    d.select(CSR);
    d.send_single(0x02, 8);
    d.send_single(0x000100, 24);
    d.send_single(0xAB, 8);
    d.send_single(0x1, 4);
    d.end();
    REQUIRE_ERR(d, "mid-byte");
  }
  {  // SPI 0x38 ending after 3 quad edges
    Drv d;
    d.psram_reset_spi();
    d.select(CSR);
    d.send_single(0x38, 8);
    d.send_quad(0x000100, 6);
    d.send_quad(0xABC, 3);
    d.end();
    REQUIRE_ERR(d, "mid-byte");
  }
  {  // a write ending inside its address field
    Drv d;
    d.psram_reset_spi();
    d.select(CSR);
    d.send_single(0x02, 8);
    d.send_single(0x01, 12);
    d.end();
    REQUIRE_ERR(d, "mid-byte");
  }
  {  // flash 0x02 ending mid-byte
    Drv d;
    d.select(CSF);
    d.send_single(0x02, 8);
    d.send_single(0x000100, 24);
    d.send_single(0x5, 3);
    d.end();
    REQUIRE_ERR(d, "mid-byte");
  }
  return "";
}

std::string test_command_legality() {
  {  // flash 0xFF takes no fields
    Drv d;
    d.select(CSF);
    d.send_single(0xFF, 8);
    d.clk(1, SD0);
    REQUIRE_ERR(d, "no fields");
  }
  {  // PSRAM 0x35 takes no fields
    Drv d;
    d.select(CSR);
    d.send_single(0x35, 8);
    d.clk(1, SD0);
    REQUIRE_ERR(d, "no fields");
  }
  {  // 0x99 without 0x66 just before
    Drv d;
    d.select(CSR);
    d.send_single(0x99, 8);
    REQUIRE_ERR(d, "0x99");
  }
  {  // 0x66 followed by something other than 0x99
    Drv d;
    d.select(CSR);
    d.send_single(0x66, 8);
    d.end();
    d.select(CSR);
    d.send_single(0x35, 8);
    REQUIRE_ERR(d, "0x66");
  }
  {  // wrap toggling (0xC0) is not allowed
    Drv d;
    d.psram_reset_spi();
    d.select(CSR);
    d.send_single(0xC0, 8);
    REQUIRE_ERR(d, "illegal command");
  }
  {  // 0x03 is not a QPI command
    Drv d;
    d.psram_reset_spi();
    d.select(CSR);
    d.send_single(0x35, 8);
    d.end();
    d.select(CSR);
    d.send_quad(0x03, 2);
    REQUIRE_ERR(d, "illegal command");
  }
  {  // a full 8-edge 0xF5 is not an SPI-mode command
    Drv d;
    d.select(CSR);
    d.send_single(0xF5, 8);
    REQUIRE_ERR(d, "illegal command");
  }
  {  // QPI-form reset works from QPI mode
    Drv d(PmodInit{FlashMode::Standard, PsramMode::Qpi});
    d.select(CSR);
    d.send_quad(0x66, 2);
    d.end();
    d.select(CSR);
    d.send_quad(0x99, 2);
    d.end();
    d.idle(2);
    REQUIRE_OK(d);
    REQUIRE(d.bus->psram_initialized(), "QPI-form reset did not initialize");
    d.psram_write02(0x20, Bytes{0x42});  // SPI mode now
    Bytes got = d.psram_read03(0x20, 1);
    REQUIRE_OK(d);
    REQUIRE_BYTES(got, Bytes{0x42}, "SPI access after the QPI-form reset");
  }
  return "";
}

}  // namespace

int main() {
  const std::pair<const char*, std::function<std::string()>> tests[] = {
      {"test_flash_read03", test_flash_read03},
      {"test_flash_fastread_0b_6b_eb", test_flash_fastread_0b_6b_eb},
      {"test_flash_continuous", test_flash_continuous},
      {"test_flash_program_ignored", test_flash_program_ignored},
      {"test_psram_requires_reset", test_psram_requires_reset},
      {"test_psram_spi_rw", test_psram_spi_rw},
      {"test_psram_quad_and_qpi", test_psram_quad_and_qpi},
      {"test_reset_sequence_from_every_state", test_reset_sequence_from_every_state},
      {"test_trst", test_trst},
      {"test_tcem_limit", test_tcem_limit},
      {"test_contention", test_contention},
      {"test_waveform_rules", test_waveform_rules},
      {"test_partial_command_ignored", test_partial_command_ignored},
      {"test_max_sck", test_max_sck},
      {"test_write_byte_boundary", test_write_byte_boundary},
      {"test_command_legality", test_command_legality},
  };
  int failed = 0;
  for (const auto& tc : tests) {
    std::string why = tc.second();
    if (why.empty()) {
      std::printf("PASS %s\n", tc.first);
    } else {
      std::printf("FAIL %s: %s\n", tc.first, why.c_str());
      failed++;
    }
    std::fflush(stdout);
  }
  return failed;
}
