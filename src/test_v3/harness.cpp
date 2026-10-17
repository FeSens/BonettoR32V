// Seed unit-test harness for the contract-v3 MCU (task 8). Not a contract gate: Tasks 1-5 build
// the real one (verif/cosim). Verilator top: tt_um_fesens_r32v.
//
//   harness <flash.bin> [options]
//     --seed S          flop randomization, PSRAM content and noise; also the Pmod power-up mode
//                       unless --pmod is given: (S-1) % 4 -> std+uninit, cont+uninit, std+qpi,
//                       cont+qpi (flash standard/continuous, PSRAM SPI-not-reset/QPI)
//     --pmod M          force the power-up mode 0..3
//     --max-cycles N    give up (fail) after N cycles
//     --loop            ui_in follows uo_out one cycle late: ui0<-uo7 ui1<-uo0 ui2<-uo1 ui3<-uo6
//                       ui4<-uo2 ui5<-uo3 ui6<-uo4 ui7<-uo5
//     --uart-div D      decode UART TX on uo0 at bit time D+1 (printed as "uart_tx")
//     --uart-rx HEX     drive these bytes on ui1 (8N1, bit time D+1, scaled by --uart-ppm), the
//                       first one --uart-rx-start cycles after the first retirement, then every
//                       --uart-rx-gap cycles
//     --expect-tx HEX   the decoded TX bytes must equal this
//     --pwm CH:PER:Q:DUTY[,...]  after the program raises uo0 (GPIO marker) measure channel CH
//                       (uo pin) period and high time; must be (PER+1)*2^Q and min(DUTY,PER+1)*2^Q
//     --end-ebreak      (rvfi mode) a retired EBREAK ends the run (random streams)
//     --quiet, --trace-retire, --vcd FILE (trace builds only)
// rvfi build (-DRISCV_FORMAL): every retirement is compared with the ISS (all RVFI fields; IO
// volatile bits from the DUT), interrupts must be allowed in the ISS, the 2,048-cycle cap holds,
// PSRAM writes outside the core region carry the ISS's data, the GPIO-mode uo pins follow the
// ISS within a cycle, and the final PSRAM image matches. Both builds: the Pmod rules, and the
// program's verdict on uo_out (0x5A pass, 0xEE fail), written with ALTFN = 0, then a self-loop.
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "Vtt_um_fesens_r32v.h"
#include "iss3.h"
#include "pmod.h"
#include "verilated.h"
#ifdef VCD_TRACE
#include "verilated_vcd_c.h"
#endif

using namespace v3;

namespace {

std::vector<uint8_t> parse_hex_bytes(const std::string& s) {
  std::vector<uint8_t> v;
  for (size_t i = 0; i + 1 < s.size(); i += 2) v.push_back((uint8_t)strtoul(s.substr(i, 2).c_str(), nullptr, 16));
  return v;
}

struct PwmSpec {
  int ch, per, q, duty;
};

struct PinMeter {  // period and high time of one pin, measured from rising edge to rising edge
  int last = -1;
  uint64_t rise = 0, high = 0, n_periods = 0;
  uint64_t period = 0, hi_time = 0;
  bool bad = false;
  uint64_t first_period = 0, first_high = 0;
  void sample(uint64_t t, int v) {
    if (last == 0 && v == 1) {
      if (rise) {
        uint64_t p = t - rise;
        if (n_periods == 0) {
          first_period = p;
          first_high = high;
        } else if (p != first_period || high != first_high) {
          bad = true;
        }
        n_periods++;
      }
      rise = t;
      high = 0;
    }
    if (v) high++;
    last = v;
  }
};

}  // namespace

