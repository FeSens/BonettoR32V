// Unit test harness for the bit-serial core (hypothesis 001). Not a contract gate.
//
// Verilator top: tt_um_fesens_r32v. A 23LC512 model checks contract section 2 on every clock
// edge and drives noise on MISO whenever SCK is low. Two modes:
//   rvfi  (built with -DRISCV_FORMAL): every retirement is compared with the reference ISS, and
//         every SPI transaction is matched against the retiring instruction's fetch/load/store.
//   pins  (plain build): the final RAM image and the uo_out change trace are compared with the
//         ISS run on the same program.
// Both modes enforce the 1,024-cycle latency cap (rvfi: per retirement; pins: per SPI fetch).
//
// usage: harness <prog.hex> [--seed S] [--max-cycles N] [--quiet]
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <string>
#include <vector>

#include "Vtt_um_fesens_r32v.h"
#ifdef TRACE_PUBLIC
#include "Vtt_um_fesens_r32v___024root.h"
#endif
#include "iss.h"
#include "verilated.h"

namespace {

struct Txn {
  bool write = false;
  uint16_t addr = 0;
  std::vector<uint8_t> data;  // bytes moved, in order
  uint64_t start_cycle = 0;
};

uint64_t g_rng = 0x9E3779B97F4A7C15ull;
int rnd_bit() {
  g_rng ^= g_rng << 13;
  g_rng ^= g_rng >> 7;
  g_rng ^= g_rng << 17;
  return (int)(g_rng >> 63);
}

class SpiRam {
 public:
  uint8_t mem[65536];
  std::deque<Txn> done;
  std::string err;
  uint64_t cycle = 0;

  // Called after every rising clock edge with the pin values after the edge.
  void edge(int s, int c, int m) {
    if (!err.empty()) return;
    if (first_) {
      first_ = false;
    } else {
      if (c != pc_ && !(ps_ == 0 && s == 0))
        fail("CS_n changed while SCK high (rule 1)");
      if (pc_ == 0 && c == 0 && ps_ == 0 && s == 1) {  // rising SCK edge inside a transaction
        if (m != pm_) fail("MOSI changed on a rising SCK edge (rule 2)");
        rising(m);
      }
      if (pc_ == 1 && c == 0) begin_txn();
      if (pc_ == 0 && c == 1) end_txn();
    }
    ps_ = s;
    pc_ = c;
    pm_ = m;
    // MISO for the coming cycle: valid while SCK is high in the data phase of a READ
    if (c == 0 && s == 1 && have_out_)
      miso_ = out_bit_;
    else
      miso_ = rnd_bit();
  }
  int miso() const { return miso_; }
  bool in_txn() const { return pc_ == 0; }

 private:
  bool first_ = true;
  int ps_ = 0, pc_ = 1, pm_ = 0, miso_ = 0;
  int nbits_ = 0;
  uint8_t cmd_ = 0;
  uint16_t addr_ = 0;
  uint8_t wbyte_ = 0;
  bool have_out_ = false;
  int out_bit_ = 0;
  Txn cur_;

  void fail(const std::string& s) {
    if (err.empty()) {
      char b[64];
      snprintf(b, sizeof b, " at cycle %llu", (unsigned long long)cycle);
      err = s + b;
    }
  }
  void begin_txn() {
    nbits_ = 0;
    cmd_ = 0;
    addr_ = 0;
    have_out_ = false;
    cur_ = Txn();
    cur_.start_cycle = cycle;
  }
  void rising(int m) {
    int b = nbits_++;
    have_out_ = false;
    if (b < 8) {
      cmd_ = (uint8_t)((cmd_ << 1) | m);
      if (b == 7 && cmd_ != 0x03 && cmd_ != 0x02) fail("bad SPI command");
      return;
    }
    if (b < 24) {
      addr_ = (uint16_t)((addr_ << 1) | m);
      if (b == 23) {
        cur_.addr = addr_;
        cur_.write = cmd_ == 0x02;
      }
      return;
    }
    int db = b - 24;
    uint16_t a = (uint16_t)(addr_ + db / 8);
    if (cmd_ == 0x03) {
      out_bit_ = (mem[a] >> (7 - db % 8)) & 1;
      have_out_ = true;
      if (db % 8 == 0) cur_.data.push_back(mem[a]);
    } else {
      wbyte_ = (uint8_t)((wbyte_ << 1) | m);
      if (db % 8 == 7) {
        mem[a] = wbyte_;
        cur_.data.push_back(wbyte_);
      }
    }
  }
  void end_txn() {
    if (nbits_ < 24) {
      fail("transaction ended inside command/address");
      return;
    }
    int db = nbits_ - 24;
    if (cmd_ == 0x02 && (db == 0 || db % 8 != 0)) fail("WRITE not on a byte boundary (rule 5)");
    if (cmd_ == 0x03 && db % 8 != 0) {
      // a partial last byte: keep only complete bytes in the record
      if (!cur_.data.empty()) cur_.data.pop_back();
    }
    done.push_back(cur_);
    have_out_ = false;
  }
};

struct Opts {
  const char* hex = nullptr;
  uint32_t seed = 1;
  uint64_t max_cycles = 400000000ull;
  bool quiet = false;
};

}  // namespace

