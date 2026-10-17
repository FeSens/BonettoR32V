// Seed unit-test model of TT's QSPI Pmod (docs/contract-v3.md section 2). Not a contract gate:
// the gates' PmodBus (verif/cosim/pmod.h, Task 1) replaces it. It covers what the seed uses:
//   flash (W25Q128JV) on uio[0]: 0x03 read, 0x02 program (ignored), 0xFF mode reset, and the
//     continuous-read exit (8 quad edges with M[5:4] != 10);
//   PSRAM A (APS6404L) on uio[6]: 0x03 read, 0x02 write, 0x66 + 0x99 reset, QPI 0xF5 exit, and
//     ignored partial commands; reads and writes before the reset fail.
// Every rising clk edge: edge(t, uio_out, uio_oe) with the design's outputs after that edge.
// Rules checked (2.1): fixed oe/out bits, one CS_n low at most, CS_n changes only with SCK low
// before and after, SCK low while both CS_n high, driven SD lines stable across rising SCK edges,
// no contention with a driving memory, PSRAM CS_n low <= tCEM, tRST after 0x99, write data on
// byte boundaries.
#pragma once

#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

namespace v3 {

enum class FlashMode { Standard, Continuous };
enum class PsramMode { SpiUninit, Spi, Qpi };

struct PsramWrite {
  uint64_t t;
  uint32_t addr;
  uint8_t data;
};

class Pmod {
 public:
  Pmod(uint8_t* flash, uint8_t* psram, FlashMode fm, PsramMode pm, double clk_ns, uint32_t seed)
      : flash_(flash), psram_(psram), fmode_(fm), pmode_(pm), rng_(seed * 0x9E3779B97F4A7C15ull + 1) {
    tcem_ = (uint64_t)(8000.0 / clk_ns);
  }

  std::string err;
  std::vector<PsramWrite> writes;   // drained by the harness
  uint64_t flash_bytes = 0, psram_bytes = 0, txns = 0;

  PsramMode psram_mode() const { return pmode_; }
  FlashMode flash_mode() const { return fmode_; }

  // The byte on uio_in for the next cycle.
  uint8_t uio_in(uint8_t uio_out, uint8_t uio_oe) {
    uint8_t v = (uint8_t)rnd();
    v = (uint8_t)((v & ~uio_oe) | (uio_out & uio_oe));
    if (drive_) v = (uint8_t)((v & ~0x04) | (out_bit_ ? 0x04 : 0));
    return v;
  }

  void edge(uint64_t t, uint8_t out, uint8_t oe) {
    if (!err.empty()) return;
    t_ = t;
    if ((oe & 0xC9) != 0xC9) return fail("uio_oe[0], [3], [6] or [7] is 0");
    if (!(out & 0x80)) return fail("uio_out[7] (PSRAM B CS_n) is 0");
    int cf = out & 1, cr = (out >> 6) & 1, s = (out >> 3) & 1;
    if (!cf && !cr) return fail("both CS_n low");
    if (cf && cr && s) return fail("SCK high with both CS_n high");
    {  // the previous state starts as reset: both CS_n high, SCK low (the harness checks it)
      if ((cf != pcf_ || cr != pcr_) && (ps_ || s)) return fail("CS_n changed while SCK high");
      // transaction ends
      if (!pcf_ && cf) end_txn(0);
      if (!pcr_ && cr) end_txn(1);
      // transaction starts
      if (pcf_ && !cf) begin_txn(0);
      if (pcr_ && !cr) {
        if (t < trst_until_) return fail("PSRAM command within tRST of 0x99");
        begin_txn(1);
      }
      if (!cr && t - cs_low_t_ + 1 > tcem_ && chip_ == 1) return fail("tCEM: PSRAM CS_n low too long");
      bool sel = !cf || !cr;
      if (ps_ && !s) drive_ = false;  // SCK fell: memory outputs end
      if (sel && !ps_ && s) {         // rising SCK edge
        uint8_t d = (uint8_t)((out ^ pout_) & oe & poe_ & 0x36);
        if (d) return fail("driven SD line changed across a rising SCK edge");
        rising(out, oe);
        if (!err.empty()) return;
        if (drive_ && (oe & 0x04)) return fail("contention on SD1 (core drives during memory data-out)");
      }
    }
    ps_ = s;
    pcf_ = cf;
    pcr_ = cr;
    pout_ = out;
    poe_ = oe;
  }

 private:
  uint8_t* flash_;
  uint8_t* psram_;
  FlashMode fmode_;
  PsramMode pmode_;
  uint64_t rng_;
  uint64_t tcem_;
  uint64_t t_ = 0;
  int ps_ = 0, pcf_ = 1, pcr_ = 1;
  uint8_t pout_ = 0, poe_ = 0;
  // transaction
  int chip_ = -1;
  int edges_ = 0;
  uint32_t cmd_ = 0, addr_ = 0;
  uint8_t wbyte_ = 0;
  uint64_t cs_low_t_ = 0;
  bool drive_ = false;
  int out_bit_ = 0;
  bool quad_cmd_ = false;      // PSRAM in QPI: command nibbles
  bool cont_ = false;          // flash continuous read transaction
  uint32_t qaddr_ = 0;         // quad bits clocked in continuous read
  uint32_t last_psram_cmd_ = 0xFFFF;  // previous complete PSRAM command (0x66 before 0x99)
  uint64_t trst_until_ = 0;