int main(int argc, char** argv) {
  const char* bin = nullptr;
  uint32_t seed = 1;
  int pmode = -1;
  uint64_t max_cycles = 200000000ull;
  bool loop = false, quiet = false, trace_ret = false, end_ebreak = false;
  int uart_div = -1;
  std::vector<uint8_t> uart_rx, expect_tx;
  bool have_expect_tx = false;
  uint64_t rx_start = 2000, rx_gap = 0;
  double ppm = 0;
  std::vector<PwmSpec> pwm;
  const char* vcd = nullptr;
  for (int i = 1; i < argc; i++) {
    std::string a = argv[i];
    auto next = [&]() { return std::string(i + 1 < argc ? argv[++i] : ""); };
    if (a == "--seed") seed = (uint32_t)strtoul(next().c_str(), nullptr, 0);
    else if (a == "--pmod") pmode = atoi(next().c_str());
    else if (a == "--max-cycles") max_cycles = strtoull(next().c_str(), nullptr, 0);
    else if (a == "--loop") loop = true;
    else if (a == "--uart-div") uart_div = atoi(next().c_str());
    else if (a == "--uart-rx") uart_rx = parse_hex_bytes(next());
    else if (a == "--uart-rx-start") rx_start = strtoull(next().c_str(), nullptr, 0);
    else if (a == "--uart-rx-gap") rx_gap = strtoull(next().c_str(), nullptr, 0);
    else if (a == "--uart-ppm") ppm = atof(next().c_str());
    else if (a == "--expect-tx") { expect_tx = parse_hex_bytes(next()); have_expect_tx = true; }
    else if (a == "--pwm") {
      std::string s = next();
      size_t p = 0;
      while (p < s.size()) {
        size_t e = s.find(',', p);
        if (e == std::string::npos) e = s.size();
        PwmSpec w;
        if (sscanf(s.substr(p, e - p).c_str(), "%d:%d:%d:%d", &w.ch, &w.per, &w.q, &w.duty) == 4) pwm.push_back(w);
        p = e + 1;
      }
    }
    else if (a == "--end-ebreak") end_ebreak = true;
    else if (a == "--quiet") quiet = true;
    else if (a == "--trace-retire") trace_ret = true;
    else if (a == "--vcd") vcd = argv[++i];
    else if (a[0] != '-') bin = argv[i];
  }
  if (!bin) {
    fprintf(stderr, "usage: harness flash.bin [options]\n");
    return 2;
  }
  std::vector<uint8_t> flash(FLASH_SIZE, 0xFF), psram(PSRAM_SIZE);
  {
    FILE* f = fopen(bin, "rb");
    if (!f) {
      fprintf(stderr, "cannot open %s\n", bin);
      return 2;
    }
    size_t n = fread(flash.data(), 1, FLASH_SIZE, f);
    fclose(f);
    (void)n;
  }
  uint64_t rs = seed * 0x2545F4914F6CDD1Dull + 7;
  for (auto& b : psram) {
    rs ^= rs << 13;
    rs ^= rs >> 7;
    rs ^= rs << 17;
    b = (uint8_t)(rs >> 24);
  }
  if (pmode < 0) pmode = (int)((seed - 1) % 4);
  FlashMode fm = (pmode & 1) ? FlashMode::Continuous : FlashMode::Standard;
  PsramMode pm = (pmode & 2) ? PsramMode::Qpi : PsramMode::SpiUninit;

  Iss3 iss;
  iss.load_flash(flash.data(), FLASH_SIZE);
  iss.set_psram(psram.data());
  Pmod pmod(flash.data(), psram.data(), fm, pm, 25.0, seed);

  auto ctx = new VerilatedContext;
  ctx->randReset(2);
  ctx->randSeed((int)seed);
  auto top = new Vtt_um_fesens_r32v(ctx);
#ifdef VCD_TRACE
  VerilatedVcdC* tfp = nullptr;
  if (vcd) {
    ctx->traceEverOn(true);
    tfp = new VerilatedVcdC;
    top->trace(tfp, 99);
    tfp->open(vcd);
  }
#else
  (void)vcd;
#endif
  std::string err;
  auto fail = [&](const std::string& m, uint64_t t) {
    if (err.empty()) {
      char b[64];
      snprintf(b, sizeof b, " (cycle %llu)", (unsigned long long)t);
      err = m + b;
    }
  };

  top->ena = 1;
  top->ui_in = 0;
  top->uio_in = 0;
  top->rst_n = 0;
  top->clk = 0;
  top->eval();
  uint64_t t = 0;      // edge index; edge 0 is the last edge with rst_n sampled low
  uint64_t simtime = 0;
  const int RESET_EDGES = 3;
  // reset: rst_n low for RESET_EDGES rising edges; uo_out must be 0, CS_n high, SCK low
  for (int i = 0; i < RESET_EDGES; i++) {
    top->clk = 1;
    top->eval();
#ifdef VCD_TRACE
    if (tfp) tfp->dump(simtime);
#endif
    simtime++;
    if (i > 0) {
      if (top->uo_out != 0) fail("uo_out not 0 during reset", 0);
      if (!(top->uio_out & 1) || !((top->uio_out >> 6) & 1) || ((top->uio_out >> 3) & 1))
        fail("CS_n low or SCK high during reset", 0);
    }
    top->clk = 0;
    top->eval();
#ifdef VCD_TRACE
    if (tfp) tfp->dump(simtime);
#endif
    simtime++;
  }
  top->rst_n = 1;

  // UART RX bit edges
  std::vector<std::pair<uint64_t, int>> rx_events;  // (cycle after first retirement, level)
  // GPIO-mode check
  uint8_t prev_uo = 0;
  uint64_t retired = 0, last_ret_t = 0, max_gap = 0, first_ret_t = 0;
  uint64_t end_t = 0;
  bool ended = false, passed = false;
  uint32_t last_pc = 0xFFFFFFFF, last_insn = 0;
  int selfloops = 0;
  uint64_t gpio_settle = 0;  // the GPIO compare skips this edge (one cycle after a store)
  bool intr_seen = false;
  uint64_t n_intr = 0, n_trap = 0;
  // UART TX decoder
  std::vector<uint8_t> tx_bytes;
  int tx_state = 0;
  uint64_t tx_t0 = 0;
  int tx_bit = 0;
  uint8_t tx_byte = 0;
  int tx_prev = 0;
  uint64_t tx_idle = 0, tx_idle_prev = 0;  // a start bit needs a full idle bit time before it
  // PWM meters
  bool pwm_on = false, pwm_done = false;
  std::vector<PinMeter> meters(pwm.size());
  uint8_t ui_next = 0;
  size_t rx_idx = 0;
  int rx_level = 1;
  std::vector<uint64_t> gaps_hist;

  for (;;) {
    t++;
    top->clk = 1;
    top->eval();
#ifdef VCD_TRACE
    if (tfp) tfp->dump(simtime);
#endif
    simtime++;
    uint8_t out = top->uio_out, oe = top->uio_oe, uo = top->uo_out;
    pmod.edge(t, out, oe);
    if (!pmod.err.empty()) fail("pmod: " + pmod.err, t);

#ifdef RISCV_FORMAL
    if (top->rvfi_valid && err.empty() && !ended) {
      uint64_t gap = t - last_ret_t;
      if (retired == 0) {
        first_ret_t = t;
        if (t > 2048) fail("first retirement after edge 2048: " + std::to_string(t), t);
      } else if (gap > 2048) {
        fail("retirement gap " + std::to_string(gap) + " > 2048", t);
      }
      if (gap > max_gap) max_gap = gap;
      last_ret_t = t;
      if (top->rvfi_intr) {
        if (!iss.irq_allowed()) fail("interrupt while masked", t);
        iss.take_interrupt();
        n_intr++;
        intr_seen = true;
      }
      uint32_t dut_mrd = top->rvfi_mem_rdata, dut_maddr = top->rvfi_mem_addr, dut_rd = top->rvfi_rd_wdata;
      iss.io_read = [&](unsigned, uint32_t) { return dut_mrd << (8 * (dut_maddr & 3)); };
      iss.mip_read = [&]() { return dut_rd; };
      Retire r = iss.step();
      retired++;
      if (end_ebreak && r.insn == 0x00100073u) {  // random streams end at their EBREAK (mtvec unset)
        ended = true;
        passed = err.empty();
        end_t = t;
        r.npc = top->rvfi_pc_wdata;
      }
      if (r.region || r.reserved) {
        fail(std::string("program executed ") + (r.region ? "a core-region access" : "a reserved encoding"), t);
      }
      char b[512];
      auto mism = [&](const char* f, uint32_t d, uint32_t e) {
        snprintf(b, sizeof b, "mismatch %s: dut %08x iss %08x at pc %08x insn %08x (retirement %llu)", f, d, e,
                 r.pc, r.insn, (unsigned long long)retired);
        fail(b, t);
      };
      if (top->rvfi_pc_rdata != r.pc) mism("pc_rdata", top->rvfi_pc_rdata, r.pc);
      else if (top->rvfi_insn != r.insn) mism("insn", top->rvfi_insn, r.insn);
      else if (top->rvfi_trap != r.trap) mism("trap", top->rvfi_trap, r.trap);
      else if (top->rvfi_halt) mism("halt", 1, 0);
      else if (top->rvfi_intr != intr_seen) mism("intr", top->rvfi_intr, intr_seen);
      else if (top->rvfi_pc_wdata != r.npc) mism("pc_wdata", top->rvfi_pc_wdata, r.npc);
      else if (top->rvfi_rd_addr != r.rd) mism("rd_addr", top->rvfi_rd_addr, r.rd);
      else if (top->rvfi_rd_wdata != r.rdv) mism("rd_wdata", top->rvfi_rd_wdata, r.rdv);
      else if (top->rvfi_mem_wmask != r.wmask) mism("mem_wmask", top->rvfi_mem_wmask, r.wmask);
      else if (top->rvfi_mem_rmask != r.rmask) mism("mem_rmask", top->rvfi_mem_rmask, r.rmask);
      else if ((r.wmask || r.rmask) && top->rvfi_mem_addr != r.maddr) mism("mem_addr", top->rvfi_mem_addr, r.maddr);
      else if (r.wmask && top->rvfi_mem_wdata != r.mwdata) mism("mem_wdata", top->rvfi_mem_wdata, r.mwdata);
      else if (r.rmask && ((top->rvfi_mem_rdata ^ r.mrdata) & ~r.io_vmask))
        mism("mem_rdata", top->rvfi_mem_rdata, r.mrdata);
      else if (!r.trap || (r.insn & 0x7F) == 0x63 || (r.insn & 0x7F) == 0x67) {
        if (top->rvfi_rs1_addr != r.rs1) mism("rs1_addr", top->rvfi_rs1_addr, r.rs1);
        else if (top->rvfi_rs1_rdata != r.rs1v) mism("rs1_rdata", top->rvfi_rs1_rdata, r.rs1v);
        else if (top->rvfi_rs2_addr != r.rs2) mism("rs2_addr", top->rvfi_rs2_addr, r.rs2);
        else if (top->rvfi_rs2_rdata != r.rs2v) mism("rs2_rdata", top->rvfi_rs2_rdata, r.rs2v);
      }
      if (r.trap) n_trap++;
      intr_seen = false;
      if (trace_ret)
        fprintf(stderr, "ret %llu t=%llu gap=%llu pc=%08x insn=%08x%s%s rd=%u:%08x\n",
                (unsigned long long)retired, (unsigned long long)t, (unsigned long long)gap, r.pc, r.insn,
                r.trap ? " TRAP" : "", top->rvfi_intr ? " INTR" : "", r.rd, r.rdv);
      // PSRAM writes since the previous retirement must carry the ISS's bytes (core region excluded)
      for (auto& w : pmod.writes) {
        if (w.addr >= REGION_LO) continue;
        if (w.data != iss.psram(w.addr)) {
          snprintf(b, sizeof b, "PSRAM write %06x=%02x does not match the ISS (%02x) at retirement %llu", w.addr,
                   w.data, iss.psram(w.addr), (unsigned long long)retired);
          fail(b, t);
        }
      }
      pmod.writes.clear();
      if (r.wmask && r.io) gpio_settle = t + 1;
      if (r.insn == 0x0000006Fu && r.pc == last_pc && last_insn == 0x0000006Fu) selfloops++;
      last_pc = r.pc;
      last_insn = r.insn;
    }
    // GPIO-mode uo pins follow the ISS's GPIO_OUT (ALTFN = 0) from one cycle after each store
    if (retired && err.empty() && !ended && t > gpio_settle) {
      uint8_t alt = iss.altfn(), g = iss.gpio_out();
      if ((uo & ~alt) != (g & ~alt)) {
        char b[128];
        snprintf(b, sizeof b, "uo_out %02x differs from ISS GPIO_OUT %02x on GPIO-mode pins (ALTFN %02x)", uo, g, alt);
        fail(b, t);
      }
    }
#endif

    // UART TX decoder on uo0
    if (uart_div >= 0) {
      int v = uo & 1;
      uint64_t bt = (uint64_t)uart_div + 1;
      tx_idle = v ? tx_idle + 1 : 0;
      if (tx_state == 0 && tx_prev == 1 && v == 0 && tx_idle_prev >= bt) {
        tx_state = 1;
        tx_t0 = t;
        tx_bit = 0;
        tx_byte = 0;
      } else if (tx_state == 1) {
        uint64_t dt = t - tx_t0;
        if (dt == bt * (uint64_t)tx_bit + bt / 2) {
          if (tx_bit == 0) {
            if (v != 0) tx_state = 0;  // glitch
          } else if (tx_bit <= 8) {
            tx_byte |= (uint8_t)(v << (tx_bit - 1));
          } else {
            if (v == 1) tx_bytes.push_back(tx_byte);  // a 0 stop bit: the pin left UART mode
            tx_state = 0;
          }
          tx_bit++;
        }
        // exact bit boundaries: the line may change only at multiples of bt after the start
        if (v != tx_prev && dt % bt != 0 && tx_state == 1) fail("UART TX edge off the bit grid", t);
      }
      tx_prev = v;
      tx_idle_prev = tx_idle;
    }
    // PWM meters: start after uo0 rises as a GPIO marker
    if (!pwm.empty()) {
      if ((uo & 1) && (prev_uo & 1) == 0 && !pwm_on) pwm_on = true;
      if (pwm_on && !(uo & 1)) pwm_done = true;
      if (pwm_on && !pwm_done)
        for (size_t i = 0; i < pwm.size(); i++) meters[i].sample(t, (uo >> pwm[i].ch) & 1);
    }

    // inputs for the next cycle
    uint8_t ui = 0;
    if (loop) {
      ui = (uint8_t)(((prev_uo >> 7) & 1) | ((prev_uo & 1) << 1) | (((prev_uo >> 1) & 1) << 2) |
                     (((prev_uo >> 6) & 1) << 3) | (((prev_uo >> 2) & 1) << 4) | (((prev_uo >> 3) & 1) << 5) |
                     (((prev_uo >> 4) & 1) << 6) | (((prev_uo >> 5) & 1) << 7));
    }
    if (!uart_rx.empty()) {
      // schedule bit edges once the first retirement is known (rvfi) or at a fixed cycle (pins)
      uint64_t base = first_ret_t ? first_ret_t : 3000;
      if (rx_events.empty() && (first_ret_t || t > 3000)) {
        double bt = (uart_div + 1) * (1.0 + ppm * 1e-6);
        for (size_t k = 0; k < uart_rx.size(); k++) {
          double s0 = (double)(base + rx_start + k * rx_gap);
          rx_events.push_back({(uint64_t)llround(s0), 0});
          for (int b = 0; b < 8; b++) rx_events.push_back({(uint64_t)llround(s0 + bt * (b + 1)), (uart_rx[k] >> b) & 1});
          rx_events.push_back({(uint64_t)llround(s0 + bt * 9), 1});
        }
      }
      while (rx_idx < rx_events.size() && rx_events[rx_idx].first <= t) rx_level = rx_events[rx_idx++].second;
      ui = (uint8_t)((ui & ~2) | (rx_level << 1));
    }
    ui_next = ui;
    prev_uo = uo;
    top->uio_in = pmod.uio_in(out, oe);
    top->ui_in = ui_next;
    top->clk = 0;
    top->eval();
#ifdef VCD_TRACE
    if (tfp) tfp->dump(simtime);
#endif
    simtime++;

    if (!err.empty()) break;
    if (!ended) {
      bool verdict = (uo == 0x5A || uo == 0xEE);
#ifdef RISCV_FORMAL
      verdict = verdict && iss.altfn() == 0 && selfloops > 0;
#endif
      if (verdict) {
        ended = true;
        passed = (uo == 0x5A);
        end_t = t;
        if (!passed) fail("program reported failure (uo_out = 0xEE)", t);
      }
    }
    if (ended && t >= end_t + 2048) break;
    if (t >= max_cycles) {
      fail("max cycles reached", t);
      break;
    }
  }

#ifdef RISCV_FORMAL
  if (err.empty()) {
    for (uint32_t a = 0; a < REGION_LO; a++)
      if (psram[a] != iss.psram(a)) {
        char b[96];
        snprintf(b, sizeof b, "final PSRAM byte %06x: dut %02x iss %02x", a, psram[a], iss.psram(a));
        fail(b, t);
        break;
      }
  }
#endif
  if (err.empty() && have_expect_tx && tx_bytes != expect_tx) {
    std::string got, exp;
    char h[4];
    for (auto c : tx_bytes) { snprintf(h, sizeof h, "%02x", c); got += h; }
    for (auto c : expect_tx) { snprintf(h, sizeof h, "%02x", c); exp += h; }
    fail("UART TX bytes " + got + " expected " + exp, t);
  }
  if (err.empty() && !pwm.empty()) {
    for (size_t i = 0; i < pwm.size(); i++) {
      auto& m = meters[i];
      uint64_t P = (uint64_t)(pwm[i].per + 1) << pwm[i].q;
      uint64_t H = (uint64_t)std::min(pwm[i].duty, pwm[i].per + 1) << pwm[i].q;
      bool flat = (H == 0 || H == P);
      if (flat) {
        if (m.n_periods) fail("PWM ch" + std::to_string(pwm[i].ch) + " toggled but should be flat", t);
        else if (m.last != (H == P ? 1 : 0)) fail("PWM ch" + std::to_string(pwm[i].ch) + " flat at the wrong level", t);
      } else if (m.n_periods < 2 || m.bad || m.first_period != P || m.first_high != H) {
        char b[160];
        snprintf(b, sizeof b, "PWM ch%d: period %llu high %llu (%llu periods%s), expected %llu / %llu", pwm[i].ch,
                 (unsigned long long)m.first_period, (unsigned long long)m.first_high,
                 (unsigned long long)m.n_periods, m.bad ? ", unstable" : "", (unsigned long long)P,
                 (unsigned long long)H);
        fail(b, t);
      }
    }
  }
  if (err.empty() && !ended) fail("no verdict", t);
  bool ok = err.empty() && passed;
  std::string txs;
  for (auto c : tx_bytes) {
    char h[4];
    snprintf(h, sizeof h, "%02x", c);
    txs += h;
  }
  printf("{\"pass\":%s,\"seed\":%u,\"pmod\":%d,\"cycles\":%llu,\"retired\":%llu,\"first_retire\":%llu,"
         "\"max_gap\":%llu,\"cpi\":%.1f,\"intr\":%llu,\"traps\":%llu,\"uart_tx\":\"%s\",\"error\":\"%s\"}\n",
         ok ? "true" : "false", seed, pmode, (unsigned long long)t, (unsigned long long)retired,
         (unsigned long long)first_ret_t, (unsigned long long)max_gap,
         retired > 1 ? (double)(last_ret_t - first_ret_t) / (double)(retired - 1) : 0.0,
         (unsigned long long)n_intr, (unsigned long long)n_trap, txs.c_str(), err.c_str());
  (void)quiet;
#ifdef VCD_TRACE
  if (tfp) tfp->close();
#endif
  delete top;
  delete ctx;
  return ok ? 0 : 1;
}
