// 23LC512-compatible SPI RAM model (mode 0, READ 0x03 / WRITE 0x02, 16-bit address, sequential
// mode wrapping at 64 KB) and checker for the waveform rules of docs/contract.md section 2.
//
// Call edge() once after every rising clk edge t with the pin values after that edge
// (S = SCK, M = MOSI, C = CS_n). Then drive miso() until the next edge. Rules:
//   1. C(t) != C(t-1) requires S(t-1) = 0 and S(t) = 0.
//   2. A rising SCK edge (S(t-1) = 0, S(t) = 1, C = 0) samples MOSI; M(t) must equal M(t-1).
//   3. The memory drives response bit k on MISO from the rising SCK edge that clocks it until SCK
//      falls. Every other time MISO carries noise, so a core that samples it while SCK is low
//      reads garbage.
//   4. A transaction (C falling to C rising) carries an 8-bit command (0x03 or 0x02), a 16-bit
//      address, then data bits, MSB first. Data byte k is at (A + k) mod 2^16. A transaction
//      that ends before its command and address are complete is a violation.
//   5. A WRITE carries a positive multiple of 8 data bits, and every byte sent is written.
//   6. SCK phases are at least one cycle by construction (pins are sampled once per edge).
// Any violation stops the model: edge() returns false and error() says why.
#pragma once

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

struct SpiTxn {
  bool write = false;
  uint16_t addr = 0;
  uint32_t bits = 0;       // rising SCK edges in the transaction
  uint32_t data_bits = 0;  // bits after command and address
  // READ: every byte with at least one bit clocked (its value). WRITE: every complete byte.
  std::vector<uint8_t> bytes;
  uint64_t start = 0, end = 0;  // clk edges of the CS_n fall and rise

  // Address of data byte k.
  uint16_t at(size_t k) const { return (uint16_t)(addr + k); }
  // Number of READ bytes whose 8 bits were all clocked.
  uint32_t full_bytes() const { return data_bits / 8; }
};

class SpiRam {
 public:
  static constexpr uint32_t SIZE = 1u << 16;

  explicit SpiRam(uint64_t noise_seed) : rng_(noise_seed * 0x9E3779B97F4A7C15ull + 1) {
    std::memset(mem_, 0, sizeof mem_);
  }

  void load(const uint8_t* img) { std::memcpy(mem_, img, SIZE); }
  const uint8_t* mem() const { return mem_; }
  void poke(uint16_t a, uint8_t v) { mem_[a] = v; }

  bool edge(uint64_t t, bool cs_n, bool sck, bool mosi) {
    if (!err_.empty()) return false;
    if (!have_prev_) {
      have_prev_ = true;
      pc_ = cs_n;
      ps_ = sck;
      pm_ = mosi;
      return true;
    }
    if (cs_n != pc_ && (ps_ || sck))
      return fail(t, "CS_n changed while SCK was high (rule 1: CS_n changes only while SCK is low)");
    if (pc_ && !cs_n) {  // CS_n falls: a transaction starts
      open_ = true;
      cur_ = SpiTxn();
      cur_.start = t;
      cmd_ = 0;
      wbyte_ = 0;
    }
    if (!ps_ && sck && !cs_n) {  // rising SCK edge inside a transaction
      if (mosi != pm_)
        return fail(t, "MOSI changed on a rising SCK edge (rule 2: MOSI must be stable there)");
      if (!bit(t, mosi)) return false;
    }
    if (!pc_ && cs_n && open_) {  // CS_n rises: the transaction ends
      open_ = false;
      cur_.end = t;
      if (cur_.bits < 24) {
        char m[128];
        std::snprintf(m, sizeof m,
                      "transaction ended after %u bits, before its command and address were "
                      "complete (rule 4)",
                      cur_.bits);
        return fail(t, m);
      }
      if (cur_.write && (cur_.data_bits == 0 || cur_.data_bits % 8 != 0)) {
        char m[128];
        std::snprintf(m, sizeof m,
                      "WRITE ended mid-byte or without data: %u data bits (rule 5: a positive "
                      "multiple of 8)",
                      cur_.data_bits);
        return fail(t, m);
      }
      done_.push_back(cur_);
    }
    if (!sck) drive_ = false;  // MISO is only valid while SCK is high
    pc_ = cs_n;
    ps_ = sck;
    pm_ = mosi;
    return true;
  }

