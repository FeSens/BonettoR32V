// Pin-level models of the devices around the MCU's uo/ui pins (contract v3 section 5), for the
// cosim harness (harness.cpp) and their unit tests (test_periph.cpp). Header-only, C++17.
//
// Time is the harness's clk edge count t. A model is fed the design's pins as they are right
// after rising edge t (step(t, ...)), and answers the level an input pin must have for the
// design's flops to sample at edge t + 1 (the harness drives ui_in at the falling edge between).
//
//   UartTerm   UART TX decoder on uo[0] (exact bit times: every line change inside a frame lies on
//              a bit boundary t0 + k(DIV+1)) and RX driver on ui[1] (8N1, LSB first; bit edges at
//              round(k (DIV+1) (1 + ppm / 10^6)) after the frame start, k = 0..10).
//   SpiDevice  an SPI mode-0 echo device on uo[4] CS_n, uo[5] SCK, uo[6] MOSI, ui[3] MISO: it
//              replies to each byte with the previous byte it received (0xFF before the first).
//              Also the transfer tracker: groups SCK rising edges into 8-bit transfers and checks
//              the master's waveform (MOSI stable while SCK is high, high and low phases exactly
//              2^D cycles).
//   PwmMeter   measures one output's period and high time after a settling period.
//   TimerLoop  ui[0] follows uo[7] (the IR loopback).
//   GpioStim   ui_in from a script of [cycle, value] pairs.
#pragma once

#include <algorithm>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <string>
#include <utility>
#include <vector>

namespace periph {

inline std::string pfmt(const char* f, ...) __attribute__((format(printf, 1, 2)));
inline std::string pfmt(const char* f, ...) {
  char b[512];
  va_list ap;
  va_start(ap, f);
  std::vsnprintf(b, sizeof b, f, ap);
  va_end(ap);
  return b;
}

// ---- UART (contract 5.5) ----------------------------------------------------------------------
struct UartFrame {
  uint64_t t0;   // edge after which the start bit is on the line (the line fell at t0)
  uint64_t end;  // t0 + 10 (DIV+1): the stop bit ends here
  uint8_t byte;
};

class UartTerm {
 public:
  explicit UartTerm(uint32_t div = 15, int32_t rate_ppm = 0) : div_(div), ppm_(rate_ppm) {}

  // ---- RX: the harness sends bytes to the design on ui[1] ----
  // Offset of bit edge k (0 = start bit, 9 = stop bit, 10 = end of the stop bit) from the frame
  // start: round(k (div+1) (1 + ppm / 10^6)), rounding halves up (exact integer arithmetic).
  static uint64_t rx_edge(uint32_t div, int32_t ppm, unsigned k) {
    const int64_t num = (int64_t)k * (int64_t)(div + 1) * (1000000 + (int64_t)ppm);
    return (uint64_t)((num + 500000) / 1000000);
  }
  // Queue a byte whose start bit begins at edge `at`, or when the previous frame's stop bit ends
  // if that is later. ppm: the bit-time offset for this byte.
  void rx_queue(uint64_t at, uint8_t byte, int32_t ppm) {
    uint64_t s = at;
    if (!rx_.empty()) s = std::max(s, rx_.back().start + rx_edge(div_, rx_.back().ppm, 10));
    rx_.push_back(Rx{s, byte, ppm});
  }
  void rx_queue(uint64_t at, uint8_t byte) { rx_queue(at, byte, ppm_); }
  // The RX line level the design samples at edge t (call with non-decreasing t). Idle is 1.
  bool rx_level(uint64_t t) {
    while (!rx_.empty() && t >= rx_.front().start + rx_edge(div_, rx_.front().ppm, 10)) {
      rx_sent_++;
      rx_.pop_front();
    }
    if (rx_.empty() || t < rx_.front().start) return true;
    const Rx& f = rx_.front();
    const uint64_t r = t - f.start;
    unsigned k = 0;
    while (k < 9 && rx_edge(div_, f.ppm, k + 1) <= r) k++;
    if (k == 0) return false;                 // start bit
    if (k <= 8) return (f.byte >> (k - 1)) & 1;  // data, LSB first
    return true;                              // stop bit
  }
  bool rx_idle() const { return rx_.empty(); }
  uint64_t rx_sent() const { return rx_sent_; }
  uint64_t rx_last_end() const {
    return rx_.empty() ? 0 : rx_.back().start + rx_edge(div_, rx_.back().ppm, 10);
  }