int main(int argc, char** argv) {
  Opts o;
  std::vector<const char*> vargs = {argv[0]};
  for (int i = 1; i < argc; i++) {
    std::string a = argv[i];
    if (a == "--seed" && i + 1 < argc)
      o.seed = (uint32_t)strtoul(argv[++i], nullptr, 0);
    else if (a == "--max-cycles" && i + 1 < argc)
      o.max_cycles = strtoull(argv[++i], nullptr, 0);
    else if (a == "--quiet")
      o.quiet = true;
    else if (a.rfind("+", 0) == 0)
      vargs.push_back(argv[i]);
    else
      o.hex = argv[i];
  }
  if (!o.hex) {
    fprintf(stderr, "usage: harness prog.hex [--seed S] [--max-cycles N]\n");
    return 1;
  }
  static uint8_t img[65536];
  std::string e;
  if (!load_hex_image(o.hex, img, &e)) {
    fprintf(stderr, "%s\n", e.c_str());
    return 1;
  }
  g_rng ^= (uint64_t)o.seed * 0xD1B54A32D192ED03ull;
  char seedarg[64];
  snprintf(seedarg, sizeof seedarg, "+verilator+seed+%u", o.seed);
  vargs.push_back(seedarg);
  vargs.push_back("+verilator+rand+reset+2");

  VerilatedContext ctx;
  ctx.commandArgs((int)vargs.size(), vargs.data());
  Vtt_um_fesens_r32v top(&ctx);
  SpiRam ram;
  memcpy(ram.mem, img, sizeof img);

  Iss iss;
  iss.load_image(img);
#ifdef RISCV_FORMAL
  const bool rvfi = true;
#else
  const bool rvfi = false;
#endif

  std::string fail;
  uint64_t cycle = 0, last_ret = 0, retired = 0, max_gap = 0;
  uint64_t uo_changes = 0;
  uint8_t uo_prev = 0;
  std::vector<uint8_t> uo_trace;
  bool halted = false;
  uint64_t halt_cycle = 0;
  uint64_t fetches = 0, last_fetch = 0;
  std::vector<uint64_t> cls_cycles(32, 0), cls_count(32, 0), cls_max(32, 0);
  const char* cls_names[32] = {nullptr};
  int ncls = 0;

  auto ui_now = [&]() -> uint8_t {
    return rvfi ? ui_value(o.seed, retired) : ui_value(o.seed, uo_changes);
  };

  // power-up: a few cycles with random state and rst_n high, then reset for 2 edges
  top.ena = 1;
  top.ui_in = 0;
  top.uio_in = 0;
  top.clk = 0;
  top.rst_n = 1;
  top.eval();
  for (int i = 0; i < 6; i++) {
    top.rst_n = i < 3 ? 1 : 0;
    top.uio_in = (uint8_t)(rnd_bit() << 2);
    top.clk = 1;
    top.eval();
    top.clk = 0;
    top.eval();
  }
  // the reset requirements hold from the first edge with rst_n low
  if ((top.uio_out & 1) != 1 || (top.uio_out & 8) != 0 || top.uo_out != 0)
    fail = "reset: CS_n/SCK/uo_out not at reset values";
  top.rst_n = 1;
  top.ui_in = ui_now();

  while (fail.empty() && cycle < o.max_cycles) {
    top.uio_in = (uint8_t)(ram.miso() << 2);
    top.clk = 1;
    top.eval();
    cycle++;
    ram.cycle = cycle;
    int s = (top.uio_out >> 3) & 1, c = top.uio_out & 1, m = (top.uio_out >> 1) & 1;
    if (top.uio_oe != 0x0B) fail = "uio_oe is not 0x0B";
    if ((top.uio_out & 0xF4) != 0) fail = "uio_out[7:4] or [2] not 0";
    ram.edge(s, c, m);
#ifdef TRACE_PUBLIC
    if (getenv("TRACE"))
      printf("c%llu st=%d ph=%d j=%d cs=%d sck=%d mosi=%d miso=%d buf=%02x cy=%d cmp=%d\n",
             (unsigned long long)cycle, top.rootp->tt_um_fesens_r32v__DOT__st,
             top.rootp->tt_um_fesens_r32v__DOT__ph, top.rootp->tt_um_fesens_r32v__DOT__j, c, s, m,
             (top.uio_in >> 2) & 1, top.rootp->tt_um_fesens_r32v__DOT__bufr,
             top.rootp->tt_um_fesens_r32v__DOT__cy, top.rootp->tt_um_fesens_r32v__DOT__cmp);
#endif
    if (!ram.err.empty()) {
      fail = "SPI: " + ram.err;
      break;
    }
    if (halted && (!ram.done.empty() || ram.in_txn())) {
      fail = "SPI traffic after the halt";
      break;
    }
    // pins-mode latency proxy: fetches are READs at the ISS-free level; count transaction gaps
    if (!rvfi) {
      while (!ram.done.empty()) {
        Txn t = ram.done.front();
        ram.done.pop_front();
        (void)t;
      }
    }
    if (top.uo_out != uo_prev) {
      uo_prev = top.uo_out;
      uo_trace.push_back(uo_prev);
      uo_changes++;
      if (!rvfi) top.ui_in = ui_now();
    }
#ifdef RISCV_FORMAL
    if (top.rvfi_valid) {
      if (halted) {
        fail = "retirement after the halt";
        break;
      }
      uint8_t ui = ui_now();
      Retire r = iss.step(ui);
      char b[512];
      auto chk = [&](const char* name, uint64_t got, uint64_t exp) {
        if (fail.empty() && got != exp) {
          snprintf(b, sizeof b, "retire %llu pc %08x %s: %s = %llx, expected %llx",
                   (unsigned long long)retired, r.pc, iss_disasm(r.insn).c_str(), name,
                   (unsigned long long)got, (unsigned long long)exp);
          fail = b;
        }
      };
      if (r.reserved) {
        snprintf(b, sizeof b, "program executes a reserved encoding at %08x", r.pc);
        fail = b;
        break;
      }
      chk("order", top.rvfi_order, retired);
      chk("insn", top.rvfi_insn, r.insn);
      chk("trap", top.rvfi_trap, r.trap);
      chk("halt", top.rvfi_halt, r.halt);
      chk("intr", top.rvfi_intr, 0);
      chk("mode", top.rvfi_mode, 3);
      chk("ixl", top.rvfi_ixl, 1);
      chk("pc_rdata", top.rvfi_pc_rdata, r.pc);
      chk("pc_wdata", top.rvfi_pc_wdata, r.npc);
      chk("rs1_addr", top.rvfi_rs1_addr, r.rs1);
      chk("rs2_addr", top.rvfi_rs2_addr, r.rs2);
      chk("rs1_rdata", top.rvfi_rs1_rdata, r.rs1v);
      chk("rs2_rdata", top.rvfi_rs2_rdata, r.rs2v);
      chk("rd_addr", top.rvfi_rd_addr, r.rd);
      chk("rd_wdata", top.rvfi_rd_wdata, r.rdv);
      chk("mem_addr", top.rvfi_mem_addr, (r.rmask | r.wmask) ? r.maddr : 0);
      chk("mem_rmask", top.rvfi_mem_rmask, r.rmask);
      chk("mem_wmask", top.rvfi_mem_wmask, r.wmask);
      chk("mem_rdata", top.rvfi_mem_rdata, r.mrdata);
      chk("mem_wdata", top.rvfi_mem_wdata, r.mwdata);
      // SPI transactions of this instruction: fetch, then optional data access
      if (fail.empty()) {
        if (ram.in_txn()) fail = "SPI transaction still open at retirement";
        std::vector<Txn> ts(ram.done.begin(), ram.done.end());
        ram.done.clear();
        size_t want = 1;
        bool io = (r.maddr >> 31) != 0;
        if ((r.rmask | r.wmask) && !io) want = 2;
        if (ts.size() != want) {
          snprintf(b, sizeof b, "retire %llu pc %08x %s: %zu SPI transactions, expected %zu",
                   (unsigned long long)retired, r.pc, iss_disasm(r.insn).c_str(), ts.size(), want);
          fail = b;
        } else {
          const Txn& f = ts[0];
          if (f.write || f.addr != (uint16_t)r.pc || f.data.size() < 4) {
            snprintf(b, sizeof b, "retire %llu: bad fetch transaction", (unsigned long long)retired);
            fail = b;
          }
          if (want == 2) {
            const Txn& d = ts[1];
            unsigned n = (r.rmask | r.wmask) == 1 ? 1 : (r.rmask | r.wmask) == 3 ? 2 : 4;
            bool okd = d.addr == (uint16_t)r.maddr && d.data.size() == n && d.write == (r.wmask != 0);
            for (unsigned k = 0; okd && k < n; k++) {
              uint8_t exp = (uint8_t)((r.wmask ? r.mwdata : r.mrdata) >> (8 * k));
              if (d.data[k] != exp) okd = false;
            }
            if (!okd) {
              snprintf(b, sizeof b, "retire %llu pc %08x %s: data transaction mismatch",
                       (unsigned long long)retired, r.pc, iss_disasm(r.insn).c_str());
              fail = b;
            }
          }
        }
      }
      // timing by class
      uint64_t gap = cycle - last_ret;
      if (gap > max_gap) max_gap = gap;
      const char* cn = iss_class(r);
      int ci = 0;
      for (; ci < ncls; ci++)
        if (!strcmp(cls_names[ci], cn)) break;
      if (ci == ncls && ncls < 32) cls_names[ncls++] = cn;
      cls_cycles[ci] += gap;
      cls_count[ci]++;
      if (gap > cls_max[ci]) cls_max[ci] = gap;
      last_ret = cycle;
      retired++;
      top.ui_in = ui_now();
      if (r.halt) {
        halted = true;
        halt_cycle = cycle;
      }
    }
    if (!halted && cycle - last_ret > 1024) {
      char b[128];
      snprintf(b, sizeof b, "latency cap: no retirement for 1024 cycles (retired %llu)",
               (unsigned long long)retired);
      fail = b;
    }
#endif
    top.clk = 0;
    top.eval();
    if (halted && cycle - halt_cycle > 4096) break;
    if (!rvfi) {
      // pins mode: detect the halt as a long quiet period with CS_n high
      static uint64_t quiet = 0;
      quiet = ram.in_txn() ? 0 : quiet + 1;
      if (quiet > 4096) {
        halted = true;
        break;
      }
    }
  }
  (void)fetches;
  (void)last_fetch;

  // reference run with the same GPIO schedule
  Iss ref;
  ref.load_image(img);
  std::vector<uint8_t> ref_trace;
  uint64_t ref_ret = 0, ref_changes = 0;
  uint8_t ref_prev = 0;
  while (!ref.halted() && ref_ret < 50000000ull) {
    uint8_t ui = rvfi ? ui_value(o.seed, ref_ret) : ui_value(o.seed, ref_changes);
    Retire r = ref.step(ui);
    if (r.reserved) break;
    ref_ret++;
    if (ref.uo_out() != ref_prev) {
      ref_prev = ref.uo_out();
      ref_trace.push_back(ref_prev);
      ref_changes++;
    }
  }
  if (fail.empty() && !halted) fail = "did not halt within max cycles";
  if (fail.empty() && uo_trace != ref_trace) fail = "uo_out trace differs from the ISS";
  if (fail.empty() && memcmp(ram.mem, ref.ram(), 65536) != 0) {
    for (int a = 0; a < 65536; a++)
      if (ram.mem[a] != ref.ram()[a]) {
        char b[96];
        snprintf(b, sizeof b, "final RAM differs at %04x: %02x vs %02x", a, ram.mem[a], ref.ram()[a]);
        fail = b;
        break;
      }
  }
  if (rvfi && fail.empty() && retired != ref_ret) fail = "retired count differs from the ISS";

  printf("{\"prog\":\"%s\",\"mode\":\"%s\",\"seed\":%u,\"pass\":%s,\"retired\":%llu,\"cycles\":%llu,"
         "\"cpi\":%.1f,\"max_cycles_per_insn\":%llu,\"error\":\"%s\"}\n",
         o.hex, rvfi ? "rvfi" : "pins", o.seed, fail.empty() ? "true" : "false",
         (unsigned long long)retired, (unsigned long long)cycle,
         retired ? (double)cycle / retired : 0.0, (unsigned long long)max_gap, fail.c_str());
  if (rvfi && !o.quiet) {
    for (int i = 0; i < ncls; i++)
      fprintf(stderr, "  %-18s n=%-7llu avg=%-7.1f max=%llu\n", cls_names[i],
              (unsigned long long)cls_count[i], (double)cls_cycles[i] / cls_count[i],
              (unsigned long long)cls_max[i]);
  }
  return fail.empty() ? 0 : 1;
}
