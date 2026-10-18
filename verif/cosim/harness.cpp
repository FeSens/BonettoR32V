// Pin-level cosim harness for tt_um_fesens_r32v under contract v3 (docs/contract.md; see
// docs/cosim.md).
//
//   harness <prog.hex> [--seed S] [--expect PATH] [--clk-ns 25] [--max-cycles C] [--trace PATH]
//           [--dump-psram PATH] [--dump-pins PATH] [-v]
//   harness --fuzz-liveness K [--fuzz-cap N] [--seed S] [--clk-ns 25]
//   Arguments starting with '+' go to the design ($value$plusargs; the test fixtures use them).
//
// prog.hex is a sparse $readmemh flash image (unset bytes 0xFF), loaded once into one buffer that
// the QSPI Pmod model (pmod.h) uses and the reference ISS (iss3.h) copies. --seed picks the random
// power-up state of every flip-flop, the bus noise, the reset length and the memories' power-up
// modes (pmod_init_for_seed: seeds 1-4 cover all four). --expect is the program's expect.json.
//
// Built twice by build.sh:
//   rvfi mode (RVFI_MODE, design built with RISCV_FORMAL): the ISS runs in lockstep with the
//     design's retirements. Every RVFI field is compared (contract 8). IO loads take the design's
//     values for volatile bits (IoHooks, from rvfi_mem_rdata; mip.MEIP from rvfi_rd_wdata). A
//     retirement with rvfi_intr = 1 that doesn't follow a trap is an interrupt entry: the ISS
//     takes it (take_interrupt), and it fails if the ISS has interrupts masked. The latency cap
//     (2,048 cycles, contract 4; only the gap that starts at a WFI's retirement is exempt, R31) is
//     checked at each retirement and by a watchdog. GPIO-mode uo pins follow the ISS's
//     GPIO_OUT/ALTFN within one cycle of the store's retirement and change for no other reason
//     (contract 6). GPIO_IN reads equal ui_in at an edge of the load's execution or the 3 before
//     (R30). TX_BUSY, IRQ_PEND.TXIDLE and SPI BUSY reads match the transfers the pin models see,
//     a UART start bit comes within DIV+2 cycles after its UART_DATA store retires (R28), and
//     UART/SPI bytes on the pins are the bytes the program wrote. The program ends with
//     jal x0, 0 retired twice at the same pc; the harness runs 2,048 more cycles, then compares
//     every PSRAM byte the program wrote; no transfer may still be in flight.
//   pins mode (plain design): no RVFI. A program whose standalone ISS run reaches its self-loop
//     without reading a volatile bit or enabling interrupts (mstatus.MIE and mie.MEIE both 1) is
//     deterministic: the uo pins must go through the ISS's GPIO states in order, every PSRAM
//     write outside the core region must write the byte of some store of that run, the final
//     PSRAM must match it, and the UART frames and SPI transfers must carry its UART_DATA and
//     SPI_DATA bytes (R33). Any other program is judged by its self-check (the GPIO-mode pins,
//     ~final_altfn, show result_gpio; R24), the pin checkers and the expected UART/SPI bytes;
//     PSRAM writes go unchecked there (no reference stores).
// Both modes: the Pmod chip models and bus rules (contract 2, pmod.h) on every cycle, the reset
// state (section 1), outputs that only change at rising clk edges, the peripheral pin models
// (periph_models.h: UART bit times, SPI waveform and echo device, PWM periods after the 0xA5
// mark), and CoreMark timing marks (the GPIO-mode pins change to 0xC0, then 0xC1;
// coremark_cycles_per_iter = the cycles between them / expect.json coremark_iters). The marks
// are masked with ALTFN (R24) and looked for only when expect.json lists pwm / coremark_iters.
// Opt-in: expect.json ir_gate checks the IR gate (contract 5.2) after the 0xA6 mark
// (periph_models.h IrGateMeter); --dump-pins PATH writes every uo_out change with its cycle
// (observation only: no check reads it).
//
// The last stdout line is a JSON summary. Exit 0 = pass, 1 = fail, 2 = usage or setup error.
#include <verilated.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <map>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include "Vtt_um_fesens_r32v.h"
#include "iss3.h"
#include "periph_models.h"
#include "pmod.h"

#ifdef HARNESS_TRACE
#include <verilated_fst_c.h>
#endif

namespace {

using v3::Area;
using v3::area_of;
using v3::in_core_region;

constexpr uint64_t LAT_CAP = 2048;  // contract 4
constexpr uint64_t DRAIN = 2048;    // cycles after the program end before the memory compare
constexpr uint64_t INF = ~0ull;
constexpr uint32_t INSN_WFI = 0x10500073u;
constexpr uint8_t GPIO_FAIL = 0xEE, PWM_MARKER = 0xA5, CM_START = 0xC0, CM_END = 0xC1;
constexpr uint8_t IR_MARKER = 0xA6;  // expect.json ir_gate: the IR gate meter starts here
constexpr uint64_t BUSY_SLACK = 2;            // sampling slack for TX_BUSY / BUSY reads (R20)
constexpr uint64_t DEFAULT_BUDGET = 50000000;  // cycles, for programs the ISS can't run alone
constexpr uint64_t PRE_RUN_CAP = 200000000;    // standalone ISS retirements
constexpr uint64_t RESULT_STABLE = 3 * LAT_CAP;  // pins mode: result_gpio held this long = end
constexpr uint32_t FUZZ_SPAN = 0x10000;          // fuzz: random program bytes at flash 0..64 KB
constexpr uint32_t SLOT_GPIO_IN = 1, SLOT_UART_DATA = 11, SLOT_UART_CTRL = 12, SLOT_SPI_DATA = 13,
                   SLOT_SPI_CTRL = 14, SLOT_IRQ_PEND = 16;
constexpr uint64_t UI_STALE = 3;      // contract 6: GPIO_IN at most 3 cycles stale
constexpr uint64_t UI_HIST = 1 << 13;  // ui_in history kept for the GPIO_IN check (edges)

std::string fmt(const char* f, ...) __attribute__((format(printf, 1, 2)));
std::string fmt(const char* f, ...) {
  char b[1024];
  va_list ap;
  va_start(ap, f);
  std::vsnprintf(b, sizeof b, f, ap);
  va_end(ap);
  return b;
}

std::string json_str(const std::string& s) {
  std::string o = "\"";
  for (unsigned char c : s) {
    if (c == '"' || c == '\\') o += '\\', o += (char)c;
    else if (c == '\n') o += "\\n";
    else if (c < 0x20 || c >= 0x7F) o += fmt("\\u%04x", c);
    else o += (char)c;
  }
  return o + "\"";
}

std::string trace_json(const std::vector<uint8_t>& t) {
  std::string s = "[";
  for (size_t i = 0; i < t.size(); i++) s += (i ? "," : "") + std::to_string(t[i]);
  return s + "]";
}

// ---- options and expect.json ------------------------------------------------------------------
struct Opts {
  const char* hex = nullptr;
  const char* expect = nullptr;
  const char* trace = nullptr;
  const char* dump_psram = nullptr;
  const char* dump_pins = nullptr;  // every uo_out change: "cycle value" lines (observation only)
  uint32_t seed = 1;
  double clk_ns = 25.0;
  uint64_t max_cycles = 0;
  int verbose = 0;
  uint32_t fuzz_runs = 0;    // --fuzz-liveness K
  uint64_t fuzz_cap = 2000;  // retirements per fuzz sub-run
  int argc = 0;
  char** argv = nullptr;
};

// A minimal JSON reader for expect.json (objects, arrays, numbers, strings, true/false/null).
struct JVal {
  enum T { Null, Bool, Num, Str, Arr, Obj } t = Null;
  bool b = false;
  double n = 0;
  std::string s;
  std::vector<JVal> a;
  std::vector<std::pair<std::string, JVal>> o;
  const JVal* get(const char* k) const {
    for (auto& kv : o)
      if (kv.first == k) return &kv.second;
    return nullptr;
  }
};

class JParser {
 public:
  explicit JParser(const std::string& s) : s_(s) {}
  bool parse(JVal* v, std::string* err) {
    if (!value(v) || (ws(), i_ != s_.size())) {
      *err = fmt("JSON syntax error at byte %zu", i_);
      return false;
    }
    return true;
  }