  uint64_t rnd() {
    rng_ ^= rng_ << 13;
    rng_ ^= rng_ >> 7;
    rng_ ^= rng_ << 17;
    return rng_ >> 11;
  }
  void fail(const std::string& m) {
    if (err.empty()) {
      char b[96];
      snprintf(b, sizeof b, " (cycle %llu)", (unsigned long long)t_);
      err = m + b;
    }
  }
  void begin_txn(int c) {
    chip_ = c;
    edges_ = 0;
    cmd_ = addr_ = 0;
    drive_ = false;
    cs_low_t_ = t_;
    quad_cmd_ = (c == 1 && pmode_ == PsramMode::Qpi);
    cont_ = (c == 0 && fmode_ == FlashMode::Continuous);
    qaddr_ = 0;
    txns++;
  }
  void end_txn(int c) {
    drive_ = false;
    if (c != chip_) return fail("model: transaction bookkeeping");
    if (c == 0) {
      if (cont_) {
        if (edges_ >= 8) {
          uint8_t m = (uint8_t)(qaddr_ & 0xFF);
          if (((m >> 4) & 3) != 2) fmode_ = FlashMode::Standard;
          if (edges_ > 8) fail("model: flash continuous read with a data phase (unsupported)");
        }
      } else if (edges_ >= 8) {
        int db = edges_ - 32;
        if (cmd_ == 0x02 && edges_ > 32 && db % 8) fail("flash 0x02 not on a byte boundary");
        if (cmd_ == 0xFF && edges_ != 8) fail("flash 0xFF with extra edges");
      }
    } else {
      if (quad_cmd_) {
        if (edges_ >= 2) {
          if (cmd_ == 0xF5 && edges_ == 2) pmode_ = PsramMode::Spi;
          else fail("PSRAM QPI command other than a bare 0xF5");
        }
      } else if (edges_ >= 8) {
        if (cmd_ == 0x66) {
          if (edges_ != 8) fail("PSRAM 0x66 with extra edges");
        } else if (cmd_ == 0x99) {
          if (edges_ != 8) fail("PSRAM 0x99 with extra edges");
          if (last_psram_cmd_ != 0x66) fail("PSRAM 0x99 not preceded by 0x66");
          pmode_ = PsramMode::Spi;
          trst_until_ = t_ + 2;  // CS_n high for >= 2 cycles (tRST 50 ns at 25 ns)
        } else if (cmd_ == 0x02) {
          if (edges_ <= 32 || (edges_ - 32) % 8) fail("PSRAM write not on a byte boundary");
        }
        last_psram_cmd_ = cmd_;
      }
    }
    chip_ = -1;
  }
  void rising(uint8_t out, uint8_t oe) {
    (void)oe;
    int e = edges_++;
    int sd0 = (out >> 1) & 1;
    uint32_t nib = (uint32_t)(((out >> 5) & 1) << 3 | ((out >> 4) & 1) << 2 | ((out >> 2) & 1) << 1 | sd0);
    if (chip_ == 0) {
      if (cont_) {
        if (e < 8) qaddr_ = (qaddr_ << 4) | nib;
        return;
      }
      if (e < 8) {
        cmd_ = (cmd_ << 1) | sd0;
        if (e == 7 && cmd_ != 0x03 && cmd_ != 0x02 && cmd_ != 0xFF)
          fail("illegal flash command");
        return;
      }
      if (cmd_ == 0xFF) return;  // extra edges reported at the end
      if (e < 32) {
        addr_ = (addr_ << 1) | sd0;
        return;
      }
      int db = e - 32;
      uint32_t a = (addr_ + db / 8) & (FLASH_SIZE_ - 1);
      if (cmd_ == 0x03) {
        out_bit_ = (flash_[a] >> (7 - db % 8)) & 1;
        drive_ = true;
        if (db % 8 == 0) flash_bytes++;
      }
      return;  // 0x02: data ignored
    }
    // PSRAM
    if (quad_cmd_) {
      if (e < 2) cmd_ = (cmd_ << 4) | nib;
      else fail("PSRAM QPI transaction longer than a command");
      return;
    }
    if (e < 8) {
      cmd_ = (cmd_ << 1) | sd0;
      if (e == 7) {
        if (cmd_ == 0x03 || cmd_ == 0x02) {
          if (pmode_ != PsramMode::Spi) fail("PSRAM access before reset");
        } else if (cmd_ != 0x66 && cmd_ != 0x99) {
          fail("illegal PSRAM command");
        }
      }
      return;
    }
    if (cmd_ == 0x66 || cmd_ == 0x99) return;  // extra edges reported at the end
    if (e < 32) {
      addr_ = (addr_ << 1) | sd0;
      return;
    }
    int db = e - 32;
    uint32_t a = (addr_ + db / 8) & (PSRAM_SIZE_ - 1);
    if (cmd_ == 0x03) {
      out_bit_ = (psram_[a] >> (7 - db % 8)) & 1;
      drive_ = true;
      if (db % 8 == 0) psram_bytes++;
    } else {
      wbyte_ = (uint8_t)((wbyte_ << 1) | sd0);
      if (db % 8 == 7) {
        psram_[a] = wbyte_;
        writes.push_back({t_, a, wbyte_});
      }
    }
  }
  static constexpr uint32_t FLASH_SIZE_ = 1u << 24;
  static constexpr uint32_t PSRAM_SIZE_ = 1u << 23;
};

}  // namespace v3