  // MISO until the next rising clk edge.
  bool miso() {
    if (drive_ && ps_ && !pc_) return drive_bit_;
    return noise() & 1;
  }
  // Noise for the other uio_in bits.
  uint8_t noise() {
    rng_ ^= rng_ << 13;
    rng_ ^= rng_ >> 7;
    rng_ ^= rng_ << 17;
    return (uint8_t)(rng_ >> 24);
  }

  bool cs_low() const { return !pc_; }
  bool in_txn() const { return open_; }
  const SpiTxn& current() const { return cur_; }
  // Set when a WRITE command is decoded (8th bit); the harness clears it.
  bool write_started = false;
  uint64_t write_started_at = 0;
  // Set when a WRITE's 16-bit address is complete (24th bit), with that address; the harness
  // clears it. It tells a region WRITE from a program WRITE before the transaction ends.
  bool write_addr_seen = false;
  uint16_t write_addr = 0;
  uint64_t write_addr_at = 0;

  std::vector<SpiTxn>& done() { return done_; }
  const std::string& error() const { return err_; }

 private:
  uint8_t mem_[SIZE];
  uint64_t rng_;
  bool have_prev_ = false;
  bool pc_ = true, ps_ = false, pm_ = false;
  bool open_ = false;
  SpiTxn cur_;
  uint8_t cmd_ = 0, wbyte_ = 0;
  bool drive_ = false, drive_bit_ = false;
  std::vector<SpiTxn> done_;
  std::string err_;

  bool fail(uint64_t t, const std::string& why) {
    char m[96];
    std::snprintf(m, sizeof m, "SPI protocol violation at cycle %llu: ", (unsigned long long)t);
    err_ = m + why;
    if (open_) {
      char d[128];
      std::snprintf(d, sizeof d, " [transaction from cycle %llu, %u bits, cmd 0x%02x, addr 0x%04x]",
                    (unsigned long long)cur_.start, cur_.bits, cmd_, cur_.addr);
      err_ += d;
    }
    return false;
  }

  bool bit(uint64_t t, bool b) {
    uint32_t n = cur_.bits++;
    if (n < 8) {
      cmd_ = (uint8_t)((cmd_ << 1) | b);
      if (n == 7) {
        if (cmd_ != 0x03 && cmd_ != 0x02) {
          char m[96];
          std::snprintf(m, sizeof m, "invalid command 0x%02x (rule 4: only READ 0x03 or WRITE 0x02)",
                        cmd_);
          return fail(t, m);
        }
        cur_.write = cmd_ == 0x02;
        if (cur_.write) {
          write_started = true;
          write_started_at = t;
        }
      }
      return true;
    }
    if (n < 24) {
      cur_.addr = (uint16_t)((cur_.addr << 1) | b);
      if (n == 23 && cur_.write) {
        write_addr_seen = true;
        write_addr = cur_.addr;
        write_addr_at = t;
      }
      return true;
    }
    uint32_t d = n - 24, k = d / 8, pos = 7 - d % 8;
    cur_.data_bits++;
    uint16_t a = (uint16_t)(cur_.addr + k);
    if (!cur_.write) {
      drive_ = true;
      drive_bit_ = (mem_[a] >> pos) & 1;
      if (pos == 7) cur_.bytes.push_back(mem_[a]);
    } else {
      wbyte_ = (uint8_t)((wbyte_ << 1) | b);
      if (pos == 0) {
        mem_[a] = wbyte_;
        cur_.bytes.push_back(wbyte_);
      }
    }
    return true;
  }
};