 private:
  const std::string& s_;
  size_t i_ = 0;
  void ws() {
    while (i_ < s_.size() && std::strchr(" \t\r\n", s_[i_])) i_++;
  }
  bool lit(const char* w) {
    size_t n = std::strlen(w);
    if (s_.compare(i_, n, w) != 0) return false;
    i_ += n;
    return true;
  }
  bool str(std::string* out) {
    if (s_[i_] != '"') return false;
    i_++;
    while (i_ < s_.size() && s_[i_] != '"') {
      char c = s_[i_++];
      if (c == '\\') {
        if (i_ >= s_.size()) return false;
        char e = s_[i_++];
        if (e == 'n') c = '\n';
        else if (e == 't') c = '\t';
        else if (e == 'r') c = '\r';
        else if (e == 'b') c = '\b';
        else if (e == 'f') c = '\f';
        else if (e == 'u') {
          if (i_ + 4 > s_.size()) return false;
          unsigned long u = std::strtoul(s_.substr(i_, 4).c_str(), nullptr, 16);
          i_ += 4;
          if (u > 0xFF) return false;  // expect files carry bytes only
          c = (char)u;
        } else c = e;
      }
      *out += c;
    }
    if (i_ >= s_.size()) return false;
    i_++;
    return true;
  }
  bool value(JVal* v) {
    ws();
    if (i_ >= s_.size()) return false;
    char c = s_[i_];
    if (c == '{') {
      v->t = JVal::Obj;
      i_++;
      ws();
      if (i_ < s_.size() && s_[i_] == '}') return ++i_, true;
      for (;;) {
        ws();
        std::string k;
        if (i_ >= s_.size() || !str(&k)) return false;
        ws();
        if (i_ >= s_.size() || s_[i_++] != ':') return false;
        JVal x;
        if (!value(&x)) return false;
        v->o.emplace_back(k, std::move(x));
        ws();
        if (i_ < s_.size() && s_[i_] == ',') { i_++; continue; }
        if (i_ < s_.size() && s_[i_] == '}') return ++i_, true;
        return false;
      }
    }
    if (c == '[') {
      v->t = JVal::Arr;
      i_++;
      ws();
      if (i_ < s_.size() && s_[i_] == ']') return ++i_, true;
      for (;;) {
        JVal x;
        if (!value(&x)) return false;
        v->a.push_back(std::move(x));
        ws();
        if (i_ < s_.size() && s_[i_] == ',') { i_++; continue; }
        if (i_ < s_.size() && s_[i_] == ']') return ++i_, true;
        return false;
      }
    }
    if (c == '"') return v->t = JVal::Str, str(&v->s);
    if (lit("true")) return v->t = JVal::Bool, v->b = true, true;
    if (lit("false")) return v->t = JVal::Bool, v->b = false, true;
    if (lit("null")) return v->t = JVal::Null, true;
    char* end = nullptr;
    v->n = std::strtod(s_.c_str() + i_, &end);
    if (end == s_.c_str() + i_) return false;
    v->t = JVal::Num;
    i_ = end - s_.c_str();
    return true;
  }
};

// The expect.json fields the harness reads (Task 4's mkexpect.py writes them). All optional.
struct Expect {
  bool have_uart = false;
  uint32_t uart_div = 0;
  int32_t uart_rate_ppm = 0;
  struct Rx {
    uint64_t cycle;
    uint8_t byte;
    int32_t ppm;
  };
  std::vector<Rx> uart_rx;  // [cycle_after_first_retire, byte] or [cycle, byte, ppm]
  bool have_tx_expect = false;
  std::string uart_tx_expect;
  bool have_spi = false;
  uint32_t spi_div_d = 0;
  bool have_spi_tx_expect = false;
  std::string spi_tx_expect;  // the MOSI bytes, in order
  struct Pwm {
    int ch;
    uint32_t per, q, duty;
  };
  std::vector<Pwm> pwm;
  struct IrGate {  // opt-in: the IR gate meter after the 0xA6 mark (periph_models.h)
    bool on = false;
    uint32_t per = 0, q = 0, duty = 0;
    uint64_t marks = 0, within = 0;
  } ir_gate;
  bool loop_uo7_to_ui0 = false;
  std::vector<std::pair<uint64_t, uint8_t>> gpio_script;
  bool have_result = false;
  uint8_t result_gpio = 0;
  uint8_t final_altfn = 0;  // ALTFN at the end: result_gpio is compared on the other pins (R24)
  uint64_t coremark_iters = 0;
  uint64_t max_cycles = 0;  // R35: the run budget (CLI --max-cycles wins), 0 = the default
};

bool load_expect(const char* path, Expect* ex, std::string* err) {
  FILE* f = std::fopen(path, "rb");
  if (!f) return *err = std::string("cannot open ") + path, false;
  std::string s;
  char buf[65536];
  size_t n;
  while ((n = std::fread(buf, 1, sizeof buf, f)) > 0) s.append(buf, n);
  std::fclose(f);
  JVal j;
  if (!JParser(s).parse(&j, err)) return *err = std::string(path) + ": " + *err, false;
  if (j.t != JVal::Obj) return *err = std::string(path) + ": not a JSON object", false;
  auto bad = [&](const char* k, const char* want) {
    *err = fmt("%s: field %s must be %s", path, k, want);
    return false;
  };
  auto uint_of = [](const JVal* v, uint64_t* out) {
    if (!v || v->t != JVal::Num || v->n < 0 || v->n != std::floor(v->n) || v->n > 1e18)
      return false;
    *out = (uint64_t)v->n;
    return true;
  };
  auto int_of = [](const JVal* v, int64_t* out) {
    if (!v || v->t != JVal::Num || v->n != std::floor(v->n) || std::fabs(v->n) > 1e9) return false;
    *out = (int64_t)v->n;
    return true;
  };
  uint64_t u;
  int64_t i;
  if (const JVal* v = j.get("uart_div"); v && v->t != JVal::Null) {
    if (!uint_of(v, &u) || u > 0x1FFF) return bad("uart_div", "an integer 0..8191");
    ex->have_uart = true;
    ex->uart_div = (uint32_t)u;
  }
  if (const JVal* v = j.get("uart_rate_ppm"); v && v->t != JVal::Null) {
    if (!int_of(v, &i) || i <= -1000000) return bad("uart_rate_ppm", "an integer > -10^6");
    ex->uart_rate_ppm = (int32_t)i;
  }
  if (const JVal* v = j.get("uart_rx"); v && v->t != JVal::Null) {
    if (v->t != JVal::Arr) return bad("uart_rx", "a list of [cycle, byte] or [cycle, byte, ppm]");
    for (const JVal& e : v->a) {
      uint64_t c, b;
      int64_t p = ex->uart_rate_ppm;
      if (e.t != JVal::Arr || (e.a.size() != 2 && e.a.size() != 3) || !uint_of(&e.a[0], &c) ||
          !uint_of(&e.a[1], &b) || b > 255 || (e.a.size() == 3 && !int_of(&e.a[2], &p)))
        return bad("uart_rx", "a list of [cycle, byte] or [cycle, byte, ppm]");
      ex->uart_rx.push_back(Expect::Rx{c, (uint8_t)b, (int32_t)p});
    }
    if (!ex->have_uart && !ex->uart_rx.empty()) return bad("uart_rx", "given with uart_div");
  }
  if (const JVal* v = j.get("uart_tx_expect"); v && v->t != JVal::Null) {
    if (v->t != JVal::Str) return bad("uart_tx_expect", "a string");
    ex->have_tx_expect = true;
    ex->uart_tx_expect = v->s;
  }
  if (const JVal* v = j.get("spi_div_d"); v && v->t != JVal::Null) {
    // contract 5.6: D >= 1 (D = 0 is UNSPECIFIED; the device's MISO hold needs 2-cycle phases)
    if (!uint_of(v, &u) || u < 1 || u > 7) return bad("spi_div_d", "an integer 1..7");
    ex->have_spi = true;
    ex->spi_div_d = (uint32_t)u;
  }
  if (const JVal* v = j.get("spi_tx_expect"); v && v->t != JVal::Null) {
    if (v->t == JVal::Str) {
      ex->spi_tx_expect = v->s;
    } else if (v->t == JVal::Arr) {
      for (const JVal& e : v->a) {
        if (!uint_of(&e, &u) || u > 255) return bad("spi_tx_expect", "a string or a list of bytes");
        ex->spi_tx_expect += (char)u;
      }
    } else {
      return bad("spi_tx_expect", "a string or a list of bytes");
    }
    ex->have_spi_tx_expect = true;
  }
  if (const JVal* v = j.get("pwm"); v && v->t != JVal::Null) {
    if (v->t != JVal::Arr) return bad("pwm", "a list of {ch, per, q, duty}");
    for (const JVal& e : v->a) {
      uint64_t ch, per, q, duty;
      if (e.t != JVal::Obj || !uint_of(e.get("ch"), &ch) || !uint_of(e.get("per"), &per) ||
          !uint_of(e.get("q"), &q) || !uint_of(e.get("duty"), &duty) ||
          !(ch <= 2 || ch == 7) || per > 65535 || q > 15 || duty > 65535)
        return bad("pwm", "a list of {ch: 0-2 or 7, per, q, duty}");
      ex->pwm.push_back(Expect::Pwm{(int)ch, (uint32_t)per, (uint32_t)q, (uint32_t)duty});
    }
  }
  if (const JVal* v = j.get("ir_gate"); v && v->t != JVal::Null) {
    uint64_t per, q, duty, marks, within;
    if (v->t != JVal::Obj || !uint_of(v->get("per"), &per) || !uint_of(v->get("q"), &q) ||
        !uint_of(v->get("duty"), &duty) || !uint_of(v->get("marks"), &marks) ||
        !uint_of(v->get("within"), &within) || per > 1023 || q > 7 || duty < 1 || duty > per ||
        marks < 1 || within < 1)
      return bad("ir_gate",
                 "{per: 1-1023, q: 0-7, duty: 1-per (a carrier with 0 < high < period), marks: "
                 ">= 1, within: >= 1}");
    ex->ir_gate = Expect::IrGate{true, (uint32_t)per, (uint32_t)q, (uint32_t)duty, marks, within};
  }
  if (const JVal* v = j.get("loop_uo7_to_ui0"); v && v->t != JVal::Null) {
    if (v->t != JVal::Bool) return bad("loop_uo7_to_ui0", "true or false");
    ex->loop_uo7_to_ui0 = v->b;
  }
  if (const JVal* v = j.get("gpio_script"); v && v->t != JVal::Null) {
    if (v->t != JVal::Arr) return bad("gpio_script", "a list of [cycle, ui_value]");
    for (const JVal& e : v->a) {
      uint64_t c, b;
      if (e.t != JVal::Arr || e.a.size() != 2 || !uint_of(&e.a[0], &c) || !uint_of(&e.a[1], &b) ||
          b > 255)
        return bad("gpio_script", "a list of [cycle, ui_value]");
      ex->gpio_script.emplace_back(c, (uint8_t)b);
    }
  }
  if (const JVal* v = j.get("result_gpio"); v && v->t != JVal::Null) {
    if (!uint_of(v, &u) || u > 255) return bad("result_gpio", "an integer 0..255");
    ex->have_result = true;
    ex->result_gpio = (uint8_t)u;
  }
  if (const JVal* v = j.get("final_altfn"); v && v->t != JVal::Null) {
    if (!uint_of(v, &u) || u > 254) return bad("final_altfn", "an integer 0..254");
    ex->final_altfn = (uint8_t)u;
  }
  if (ex->have_result && (ex->result_gpio & ~ex->final_altfn & 0xFF) == 0) {
    // R24: the self-check compares the GPIO-mode pins with result_gpio & ~final_altfn; all 0 is
    // what an idle design shows
    *err = fmt("%s: result_gpio 0x%02x & ~final_altfn 0x%02x is 0, a degenerate self-check (an "
               "idle design passes it)",
               path, ex->result_gpio, ex->final_altfn);
    return false;
  }
  if (const JVal* v = j.get("coremark_iters"); v && v->t != JVal::Null) {
    if (!uint_of(v, &u) || u == 0) return bad("coremark_iters", "a positive integer");
    ex->coremark_iters = u;
  }
  if (const JVal* v = j.get("max_cycles"); v && v->t != JVal::Null) {
    if (!uint_of(v, &u) || u == 0) return bad("max_cycles", "a positive integer");
    ex->max_cycles = u;
  }
  return true;
}

// ---- small helpers ----------------------------------------------------------------------------
struct XorShift {
  uint64_t s;
  explicit XorShift(uint64_t seed) : s(seed * 0x9E3779B97F4A7C15ull + 0x1234567) {}
  uint32_t next() {
    s ^= s << 13;
    s ^= s >> 7;
    s ^= s << 17;
    return (uint32_t)(s >> 16);
  }
};

// A set of bytes per PSRAM address: the bytes some store wrote there (contract 8 attribution).
class StoreSet {
 public:
  void add(uint32_t a, uint8_t v) { m_[a][v >> 6] |= 1ull << (v & 63); }
  bool has(uint32_t a, uint8_t v) const {
    auto it = m_.find(a);
    return it != m_.end() && ((it->second[v >> 6] >> (v & 63)) & 1);
  }
  bool any(uint32_t a) const { return m_.count(a) != 0; }

 private:
  std::unordered_map<uint32_t, std::array<uint64_t, 4>> m_;
};

// PSRAM bytes the program wrote (23-bit addresses), in first-write order.
class Written {
 public:
  Written() : bits_(v3::PSRAM_SIZE / 64, 0) {}
  void mark(uint32_t a) {
    a &= v3::PSRAM_SIZE - 1;
    uint64_t& w = bits_[a >> 6];
    if (!((w >> (a & 63)) & 1)) w |= 1ull << (a & 63), list_.push_back(a);
  }
  bool has(uint32_t a) const {
    a &= v3::PSRAM_SIZE - 1;
    return (bits_[a >> 6] >> (a & 63)) & 1;
  }
  const std::vector<uint32_t>& list() const { return list_; }

 private:
  std::vector<uint64_t> bits_;
  std::vector<uint32_t> list_;
};

// The instruction word at pc as the ISS sees it, or false (IO, core region).
bool peek_insn(const Iss3& iss, uint32_t pc, uint32_t* w) {
  if (area_of(pc) == Area::Io) return false;
  uint32_t x = 0;
  for (uint32_t k = 0; k < 4; k++) {
    const uint32_t a = pc + k;
    if (in_core_region(a)) return false;
    x |= (uint32_t)(area_of(a) == Area::Psram ? iss.psram(a) : iss.flash(a)) << (8 * k);
  }
  *w = x;
  return true;
}

// ---- GPIO pin tracking (contract 6) -----------------------------------------------------------
// A GPIO state: GPIO_OUT g and ALTFN a. The uo pins show it when every pin with a = 0 equals g;
// pins with a = 1 carry their peripheral and are free here.
struct GState {
  uint8_t g = 0, a = 0;
};
inline bool gconsistent(uint8_t uo, GState s) { return (((uo ^ s.g) & ~s.a) & 0xFF) == 0; }
inline bool gsame(GState x, GState y) {
  return x.a == y.a && (((x.g ^ y.g) & ~x.a) & 0xFF) == 0;
}
std::string gdesc(GState s) {
  if (!s.a) return fmt("0x%02x", s.g);
  return fmt("0x%02x on the GPIO pins (ALTFN 0x%02x: those pins carry their peripheral)",
             (uint8_t)(s.g & ~s.a), s.a);
}

// The GPIO state after the store at pc, if it is a store to GPIO_OUT or ALTFN (else false):
// decoded from the ISS's current registers, so the harness knows the pending instruction's
// effect before it retires (the design may change the pins before its rvfi_valid).
bool predict_gpio_store(const Iss3& iss, uint32_t pc, GState* out) {
  uint32_t insn;
  if (!peek_insn(iss, pc, &insn) || (insn & 0x7F) != 0x23) return false;
  const uint32_t f3 = (insn >> 12) & 7, rs1 = (insn >> 15) & 31, rs2 = (insn >> 20) & 31;
  if (f3 > 2 || rs1 > 15 || rs2 > 15) return false;
  const uint32_t imm = ((uint32_t)((int32_t)insn >> 25) << 5) | ((insn >> 7) & 0x1F);
  const uint32_t ea = iss.reg((int)rs1) + imm, n = 1u << f3, off = ea & 3;
  if (area_of(ea) != Area::Io || ea % n) return false;
  const uint32_t slot = (ea >> 2) & 31;
  if (slot != v3::SLOT_GPIO_OUT && slot != v3::SLOT_ALTFN) return false;
  const uint32_t v = n == 4 ? iss.reg((int)rs2) : iss.reg((int)rs2) & ((1u << (8 * n)) - 1);
  v3::IoRegs io = iss.io();
  io.write(slot, v << (8 * off), ((1u << n) - 1) << off);
  *out = GState{io.gpio_out(), io.altfn()};
  return true;
}

// The design's uo pins follow a sequence of GPIO states. Tracked as the set of positions the pins
// can be at (a state can be "shown" at an edge where the pins are consistent with it; one edge
// can show several consecutive states when they agree on the visible pins). Each step moves
// forward only. rvfi mode adds deadlines (a state must be reached one cycle after the store that
// makes it retires) and tentative next states (the pending instruction's predicted effect).
class GpioTrack {
 public:
  void init(GState s0) {
    seq_.clear();
    seq_.push_back(E{s0, 0});
    base_ = 0;
    pos_.assign(1, 0);
    alt_.clear();
    altpos_ = 0;
  }
  // pins mode: append the next state of the reference run
  void push(GState s) {
    if (!gsame(s, seq_.back().s)) seq_.push_back(E{s, 0});
  }
  // rvfi mode: the state after a retirement at edge t, due at edge t + 1. Returns "" or an error.
  std::string retire(GState s, uint64_t t, const std::string& who) {
    const GState prev = seq_.back().s;
    std::string err;
    if (gsame(s, prev)) {
      if (altpos_ && pos_.empty())
        err = fmt("uo_out showed %s before %s retired, but that instruction doesn't change the "
                  "GPIO pins (they stay %s; contract 6)",
                  gdesc(first_alt()).c_str(), who.c_str(), gdesc(prev).c_str());
    } else {
      seq_.push_back(E{s, t + 1});
      bool hit = false;
      for (size_t x = 0; x < alt_.size(); x++)
        if (((altpos_ >> x) & 1) && gsame(alt_[x], s)) hit = true;
      if (hit) pos_.push_back(last());
      else if (altpos_ && pos_.empty())
        err = fmt("uo_out showed %s before %s retired, but it sets %s (contract 6)",
                  gdesc(first_alt()).c_str(), who.c_str(), gdesc(s).c_str());
    }
    alt_.clear();
    altpos_ = 0;
    return err;
  }
  void set_alts(const std::vector<GState>& alts) {
    alt_.clear();
    for (const GState& s : alts) {
      if (gsame(s, seq_.back().s)) continue;
      bool dup = false;
      for (const GState& o : alt_) dup = dup || gsame(o, s);
      if (!dup && alt_.size() < 8) alt_.push_back(s);
    }
    altpos_ = 0;
  }
  // The pins after edge t. Returns "" or an error.
  std::string advance(uint8_t uo, uint64_t t) {
    const uint64_t L = last();
    std::vector<uint64_t> np;
    for (uint64_t p : pos_) {
      if (gconsistent(uo, at(p))) np.push_back(p);
      for (uint64_t q = p + 1; q <= L && gconsistent(uo, at(q)); q++) np.push_back(q);
    }
    std::sort(np.begin(), np.end());
    np.erase(std::unique(np.begin(), np.end()), np.end());
    const bool last_reach = std::binary_search(pos_.begin(), pos_.end(), L) ||
                            std::binary_search(np.begin(), np.end(), L);
    uint32_t na = 0;
    for (size_t x = 0; x < alt_.size(); x++)
      if (gconsistent(uo, alt_[x]) && (((altpos_ >> x) & 1) || last_reach)) na |= 1u << x;
    // deadlines: every state due by now must have been reached
    uint64_t due_idx = 0;
    bool due = false;
    for (uint64_t i = base_; i <= L; i++) {
      E& e = seq_[i - base_];
      if (e.due && e.due <= t) due_idx = i, due = true, e.due = 0;
    }
    const std::vector<uint64_t> before = np;
    if (due) np.erase(np.begin(), std::lower_bound(np.begin(), np.end(), due_idx));
    if (np.empty() && !na) {
      const uint64_t lo = pos_.empty() ? L : pos_.front();
      if (due && !before.empty())
        return fmt("uo_out = 0x%02x at cycle %llu, one cycle after the store setting %s retired "
                   "(contract 6: the pins take a stored value no later than 1 cycle after the "
                   "store retires)",
                   uo, (unsigned long long)t, gdesc(at(due_idx)).c_str());
      std::string want = gdesc(at(lo));
      if (lo < L) want += ", or next " + gdesc(at(lo + 1));
      else if (!alt_.empty()) want += ", or next " + gdesc(alt_[0]) + " (the pending store)";
      return fmt("uo_out changed to 0x%02x at cycle %llu; expected %s (contract 6: GPIO pins "
                 "change only because of a store to GPIO_OUT or ALTFN)",
                 uo, (unsigned long long)t, want.c_str());
    }
    pos_ = np;
    altpos_ = na;
    const uint64_t keep = pos_.empty() ? L : pos_.front();
    while (base_ < keep) seq_.pop_front(), base_++;
    return "";
  }
  bool at_last() const { return std::binary_search(pos_.begin(), pos_.end(), last()); }
  uint64_t last() const { return base_ + seq_.size() - 1; }
  GState last_state() const { return seq_.back().s; }
  // The earliest state the pins may be showing (for gating peripheral decoders in pins mode).
  GState current() const { return pos_.empty() ? seq_.back().s : at(pos_.front()); }
  size_t size() const { return seq_.size(); }

