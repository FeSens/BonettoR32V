// progsim: run a contract-v3 test program on the reference ISS (verif/cosim/iss3.h) with a timed
// model of the peripherals (docs/contract-v3.md section 5) and of the harness's stimulus
// (<name>.stim, written by mkexpect.py from stim.h). It checks the peripheral test programs
// before the RTL harness exists and at core speeds the seed doesn't have. It is not a gate: the
// cosim harness (Task 3) and the RTL decide.
//
// The modeled core retires one instruction every CPI cycles (fixed, or uniform in [A, B] per
// instruction). An instruction's IO access happens when it starts, and its register effects are
// visible to the next instruction (contract 5). An interrupt is taken at the first boundary where
// MIE, MEIE and the line are 1; WFI stalls until mip.MEIP & mie.MEIE. Inputs pass a 2-cycle
// synchronizer. The UART receiver samples mid-bit at the programmed DIV; the driver sends at
// (DIV + 1) * (1 + ppm / 1e6) cycles per bit. TX_BUSY reads 1 on the instruction after the
// starting write (contract 5.5). The SPI echo device returns the previous byte it received while
// CS_n was low (0xFF before that).
//
// Usage: progsim <prog.hex> [--stim FILE] [--cpi N | --cpi rand:A:B] [--max-cycles C] [--seed S]
//                [--first-retire N] [-v]
// .stim lines: "uart_div D", "uart_ppm P", "uart_rx CYCLE BYTE", "gpio CYCLE UI", "loop 1", "spi_echo 1".
// Cycles count from the first retirement. Without gpio lines ui_in is 0x02 (UART RX idle high).
// The last stdout line is JSON: end (selfloop | max | unspecified | region), retired, cycles,
// gpio_out, altfn, uart_tx, spi_tx, interrupts, uo_trace (GPIO-mode uo values, one per change).
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

#include "iss3.h"

namespace {

struct Stim {
  int ppm = 0, div = 39;
  std::vector<std::pair<uint64_t, int>> uart_rx, gpio;
  bool loop = false, spi_echo = false;
};

bool load_stim(const char* path, Stim& s) {
  FILE* f = std::fopen(path, "r");
  if (!f) return false;
  char key[32];
  unsigned long long a;
  long b;
  char line[256];
  while (std::fgets(line, sizeof line, f)) {
    int n = std::sscanf(line, "%31s %llu %ld", key, &a, &b);
    if (n < 2) continue;
    std::string k = key;
    if (k == "uart_div") s.div = (int)a;
    else if (k == "uart_ppm") s.ppm = (int)(long long)std::strtoll(line + 9, nullptr, 10);
    else if (k == "uart_rx" && n == 3) s.uart_rx.push_back({a, (int)b});
    else if (k == "gpio" && n == 3) s.gpio.push_back({a, (int)b});
    else if (k == "loop") s.loop = a != 0;
    else if (k == "spi_echo") s.spi_echo = a != 0;
  }
  std::fclose(f);
  return true;
}

struct Model {
  const Stim& st;
  uint64_t first = 0;  // cycle of the first retirement
  // registers
  uint32_t gpio_out = 0, altfn = 0, cnt = 0, cmp = 0, cap = 0, tctrl = 0, pwm_ctrl = 0,
           duty[3] = {0, 0, 0}, uart_div = 0, spi_ctrl = 1, irq_en = 0, gpio_edge = 0, epend = 0;
  bool p_cmp = false, p_cap = false;
  // timer
  uint32_t presc = 0;
  bool tout = false;
  // UART
  bool tx_busy = false, sticky_now = false, sticky_next = false;
  uint64_t tx_end = 0;
  bool rx_full = false, rx_ovr = false;
  uint8_t rx_data = 0;
  bool rx_active = false;
  uint64_t rx_start = 0;
  int rx_bit = 0;
  uint8_t rx_shift = 0;
  std::string uart_tx;
  // SPI
  bool spi_busy = false;
  uint64_t spi_end = 0;
  uint8_t spi_rx = 0, spi_cur = 0, spi_prev = 0xFF;
  bool spi_cs_at_start = false;
  std::vector<int> spi_tx;
  // inputs
  uint8_t s1 = 0x02, s2 = 0x02, ui_s = 0x02, ui_prev = 0x02;
  size_t gpio_i = 0;
  uint8_t ui_script = 0x02;
  uint64_t now = 0;

  explicit Model(const Stim& s) : st(s) {}

