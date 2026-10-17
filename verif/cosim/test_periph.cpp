// Unit tests of the cosim peripheral pin models (periph_models.h). Each case prints
// "PASS name" or "FAIL name: why"; test_cosim_harness.py runs this binary (build.sh with no
// arguments builds it) and checks the lines. EDGES lines report UART RX bit edges, which the
// pytest recomputes from the contract formula independently.
#include <cstdio>
#include <string>
#include <vector>

#include "periph_models.h"

using namespace periph;

namespace {

int fails = 0;

void report(const char* name, const std::string& why) {
  if (why.empty()) {
    std::printf("PASS %s\n", name);
  } else {
    std::printf("FAIL %s: %s\n", name, why.c_str());
    fails++;
  }
}

// ---- UART RX driver: bit edges at round(k (div+1) (1 + ppm/1e6)), queueing --------------------
std::string uart_rx_edges() {
  for (uint32_t div : {15u, 16u, 39u, 100u}) {
    for (int32_t ppm : {0, 10000, -10000}) {
      UartTerm u(div, ppm);
      const uint64_t s = 1000;
      u.rx_queue(s, 0x55);  // alternating bits: a transition on every bit edge 0..9
      std::vector<uint64_t> tr;
      bool prev = true;
      for (uint64_t t = 0; t < s + 20 * (div + 1); t++) {
        bool l = u.rx_level(t);
        if (l != prev) tr.push_back(t - s);
        prev = l;
      }
      std::printf("EDGES uart_rx %u %d:", div, ppm);
      for (uint64_t e : tr) std::printf(" %llu", (unsigned long long)e);
      std::printf("\n");
      if (tr.size() != 10) return pfmt("div %u ppm %d: %zu transitions, want 10", div, ppm, tr.size());
      for (unsigned k = 0; k < 10; k++)
        if (tr[k] != UartTerm::rx_edge(div, ppm, k))
          return pfmt("div %u ppm %d: edge %u at %llu, want %llu", div, ppm, k,
                      (unsigned long long)tr[k], (unsigned long long)UartTerm::rx_edge(div, ppm, k));
      if (!u.rx_idle() || u.rx_sent() != 1) return "the frame did not complete";
    }
  }
  // the formula itself, spot checks: 40 x 1.01 = 40.4 per bit, 40 x 0.99 = 39.6
  if (UartTerm::rx_edge(39, 10000, 10) != 404 || UartTerm::rx_edge(39, -10000, 10) != 396 ||
      UartTerm::rx_edge(39, 10000, 3) != 121 || UartTerm::rx_edge(39, -10000, 3) != 119 ||
      UartTerm::rx_edge(16, 0, 7) != 119)
    return "rx_edge spot values";
  return "";
}

std::string uart_rx_back_to_back() {
  // Two bytes queued for the same cycle: the second starts exactly when the first's stop bit
  // ends (the overrun case: a second byte while RX_FULL is still set). Per-byte rates.
  UartTerm u(39, 0);
  u.rx_queue(500, 0xA5, 10000);
  u.rx_queue(500, 0x0F, -10000);
  const uint64_t s2 = 500 + UartTerm::rx_edge(39, 10000, 10);
  std::vector<int> lv;
  for (uint64_t t = 0; t < s2 + 500; t++) lv.push_back(u.rx_level(t));
  if (lv[499] != 1 || lv[500] != 0) return "first start bit";
  if (lv[s2 - 1] != 1) return "stop bit of the first byte";
  if (lv[s2] != 0) return pfmt("second start bit not at %llu", (unsigned long long)s2);
  // second byte 0x0F LSB first: bits 1,1,1,1,0,0,0,0
  for (unsigned k = 1; k <= 8; k++) {
    const uint64_t t = s2 + UartTerm::rx_edge(39, -10000, k);
    if (lv[t] != (int)((0x0F >> (k - 1)) & 1)) return pfmt("second byte bit %u", k - 1);
  }
  if (lv[s2 + UartTerm::rx_edge(39, -10000, 9)] != 1) return "second stop bit";
  if (u.rx_sent() != 2 || !u.rx_idle()) return "both frames sent";
  return "";
}

// ---- UART TX decoder: exact bit times ---------------------------------------------------------
std::vector<int> tx_wave(uint8_t byte, uint32_t bit, uint64_t idle_before, uint64_t idle_after) {
  std::vector<int> w(idle_before, 1);
  int bits[10] = {0};
  for (int k = 0; k < 8; k++) bits[k + 1] = (byte >> k) & 1;
  bits[9] = 1;
  for (int k = 0; k < 10; k++)
    for (uint32_t i = 0; i < bit; i++) w.push_back(bits[k]);
  for (uint64_t i = 0; i < idle_after; i++) w.push_back(1);
  return w;
}

std::string uart_tx_exact() {
  const std::vector<int> w = tx_wave('U', 17, 30, 40);
  {
    UartTerm u(16);
    for (uint64_t t = 0; t < w.size(); t++)
      if (!u.tx_step(t, w[t], true)) return "div 16: " + u.error();
    if (!u.tx_finish(w.size())) return u.error();
    if (u.tx_text() != "U") return "div 16 decoded '" + u.tx_text() + "'";
    if (u.tx_frames()[0].t0 != 30 || u.tx_frames()[0].end != 30 + 170) return "frame times";
  }
  {
    UartTerm u(15);
    bool ok = true;
    for (uint64_t t = 0; t < w.size() && ok; t++) ok = u.tx_step(t, w[t], true);
    if (ok) return "div 15 accepted a 17-cycle bit time";
    if (u.error().find("not on a bit boundary") == std::string::npos) return u.error();
  }
  {
    UartTerm u(17);  // a bit time one cycle too long fails too
    bool ok = true;
    for (uint64_t t = 0; t < w.size() && ok; t++) ok = u.tx_step(t, w[t], true);
    if (ok) return "div 17 accepted a 17-cycle bit time";
  }
  // two frames back to back, 0x00 and 0xFF (few transitions)
  {
    std::vector<int> a = tx_wave(0x00, 20, 5, 0), b = tx_wave(0xFF, 20, 0, 30);
    a.insert(a.end(), b.begin(), b.end());
    UartTerm u(19);
    for (uint64_t t = 0; t < a.size(); t++)
      if (!u.tx_step(t, a[t], true)) return "back to back: " + u.error();
    if (u.tx_text() != std::string("\x00\xff", 2)) return "back to back bytes";
  }
  // 0x00 at the wrong bit time: the stop bit's rising edge is off the boundary
  {
    std::vector<int> a = tx_wave(0x00, 20, 5, 30);
    UartTerm u(18);
    bool ok = true;
    for (uint64_t t = 0; t < a.size() && ok; t++) ok = u.tx_step(t, a[t], true);
    if (ok) return "0x00 at bit time 20 accepted with div 18";
  }
  return "";
}

std::string uart_tx_framing() {
  // stop bit 0, then the line rises later: framing error
  std::vector<int> w = tx_wave(0x41, 16, 10, 0);
  for (int i = 0; i < 16; i++) w[w.size() - 1 - i] = 0;  // stop bit low
  std::vector<int> held = w;
  for (int i = 0; i < 50; i++) w.push_back(0);
  for (int i = 0; i < 20; i++) w.push_back(1);
  {
    UartTerm u(15);
    bool ok = true;
    for (uint64_t t = 0; t < w.size() && ok; t++) ok = u.tx_step(t, w[t], true);
    if (ok || u.error().find("framing") == std::string::npos) return "framing error expected";
  }
  // stop bit 0 and the line stays low to the end (the pin back at GPIO level 0): no byte, no error
  for (int i = 0; i < 100; i++) held.push_back(0);
  {
    UartTerm u(15);
    for (uint64_t t = 0; t < held.size(); t++)
      if (!u.tx_step(t, held[t], true)) return "held low: " + u.error();
    if (!u.tx_finish(held.size()) || !u.tx_frames().empty()) return "held low gave a byte";
  }
  // lenient (pins mode, ALTFN unknown): stop bit 0, then the line rises: no frame, no error,
  // and the next frame decodes
  {
    std::vector<int> a = w;  // the framing-error wave above, then 'B'
    std::vector<int> b = tx_wave('B', 16, 10, 10);
    a.insert(a.end(), b.begin(), b.end());
    UartTerm u(15);
    u.set_lenient(true);
    for (uint64_t t = 0; t < a.size(); t++)
      if (!u.tx_step(t, a[t], true)) return "lenient: " + u.error();
    if (u.tx_text() != "B" || u.tx_abandoned() != 1) return "lenient decoded '" + u.tx_text() + "'";
  }
  // soft (rvfi mode, a store clearing ALTFN[0] executing): a held-low stop bit or an off-boundary
  // change abandons the frame, even in strict mode
  {
    UartTerm u(15);
    u.set_strict(true);
    for (uint64_t t = 0; t < held.size(); t++)
      if (!u.tx_step(t, held[t], true, true)) return "soft stop: " + u.error();
    if (u.tx_abandoned() != 1 || !u.tx_frames().empty()) return "soft stop: not abandoned";
    std::vector<int> a = tx_wave('A', 16, 10, 0);
    a.resize(10 + 40);
    for (int i = 0; i < 7; i++) a.push_back(1 - a.back());  // toggles off the bit grid
    UartTerm v(15);
    v.set_strict(true);
    for (uint64_t t = 0; t < a.size(); t++)
      if (!v.tx_step(t, a[t], true, true)) return "soft boundary: " + v.error();
    if (v.tx_abandoned() < 1) return "soft boundary: not abandoned";
  }
  // strict (rvfi mode: ALTFN known): the same held-low stop bit is a framing error at once
  {
    UartTerm u(15);
    u.set_strict(true);
    bool ok = true;
    for (uint64_t t = 0; t < held.size() && ok; t++) ok = u.tx_step(t, held[t], true);
    if (ok || u.error().find("stop bit of 0") == std::string::npos)
      return "strict: held-low stop bit accepted";
  }
  // the line starts low (GPIO 0 before ALTFN): the decoder waits for idle high first
  {
    std::vector<int> a(40, 0);
    std::vector<int> b = tx_wave('A', 16, 10, 10);
    a.insert(a.end(), b.begin(), b.end());
    UartTerm u(15);
    for (uint64_t t = 0; t < a.size(); t++)
      if (!u.tx_step(t, a[t], true)) return "initial low: " + u.error();
    if (u.tx_text() != "A") return "initial low decoded '" + u.tx_text() + "'";
  }
  // a frame still running at the end of the run
  {
    std::vector<int> a = tx_wave('A', 16, 10, 0);
    a.resize(a.size() - 30);
    UartTerm u(15);
    for (uint64_t t = 0; t < a.size(); t++) u.tx_step(t, a[t], true);
    if (u.tx_finish(a.size())) return "unfinished frame accepted";
  }
  // gate off mid-frame: abandoned, no error
  {
    std::vector<int> a = tx_wave('A', 16, 10, 10);
    UartTerm u(15);
    for (uint64_t t = 0; t < a.size(); t++)
      if (!u.tx_step(t, a[t], t < 60)) return "gate: " + u.error();
    if (!u.tx_frames().empty() || u.tx_abandoned() != 1) return "gate off mid-frame";
  }
  return "";
}

// ---- SPI echo device, mode 0 ------------------------------------------------------------------
struct SpiRun {
  std::vector<int> miso;  // MISO the design would sample at each edge
  std::vector<uint64_t> rises;
  std::string err;
};

// Master waveform: CS_n low from `start`, each byte 8 bits, SCK low `lo` then high `hi` cycles,
// MOSI set at the start of each low phase; mosi_glitch_at: flip MOSI at that cycle.
SpiRun spi_master(SpiDevice& dev, const std::vector<uint8_t>& bytes, uint32_t lo, uint32_t hi,
                  uint64_t mosi_glitch_at = 0) {
  struct Pin {
    int cs, sck, mosi;
  };
  std::vector<Pin> w(10, Pin{1, 0, 0});
  for (int i = 0; i < 4; i++) w.push_back(Pin{0, 0, 0});
  for (uint8_t b : bytes) {
    for (int k = 7; k >= 0; k--) {
      int m = (b >> k) & 1;
      for (uint32_t i = 0; i < lo; i++) w.push_back(Pin{0, 0, m});
      for (uint32_t i = 0; i < hi; i++) w.push_back(Pin{0, 1, m});
    }
    for (int i = 0; i < 6; i++) w.push_back(Pin{0, 0, w.back().mosi});
  }
  for (int i = 0; i < 6; i++) w.push_back(Pin{1, 0, 0});
  if (mosi_glitch_at) w[mosi_glitch_at].mosi ^= 1;
  SpiRun r;
  r.miso.push_back(1);
  for (uint64_t t = 0; t < w.size(); t++) {
    if (!dev.step(t, w[t].cs, w[t].sck, w[t].mosi, true) && r.err.empty()) r.err = dev.error();
    if (dev.rose()) r.rises.push_back(t);
    r.miso.push_back(dev.miso(t + 1, false));
  }
  return r;
}

std::string spi_device_mode0() {
  for (uint32_t d : {1u, 2u, 3u}) {
    const uint32_t h = 1u << d;
    SpiDevice dev(d);
    const std::vector<uint8_t> bytes = {0x3C, 0xA5, 0x00, 0x5A};
    SpiRun r = spi_master(dev, bytes, h, h);
    if (!r.err.empty()) return pfmt("D=%u: %s", d, r.err.c_str());
    if (r.rises.size() != 32) return pfmt("D=%u: %zu rising edges", d, r.rises.size());
    // the reply to byte i is byte i-1 (0xFF first); each bit is the MISO pin value at the rising
    // edge and the 3 cycles after it
    for (size_t i = 0; i < bytes.size(); i++) {
      const uint8_t want = i ? bytes[i - 1] : 0xFF;
      for (int k = 0; k < 8; k++) {
        const uint64_t e = r.rises[8 * i + k];
        const int bit = (want >> (7 - k)) & 1;
        for (uint64_t x = e; x <= e + 3; x++)
          if (r.miso[x] != bit)
            return pfmt("D=%u byte %zu bit %d: MISO %d at edge %llu (rise %llu), want %d", d, i, k,
                        r.miso[x], (unsigned long long)x, (unsigned long long)e, bit);
      }
    }
    if (dev.xfers().size() != 4) return pfmt("D=%u: %zu transfers", d, dev.xfers().size());
    for (size_t i = 0; i < 4; i++) {
      if (dev.xfers()[i].mosi != bytes[i]) return pfmt("D=%u: transfer %zu MOSI byte", d, i);
      if (dev.xfers()[i].miso != (i ? bytes[i - 1] : 0xFF)) return pfmt("D=%u: MISO byte", d);
      if (dev.xfers()[i].last_fall != r.rises[8 * i + 7] + h) return "last fall";
    }
  }
  return "";
}

std::string spi_device_errors() {
  {  // MOSI changes while SCK is high
    SpiDevice dev(1);
    // first byte's first high phase: 10 idle + 4 CS + 2 low = 16, 17 high
    SpiRun r = spi_master(dev, {0x3C}, 2, 2, 17);
    if (r.err.find("MOSI (uo[6]) changed") == std::string::npos) return "glitch: " + r.err;
  }
  {  // MOSI changes exactly at the rising edge
    SpiDevice dev(1);
    SpiRun r = spi_master(dev, {0x3C}, 2, 2, 16);
    std::string e = r.err;
    // the glitch at 16 makes cycle 16 differ from 15 and 17: the change at 17 is at SCK high
    if (e.find("MOSI (uo[6]) changed") == std::string::npos) return "edge: " + e;
  }
  {  // high phase too long
    SpiDevice dev(1);
    SpiRun r = spi_master(dev, {0x3C}, 2, 3);
    if (r.err.find("high phase of 3 cycles") == std::string::npos) return "high: " + r.err;
  }
  {  // low phase too long
    SpiDevice dev(1);
    SpiRun r = spi_master(dev, {0x3C}, 3, 2);
    if (r.err.find("low phase of 3 cycles") == std::string::npos) return "low: " + r.err;
  }
  return "";
}

// ---- PWM meter --------------------------------------------------------------------------------
std::string run_pwm(uint64_t period, uint64_t high, uint64_t wave_period, uint64_t wave_high,
                    uint64_t phase, bool glitch_in_settle) {
  PwmMeter m("pwm", period, high, 100);
  for (uint64_t t = 0; t < 100 + 6 * period + 10; t++) {
    bool l = wave_period ? ((t + phase) % wave_period) < wave_high : false;
    if (wave_high >= wave_period && wave_period) l = true;
    if (glitch_in_settle && t == 100 + period / 2) l = !l;
    if (!m.step(t, l)) return m.error();
    if (m.done()) return "";
  }
  return "measurement did not finish";
}

std::string pwm_meter() {
  // PER = 99, Q = 1: period 200; duty 50 -> high 100; duty 120 > PER -> always 1; duty 0 -> 0
  const uint64_t P = 100 * 2;
  std::string e;
  if (!(e = run_pwm(P, 50 * 2, P, 100, 37, false)).empty()) return "duty 50: " + e;
  if (!(e = run_pwm(P, 50 * 2, P, 100, 3, true)).empty()) return "settling glitch: " + e;
  if (!(e = run_pwm(P, 0, P, 0, 0, false)).empty()) return "duty 0: " + e;
  if (!(e = run_pwm(P, std::min<uint64_t>(120, 100) * 2, P, P, 0, false)).empty())
    return "duty 120: " + e;
  if (run_pwm(P, 100, P, 101, 0, false).find("high 101") == std::string::npos)
    return "high 101 accepted";
  // period 202: fails as soon as no rising edge came 200 cycles after the previous one
  if (run_pwm(P, 100, P + 2, 100, 0, false).find("no rising edge 201 cycles") ==
      std::string::npos)
    return "period 202 accepted";
  if (run_pwm(P, 0, P, 1, 0, false).empty()) return "duty 0 with pulses accepted";
  // timer in PWM mode, P = 0, CMP = 300: high 300 of every 65,536 cycles
  if (!(e = run_pwm(65536, 300, 65536, 300, 1234, false)).empty()) return "timer: " + e;
  return "";
}

std::string gpio_stim_and_loop() {
  GpioStim g({{50, 0x0F}, {10, 0x01}, {100, 0xF0}});
  if (g.value(0) != UI_IDLE || UI_IDLE != 0x02 || g.value(9) != 0x02 || g.value(10) != 0x01 ||
      g.value(49) != 0x01 || g.value(50) != 0x0F ||
      g.value(150) != 0xF0 || !g.finished())
    return "script values";
  if (TimerLoop::ui0(0x80) != 1 || TimerLoop::ui0(0x7F) != 0) return "loop";
  return "";
}

}  // namespace

int main() {
  report("uart_rx_edges", uart_rx_edges());
  report("uart_rx_back_to_back", uart_rx_back_to_back());
  report("uart_tx_exact", uart_tx_exact());
  report("uart_tx_framing", uart_tx_framing());
  report("spi_device_mode0", spi_device_mode0());
  report("spi_device_errors", spi_device_errors());
  report("pwm_meter", pwm_meter());
  report("gpio_stim_and_loop", gpio_stim_and_loop());
  return fails ? 1 : 0;
}