 private:
  struct E {
    GState s;
    uint64_t due;
  };
  std::deque<E> seq_;
  uint64_t base_ = 0;
  std::vector<uint64_t> pos_;
  std::vector<GState> alt_;
  uint32_t altpos_ = 0;
  GState at(uint64_t i) const { return seq_[i - base_].s; }
  GState first_alt() const {
    for (size_t x = 0; x < alt_.size(); x++)
      if ((altpos_ >> x) & 1) return alt_[x];
    return seq_.back().s;
  }
};

// ---- busy flags against the pins (R20) --------------------------------------------------------
// Episodes in which TX_BUSY (or SPI BUSY) is 1: possibly from ps, surely from ds, surely until de
// (exclusive), possibly until pe (exclusive). A read that samples somewhere in [lo, hi] must
// return 1 if every edge there is surely busy, 0 if none is possibly busy, either otherwise.
class BusyTrack {
 public:
  struct Ep {
    uint64_t ps, ds, de, pe;
  };
  size_t open(uint64_t ps, uint64_t ds) {
    eps_.push_back(Ep{ps, ds, INF, INF});
    return eps_.size() - 1;
  }
  Ep& ep(size_t i) { return eps_[i]; }
  void unknown(uint64_t from, uint64_t to) {
    ufrom_ = from;
    uto_ = to;
  }
  // 1: must read 1; 0: must read 0; -1: either
  int must(uint64_t lo, uint64_t hi) const {
    if (lo < uto_ && hi >= ufrom_) return -1;
    for (const Ep& e : eps_)
      if (e.ds <= lo && hi < e.de) return 1;
    for (const Ep& e : eps_)
      if (e.ps <= hi && lo < e.pe) return -1;
    return 0;
  }

 private:
  std::vector<Ep> eps_;
  uint64_t ufrom_ = 0, uto_ = 0;
};

// One serial port's write-to-pins bookkeeping (UART TX or the SPI master), rvfi mode.
struct XferTrack {
  BusyTrack busy;
  bool gated = false;
  uint64_t ungated_writes = 0;  // writes made while the pins didn't show the port
  struct Pending {
    bool active = false, maybe = false;
    uint64_t s = 0, p = 0, deadline = 0;
    uint8_t byte = 0;
    size_t ep = 0;
  } pend;
  bool orphan = false;  // a transfer started on the pins before its write retired
  bool orphan_done = false;  // ... and also ended before it: its byte is checked at the write
  uint64_t orphan_t0 = 0;
  size_t orphan_ep = 0;
  uint8_t orphan_byte = 0;
  const char* orphan_what = "";
  struct Fly {
    uint64_t t0;
    int byte;  // -1: unknown (started while unattributable)
    size_t ep;
  };
  std::deque<Fly> fly;
  uint64_t unknown_from = 0, unknown_to = 0;  // transfers starting before unknown_to: unattributed
};

const char* unspecified_what(const Retire& r) {
  return r.region ? "a fetch or access touching the core region (PSRAM 0x7F_FF80-0x7F_FFFF)"
                  : "an unspecified step (reserved encoding, unlisted CSR, IO fetch, misaligned "
                    "IO access or an access spanning two regions)";
}

std::string describe(uint64_t n, const Retire& e) {
  return fmt("retirement #%llu (pc 0x%08x, insn 0x%08x %s)", (unsigned long long)n, e.pc, e.insn,
             iss_disasm(e.insn).c_str());
}

// ---- the harness ------------------------------------------------------------------------------
class Harness {
 public:
  Harness(const Opts& o, const uint8_t* img, const Expect& ex)
      : o_(o),
        img_(img),
        ex_(ex),
        pmod_(o.seed, img, pmod_init_for_seed(o.seed), o.clk_ns),
        iss_(img),
        uart_(ex.have_uart ? ex.uart_div : 15, ex.uart_rate_ppm),
        spi_(ex.have_spi ? ex.spi_div_d : 1),
        stim_(ex.gpio_script),
        noise_(o.seed ^ 0x5EEDu) {}

  int run();

 private:
  const Opts& o_;
  const uint8_t* img_;
  const Expect& ex_;
  VerilatedContext* ctx_ = nullptr;
  Vtt_um_fesens_r32v* top_ = nullptr;
#ifdef HARNESS_TRACE
  VerilatedFstC* tfp_ = nullptr;
#endif
  PmodBus pmod_;
  Iss3 iss_;
  periph::UartTerm uart_;
  periph::SpiDevice spi_;
  periph::GpioStim stim_;
  XorShift noise_;
  std::vector<periph::PwmMeter> meters_;
  std::vector<uint8_t> meter_pin_;
  std::unique_ptr<periph::IrGateMeter> ir_;
  uint64_t ir_marker_ = INF;
  bool ir_marker_was_ = false;  // rvfi: GPIO_OUT showed 0xA6 last time
  FILE* pins_dump_ = nullptr;
  bool pins_dumped_ = false;
  uint8_t pins_last_ = 0;
  std::string error_;
  uint64_t t_ = 0, reset_edges_ = 1;

  // reference
  bool det_ = false;           // the standalone ISS run is deterministic (pins: the reference)
  bool in_pre_run_ = false;    // errors from the standalone run carry no cycle
  uint64_t ref_retired_ = 0;   // its retirements (deterministic)
  uint64_t ref_tail_ = 0;      // its retirements after the last GPIO change
  GState ref_final_;
  struct SerialWrite {
    uint8_t byte;
    uint32_t div;  // UART: DIV; SPI: D (from the control register at the write)
  };
  std::vector<SerialWrite> ref_uart_, ref_spi_;  // its UART_DATA / SPI_DATA byte-0 writes
  StoreSet stores_;
  Written written_;
  GpioTrack gpio_;
  uint64_t budget_ = 0;

  // run state
  uint64_t anchor_ = INF;  // stimulus time origin: first retirement (rvfi) / first fetch (pins)
  uint64_t retired_ = 0, last_ret_ = 0, max_gap_ = 0, first_gap_ = 0;
  std::map<std::string, uint64_t> per_class_;
  uint32_t last_insn_ = 0;
  bool last_trap_ = false;
  std::vector<uint8_t> ui_hist_ = std::vector<uint8_t>(UI_HIST, 0);  // ui_in seen at edge e
  uint64_t prog_end_ = INF;  // edge the program ended (selfloop / final state / result)
  uint64_t end_at_ = INF;    // edge the run stops (after the drain)
  uint64_t cycles_ = 0;
  size_t writes_checked_ = 0;
  uint64_t psram_prog_writes_ = 0;
  uint8_t uo_last_ = 0;
  std::vector<uint8_t> uo_seen_;
  uint64_t cm_start_ = INF, cm_end_ = INF;
  uint64_t result_since_ = INF;
  uint64_t pwm_marker_ = INF;
  bool marker_was_ = false, fail_was_ = false;  // rvfi: GPIO_OUT showed 0xA5 / 0xEE last time
  bool cm_was0_ = false, cm_was1_ = false;      // the pins showed 0xC0 / 0xC1 last cycle
  bool uart_soft_ = false;      // rvfi: the next instruction may clear ALTFN[0] (UART gate unknown)
  uint64_t uart_abandoned_ = 0;  // pins: lenient abandonments already judged
  bool vread_used_ = false;
  uint32_t vread_slot_ = 0, vread_val_ = 0;
  XferTrack ut_, st_;  // UART TX, SPI master (rvfi)
  const char* end_reason_ = "fail";
  uint64_t interrupts_ = 0, retired_at_end_ = 0;
  uint8_t ui_held_ = 0;
  uint64_t ui_until_ = 0;
  bool rx_queued_ = false;

  uint64_t rel(uint64_t t) const { return t - (reset_edges_ - 1); }
  bool fail(const std::string& why) {
    if (error_.empty())
      error_ = (in_pre_run_           ? std::string()
                : t_ < reset_edges_ ? fmt("reset edge %llu: ", (unsigned long long)t_)
                                    : fmt("cycle %llu: ", (unsigned long long)rel(t_))) +
               why;
    return false;
  }
  uint32_t outputs() const {
    return (uint32_t)top_->uo_out | (uint32_t)top_->uio_out << 8 | (uint32_t)top_->uio_oe << 16;
  }
  void tick_trace(uint64_t time) {
#ifdef HARNESS_TRACE
    if (tfp_) tfp_->dump(time);
#else
    (void)time;
#endif
  }
  GState iss_gpio() const { return GState{iss_.io().gpio_out(), iss_.io().altfn()}; }
  // R24: a GPIO value v "shows" want on the pins not in alt (the pins in GPIO mode)
  static bool gmatch(uint8_t v, uint8_t want, uint8_t alt) {
    return ((v ^ want) & ~alt & 0xFF) == 0;
  }
  // The ALTFN the marks (0xA5, 0xC0/0xC1) are masked with: the ISS's in rvfi mode; in pins mode
  // the tracked state's (deterministic programs) or final_altfn plus the pwm pins.
  uint8_t mark_alt() const;
  bool serial_ref_check(bool uart, size_t k, uint8_t byte, uint64_t t0);