  uint32_t irq_pend() const {
    return (p_cmp ? 1u : 0u) | (p_cap ? 2u : 0u) | (rx_full ? 4u : 0u) | (tx_busy ? 0u : 8u) |
           (epend ? 16u : 0u);
  }
  bool line() const { return (irq_pend() & irq_en & 0x1F) != 0; }
  bool uo7() const { return (altfn & 0x80) ? tout : ((gpio_out >> 7) & 1); }

  int uart_line(uint64_t t) const {
    if (st.uart_rx.empty() || t < first) return 1;
    const double bt = (st.div + 1) * (1.0 + st.ppm / 1e6);
    for (const auto& f : st.uart_rx) {
      const double s = (double)(first + f.first);
      if ((double)t < s) break;
      if ((double)t < s + 10 * bt) {
        const int k = (int)(((double)t - s) / bt);
        if (k == 0) return 0;
        if (k <= 8) return (f.second >> (k - 1)) & 1;
        return 1;
      }
    }
    return 1;
  }

  void tick() {
    const uint64_t t = now;
    // inputs
    while (gpio_i < st.gpio.size() && first + st.gpio[gpio_i].first <= t)
      ui_script = (uint8_t)st.gpio[gpio_i++].second;
    uint8_t ui = ui_script;
    if (!st.uart_rx.empty()) ui = (uint8_t)((ui & ~2) | (uart_line(t) << 1));
    if (st.loop) ui = (uint8_t)((ui & ~1) | (uo7() ? 1 : 0));
    s2 = s1;
    s1 = ui;
    ui_prev = ui_s;
    ui_s = s2;
    // GPIO edges and timer capture
    const uint8_t rise = (uint8_t)(~ui_prev & ui_s), fall = (uint8_t)(ui_prev & ~ui_s);
    const uint8_t en = (uint8_t)gpio_edge, pol = (uint8_t)(gpio_edge >> 8);
    epend |= (uint32_t)(en & ((rise & ~pol) | (fall & pol)));
    const uint32_t capmode = (tctrl >> 8) & 3;
    if (((capmode & 1) && (rise & 1)) || ((capmode & 2) && (fall & 1))) {
      cap = cnt;
      p_cap = true;
    }
    // UART receiver on synchronized ui1, mid-bit sampling at DIV + 1 cycles per bit
    const int rxl = (ui_s >> 1) & 1, rxp = (ui_prev >> 1) & 1;
    const uint64_t bt = uart_div + 1;
    if (!rx_active) {
      if (rxp && !rxl) {
        rx_active = true;
        rx_start = t;
        rx_bit = 1;
        rx_shift = 0;
      }
    } else if (t == rx_start + bt * (uint64_t)rx_bit + bt / 2) {
      if (rx_bit <= 8) {
        rx_shift = (uint8_t)(rx_shift | (rxl << (rx_bit - 1)));
        rx_bit++;
      } else {  // the stop bit
        if (rx_full) rx_ovr = true;
        else {
          rx_data = rx_shift;
          rx_full = true;
        }
        rx_active = false;
      }
    }
    // timer
    if (tctrl & 0x10) {
      if (++presc >= (1u << (tctrl & 0xF))) {
        presc = 0;
        if (cnt == cmp) {
          p_cmp = true;
          if (((tctrl >> 6) & 3) == 1) tout = !tout;
          cnt = (tctrl & 0x20) ? 0 : ((cnt + 1) & 0xFFFF);
        } else {
          cnt = (cnt + 1) & 0xFFFF;
        }
        if (((tctrl >> 6) & 3) == 2) tout = cnt < cmp;
      }
    }
    if (((tctrl >> 6) & 3) == 0) tout = false;
    // UART TX, SPI
    if (tx_busy && t >= tx_end) tx_busy = false;
    if (spi_busy && t >= spi_end) {
      spi_busy = false;
      if (st.spi_echo && spi_cs_at_start) {
        spi_rx = spi_prev;
        spi_prev = spi_cur;
      } else {
        spi_rx = 0xFF;
      }
    }
    now++;
  }
  void advance(uint64_t to) {
    while (now < to) tick();
  }