  // ---- TX: the design sends on uo[0] ----
  void set_div(uint32_t div) { div_ = div; }
  uint32_t div() const { return div_; }
  uint32_t bit_time() const { return div_ + 1; }
  // One cycle: the TX line after edge t. gate = the pin carries UART TX (ALTFN[0] = 1, or always
  // when the harness can't tell). Returns false on a timing or framing error (error()).
  // A frame: the line falls at t0 (after it was idle high); every change until t0 + 10B must lie
  // on t0 + kB; the bits are the levels after t0 + kB (k = 1..8 data, 9 stop). A stop bit read
  // as 0 is a framing error, unless the line then stays low to the end of the run (the pin went
  // back to a GPIO level of 0, or a break): such a "frame" is no byte.
  bool tx_step(uint64_t t, bool line, bool gate) {
    started_ = false;
    completed_ = false;
    if (!err_.empty()) return false;
    if (!gate) {
      if (in_frame_) abandoned_++;
      in_frame_ = held_low_ = armed_ = false;
      return true;
    }
    if (held_low_) {
      if (line)
        return fail(pfmt("UART TX framing error: the frame that started at cycle %llu has a stop "
                         "bit of 0 (the line was low at cycle %llu), then the line rose at cycle "
                         "%llu",
                         (unsigned long long)t0_, (unsigned long long)(t0_ + 9 * bit_time()),
                         (unsigned long long)t));
      return true;
    }
    if (!in_frame_) {
      if (!armed_) {
        armed_ = line;
      } else if (!line) {
        begin(t);
      }
      prev_ = line;
      return true;
    }
    const uint64_t B = bit_time(), r = t - t0_;
    if (line != prev_ && r % B != 0 && r < 10 * B)
      return fail(pfmt("UART TX: uo[0] changed %llu cycles into the frame that started at cycle "
                       "%llu, not on a bit boundary (bit time DIV+1 = %llu cycles, contract 5.5)",
                       (unsigned long long)r, (unsigned long long)t0_, (unsigned long long)B));
    if (r % B == 0 && r >= B && r <= 8 * B) cur_ |= (uint8_t)((line ? 1 : 0) << (r / B - 1));
    if (r == 9 * B && !line) {
      held_low_ = true;
      in_frame_ = false;
      return true;
    }
    if (r == 10 * B) {
      frames_.push_back(UartFrame{t0_, t, cur_});
      completed_ = true;
      in_frame_ = false;
      if (!line) begin(t);  // the next start bit right after the stop bit
    }
    prev_ = line;
    return true;
  }
  // End of the run: a frame still in progress is an error.
  bool tx_finish(uint64_t t) {
    if (!err_.empty()) return false;
    if (in_frame_)
      return fail(pfmt("UART TX: the frame that started at cycle %llu was still in progress at "
                       "the end of the run (cycle %llu)",
                       (unsigned long long)t0_, (unsigned long long)t));
    return true;
  }
  bool tx_in_frame() const { return in_frame_; }
  uint64_t tx_t0() const { return t0_; }
  bool tx_started() const { return started_; }      // a frame started at the last tx_step
  bool tx_completed() const { return completed_; }  // a frame completed at the last tx_step
  uint64_t tx_abandoned() const { return abandoned_; }
  const std::vector<UartFrame>& tx_frames() const { return frames_; }
  std::string tx_text() const {
    std::string s;
    for (const UartFrame& f : frames_) s += (char)f.byte;
    return s;
  }
  const std::string& error() const { return err_; }

 private:
  struct Rx {
    uint64_t start;
    uint8_t byte;
    int32_t ppm;
  };
  uint32_t div_;
  int32_t ppm_;
  std::deque<Rx> rx_;
  uint64_t rx_sent_ = 0;
  bool armed_ = false, in_frame_ = false, held_low_ = false, prev_ = true;
  bool started_ = false, completed_ = false;
  uint64_t t0_ = 0, abandoned_ = 0;
  uint8_t cur_ = 0;
  std::vector<UartFrame> frames_;
  std::string err_;

  void begin(uint64_t t) {
    in_frame_ = true;
    started_ = true;
    t0_ = t;
    cur_ = 0;
  }
  bool fail(const std::string& w) {
    if (err_.empty()) err_ = w;
    return false;
  }
};

// ---- SPI master pins and an echo device (contract 5.6) ----------------------------------------
struct SpiXfer {
  uint64_t first_rise;  // edge of the first rising SCK edge
  uint64_t last_fall;   // edge where SCK fell after the eighth high phase: BUSY clears here
  uint8_t mosi;         // the byte the master sent (MSB first)
  uint8_t miso;         // the byte the device drove (meaningful while it was selected)
  bool selected;        // CS_n was low at all 8 rising edges
};

class SpiDevice {
 public:
  explicit SpiDevice(uint32_t d = 1, uint8_t first_reply = 0xFF) : d_(d), reply_(first_reply) {}
  void set_d(uint32_t d) { d_ = d; }
  uint32_t d() const { return d_; }
  uint32_t half() const { return 1u << d_; }