  bool pre_run();
  bool pre_run_body();
  void set_anchor(uint64_t t);
  void start_meters(uint64_t t);
  void start_ir(uint64_t t);
  bool check_psram_writes(bool all);
  bool peripherals(uint8_t uo);
  bool end_ready();
  bool final_checks();
  uint8_t next_ui(uint8_t uo);
  void summary(bool passed, double secs);
#ifdef RVFI_MODE
  bool retirement();
  bool compare(const Retire& e, const std::string& who);
  bool xfer_write(XferTrack& x, uint64_t p, uint64_t s, uint8_t byte, uint64_t deadline);
  bool xfer_start(XferTrack& x, uint64_t t0, uint64_t lead, const char* what);
  bool xfer_done(XferTrack& x, uint64_t t0, uint64_t end, uint8_t byte, const char* what);
  bool xfer_deadline(XferTrack& x, const char* what);
  void xfer_gate(XferTrack& x, bool gate, uint64_t settle);
  bool busy_read(XferTrack& x, bool busy, int raw, uint64_t p, const char* what);
  bool gpio_in_read(uint8_t value, uint64_t p, const std::string& who);
  std::vector<GState> predictions() const;
  uint64_t uart_settle() const { return 11 * (uint64_t)uart_.bit_time() + uart_.div() + 8; }
  uint64_t spi_settle() const { return LAT_CAP + 18 * (uint64_t)spi_.half() + 8; }
#endif
};

// Standalone ISS run: is the program deterministic (no volatile read before its self-loop, and
// interrupts never enabled: once mstatus.MIE and mie.MEIE are both 1, when an interrupt comes
// depends on the design's timing)? If so it is the pins-mode reference: GPIO states, store bytes,
// final PSRAM.
bool Harness::pre_run() {
  in_pre_run_ = true;
  const bool ok = pre_run_body();
  in_pre_run_ = false;
  return ok;
}

bool Harness::pre_run_body() {
  Iss3 ref(img_);
  bool vol = false;
  v3::IoHooks hooks;
  hooks.volatile_read = [&vol](uint32_t, uint32_t v) {
    vol = true;
    return v;
  };
  GState cur{ref.io().gpio_out(), ref.io().altfn()};
  std::vector<GState> states{cur};
  uint64_t n = 0, last_change = 0;
  StoreSet stores;
  Written written;
  std::vector<SerialWrite> uart_w, spi_w;
  bool loop = false;
  while (n < PRE_RUN_CAP) {
    if (ref.irq_allowed()) {  // an interrupt may come at any boundary from here
      vol = true;
      break;
    }
    Retire r = ref.step(hooks);
    if (vol) break;
    if (r.reserved) {
      return fail(fmt("test program bug: the ISS hit %s at %s", unspecified_what(r),
                      describe(n, r).c_str()));
    }
    if (ref.uninit_read())
      return fail(fmt("test program bug: a register or arbitrary-reset CSR is read before any "
                      "write at pc 0x%08x (the design's value is unknown)",
                      ref.uninit_pc()));
    n++;
    if (r.wmask && area_of(r.maddr) == Area::Psram)
      for (int k = 0; k < __builtin_popcount(r.wmask); k++) {
        stores.add((r.maddr + k) & (v3::PSRAM_SIZE - 1), (uint8_t)(r.mwdata >> (8 * k)));
        written.mark(r.maddr + k);
      }
    if (r.wmask && area_of(r.maddr) == Area::Io && (r.maddr & 3) == 0) {
      const uint32_t slot = (r.maddr >> 2) & 31;
      const uint8_t a = ref.io().altfn();  // only writes whose port the pins show count
      if (slot == SLOT_UART_DATA && (a & 1))
        uart_w.push_back(SerialWrite{(uint8_t)r.mwdata, ref.io().r[SLOT_UART_CTRL] & 0x1FFF});
      if (slot == SLOT_SPI_DATA && (a & 0x70) == 0x70)
        spi_w.push_back(SerialWrite{(uint8_t)r.mwdata, (ref.io().r[SLOT_SPI_CTRL] >> 1) & 7});
    }
    GState s{ref.io().gpio_out(), ref.io().altfn()};
    if (!gsame(s, cur)) cur = s, states.push_back(s), last_change = n;
    if (ref.selfloop()) {
      loop = true;
      vol = ref.irq_allowed();  // the self-loop can still be interrupted
      break;
    }
  }
  if (!vol && !loop)
    return fail(fmt("test program bug: the ISS ran %llu instructions without reaching its "
                    "self-loop, reading a volatile register or enabling interrupts",
                    (unsigned long long)PRE_RUN_CAP));
  det_ = loop && !vol;
  if (det_) {
    ref_retired_ = n;
    ref_tail_ = n - last_change;
    ref_final_ = cur;
    ref_uart_ = std::move(uart_w);
    ref_spi_ = std::move(spi_w);
#ifndef RVFI_MODE
    stores_ = std::move(stores);
    for (uint32_t a : written.list()) written_.mark(a);
    gpio_.init(states[0]);
    for (size_t i = 1; i < states.size(); i++) gpio_.push(states[i]);
    // keep the final bytes in the ISS member for the compare
    iss_ = std::move(ref);
#endif
    if (ex_.have_result && !gmatch(ref_final_.g, ex_.result_gpio, ex_.final_altfn))
      return fail(fmt("test program bug: expect.json result_gpio = 0x%02x (final_altfn 0x%02x), "
                      "but the ISS ends with GPIO_OUT = 0x%02x",
                      ex_.result_gpio, ex_.final_altfn, ref_final_.g));
    if (ex_.have_result && ref_final_.a != ex_.final_altfn)
      return fail(fmt("test program bug: expect.json final_altfn = 0x%02x, but the ISS ends with "
                      "ALTFN = 0x%02x",
                      ex_.final_altfn, ref_final_.a));
  }
  // R35: CLI --max-cycles > expect.json max_cycles > the default
  budget_ = o_.max_cycles    ? o_.max_cycles
            : ex_.max_cycles ? ex_.max_cycles
            : det_           ? LAT_CAP * (n + 16)
                             : DEFAULT_BUDGET;
  return true;
}

void Harness::start_meters(uint64_t t) {
  if (pwm_marker_ != INF) return;
  pwm_marker_ = t;
  for (const Expect::Pwm& p : ex_.pwm) {
    const uint64_t scale = 1ull << p.q, period = ((uint64_t)p.per + 1) * scale;
    const uint64_t high = std::min<uint64_t>(p.duty, (uint64_t)p.per + 1) * scale;
    const int pin = p.ch == 7 ? 7 : p.ch + 1;
    meters_.emplace_back(p.ch == 7 ? std::string("timer output (uo[7])")
                                   : fmt("PWM%d (uo[%d])", p.ch, pin),
                         period, high, t);
    meter_pin_.push_back((uint8_t)pin);
  }
}

void Harness::start_ir(uint64_t t) {
  if (ir_marker_ != INF) return;
  ir_marker_ = t;
  const Expect::IrGate& g = ex_.ir_gate;
  const uint64_t scale = 1ull << g.q, period = ((uint64_t)g.per + 1) * scale;
  ir_.reset(new periph::IrGateMeter(period, (uint64_t)g.duty * scale, g.marks, t, g.within));
}

// Contract 8: every PSRAM write outside the core region writes a byte some store wrote at that
// address. rvfi mode checks writes up to the current edge at each retirement (the store set then
// holds every store retired so far, the one in flight included); pins mode checks each write
// against the reference run's stores.
bool Harness::check_psram_writes(bool all) {
  const std::vector<PsramWrite>& w = pmod_.writes();
  for (; writes_checked_ < w.size(); writes_checked_++) {
    const PsramWrite& x = w[writes_checked_];
    if (!all && x.t > t_) break;
    if (x.addr >= v3::REGION_LO) continue;
    psram_prog_writes_++;
#ifndef RVFI_MODE
    if (!det_) continue;
#endif
    if (!stores_.has(x.addr, x.data))
      return fail(fmt("PSRAM write of 0x%02x to 0x%06x (cycle %llu, CPU address 0x%08x) matches "
                      "no %s store: outside the core region the design may only write bytes of "
                      "stores, at their addresses, with their data (contract 8)%s",
                      x.data, x.addr, (unsigned long long)rel(x.t), 0x01000000u | x.addr,
#ifdef RVFI_MODE
                      "retired",
#else
                      "reference",
#endif
                      stores_.any(x.addr) ? " (a store wrote that address, with other data)"
                                          : " (no store wrote that address)"));
  }
  return true;
}


void Harness::set_anchor(uint64_t t) {
  anchor_ = t;
  if (rx_queued_) return;
  rx_queued_ = true;
  std::vector<Expect::Rx> rx = ex_.uart_rx;
  std::stable_sort(rx.begin(), rx.end(),
                   [](const Expect::Rx& a, const Expect::Rx& b) { return a.cycle < b.cycle; });
  for (const Expect::Rx& r : rx) uart_.rx_queue(t + r.cycle, r.byte, r.ppm);
}

uint8_t Harness::next_ui(uint8_t uo) {
  const bool models = ex_.have_uart || ex_.have_spi || ex_.loop_uo7_to_ui0 || !stim_.empty();
  uint8_t ui;
  if (!models && det_) {
    // a deterministic program reads no input: drive noise, changing every 16-79 cycles; ui[1]
    // (UART RX) stays idle high, as everywhere no UART RX model drives it (R24)
    if (t_ >= ui_until_) {
      ui_held_ = (uint8_t)(noise_.next() | periph::UI_IDLE);
      ui_until_ = t_ + 16 + noise_.next() % 64;
    }
    ui = ui_held_;
  } else {
    ui = (anchor_ != INF && t_ + 1 >= anchor_) ? stim_.value(t_ + 1 - anchor_) : periph::UI_IDLE;
  }
  if (ex_.have_uart) ui = (uint8_t)((ui & ~2u) | (uart_.rx_level(t_ + 1) ? 2u : 0u));
  if (ex_.have_spi) ui = (uint8_t)((ui & ~8u) | (spi_.miso(t_ + 1, noise_.next() & 1) ? 8u : 0u));
  if (ex_.loop_uo7_to_ui0) ui = (uint8_t)((ui & ~1u) | (periph::TimerLoop::ui0(uo) ? 1u : 0u));
  return ui;
}

uint8_t Harness::mark_alt() const {
#ifdef RVFI_MODE
  return iss_.io().altfn();
#else
  if (det_) return gpio_.current().a;
  uint8_t a = ex_.final_altfn;
  for (const Expect::Pwm& p : ex_.pwm) a |= (uint8_t)(1u << (p.ch == 7 ? 7 : p.ch + 1));
  if (ex_.ir_gate.on) a |= 0x82;  // uo[1] (gated PWM0) and uo[7] (timer output)
  return a;
#endif
}

// pins mode, deterministic programs (R33): UART frame / SPI transfer k carries the reference run's
// k-th UART_DATA / SPI_DATA byte-0 write.
bool Harness::serial_ref_check(bool uart, size_t k, uint8_t byte, uint64_t t0) {
  const std::vector<SerialWrite>& w = uart ? ref_uart_ : ref_spi_;
  const char* what = uart ? "UART TX" : "SPI MOSI";
  const char* reg = uart ? "UART_DATA" : "SPI_DATA";
  if (k >= w.size())
    return fail(fmt("%s sent 0x%02x (#%zu, started at cycle %llu), but the reference run wrote "
                    "only %zu bytes to %s",
                    what, byte, k, (unsigned long long)rel(t0), w.size(), reg));
  if (w[k].byte != byte)
    return fail(fmt("%s sent 0x%02x (#%zu, started at cycle %llu), but the reference run's %s "
                    "write #%zu is 0x%02x",
                    what, byte, k, (unsigned long long)rel(t0), reg, k, w[k].byte));
  return true;
}

// The pin models, once per cycle after the design's outputs at edge t_.
bool Harness::peripherals(uint8_t uo) {
#ifdef RVFI_MODE
  const uint8_t alt = iss_.io().altfn();  // the ISS's ALTFN after the last retirement
  const bool decode_uart = true, track_spi = true;
#else
  const uint8_t alt = det_ ? gpio_.current().a : 0xFF;  // volatile programs: can't tell
  const bool ref_serial = det_;  // R33: compare with the reference run's writes
  const bool decode_uart = ex_.have_uart || ex_.have_tx_expect || (det_ && !ref_uart_.empty());
  const bool track_spi = ex_.have_spi || ex_.have_spi_tx_expect || (det_ && !ref_spi_.empty());
#endif
  if (decode_uart) {
#ifndef RVFI_MODE
    // the next frame's bit time: the reference's DIV at the matching write (expect.json
    // uart_div, when given, wins, as in rvfi mode)
    if (ref_serial && !ex_.have_uart && !uart_.tx_in_frame() &&
        uart_.tx_frames().size() < ref_uart_.size())
      uart_.set_div(ref_uart_[uart_.tx_frames().size()].div);
#endif
#ifdef RVFI_MODE
    const bool soft = uart_soft_;
#else
    const bool soft = false;
#endif
    if (!uart_.tx_step(t_, uo & 1, alt & 1, soft)) return fail(uart_.error());
#ifndef RVFI_MODE
    // lenient (ALTFN unknown): an abandoned frame is judged only by uart_tx_expect
    if (!det_ && uart_.tx_abandoned() > uart_abandoned_) {
      uart_abandoned_ = uart_.tx_abandoned();
      if (!ex_.have_tx_expect)
        return fail(fmt("UART TX: the frame that started at cycle %llu has a stop bit of 0 and the "
                        "line rose later; pins mode can't tell whether uo[0] still carried UART TX "
                        "(the program's ALTFN writes are unknown), and expect.json has no "
                        "uart_tx_expect to judge the bytes",
                        (unsigned long long)rel(uart_.tx_abandoned_t0())));
    }
#endif
#ifdef RVFI_MODE
    if (uart_.tx_started() && !xfer_start(ut_, t_, uart_.div() + 2, "a UART TX frame"))
      return false;
    if (uart_.tx_completed()) {
      const periph::UartFrame& f = uart_.tx_frames().back();
      if (!xfer_done(ut_, f.t0, f.end, f.byte, "UART TX")) return false;
    }
#else
    if (ref_serial && uart_.tx_completed()) {
      const periph::UartFrame& f = uart_.tx_frames().back();
      if (!serial_ref_check(true, uart_.tx_frames().size() - 1, f.byte, f.t0)) return false;
    }
#endif
  }
  if (track_spi) {
#ifndef RVFI_MODE
    if (ref_serial && !ex_.have_spi && !spi_.in_xfer() && spi_.xfers().size() < ref_spi_.size())
      spi_.set_d(ref_spi_[spi_.xfers().size()].div);
#endif
    if (!spi_.step(t_, (uo >> 4) & 1, (uo >> 5) & 1, (uo >> 6) & 1, (alt & 0x70) == 0x70))
      return fail(spi_.error());
#ifdef RVFI_MODE
    if (spi_.rose() && spi_.in_xfer() && spi_.xfer_first_rise() == t_ &&
        !xfer_start(st_, t_, INF, "an SPI transfer"))
      return false;
    if (spi_.done()) {
      const periph::SpiXfer& x = spi_.xfers().back();
      if (!xfer_done(st_, x.first_rise, x.last_fall, x.mosi, "SPI MOSI")) return false;
    }
#else
    if (ref_serial && spi_.done()) {
      const periph::SpiXfer& x = spi_.xfers().back();
      if (!serial_ref_check(false, spi_.xfers().size() - 1, x.mosi, x.first_rise)) return false;
    }
#endif
  }
  if (!ex_.pwm.empty()) {
#ifndef RVFI_MODE
    if (pwm_marker_ == INF && gmatch(uo, PWM_MARKER, mark_alt())) start_meters(t_);
#endif
    for (size_t i = 0; i < meters_.size(); i++) {
      periph::PwmMeter& m = meters_[i];
      if (m.done()) continue;
#ifdef RVFI_MODE
      if (!((alt >> meter_pin_[i]) & 1))
        return fail(fmt("%s: ALTFN bit %d was cleared before the measurement finished (after the "
                        "0xA5 marker the output must keep running for up to 4 periods: one to "
                        "settle, then one full period measured from a rising edge)",
                        m.name().c_str(), meter_pin_[i]));
#endif
      if (!m.step(t_, (uo >> meter_pin_[i]) & 1)) return fail(m.error());
      if (!m.done() && t_ > m.deadline())
        return fail(m.name() + ": the measurement did not finish");
    }
  }
  if (ex_.ir_gate.on) {
#ifndef RVFI_MODE
    if (ir_marker_ == INF && gmatch(uo, IR_MARKER, mark_alt())) start_ir(t_);
#endif
    if (ir_ && !ir_->done()) {
#ifdef RVFI_MODE
      if ((alt & 0x82) != 0x82)
        return fail(fmt("IR gate: ALTFN = 0x%02x cleared bit 1 or 7 before the measurement "
                        "finished (after the 0xA6 marker uo[1] must carry the gated PWM0 and uo[7] "
                        "the timer output until %llu marks have passed)",
                        alt, (unsigned long long)ex_.ir_gate.marks));
#endif
      if (!ir_->step(t_, (uo >> 1) & 1, (uo >> 7) & 1)) return fail(ir_->error());
    }
  }
  if (ex_.coremark_iters) {  // R21, R24: the marks on the GPIO-mode pins
    const uint8_t ma = mark_alt();
    const bool m0 = gmatch(uo, CM_START, ma), m1 = gmatch(uo, CM_END, ma);
    if (cm_start_ == INF && m0 && !cm_was0_) cm_start_ = t_;
    else if (cm_start_ != INF && cm_end_ == INF && m1 && !cm_was1_) cm_end_ = t_;
    cm_was0_ = m0;
    cm_was1_ = m1;
  }
  if (uo != uo_last_) {
    if (uo_seen_.size() < 4096) uo_seen_.push_back(uo);
    uo_last_ = uo;
  }
#ifndef RVFI_MODE
  if (!det_) {  // judged by the self-check: the GPIO-mode pins show result_gpio, held (R24)
    const uint8_t fa = ex_.final_altfn;
    if (!gmatch(ex_.result_gpio, GPIO_FAIL, fa) && gmatch(uo, GPIO_FAIL, fa))
      return fail(fmt("the program's self-check failed: uo_out = 0x%02x shows 0xee on the pins "
                      "outside final_altfn 0x%02x (the default trap or a failed check)",
                      uo, fa));
    if (gmatch(uo, ex_.result_gpio, fa)) {
      if (result_since_ == INF) result_since_ = t_;
    } else {
      result_since_ = INF;
      if (prog_end_ != INF)
        return fail(fmt("uo_out changed from result_gpio 0x%02x (final_altfn 0x%02x) to 0x%02x "
                        "after the program ended",
                        ex_.result_gpio, fa, uo));
    }
    if (prog_end_ == INF && result_since_ != INF && t_ - result_since_ >= RESULT_STABLE) {
      prog_end_ = result_since_;
      cycles_ = rel(result_since_);
      end_at_ = t_;
      end_reason_ = "result_gpio";
    }
  }
#endif
  return true;
}

// The run may stop: past the drain, and no peripheral activity the checks still wait for.
bool Harness::end_ready() {
  if (t_ < end_at_) return false;
  if (uart_.tx_in_frame() && t_ < end_at_ + 12 * (uint64_t)uart_.bit_time()) return false;
  if (spi_.in_xfer() && t_ < end_at_ + 20 * (uint64_t)spi_.half()) return false;
#ifdef RVFI_MODE
  if (ut_.pend.active || st_.pend.active) return false;  // their deadlines bound this
#endif
  for (const periph::PwmMeter& m : meters_)
    if (!m.done() && t_ <= m.deadline()) return false;
  if (ir_ && !ir_->done() && t_ <= ir_->deadline() + periph::IrGateMeter::SKEW + 1) return false;
  return true;
}

bool Harness::final_checks() {
  if (!check_psram_writes(true)) return false;
#ifdef RVFI_MODE
  const bool cmp_mem = true;
#else
  const bool cmp_mem = det_;
#endif
  if (cmp_mem)
    for (uint32_t a : written_.list()) {
      if (a >= v3::REGION_LO) continue;
      const uint8_t got = pmod_.psram_byte(a), want = iss_.psram(a);
      if (got != want)
        return fail(fmt("final PSRAM byte 0x%06x (CPU 0x%08x) is 0x%02x, the program stored 0x%02x "
                        "(compared %llu cycles after the program end)",
                        a, 0x01000000u | a, got, want, (unsigned long long)(t_ - prog_end_)));
    }
#ifdef RVFI_MODE
  if (ex_.have_result) {
    const GState g = iss_gpio();
    if (!gmatch(g.g, ex_.result_gpio, ex_.final_altfn))
      return fail(fmt("the program's self-check failed: it ended with GPIO_OUT = 0x%02x, "
                      "expect.json result_gpio is 0x%02x (final_altfn 0x%02x)",
                      g.g, ex_.result_gpio, ex_.final_altfn));
    if (g.a != ex_.final_altfn)
      return fail(fmt("test program bug: the program ended with ALTFN = 0x%02x, but expect.json "
                      "final_altfn is 0x%02x",
                      g.a, ex_.final_altfn));
  }
  // nothing may still be in flight when the run ends
  if (spi_.in_xfer())
    return fail(fmt("an SPI transfer (first rising SCK edge at cycle %llu) was still in progress "
                    "at the end of the run",
                    (unsigned long long)rel(spi_.xfer_first_rise())));
  if (ut_.pend.active || st_.pend.active)
    return fail(fmt("the %s write that retired at cycle %llu had not started its transfer at the "
                    "end of the run",
                    ut_.pend.active ? "UART_DATA" : "SPI_DATA",
                    (unsigned long long)rel(ut_.pend.active ? ut_.pend.s : st_.pend.s)));
#endif
  if (!uart_.tx_finish(t_)) return fail(uart_.error());
  if (ex_.have_tx_expect && uart_.tx_text() != ex_.uart_tx_expect)
    return fail(fmt("UART TX sent %s, expect.json uart_tx_expect is %s",
                    json_str(uart_.tx_text()).c_str(), json_str(ex_.uart_tx_expect).c_str()));
  if (ex_.have_spi_tx_expect) {
    std::string mosi;
    for (const periph::SpiXfer& x : spi_.xfers()) mosi += (char)x.mosi;
    if (mosi != ex_.spi_tx_expect)
      return fail(fmt("SPI MOSI sent %s, expect.json spi_tx_expect is %s", json_str(mosi).c_str(),
                      json_str(ex_.spi_tx_expect).c_str()));
  }
#ifndef RVFI_MODE
  if (det_ && uart_.tx_frames().size() != ref_uart_.size())
    return fail(fmt("UART TX sent %zu bytes, the reference run wrote %zu to UART_DATA with "
                    "ALTFN[0] set (R33; a write while TX_BUSY = 1 is ignored, so a program that "
                    "doesn't poll TX_BUSY isn't deterministic)",
                    uart_.tx_frames().size(), ref_uart_.size()));
  if (det_ && spi_.xfers().size() != ref_spi_.size())
    return fail(fmt("SPI MOSI sent %zu bytes, the reference run wrote %zu to SPI_DATA with "
                    "ALTFN[6:4] set (R33)",
                    spi_.xfers().size(), ref_spi_.size()));
#endif
  if (ex_.coremark_iters && (cm_start_ == INF || cm_end_ == INF))
    return fail("expect.json has coremark_iters, but uo_out never changed to 0xc0 and then to "
                "0xc1 (the CoreMark timing marks)");
  if (!ex_.pwm.empty()) {
    if (pwm_marker_ == INF)
      return fail("expect.json lists pwm checks, but the program never wrote GPIO_OUT = 0xa5");
    for (const periph::PwmMeter& m : meters_)
      if (!m.done()) return fail(m.name() + ": the measurement did not finish");
  }
  if (ex_.ir_gate.on) {
    if (ir_marker_ == INF)
      return fail("expect.json has ir_gate, but the program never wrote GPIO_OUT = 0xa6");
    if (!ir_->done())
      return fail(fmt("IR gate: the measurement did not finish (%llu of %llu marks)",
                      (unsigned long long)ir_->marks_seen(),
                      (unsigned long long)ex_.ir_gate.marks));
  }
  return true;
}

#ifdef RVFI_MODE
std::vector<GState> Harness::predictions() const {
  std::vector<GState> v;
  GState s;
  if (predict_gpio_store(iss_, iss_.pc(), &s)) v.push_back(s);
  if (iss_.irq_allowed() && predict_gpio_store(iss_, iss_.csr_peek(0x305), &s)) v.push_back(s);
  return v;
}

void Harness::xfer_gate(XferTrack& x, bool gate, uint64_t settle) {
  if (gate == x.gated) return;
  if (!gate) {
    x.gated = false;
    x.ungated_writes = (x.pend.active || x.orphan || !x.fly.empty()) ? 1 : 0;
    x.pend.active = x.orphan = x.orphan_done = false;
    x.fly.clear();
    x.unknown_to = INF;
    x.unknown_from = t_;
    x.busy.unknown(t_, INF);
  } else {
    x.gated = true;
    if (x.ungated_writes) {
      x.unknown_to = t_ + settle;
      x.busy.unknown(x.unknown_from, x.unknown_to);
    } else {
      x.unknown_to = 0;
      x.busy.unknown(0, 0);
    }
    x.ungated_writes = 0;
  }
}

// A byte-0 write of UART_DATA or SPI_DATA retired at edge s (the previous retirement at p).
bool Harness::xfer_write(XferTrack& x, uint64_t p, uint64_t s, uint8_t byte, uint64_t deadline) {
  if (!x.gated) {
    x.ungated_writes++;
    return true;
  }
  const uint64_t lo = p + 1 > BUSY_SLACK ? p + 1 - BUSY_SLACK : 0;
  if (x.orphan && x.orphan_t0 >= p) {  // the transfer started before its write retired
    BusyTrack::Ep& e = x.busy.ep(x.orphan_ep);
    e.ps = std::max(e.ps, lo);
    x.orphan = false;
    if (x.orphan_done) {  // it already ended (xfer_done closed its episode): check its byte here
      x.orphan_done = false;
      if (x.orphan_byte != byte)
        return fail(fmt("%s sent 0x%02x (started at cycle %llu), but the program wrote 0x%02x",
                        x.orphan_what, x.orphan_byte, (unsigned long long)rel(x.orphan_t0),
                        byte));
      return true;
    }
    x.fly.push_back(XferTrack::Fly{x.orphan_t0, byte, x.orphan_ep});
    return true;
  }
  const int m = x.busy.must(p + 1, s);
  if (m == 1) return true;  // written while busy: ignored (contract 5.5, 5.6)
  x.pend.active = true;
  x.pend.maybe = m != 0;  // a transfer may have been ending: the write may have been ignored
  x.pend.s = s;
  x.pend.p = p;
  x.pend.byte = byte;
  x.pend.deadline = deadline;
  x.pend.ep = x.busy.open(lo, x.pend.maybe ? INF : s + 1);
  return true;
}

// A transfer started on the pins at t0 (UART: falling start bit; SPI: first rising SCK edge).
// lead: the most cycles its write can precede t0 (INF: unknown).
bool Harness::xfer_start(XferTrack& x, uint64_t t0, uint64_t lead, const char* what) {
  if (!x.gated) return true;
  const uint64_t back = (lead != INF && t0 > lead) ? t0 - lead : 0;
  if (t0 < x.unknown_to) {  // its write may predate the pins showing the port
    x.fly.push_back(XferTrack::Fly{t0, -1, x.busy.open(back, t0)});
    return true;
  }
  if (x.pend.active) {
    BusyTrack::Ep& e = x.busy.ep(x.pend.ep);
    if (back > BUSY_SLACK) e.ps = std::max(e.ps, back - BUSY_SLACK);
    if (x.pend.maybe) e.ds = t0;
    x.fly.push_back(XferTrack::Fly{t0, x.pend.byte, x.pend.ep});
    x.pend.active = false;
    return true;
  }
  if (x.orphan)
    return fail(fmt("%s started at cycle %llu while the one that started at cycle %llu still has "
                    "no data-register write: every transfer needs a byte-0 write",
                    what, (unsigned long long)rel(t0), (unsigned long long)rel(x.orphan_t0)));
  x.orphan = true;
  x.orphan_t0 = t0;
  const uint64_t ps = std::max(last_ret_ + 1, back);
  x.orphan_ep = x.busy.open(ps > BUSY_SLACK ? ps - BUSY_SLACK : 0, t0);
  return true;
}

bool Harness::xfer_done(XferTrack& x, uint64_t t0, uint64_t end, uint8_t byte, const char* what) {
  if (!x.gated) return true;
  if (x.fly.empty()) {
    if (!x.orphan || x.orphan_done) return true;
    // the transfer ended before its write retired: close its episode now; xfer_write checks the
    // byte (and the next retirement must be that write, or the orphan check fails)
    BusyTrack::Ep& e = x.busy.ep(x.orphan_ep);
    e.de = end > BUSY_SLACK ? end - BUSY_SLACK : 0;
    e.pe = end + BUSY_SLACK;
    x.orphan_done = true;
    x.orphan_byte = byte;
    x.orphan_what = what;
    return true;
  }
  const XferTrack::Fly f = x.fly.front();
  x.fly.pop_front();
  BusyTrack::Ep& e = x.busy.ep(f.ep);
  e.de = end > BUSY_SLACK ? end - BUSY_SLACK : 0;
  e.pe = end + BUSY_SLACK;
  if (f.byte >= 0 && f.byte != byte)
    return fail(fmt("%s sent 0x%02x (started at cycle %llu), but the program wrote 0x%02x", what,
                    byte, (unsigned long long)rel(t0), f.byte));
  return true;
}

bool Harness::xfer_deadline(XferTrack& x, const char* what) {
  if (!x.pend.active || t_ <= x.pend.deadline) return true;
  if (!x.pend.maybe)
    return fail(fmt("%s by cycle %llu after the write that retired at cycle %llu", what,
                    (unsigned long long)rel(x.pend.deadline), (unsigned long long)rel(x.pend.s)));
  x.pend.active = false;  // the write landed while busy and was ignored
  BusyTrack::Ep& e = x.busy.ep(x.pend.ep);
  e.ds = INF;
  e.de = 0;
  e.pe = t_ + BUSY_SLACK;
  return true;
}

// R20: a TX_BUSY / BUSY / TXIDLE read must match what the pins show over the reading
// instruction. busy: the busy level the read reports; raw: the bit as read.
bool Harness::busy_read(XferTrack& x, bool busy, int raw, uint64_t p, const char* what) {
  if (!x.gated) return true;
  const int m = x.busy.must(p + 1, t_);
  if (m == 1 && !busy)
    return fail(fmt("%s read %d (not busy) in the instruction retiring now (cycles %llu-%llu), but "
                    "the pins show its transfer in progress (or its start pending) over all of it "
                    "(contract 5.5, 5.6)",
                    what, raw, (unsigned long long)rel(p + 1), (unsigned long long)rel(t_)));
  if (m == 0 && busy)
    return fail(fmt("%s read %d (busy) in the instruction retiring now (cycles %llu-%llu), but no "
                    "transfer was in progress or pending on the pins (contract 5.5, 5.6)",
                    what, raw, (unsigned long long)rel(p + 1), (unsigned long long)rel(t_)));
  return true;
}

// R30 (contract 6): GPIO_IN reads ui_in as sampled during the load's execution, at most 3 cycles
// stale: the value must be the ui_in the design saw at some edge in [p + 1 - 3, t].
bool Harness::gpio_in_read(uint8_t value, uint64_t p, const std::string& who) {
  uint64_t lo = std::max(p + 1 > UI_STALE ? p + 1 - UI_STALE : 0, reset_edges_ - 1);
  if (t_ - lo >= UI_HIST) lo = t_ - UI_HIST + 1;
  std::string seen;
  int nseen = 0;
  for (uint64_t e = lo; e <= t_; e++) {
    const uint8_t v = ui_hist_[e % UI_HIST];
    if (v == value) return true;
    if ((e == lo || v != ui_hist_[(e - 1) % UI_HIST]) && nseen++ < 6)
      seen += fmt("%s0x%02x", nseen > 1 ? ", " : "", v);
  }
  return fail(fmt("GPIO_IN read 0x%02x at %s, but ui_in was %s%s at the edges of cycles %llu-%llu "
                  "(the load's execution and the 3 cycles before it; contract 6: at most 3 cycles "
                  "stale)",
                  value, who.c_str(), seen.c_str(), nseen > 6 ? ", ..." : "",
                  (unsigned long long)rel(lo), (unsigned long long)rel(t_)));
}

bool Harness::compare(const Retire& e, const std::string& who) {
  const uint32_t op = e.insn & 0x7F;
  const bool trap_reads = e.trap && (op == 0x63 || op == 0x67);
  struct F {
    const char* name;
    uint64_t got, exp;
    bool on_trap;
  } fields[] = {
      {"rvfi_order", top_->rvfi_order, retired_, true},
      {"rvfi_intr", top_->rvfi_intr, e.intr, true},
      {"rvfi_mode", top_->rvfi_mode, 3, true},
      {"rvfi_ixl", top_->rvfi_ixl, 1, true},
      {"rvfi_insn", top_->rvfi_insn, e.insn, true},
      {"rvfi_pc_rdata", top_->rvfi_pc_rdata, e.pc, true},
      {"rvfi_trap", top_->rvfi_trap, e.trap, true},
      {"rvfi_halt", top_->rvfi_halt, 0, true},
      {"rvfi_rd_addr", top_->rvfi_rd_addr, e.rd, true},
      {"rvfi_mem_wmask", top_->rvfi_mem_wmask, e.wmask, true},
      {"rvfi_pc_wdata", top_->rvfi_pc_wdata, e.npc, true},  // on a trap: mtvec (contract 8)
      {"rvfi_rs1_addr", top_->rvfi_rs1_addr, e.rs1, trap_reads},
      {"rvfi_rs1_rdata", top_->rvfi_rs1_rdata, e.rs1v, trap_reads},
      {"rvfi_rs2_addr", top_->rvfi_rs2_addr, e.rs2, trap_reads},
      {"rvfi_rs2_rdata", top_->rvfi_rs2_rdata, e.rs2v, trap_reads},
      // a trap writes no register and accesses no memory: rd_addr = 0, so rd_wdata = 0 (RVFI),
      // and its masks are 0 (contract 8)
      {"rvfi_rd_wdata", top_->rvfi_rd_wdata, e.trap ? 0u : e.rdv, true},
      {"rvfi_mem_rmask", top_->rvfi_mem_rmask, e.trap ? 0u : e.rmask, true},
      {"rvfi_mem_addr", top_->rvfi_mem_addr, (e.rmask | e.wmask) ? e.maddr : top_->rvfi_mem_addr,
       false},
  };
  for (const F& f : fields)
    if ((f.on_trap || !e.trap) && f.got != f.exp)
      return fail(fmt("RVFI mismatch at %s: %s = 0x%llx, expected 0x%llx", who.c_str(), f.name,
                      (unsigned long long)f.got, (unsigned long long)f.exp));
  const uint32_t rl = v3::lane_bits(e.rmask), wl = v3::lane_bits(e.wmask);
  if ((top_->rvfi_mem_rdata & rl) != (e.mrdata & rl))
    return fail(fmt("RVFI mismatch at %s: rvfi_mem_rdata = 0x%08x, expected 0x%08x (lanes 0x%08x)",
                    who.c_str(), top_->rvfi_mem_rdata, e.mrdata, rl));
  if ((top_->rvfi_mem_wdata & wl) != (e.mwdata & wl))
    return fail(fmt("RVFI mismatch at %s: rvfi_mem_wdata = 0x%08x, expected 0x%08x (lanes 0x%08x)",
                    who.c_str(), top_->rvfi_mem_wdata, e.mwdata, wl));
  return true;
}

// One rvfi_valid: interrupt entry if any, then the ISS steps with the design's volatile values.
bool Harness::retirement() {
  const uint64_t p = last_ret_, gap = t_ - p;
  // R31 (contract 4): only the gap that starts at a WFI's retirement is exempt (WFI retires, then
  // may stall); the gap before a WFI and an interrupt entry before one are not
  const bool exempt = retired_ > 0 && last_insn_ == INSN_WFI;
  if (gap > LAT_CAP && !exempt)
    return fail(fmt("latency cap: retirement #%llu (pc 0x%08x, insn 0x%08x) came %llu cycles after "
                    "%s (limit %llu, contract 4)",
                    (unsigned long long)retired_, top_->rvfi_pc_rdata, top_->rvfi_insn,
                    (unsigned long long)gap, retired_ ? "the previous retirement" : "reset release",
                    (unsigned long long)LAT_CAP));
  if (top_->rvfi_intr && !last_trap_) {  // an interrupt entry (not a trap handler's first insn)
    if (!iss_.irq_allowed()) {
      const uint32_t ms = iss_.csr_peek(0x300), mie = iss_.csr_peek(0x304);
      return fail(fmt("interrupt while masked: retirement #%llu (pc 0x%08x) has rvfi_intr = 1 "
                      "after a non-trap boundary, so it is an interrupt entry, but mstatus.MIE = "
                      "%u and mie.MEIE = %u (contract 4: taken only when MIE, MEIE and MEIP are "
                      "all 1)",
                      (unsigned long long)retired_, top_->rvfi_pc_rdata, (ms >> 3) & 1,
                      (mie >> 11) & 1));
    }
    iss_.take_interrupt();
    interrupts_++;
  }
  vread_used_ = false;
  v3::IoHooks hooks;
  hooks.volatile_read = [this](uint32_t slot, uint32_t) -> uint32_t {
    const uint32_t v = slot == v3::VSLOT_MIP
                           ? top_->rvfi_rd_wdata
                           : top_->rvfi_mem_rdata << (8 * (top_->rvfi_mem_addr & 3));
    vread_used_ = true;
    vread_slot_ = slot;
    vread_val_ = v;
    return v;
  };
  const Retire e = iss_.step(hooks);
  const std::string who = describe(retired_, e);
  if (e.reserved)
    return fail(fmt("test program bug: the ISS hit %s at retirement #%llu (pc 0x%08x, insn "
                    "0x%08x)",
                    unspecified_what(e), (unsigned long long)retired_, e.pc, e.insn));
  if (iss_.uninit_read())
    return fail(fmt("test program bug: a register or arbitrary-reset CSR is read before any write "
                    "at pc 0x%08x (the design's value is unknown)",
                    iss_.uninit_pc()));
  if (o_.verbose)
    std::fprintf(stderr, "%llu %s%s\n", (unsigned long long)rel(t_), who.c_str(),
                 e.intr ? " [intr]" : "");
  // PSRAM starts arbitrary (contract 3): reading a byte the program never wrote is a program bug
  if (area_of(e.pc) == Area::Psram)
    for (uint32_t k = 0; k < 4; k++)
      if (!written_.has(e.pc + k))
        return fail(fmt("test program bug: %s fetches PSRAM byte 0x%08x, which the program never "
                        "wrote (PSRAM starts arbitrary, contract 3)",
                        who.c_str(), e.pc + k));
  if (e.rmask && area_of(e.maddr) == Area::Psram)
    for (int k = 0; k < __builtin_popcount(e.rmask); k++)
      if (!written_.has(e.maddr + k))
        return fail(fmt("test program bug: %s loads PSRAM byte 0x%08x, which the program never "
                        "wrote (PSRAM starts arbitrary, contract 3)",
                        who.c_str(), e.maddr + k));
  if (!compare(e, who)) return false;
  if (e.wmask && area_of(e.maddr) == Area::Psram)
    for (int k = 0; k < __builtin_popcount(e.wmask); k++) {
      stores_.add((e.maddr + k) & (v3::PSRAM_SIZE - 1), (uint8_t)(e.mwdata >> (8 * k)));
      written_.mark(e.maddr + k);
    }
  if (!check_psram_writes(false)) return false;
  // GPIO pins: this retirement's state is due next cycle; predict the next instruction's
  const std::string ge = gpio_.retire(iss_gpio(), t_, who);
  if (!ge.empty()) return fail(ge);
  const std::vector<GState> next = predictions();
  gpio_.set_alts(next);
  // UART TX and the SPI master: pins vs writes and busy reads
  const uint8_t alt = iss_.io().altfn();
  // the UART gate is unknown while a store clearing ALTFN[0] executes (the pin may take its GPIO
  // value any time before the store retires; from its retirement the gate is 0)
  uart_soft_ = false;
  for (const GState& g : next) uart_soft_ = uart_soft_ || ((alt & 1) && !(g.a & 1));
  xfer_gate(ut_, alt & 1, uart_settle());
  xfer_gate(st_, (alt & 0x70) == 0x70, spi_settle());
  if (e.wmask && area_of(e.maddr) == Area::Io && (e.maddr & 3) == 0) {
    const uint32_t slot = (e.maddr >> 2) & 31;
    if (slot == SLOT_UART_DATA) {
      if (!ex_.have_uart && !uart_.tx_in_frame())
        uart_.set_div(iss_.io().r[SLOT_UART_CTRL] & 0x1FFF);
      // R28 (contract 5.5): the start bit begins within DIV+2 cycles after the store retires
      if (!xfer_write(ut_, p, t_, (uint8_t)e.mwdata, t_ + uart_.div() + 2)) return false;
    } else if (slot == SLOT_SPI_DATA) {
      if (!ex_.have_spi && !spi_.in_xfer()) spi_.set_d((iss_.io().r[SLOT_SPI_CTRL] >> 1) & 7);
      if (!xfer_write(st_, p, t_, (uint8_t)e.mwdata, t_ + LAT_CAP)) return false;
    }
  }
  if (ut_.orphan)
    return fail(fmt("a UART TX frame started at cycle %llu, but %s is not a UART_DATA write",
                    (unsigned long long)rel(ut_.orphan_t0), who.c_str()));
  if (st_.orphan)
    return fail(fmt("an SPI transfer started at cycle %llu, but %s is not an SPI_DATA write",
                    (unsigned long long)rel(st_.orphan_t0), who.c_str()));
  if (vread_used_) {
    const int tx_busy = (vread_val_ >> 13) & 1, spi_busy = (vread_val_ >> 4) & 1,
              txidle = (vread_val_ >> 3) & 1;
    if (vread_slot_ == SLOT_UART_CTRL &&
        !busy_read(ut_, tx_busy, tx_busy, p, "TX_BUSY (UART_CTRL[13])"))
      return false;
    if (vread_slot_ == SLOT_IRQ_PEND &&
        !busy_read(ut_, !txidle, txidle, p, "TXIDLE (IRQ_PEND[3], = !TX_BUSY)"))
      return false;
    if (vread_slot_ == SLOT_SPI_CTRL &&
        !busy_read(st_, spi_busy, spi_busy, p, "BUSY (SPI_CTRL[4])"))
      return false;
    if (vread_slot_ == SLOT_GPIO_IN && !gpio_in_read((uint8_t)vread_val_, p, who)) return false;
    if (vread_slot_ == SLOT_SPI_DATA && ex_.have_spi && st_.gated && !spi_.xfers().empty() &&
        st_.busy.must(p + 1, t_) == 0) {
      const periph::SpiXfer& x = spi_.xfers().back();
      if (x.selected && (vread_val_ & 0xFF) != x.miso)
        return fail(fmt("SPI_DATA read 0x%02x at %s, but the device sent 0x%02x on MISO in the "
                        "last transfer (contract 5.6)",
                        vread_val_ & 0xFF, who.c_str(), x.miso));
    }
  }
  // R24: the marks and 0xEE on the GPIO-mode pins (GPIO_OUT outside ALTFN / final_altfn)
  const uint8_t go = iss_.io().gpio_out();
  const bool mk = gmatch(go, PWM_MARKER, iss_.io().altfn());
  if (!ex_.pwm.empty() && mk && !marker_was_) start_meters(t_);
  marker_was_ = mk;
  const bool imk = gmatch(go, IR_MARKER, iss_.io().altfn());
  if (ex_.ir_gate.on && imk && !ir_marker_was_) start_ir(t_);
  ir_marker_was_ = imk;
  const bool fl = gmatch(go, GPIO_FAIL, ex_.final_altfn);
  if (ex_.have_result && !gmatch(ex_.result_gpio, GPIO_FAIL, ex_.final_altfn) && fl &&
      !fail_was_)
    return fail(fmt("the program's self-check failed: %s wrote GPIO_OUT = 0x%02x, 0xee outside "
                    "final_altfn 0x%02x (the default trap or a failed check)",
                    who.c_str(), go, ex_.final_altfn));
  fail_was_ = fl;
  // bookkeeping
  if (retired_ == 0) {
    first_gap_ = gap;
    set_anchor(t_);
  }
  if (!exempt) max_gap_ = std::max(max_gap_, gap);
  uint64_t& pc = per_class_[iss_class(e)];
  pc = std::max(pc, gap);
  last_ret_ = t_;
  last_insn_ = e.insn;
  last_trap_ = e.trap;
  retired_++;
  if (iss_.selfloop() && prog_end_ == INF) {
    prog_end_ = t_;
    cycles_ = rel(t_);
    retired_at_end_ = retired_;
    end_at_ = t_ + DRAIN;
    end_reason_ = "selfloop";
  }
  return true;
}
#endif  // RVFI_MODE

int Harness::run() {
  const auto clock0 = std::chrono::steady_clock::now();
  ctx_ = new VerilatedContext;
  ctx_->commandArgs(o_.argc, const_cast<const char**>(o_.argv));
  ctx_->randReset(2);  // random power-up values for every state element (--x-initial unique)
  ctx_->randSeed((int)o_.seed);
  top_ = new Vtt_um_fesens_r32v(ctx_);
#ifdef HARNESS_TRACE
  if (o_.trace) {
    ctx_->traceEverOn(true);
    tfp_ = new VerilatedFstC;
    top_->trace(tfp_, 99);
    tfp_->open(o_.trace);
  }
#endif
  reset_edges_ = 1 + ((o_.seed + 8) * 5) % 9;  // seed 1: the minimum, one edge
  if (o_.dump_pins && !(pins_dump_ = std::fopen(o_.dump_pins, "w"))) {
    std::fprintf(stderr, "cannot write %s\n", o_.dump_pins);
    return 2;
  }
  last_ret_ = reset_edges_ - 1;
  if (pre_run()) {
#ifdef RVFI_MODE
    gpio_.init(iss_gpio());
    gpio_.set_alts(predictions());  // ALTFN resets to 0: no UART soft window yet
    marker_was_ = gmatch(iss_.io().gpio_out(), PWM_MARKER, iss_.io().altfn());
    ir_marker_was_ = gmatch(iss_.io().gpio_out(), IR_MARKER, iss_.io().altfn());
    fail_was_ = gmatch(iss_.io().gpio_out(), GPIO_FAIL, ex_.final_altfn);
    uart_.set_strict(true);  // ALTFN is known: a stop bit of 0 is a framing error
#else
    uart_.set_lenient(!det_);  // ALTFN unknown: a cut frame may be the pin going back to GPIO
    if (!det_ && !ex_.have_result)
      fail("pins mode can't judge this program: it reads volatile registers or enables "
           "interrupts, so the ISS can't run it alone, and expect.json has no result_gpio");
    else if (!det_ && ex_.have_tx_expect && !ex_.have_uart)
      fail("pins mode needs expect.json uart_div to decode uart_tx_expect for a program the ISS "
           "can't run alone");
    else if (!det_ && ex_.have_spi_tx_expect && !ex_.have_spi)
      fail("pins mode needs expect.json spi_div_d to check spi_tx_expect for a program the ISS "
           "can't run alone");
#endif
  }
  bool passed = false;
  top_->clk = 0;
  top_->rst_n = 0;
  top_->ena = 1;
  top_->ui_in = 0;
  top_->uio_in = pmod_.uio_in();
  top_->eval();
  tick_trace(0);
  for (t_ = 0; error_.empty(); t_++) {
    top_->clk = 1;
    top_->eval();
    tick_trace(2 * t_ + 1);
    const uint32_t out = outputs();
    const uint8_t uo = top_->uo_out, uio = top_->uio_out, oe = top_->uio_oe;
    const bool in_reset = t_ < reset_edges_;
    if (pins_dump_ && !in_reset && (!pins_dumped_ || uo != pins_last_)) {
      // observation only: "cycle uo_out" (hex), cycles counted as in error messages
      std::fprintf(pins_dump_, "%llu %02x\n", (unsigned long long)rel(t_), uo);
      pins_dumped_ = true;
      pins_last_ = uo;
    }
    if (in_reset && ((uio & 0x41) != 0x41 || (uio & 0x08) || uo)) {
      fail(fmt("during reset both CS_n must be 1, SCK 0 and uo_out 0 (section 1): uio_out = "
               "0x%02x, uo_out = 0x%02x",
               uio, uo));
      break;
    }
#ifdef RVFI_MODE
    if (in_reset && top_->rvfi_valid) {
      fail("rvfi_valid during reset");
      break;
    }
#endif
    if (!pmod_.edge(t_, uio, oe)) {
      fail(pmod_.error());
      break;
    }
    if (!in_reset) {
#ifdef RVFI_MODE
      if (top_->rvfi_valid && !retirement()) break;
      if (!xfer_deadline(ut_, "no UART start bit within DIV+2 cycles (contract 5.5)") ||
          !xfer_deadline(st_, "no SPI transfer within the latency cap"))
        break;
      if (!(retired_ > 0 && last_insn_ == INSN_WFI) && t_ - last_ret_ >= LAT_CAP + 1) {
        fail(fmt("latency cap: no retirement within %llu cycles of %s (next pc 0x%08x)",
                 (unsigned long long)LAT_CAP,
                 retired_ ? "the previous retirement" : "reset release", iss_.pc()));
        break;
      }
      const bool track = true;
#else
      if (anchor_ == INF && (pmod_.flash_reads() >= 4 || rel(t_) >= LAT_CAP)) set_anchor(t_);
      const bool track = det_;
#endif
      if (track) {
        const std::string e = gpio_.advance(uo, t_);
        if (!e.empty()) {
          fail(e);
          break;
        }
      }
      if (!peripherals(uo)) break;
#ifndef RVFI_MODE
      if (!check_psram_writes(false)) break;
      if (det_ && prog_end_ == INF && gpio_.at_last()) {
        // the pins show the reference's final state: the rest of the program (ref_tail_
        // instructions, each within the cap) and the drain fit in (tail + 2) x LAT_CAP cycles
        prog_end_ = t_;
        cycles_ = rel(t_);
        end_at_ = t_ + (ref_tail_ + 2) * LAT_CAP;
        end_reason_ = "selfloop";
      }
#endif
      if (prog_end_ == INF && rel(t_) > budget_) {
        fail(fmt("timeout after %llu cycles (%s)", (unsigned long long)rel(t_),
                 o_.max_cycles    ? "--max-cycles"
                 : ex_.max_cycles ? "expect.json max_cycles"
                 : det_ ? "the latency cap bounds a correct run by 2,048 x (ISS retirements + 16)"
                        : "default budget of 50,000,000 cycles for programs the ISS can't run "
                          "alone; see expect.json max_cycles or --max-cycles"));
        break;
      }
      if (end_ready()) {
        passed = final_checks();
        break;
      }
    }
    // falling edge: inputs change; outputs must not
    top_->clk = 0;
    const bool rst_change = (t_ + 1 == reset_edges_);
    top_->rst_n = t_ + 1 >= reset_edges_;
    top_->ui_in = next_ui(uo);
    ui_hist_[(t_ + 1) % UI_HIST] = top_->ui_in;  // what the design samples at edge t_ + 1
    top_->uio_in = pmod_.uio_in();
    top_->eval();
    tick_trace(2 * t_ + 2);
    if (!rst_change && outputs() != out) {
      fail(fmt("outputs changed between rising clk edges (uo_out/uio_out/uio_oe 0x%06x -> 0x%06x): "
               "they must come from posedge flip-flops, not from clk or an input",
               out, outputs()));
      break;
    }
  }
  if (!error_.empty()) passed = false;
  if (o_.dump_psram) {
    if (FILE* f = std::fopen(o_.dump_psram, "wb")) {
      std::vector<uint8_t> m(v3::PSRAM_SIZE);
      for (uint32_t a = 0; a < v3::PSRAM_SIZE; a++) m[a] = pmod_.psram_byte(a);
      std::fwrite(m.data(), 1, m.size(), f);
      std::fclose(f);
    }
  }
  if (pins_dump_) {
    std::fprintf(pins_dump_, "%llu end\n", (unsigned long long)rel(t_));
    std::fclose(pins_dump_);
  }
#ifdef HARNESS_TRACE
  if (tfp_) tfp_->close();
#endif
  top_->final();
  summary(passed, std::chrono::duration<double>(std::chrono::steady_clock::now() - clock0).count());
  return passed ? 0 : 1;
}

void Harness::summary(bool passed, double secs) {
#ifdef RVFI_MODE
  const char* mode = "rvfi";
  const bool timed = retired_ > 0;
  const uint64_t retired = prog_end_ != INF ? retired_at_end_ : retired_;
  const bool have_retired = true;
#else
  const char* mode = "pins";
  const bool timed = false;
  const uint64_t retired = ref_retired_;
  const bool have_retired = det_;
#endif
  const bool have_cycles = prog_end_ != INF;
  std::string cpi = "null";
  if (have_retired && have_cycles && retired) cpi = fmt("%.3f", (double)cycles_ / (double)retired);
  std::string cm = "null";
  if (ex_.coremark_iters && cm_start_ != INF && cm_end_ != INF && cm_end_ > cm_start_) {
    const uint64_t c = cm_end_ - cm_start_;
    cm = c % ex_.coremark_iters == 0 ? std::to_string(c / ex_.coremark_iters)
                                     : fmt("%.6f", (double)c / (double)ex_.coremark_iters);
  }
  std::string pcj = "{";
  for (auto& kv : per_class_)
    pcj += (pcj.size() > 1 ? "," : "") + json_str(kv.first) + ":" + std::to_string(kv.second);
  pcj += "}";
  std::printf(
      "{\"pass\":%s,\"retired\":%s,\"cycles\":%s,\"cpi\":%s,\"max_cycles_per_insn\":%s,"
      "\"first_retire_cycle\":%s,\"end\":%s,\"uart_tx\":%s,\"error\":%s,\"psram_writes\":%llu,"
      "\"coremark_cycles_per_iter\":%s,\"mode\":\"%s\",\"deterministic\":%s,\"per_class\":%s,"
      "\"interrupts\":%llu,\"uo_trace\":%s,\"reset_edges\":%llu,\"sim_cycles\":%llu,"
      "\"sim_seconds\":%.3f}\n",
      passed ? "true" : "false", have_retired ? std::to_string(retired).c_str() : "null",
      have_cycles ? std::to_string(cycles_).c_str() : "null", cpi.c_str(),
      timed ? std::to_string(max_gap_).c_str() : "null",
      timed ? std::to_string(first_gap_).c_str() : "null",
      passed ? json_str(end_reason_).c_str() : "\"fail\"", json_str(uart_.tx_text()).c_str(),
      error_.empty() ? "null" : json_str(error_).c_str(), (unsigned long long)psram_prog_writes_,
      cm.c_str(), mode, det_ ? "true" : "false", timed ? pcj.c_str() : "null",
      (unsigned long long)interrupts_, trace_json(uo_seen_).c_str(),
      (unsigned long long)reset_edges_, (unsigned long long)t_, secs);
  std::fflush(stdout);
}

// ---- liveness fuzz (--fuzz-liveness K) ---------------------------------------------------------
// Contract 4: every instruction retires within 2,048 cycles of the previous one (a WFI stall
// excepted), reserved encodings, traps and unspecified accesses included. The fuzz keeps the
// contract v2 fuzz's structure on random flash images (bytes 0-64 KB; the rest erased, 0xFF): K
// probe sub-runs, then K/256 (at least 1) walk sub-runs, each from reset with its own power-up
// modes (all four cycle through), flip-flop state and noise, and no reference: only liveness and
// the pin rules count.
//
// Probe sub-run k (seed S): 0..8 valid, non-trapping, non-control RV32E instructions (random
// registers x0..x15, so loads and stores reach flash, PSRAM, IO and the core region), then a
// probe word: bits [6:0] are entry k mod 128 of a per-seed shuffle of all 128 values; on visit
// v = k / 128 of that opcode funct3 = (v + a per-seed offset) mod 8; rd, rs1, rs2 name x0..x15
// when v mod 16 < 8 and are random otherwise; funct7 is class (2 (v / 8) + S) mod 4 of {random,
// 0000000, 0100000, 0000001}; never 0x00000000, 0xFFFFFFFF, ECALL or EBREAK. Random words follow.
// The sub-run ends (passing) once the probe and 8 more instructions have retired (traps go to the
// arbitrary-reset mtvec, so the core wanders), or at the retirement cap.
// Walk sub-runs: LUI x15, 0x01008 at 0, then valid, non-trapping RV32E words (the probe prefix's
// mix plus branches and JALs to multiples of 4 within +-4 KB / +-32 KB, rd != x15, stores based on
// x15, i.e. PSRAM 0x0100_7800-0x0100_87FF), run to the retirement cap.
// A sub-run whose design stalls after a WFI (legal while mip.MEIP & mie.MEIE = 0) ends there.
// rvfi mode checks the cap at each retirement and by watchdog, no rvfi_valid during reset, the
// Pmod rules (pmod.h), the reset state and output stability; pins mode checks only the pin rules,
// for 16 x 2,048 cycles per sub-run.
struct FuzzRng {
  uint64_t s;
  explicit FuzzRng(uint64_t seed) : s(seed) {}
  uint32_t next() {  // splitmix64
    uint64_t z = (s += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return (uint32_t)((z ^ (z >> 31)) >> 16);
  }
};

uint32_t fuzz_valid_word(FuzzRng& g, bool walk) {
  const uint32_t rd = walk ? g.next() % 15 : g.next() & 15;
  const uint32_t rs1 = g.next() & 15, rs2 = g.next() & 15;
  const uint32_t imm12 = g.next() & 0xFFF, cls = g.next() % (walk ? 16 : 13);
  if (cls == 13 || cls == 14) {  // BEQ BNE BLT BGE BLTU BGEU, offset a multiple of 4 in +-4 KB
    static const uint32_t f3s[6] = {0, 1, 4, 5, 6, 7};
    const uint32_t off = (g.next() & 0x1FFCu);
    return ((off >> 12) & 1) << 31 | ((off >> 5) & 0x3F) << 25 | rs2 << 20 | rs1 << 15 |
           f3s[g.next() % 6] << 12 | ((off >> 1) & 0xF) << 8 | ((off >> 11) & 1) << 7 | 0x63;
  }
  if (cls == 15) {  // JAL, offset a multiple of 4 in +-32 KB
    const uint32_t mag = g.next() & 0x7FFCu;
    const uint32_t off = ((g.next() & 1) ? 0u - mag : mag) & 0x1FFFFEu;
    return ((off >> 20) & 1) << 31 | ((off >> 1) & 0x3FF) << 21 | ((off >> 11) & 1) << 20 |
           ((off >> 12) & 0xFF) << 12 | rd << 7 | 0x6F;
  }
  if (cls == 0) return (g.next() & 0xFFFFF000u) | rd << 7 | 0x37;  // LUI
  if (cls == 1) return (g.next() & 0xFFFFF000u) | rd << 7 | 0x17;  // AUIPC
  if (cls <= 4) {                                                   // OP-IMM
    const uint32_t f3 = g.next() & 7;
    uint32_t imm = imm12;
    if (f3 == 1) imm = g.next() & 31;
    if (f3 == 5) imm = (g.next() & 31) | ((g.next() & 1) ? 0x400 : 0);
    return imm << 20 | rs1 << 15 | f3 << 12 | rd << 7 | 0x13;
  }
  if (cls <= 7) {  // OP
    const uint32_t f3 = g.next() & 7;
    const uint32_t f7 = ((f3 == 0 || f3 == 5) && (g.next() & 1)) ? 0x20 : 0;
    return f7 << 25 | rs2 << 20 | rs1 << 15 | f3 << 12 | rd << 7 | 0x33;
  }
  if (cls <= 9) {  // LOAD: LB LH LW LBU LHU
    static const uint32_t f3s[5] = {0, 1, 2, 4, 5};
    return imm12 << 20 | rs1 << 15 | f3s[g.next() % 5] << 12 | rd << 7 | 0x03;
  }
  if (cls <= 11)  // STORE: SB SH SW
    return (imm12 >> 5) << 25 | rs2 << 20 | (walk ? 15 : rs1) << 15 | (g.next() % 3) << 12 |
           (imm12 & 31) << 7 | 0x23;
  return (g.next() & 0xFFFF8F80u) | 0x0F;  // FENCE: any MISC-MEM word with funct3 = 0
}

// Sub-run k's program into img[0, FUZZ_SPAN) (the rest of img stays 0xFF).
void fuzz_image(uint32_t seed, uint32_t k, uint32_t probes, uint8_t* img, uint32_t* probe_addr,
                uint32_t* probe) {
  uint8_t order[128], off[128];
  for (int i = 0; i < 128; i++) order[i] = (uint8_t)i;
  FuzzRng og(0xF0221E55ull * seed + 7);
  for (int i = 127; i > 0; i--) {
    const int j = (int)(og.next() % (uint32_t)(i + 1));
    std::swap(order[i], order[j]);
  }
  for (int i = 0; i < 128; i++) off[i] = (uint8_t)(og.next() & 7);
  FuzzRng g(((uint64_t)seed << 32) ^ (0xC0FFEEull * (k + 1)));
  auto put = [&](uint32_t a, uint32_t w) {
    for (int b = 0; b < 4; b++) img[a + b] = (uint8_t)(w >> (8 * b));
  };
  if (k >= probes) {  // walk
    put(0, 0x010087B7u);  // LUI x15, 0x01008
    for (uint32_t a = 4; a < FUZZ_SPAN; a += 4) put(a, fuzz_valid_word(g, true));
    *probe_addr = ~0u;
    *probe = 0;
    return;
  }
  const uint32_t prefix = g.next() % 9;
  uint32_t a = 0;
  for (uint32_t i = 0; i < prefix; i++, a += 4) put(a, fuzz_valid_word(g, false));
  const uint32_t op = order[k % 128], v = k / 128, f3 = (v + off[op]) & 7;
  const bool low_regs = (v & 15) < 8;
  const uint32_t f7 = (2 * (v / 8) + seed) & 3;  // random, 0000000, 0100000, 0000001
  uint32_t w;
  do {
    w = (g.next() & ~0x707Fu) | f3 << 12 | op;
    if (f7) w = (w & 0x01FFFFFFu) | (f7 == 2 ? 0x40000000u : f7 == 3 ? 0x02000000u : 0);
    if (low_regs) w &= ~(1u << 11 | 1u << 19 | 1u << 24);
  } while (w == 0 || w == 0xFFFFFFFFu || w == 0x00000073u || w == 0x00100073u);
  put(a, w);
  *probe_addr = a;
  *probe = w;
  for (a += 4; a < FUZZ_SPAN; a += 4) put(a, (g.next() & 1) ? g.next() : (g.next() | 3u));
}

struct FuzzRun {
  bool pass = true, capped = false, probed = false, ended = false, wfi_stall = false;
  uint64_t retired = 0, cycles = 0, max_gap = 0;
  std::string error;
};

FuzzRun fuzz_one(const Opts& o, uint32_t k, const uint8_t* img, uint32_t probe_addr,
                 uint32_t probe, uint64_t cap) {
  const uint32_t seed = o.seed;
  FuzzRun res;
  VerilatedContext ctx;
  ctx.commandArgs(o.argc, const_cast<const char**>(o.argv));
  ctx.randReset(2);
  ctx.randSeed((int)(seed * 7919u + k + 1));
  Vtt_um_fesens_r32v top(&ctx);
  PmodBus pmod(seed * 2654435761u + k + 1, img, pmod_init_for_seed(seed + k), o.clk_ns);
  const uint64_t reset_edges = 1 + (seed + k) % 9;
  FuzzRng ug(((uint64_t)k << 32) ^ (seed * 0x51ED27ull));
  uint8_t ui = (uint8_t)ug.next(), uo_last = 0;
  uint64_t last_ret = reset_edges - 1;
  uint32_t last_pc = 0, last_insn = 0, next_pc = 0;
  int64_t after_probe = -1;
  auto failf = [&](uint64_t t, const std::string& why) {
    res.pass = false;
    res.error =
        (t < reset_edges ? fmt("reset edge %llu: ", (unsigned long long)t)
                         : fmt("cycle %llu: ", (unsigned long long)(t - (reset_edges - 1)))) +
        why;
  };
  auto word_at = [&](uint32_t pc, uint32_t* w) {  // flash only (the image); else unknown
    if (area_of(pc) != Area::Flash) return false;
    *w = 0;
    for (uint32_t b = 0; b < 4; b++)
      *w |= (uint32_t)img[(pc + b) & (v3::FLASH_SIZE - 1)] << (8 * b);
    return true;
  };
  // R31: only the gap that starts at a WFI's retirement is exempt
  auto after_wfi = [&]() { return res.retired > 0 && last_insn == INSN_WFI; };
  top.clk = 0;
  top.rst_n = 0;
  top.ena = 1;
  top.ui_in = ui;
  top.uio_in = pmod.uio_in();
  top.eval();
#ifdef RVFI_MODE
  const uint64_t limit = reset_edges + LAT_CAP * (cap + 16);
#else
  const uint64_t limit = reset_edges + 16 * LAT_CAP;
  (void)cap;
  (void)probe_addr;
  (void)probe;
  (void)after_wfi;
  (void)last_pc;
  (void)after_probe;
#endif
  for (uint64_t t = 0;; t++) {
    top.clk = 1;
    top.eval();
    const uint32_t out =
        (uint32_t)top.uo_out | (uint32_t)top.uio_out << 8 | (uint32_t)top.uio_oe << 16;
    const uint8_t uio = top.uio_out;
    const bool in_reset = t < reset_edges;
    if (in_reset && ((uio & 0x41) != 0x41 || (uio & 0x08) || top.uo_out)) {
      failf(t, fmt("during reset both CS_n must be 1, SCK 0 and uo_out 0: uio_out = 0x%02x, uo_out "
                   "= 0x%02x",
                   uio, top.uo_out));
      break;
    }
    if (!pmod.edge(t, uio, top.uio_oe)) {
      failf(t, pmod.error());
      break;
    }
    bool new_ui = false;
#ifdef RVFI_MODE
    if (in_reset && top.rvfi_valid) {
      failf(t, "rvfi_valid during reset");
      break;
    }
    if (!in_reset) {
      if (top.rvfi_valid) {
        const uint64_t gap = t - last_ret;
        const bool exempt = after_wfi();
        if (gap > LAT_CAP && !exempt) {
          failf(t, fmt("latency cap: retirement #%llu (pc 0x%08x, insn 0x%08x) came %llu cycles "
                       "after %s (limit %llu)",
                       (unsigned long long)res.retired, top.rvfi_pc_rdata, top.rvfi_insn,
                       (unsigned long long)gap,
                       res.retired ? "the previous retirement" : "reset release",
                       (unsigned long long)LAT_CAP));
          break;
        }
        if (!exempt && gap > res.max_gap) res.max_gap = gap;
        if (!res.probed && top.rvfi_pc_rdata == probe_addr && top.rvfi_insn == probe) {
          res.probed = true;
          after_probe = 0;
        } else if (res.probed) {
          after_probe++;
        }
        last_ret = t;
        last_pc = top.rvfi_pc_rdata;
        last_insn = top.rvfi_insn;
        next_pc = top.rvfi_pc_wdata;
        res.retired++;
        new_ui = true;
        if (probe_addr != ~0u && after_probe >= 8) {
          res.ended = true;
          res.cycles = t - (reset_edges - 1);
          break;
        }
        if (res.retired >= cap) {
          res.capped = true;
          res.cycles = t - (reset_edges - 1);
          break;
        }
      }
      if (t - last_ret >= LAT_CAP + 1) {
        if (after_wfi()) {  // a WFI stall: legal while mip.MEIP & mie.MEIE = 0
          res.wfi_stall = true;
          res.cycles = t - (reset_edges - 1);
          break;
        }
        uint32_t w = 0;
        const uint32_t npc = res.retired ? next_pc : 0;
        const bool known = word_at(npc, &w);
        const std::string after =
            res.retired ? fmt("retirement #%llu (pc 0x%08x, insn 0x%08x)",
                              (unsigned long long)(res.retired - 1), last_pc, last_insn)
                        : std::string("reset release");
        failf(t, fmt("latency cap: no retirement within %llu cycles of %s; the next pc 0x%08x "
                     "holds %s",
                     (unsigned long long)LAT_CAP, after.c_str(), npc,
                     known ? fmt("0x%08x", w).c_str() : "(not flash)"));
        break;
      }
    }
#else
    if (!in_reset && top.uo_out != uo_last) new_ui = true;
#endif
    uo_last = top.uo_out;
    if (t >= limit) {
#ifdef RVFI_MODE
      failf(t, "fuzz budget exhausted without reaching the retirement cap");
#else
      res.cycles = t - (reset_edges - 1);
#endif
      break;
    }
    top.clk = 0;
    const bool rst_change = (t + 1 == reset_edges);
    top.rst_n = t + 1 >= reset_edges;
    if (new_ui) ui = (uint8_t)ug.next();
    top.ui_in = ui;
    top.uio_in = pmod.uio_in();
    top.eval();
    const uint32_t out2 =
        (uint32_t)top.uo_out | (uint32_t)top.uio_out << 8 | (uint32_t)top.uio_oe << 16;
    if (!rst_change && out2 != out) {
      failf(t, fmt("outputs changed between rising clk edges (0x%06x -> 0x%06x)", out, out2));
      break;
    }
  }
  top.final();
  return res;
}

int fuzz_main(const Opts& o) {
  std::vector<uint8_t> img(v3::FLASH_SIZE, 0xFF);
  uint64_t retired = 0, cycles = 0, max_gap = 0, ended = 0, capped = 0, stalls = 0, runs = 0,
           probed = 0;
  const uint32_t walks = o.fuzz_runs / 256 ? o.fuzz_runs / 256 : 1;
  std::string error;
  const auto clock0 = std::chrono::steady_clock::now();
  for (uint32_t k = 0; k < o.fuzz_runs + walks; k++) {
    uint32_t probe_addr = 0, probe = 0;
    fuzz_image(o.seed, k, o.fuzz_runs, img.data(), &probe_addr, &probe);
    FuzzRun r = fuzz_one(o, k, img.data(), probe_addr, probe, o.fuzz_cap);
    runs++;
    retired += r.retired;
    cycles += r.cycles;
    max_gap = std::max(max_gap, r.max_gap);
    ended += r.ended;
    capped += r.capped;
    stalls += r.wfi_stall;
    probed += r.probed;
    if (!r.pass) {
      error = (k < o.fuzz_runs ? fmt("fuzz sub-run %u (probe word 0x%08x at 0x%04x, opcode 0x%02x, "
                                     "funct3 %u): ",
                                     k, probe, probe_addr, probe & 0x7F, (probe >> 12) & 7)
                               : fmt("fuzz sub-run %u (walk %u): ", k, k - o.fuzz_runs)) +
              r.error;
      break;
    }
  }
  const double secs =
      std::chrono::duration<double>(std::chrono::steady_clock::now() - clock0).count();
#ifdef RVFI_MODE
  const char* mode = "rvfi";
  const bool live = true;
#else
  const char* mode = "pins";
  const bool live = false;
#endif
  std::printf(
      "{\"pass\":%s,\"mode\":\"%s\",\"fuzz\":\"liveness\",\"liveness_checked\":%s,\"runs\":%llu,"
      "\"walks\":%u,\"probes_retired\":%s,\"ended\":%llu,\"capped\":%llu,\"wfi_stalls\":%llu,"
      "\"retired\":%llu,\"cycles\":%llu,\"max_cycles_per_insn\":%s,\"retire_cap\":%llu,"
      "\"sim_seconds\":%.3f,\"error\":%s}\n",
      error.empty() ? "true" : "false", mode, live ? "true" : "false", (unsigned long long)runs,
      walks, live ? std::to_string(probed).c_str() : "null", (unsigned long long)ended,
      (unsigned long long)capped, (unsigned long long)stalls, (unsigned long long)retired,
      (unsigned long long)cycles, live ? std::to_string(max_gap).c_str() : "null",
      (unsigned long long)o.fuzz_cap, secs, error.empty() ? "null" : json_str(error).c_str());
  std::fflush(stdout);
  return error.empty() ? 0 : 1;
}

int usage() {
  std::fprintf(stderr,
               "usage: harness <prog.hex> [--seed S] [--expect PATH] [--clk-ns 25] "
               "[--max-cycles C] [--trace PATH] [--dump-psram PATH] [--dump-pins PATH] [-v] "
               "[+PLUSARG...]\n"
               "       harness --fuzz-liveness K [--fuzz-cap N] [--seed S] [--clk-ns 25]\n");
  return 2;
}

}  // namespace

int main(int argc, char** argv) {
  Opts o;
  o.argc = argc;
  o.argv = argv;
  for (int i = 1; i < argc; i++) {
    const std::string a = argv[i];
    auto next = [&]() -> const char* {
      if (i + 1 >= argc) std::exit(usage());
      return argv[++i];
    };
    if (a == "--seed") o.seed = (uint32_t)std::strtoul(next(), nullptr, 0);
    else if (a == "--expect") o.expect = next();
    else if (a == "--clk-ns") o.clk_ns = std::strtod(next(), nullptr);
    else if (a == "--max-cycles") o.max_cycles = std::strtoull(next(), nullptr, 0);
    else if (a == "--trace") o.trace = next();
    else if (a == "--dump-psram") o.dump_psram = next();
    else if (a == "--dump-pins") o.dump_pins = next();
    else if (a == "-v") o.verbose++;
    else if (a == "--fuzz-liveness") o.fuzz_runs = (uint32_t)std::strtoul(next(), nullptr, 0);
    else if (a == "--fuzz-cap") o.fuzz_cap = std::strtoull(next(), nullptr, 0);
    else if (a[0] == '+') continue;  // for the design ($value$plusargs)
    else if (a[0] == '-') return usage();
    else o.hex = argv[i];
  }
  if (o.seed == 0 || !(o.clk_ns > 0)) {
    std::fprintf(stderr, "--seed and --clk-ns must be positive\n");
    return 2;
  }
  if (o.fuzz_runs) {
    if (o.fuzz_cap == 0) {
      std::fprintf(stderr, "--fuzz-cap must be positive\n");
      return 2;
    }
    return fuzz_main(o);
  }
  if (!o.hex) return usage();
  std::vector<uint8_t> img(v3::FLASH_SIZE);
  std::string err;
  if (!load_flash_hex(o.hex, img.data(), &err)) {
    std::fprintf(stderr, "%s\n", err.c_str());
    return 2;
  }
  Expect ex;
  if (o.expect && !load_expect(o.expect, &ex, &err)) {
    std::fprintf(stderr, "%s\n", err.c_str());
    return 2;
  }
  Harness h(o, img.data(), ex);
  return h.run();
}