  uint32_t read(uint32_t slot, uint32_t iss_value) const {
    switch (slot) {
      case v3::SLOT_GPIO_IN: return ui_s;
      case v3::SLOT_TIMER_CNT: return cnt;
      case v3::SLOT_TIMER_CAP: return cap;
      case v3::SLOT_UART_DATA: return rx_data;
      case v3::SLOT_UART_CTRL:
        return uart_div | ((tx_busy || sticky_now) ? v3::UART_TX_BUSY : 0) |
               (rx_full ? v3::UART_RX_FULL : 0) | (rx_ovr ? v3::UART_RX_OVR : 0);
      case v3::SLOT_SPI_DATA: return spi_rx;
      case v3::SLOT_SPI_CTRL: return spi_ctrl | (spi_busy ? v3::SPI_BUSY : 0);
      case v3::SLOT_IRQ_PEND: return irq_pend();
      case v3::SLOT_GPIO_EPEND: return epend;
      case v3::VSLOT_MIP: return line() ? v3::MIP_MEIP : 0;
    }
    return iss_value;
  }

  void write(uint32_t slot, uint32_t v, uint32_t lanes) {
    const uint32_t bm = v3::lane_bits(lanes);
    auto upd = [&](uint32_t& r, uint32_t mask) { r = ((r & ~bm) | (v & bm)) & mask; };
    switch (slot) {
      case 0: upd(gpio_out, 0xFF); break;
      case 2: upd(altfn, 0xFF); break;
      case 3:
        upd(cnt, 0xFFFF);
        presc = 0;
        break;
      case 4: upd(cmp, 0xFFFF); break;
      case 6: {
        const uint32_t old = tctrl;
        upd(tctrl, 0x3FF);
        if (lanes & 1) tout = false;  // the mode was written: toggle mode starts at 0
        if (!(old & 0x10) && (tctrl & 0x10)) presc = 0;
        if (((tctrl >> 6) & 3) == 2) tout = cnt < cmp;
        break;
      }
      case 7: upd(pwm_ctrl, 0x7FFF); break;
      case 8: case 9: case 10: upd(duty[slot - 8], 0x3FF); break;
      case 11:
        if ((lanes & 1) && !tx_busy) {
          tx_busy = true;
          tx_end = now + (uart_div + 2) + 10ull * (uart_div + 1);
          sticky_next = true;
          uart_tx.push_back((char)(v & 0xFF));
        }
        break;
      case 12: upd(uart_div, 0x1FFF); break;
      case 13:
        if ((lanes & 1) && !spi_busy) {
          spi_busy = true;
          spi_end = now + 16ull * (1u << ((spi_ctrl >> 1) & 7)) + 2;
          spi_cur = (uint8_t)v;
          spi_cs_at_start = !(spi_ctrl & 1);
          spi_tx.push_back(v & 0xFF);
        }
        break;
      case 14: upd(spi_ctrl, 0xF); break;
      case 15: upd(irq_en, 0x1F); break;
      case 16:
        if (v & bm & 1) p_cmp = false;
        if (v & bm & 2) p_cap = false;
        break;
      case 17: upd(gpio_edge, 0xFFFF); break;
      case 18: epend &= ~(v & bm & 0xFF); break;
    }
  }
};

std::string json_str(const std::string& s) {
  std::string o = "\"";
  for (unsigned char c : s) {
    if (c == '"' || c == '\\') o += '\\', o += (char)c;
    else if (c < 0x20 || c >= 0x7F) {
      char b[8];
      std::snprintf(b, sizeof b, "\\u%04x", c);
      o += b;
    } else o += (char)c;
  }
  return o + "\"";
}

int usage() {
  std::fprintf(stderr,
               "usage: progsim <prog.hex> [--stim FILE] [--cpi N | --cpi rand:A:B] "
               "[--max-cycles C] [--seed S] [--first-retire N] [-v]\n");
  return 1;
}

}  // namespace