  // One cycle: uo[4] CS_n, uo[5] SCK, uo[6] MOSI after edge t. gate = the pins carry the SPI
  // master (ALTFN[4..6] = 1, or always when the harness can't tell). Returns false on a waveform
  // error.
  bool step(uint64_t t, bool cs_n, bool sck, bool mosi, bool gate) {
    rose_ = done_ = false;
    if (!err_.empty()) return false;
    if (!gate) {
      have_prev_ = seen_rise_ = false;
      xbits_ = 0;
      return true;
    }
    if (have_prev_) {
      // MOSI is set while SCK is low: it may not change at a rising SCK edge or while SCK is high
      if (sck && mosi != pmosi_)
        return fail(pfmt("SPI: MOSI (uo[6]) changed at cycle %llu while SCK (uo[5]) was high "
                         "(contract 5.6: MOSI is set while SCK is low)",
                         (unsigned long long)t));
      if (!pcs_ && cs_n) nbits_ = 0, pending_ = false;  // deselected: the device restarts
      if (!psck_ && sck) rise(t, cs_n, mosi);
      if (psck_ && !sck && seen_rise_) fall(t);
    }
    have_prev_ = true;
    pcs_ = cs_n;
    psck_ = sck;
    pmosi_ = mosi;
    return true;
  }
  // The MISO level the design samples at edge t (call after step(t - 1, ...)). The bit for the
  // next rising edge goes on the pin at the falling SCK edge, but no earlier than 4 cycles after
  // the previous rising edge, so the pin holds each bit from the rising edge through 3 cycles
  // after it (contract 5.6). Deselected: noise.
  bool miso(uint64_t t, bool noise) {
    if (pending_ && fell_ && t >= change_at_) pending_ = false, shown_ = next_level();
    if (pcs_) return noise;
    if (!pending_) shown_ = next_level();
    return shown_;
  }
  bool rose() const { return rose_; }   // a rising SCK edge at the last step
  bool done() const { return done_; }   // a transfer completed (its last fall) at the last step
  const std::vector<SpiXfer>& xfers() const { return xfers_; }
  bool in_xfer() const { return xbits_ > 0; }
  uint64_t xfer_first_rise() const { return xcur_.first_rise; }
  const std::string& error() const { return err_; }

 private:
  uint32_t d_;
  uint8_t reply_;                  // byte the device shifts out
  uint8_t rx_ = 0;                 // byte the device shifts in
  unsigned nbits_ = 0;             // device bits this byte (while selected)
  bool have_prev_ = false, seen_rise_ = false, pcs_ = true, psck_ = false, pmosi_ = false;
  bool pending_ = false, fell_ = false, shown_ = true, rose_ = false, done_ = false;
  uint64_t change_at_ = 0, last_rise_ = 0, last_fall_ = 0;
  // transfer tracker (master side, independent of CS_n)
  unsigned xbits_ = 0;
  SpiXfer xcur_{0, 0, 0, 0, false};
  std::vector<SpiXfer> xfers_;
  std::string err_;

  bool next_level() const { return (reply_ >> (7 - nbits_)) & 1; }
  bool fail(const std::string& w) {
    if (err_.empty()) err_ = w;
    return false;
  }
  void rise(uint64_t t, bool cs_n, bool mosi) {
    rose_ = true;
    if (xbits_ > 0 && t - last_fall_ != half())
      fail(pfmt("SPI: SCK low phase of %llu cycles before the rising edge at cycle %llu, "
                "contract 5.6 says 2^D = %u (D = %u)",
                (unsigned long long)(t - last_fall_), (unsigned long long)t, half(), d_));
    if (xbits_ == 0) xcur_ = SpiXfer{t, 0, 0, 0, true};
    if (cs_n) xcur_.selected = false;
    xcur_.mosi = (uint8_t)((xcur_.mosi << 1) | mosi);
    xcur_.miso = (uint8_t)((xcur_.miso << 1) | next_level());
    xbits_++;
    last_rise_ = t;
    seen_rise_ = true;
    if (!cs_n) {
      rx_ = (uint8_t)((rx_ << 1) | mosi);
      if (++nbits_ == 8) {
        nbits_ = 0;
        reply_ = rx_;  // echo: the next reply is the byte just received
      }
      pending_ = true;
      fell_ = false;
      change_at_ = t + 4;
    }
  }
  void fall(uint64_t t) {
    if (t - last_rise_ != half())
      fail(pfmt("SPI: SCK high phase of %llu cycles ending at cycle %llu, contract 5.6 says "
                "2^D = %u (D = %u)",
                (unsigned long long)(t - last_rise_), (unsigned long long)t, half(), d_));
    last_fall_ = t;
    if (pending_) {
      fell_ = true;
      change_at_ = std::max(change_at_, t + 1);
    }
    if (xbits_ == 8) {
      xcur_.last_fall = t;
      xfers_.push_back(xcur_);
      xbits_ = 0;
      done_ = true;
    }
  }
};

// ---- PWM and timer outputs (contract 5.3, 5.4) ------------------------------------------------
// After `start` (the 0xA5 marker), wait one full period, then measure exactly: the pin is
// constant for one period when high is 0 or the full period; otherwise from a rising edge to the
// next rising edge is `period` cycles, high for `high` of them.
class PwmMeter {
 public:
  PwmMeter(std::string name, uint64_t period, uint64_t high, uint64_t start)
      : name_(std::move(name)), period_(period), high_(std::min(high, period)), start_(start) {}