int main(int argc, char** argv) {
  const char *hex = nullptr, *stim_path = nullptr;
  uint64_t cpi_lo = 800, cpi_hi = 800, max_cycles = 20000000, first = 200;
  uint64_t seed = 1;
  bool verbose = false;
  for (int i = 1; i < argc; i++) {
    std::string a = argv[i];
    auto next = [&]() -> const char* {
      if (i + 1 >= argc) std::exit(usage());
      return argv[++i];
    };
    if (a == "--stim") stim_path = next();
    else if (a == "--cpi") {
      std::string v = next();
      if (v.rfind("rand:", 0) == 0) {
        cpi_lo = std::strtoull(v.c_str() + 5, nullptr, 0);
        cpi_hi = std::strtoull(std::strchr(v.c_str() + 5, ':') + 1, nullptr, 0);
      } else {
        cpi_lo = cpi_hi = std::strtoull(v.c_str(), nullptr, 0);
      }
      if (!cpi_lo || cpi_hi < cpi_lo) return usage();
    } else if (a == "--max-cycles") max_cycles = std::strtoull(next(), nullptr, 0);
    else if (a == "--seed") seed = std::strtoull(next(), nullptr, 0);
    else if (a == "--first-retire") first = std::strtoull(next(), nullptr, 0);
    else if (a == "-v") verbose = true;
    else if (a[0] == '-') return usage();
    else hex = argv[i];
  }
  if (!hex) return usage();
  Stim st;
  if (stim_path && !load_stim(stim_path, st)) {
    std::fprintf(stderr, "cannot read %s\n", stim_path);
    return 1;
  }
  std::vector<uint8_t> img(v3::FLASH_SIZE);
  std::string err;
  if (!v3::load_flash_hex(hex, img.data(), &err)) {
    std::fprintf(stderr, "%s\n", err.c_str());
    return 1;
  }
  v3::Iss3 iss(img.data());
  Model m(st);
  m.first = first;
  uint64_t rng = seed * 0x9E3779B97F4A7C15ull + 1;
  auto next_cpi = [&]() -> uint64_t {
    if (cpi_lo == cpi_hi) return cpi_lo;
    rng = rng * 6364136223846793005ull + 1442695040888963407ull;
    return cpi_lo + (rng >> 33) % (cpi_hi - cpi_lo + 1);
  };
  v3::IoHooks hooks;
  hooks.volatile_read = [&](uint32_t slot, uint32_t iss_value) { return m.read(slot, iss_value); };

  std::string end = "max";
  std::vector<uint8_t> uo_trace;
  uint8_t uo_prev = iss.uo_gpio();
  m.advance(first);
  while (m.now < max_cycles) {
    if (iss.irq_allowed() && m.line()) {
      iss.take_interrupt();
      if (verbose) std::fprintf(stderr, "%llu: interrupt, mepc 0x%08x\n",
                                (unsigned long long)m.now, iss.csr_peek(0x341));
    }
    m.sticky_now = m.sticky_next;
    m.sticky_next = false;
    v3::Retire r = iss.step(hooks);
    if (r.reserved) {
      end = r.region ? "region" : "unspecified";
      std::fprintf(stderr, "%s at pc 0x%08x: 0x%08x (%s)\n", end.c_str(), r.pc, r.insn,
                   v3::iss_disasm(r.insn).c_str());
      break;
    }
    if ((r.maddr >> 31) && (r.rmask || r.wmask)) {
      const uint32_t slot = (r.maddr >> 2) & 31, off = r.maddr & 3;
      if (r.rmask && slot == v3::SLOT_UART_DATA && off == 0) m.rx_full = m.rx_ovr = false;
      if (r.wmask) {
        const uint32_t n = r.wmask == 1 ? 1 : r.wmask == 3 ? 2 : 4;
        m.write(slot, r.mwdata << (8 * off), ((1u << n) - 1) << off);
      }
    }
    if (iss.uo_gpio() != uo_prev) uo_trace.push_back(uo_prev = iss.uo_gpio());
    if (iss.selfloop()) {
      end = "selfloop";
      break;
    }
    m.advance(m.now + next_cpi());
    if (r.insn == 0x10500073u) {  // WFI: stall until mip.MEIP & mie.MEIE
      while (m.now < max_cycles && !(m.line() && (iss.csr_peek(0x304) & v3::MIP_MEIP))) m.tick();
    }
  }
  std::string trace = "[";
  for (size_t i = 0; i < uo_trace.size(); i++) trace += (i ? "," : "") + std::to_string(uo_trace[i]);
  trace += "]";
  std::string spi = "[";
  for (size_t i = 0; i < m.spi_tx.size(); i++) spi += (i ? "," : "") + std::to_string(m.spi_tx[i]);
  spi += "]";
  std::printf(
      "{\"end\":\"%s\",\"retired\":%llu,\"cycles\":%llu,\"gpio_out\":%u,\"altfn\":%u,"
      "\"uart_tx\":%s,\"spi_tx\":%s,\"interrupts\":%llu,\"uo_trace\":%s}\n",
      end.c_str(), (unsigned long long)iss.retired(), (unsigned long long)m.now,
      iss.io().gpio_out(), iss.io().altfn(), json_str(m.uart_tx).c_str(), spi.c_str(),
      (unsigned long long)iss.interrupts(), trace.c_str());
  return end == "selfloop" && iss.io().gpio_out() == 0x5A ? 0 : 1;
}