  bool step(uint64_t t, bool level) {
    if (done_ || !err_.empty()) return err_.empty();
    const bool rising = have_prev_ && !prev_ && level;
    const bool falling = have_prev_ && prev_ && !level;
    have_prev_ = true;
    prev_ = level;
    const uint64_t settle = start_ + period_;
    if (t < settle) return true;
    if (high_ == 0 || high_ == period_) {
      const bool want = high_ != 0;
      if (level != want)
        return fail(pfmt("%s: expected constant %d (high %llu of %llu cycles), saw %d at cycle "
                         "%llu",
                         name_.c_str(), want, (unsigned long long)high_,
                         (unsigned long long)period_, level, (unsigned long long)t));
      if (t >= settle + period_) done_ = true;
      return true;
    }
    if (!r1_) {
      if (rising) r1_ = t;
      else if (t > settle + 2 * period_)
        return fail(pfmt("%s: no rising edge in the two periods after settling (cycles %llu-%llu; "
                         "expected period %llu, high %llu)",
                         name_.c_str(), (unsigned long long)settle,
                         (unsigned long long)(settle + 2 * period_), (unsigned long long)period_,
                         (unsigned long long)high_));
      return true;
    }
    if (falling && !f_) f_ = t;
    if (rising) {
      const uint64_t p = t - r1_, h = f_ ? f_ - r1_ : 0;
      if (p != period_ || h != high_)
        return fail(pfmt("%s: measured period %llu, high %llu cycles (rising edges at %llu and "
                         "%llu); expected period %llu, high %llu",
                         name_.c_str(), (unsigned long long)p, (unsigned long long)h,
                         (unsigned long long)r1_, (unsigned long long)t,
                         (unsigned long long)period_, (unsigned long long)high_));
      done_ = true;
      return true;
    }
    if (t - r1_ > period_)
      return fail(pfmt("%s: no rising edge %llu cycles after the one at %llu (expected period "
                       "%llu, high %llu)",
                       name_.c_str(), (unsigned long long)(t - r1_), (unsigned long long)r1_,
                       (unsigned long long)period_, (unsigned long long)high_));
    return true;
  }
  bool done() const { return done_; }
  // The latest edge by which a correct output completes the measurement.
  uint64_t deadline() const { return start_ + 4 * period_ + 2; }
  const std::string& error() const { return err_; }
  const std::string& name() const { return name_; }

 private:
  std::string name_;
  uint64_t period_, high_, start_;
  bool have_prev_ = false, prev_ = false, done_ = false;
  uint64_t r1_ = 0, f_ = 0;
  std::string err_;

  bool fail(const std::string& w) {
    if (err_.empty()) err_ = w;
    return false;
  }
};

// ---- loopback and stimulus --------------------------------------------------------------------
// ui[0] follows uo[7]: the level the design samples at edge t + 1 is uo[7] after edge t.
struct TimerLoop {
  static bool ui0(uint8_t uo) { return (uo >> 7) & 1; }
};

// ui_in from a script: entry [c, v] drives v from relative cycle c on (0 before the first entry).
class GpioStim {
 public:
  explicit GpioStim(std::vector<std::pair<uint64_t, uint8_t>> script = {})
      : s_(std::move(script)) {
    std::stable_sort(s_.begin(), s_.end(),
                     [](const std::pair<uint64_t, uint8_t>& a,
                        const std::pair<uint64_t, uint8_t>& b) { return a.first < b.first; });
  }
  uint8_t value(uint64_t rel) {
    while (i_ < s_.size() && s_[i_].first <= rel) v_ = s_[i_++].second;
    return v_;
  }
  bool empty() const { return s_.empty(); }
  bool finished() const { return i_ >= s_.size(); }

 private:
  std::vector<std::pair<uint64_t, uint8_t>> s_;
  size_t i_ = 0;
  uint8_t v_ = 0;
};

}  // namespace periph
