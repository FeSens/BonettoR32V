// Unit test harness for the register-file-and-pc-in-RAM core (hypothesis 002, variant 2). Not a
// contract gate.
//
// Verilator top: tt_um_fesens_r32v. A 23LC512 model checks contract section 2 on every clock
// edge and drives noise on MISO whenever SCK is low. The reserved region 0xFF80..0xFFFF starts
// with random bytes: x_r at 0xFFC0 + 4r (stored LSB first: bit 8n+i of x_r is bit 7-i of byte
// 4r+n), the pc in the x0 slot at 0xFFC0, a scratch word at 0xFF80. Two modes:
//   rvfi  (built with -DRISCV_FORMAL): every retirement is compared with the reference ISS. Its
//         SPI transactions outside the region must be: the fetch, then the data access of a RAM
//         load or store (a SW is two 2-byte WRITEs at addr and addr + 2); region transactions
//         move 2 or 4 bytes. After every retirement the region must hold the ISS's value of
//         every register written so far, and the x0 slot the next pc.
//   pins  (plain build): the final RAM image outside the region, the final register values in
//         the region and the uo_out change trace are compared with the ISS run on the program.
// The rvfi mode enforces the 1,024-cycle latency cap per retirement. Programs must not access
// the region (checked on the ISS trace).
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

const int RF_BASE = 0xFF80;  // the reserved region
const int REG_BASE = 0xFFC0;  // x_r at REG_BASE + 4r; the pc in the x0 slot

// x_r as stored in the region: LSB first on the wire, so bit 8n+i is bit 7-i of byte 4r+n.
uint32_t region_reg(const uint8_t* mem, int r) {
  uint32_t v = 0;
  for (int n = 0; n < 4; n++)
    for (int i = 0; i < 8; i++)
      v |= (uint32_t)((mem[REG_BASE + 4 * r + n] >> (7 - i)) & 1) << (8 * n + i);
  return v;
}

bool in_region(uint16_t a, size_t n) { return a >= RF_BASE && a + n <= 0x10000; }

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
  for (int a = RF_BASE; a < 65536; a++) {
    uint8_t v = 0;
    for (int b = 0; b < 8; b++) v = (uint8_t)((v << 1) | rnd_bit());
    ram.mem[a] = v;
  }
  uint32_t written = 1;  // registers the ISS has written (x0 always)

  Iss iss;
  iss.load_image(img);
#ifdef RISCV_FORMAL
  const bool rvfi = true;
#else
  const bool rvfi = false;
#endif

  std::string fail;
  uint64_t cycle = 0, last_ret = 0, retired = 0, max_gap = 0, first_gap = 0;
  uint64_t uo_changes = 0;
  uint8_t uo_prev = 0;
  std::vector<uint8_t> uo_trace;
  bool halted = false;
  uint64_t halt_cycle = 0;
  uint64_t fetches = 0, last_fetch = 0, reg_txns = 0;
  std::vector<uint64_t> cls_cycles(48, 0), cls_count(48, 0), cls_max(48, 0), cls_min(48, ~0ull);
  std::vector<std::string> cls_names(48);
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
    if (getenv("TRACE")) {
      auto* R = top.rootp;
      printf("c%llu sl=%d ep=%d ph=%d j=%d cs=%d sck=%d mosi=%d miso=%d cy=%d cmp=%d mem=%d bf=%09llx\n",
             (unsigned long long)cycle, R->tt_um_fesens_r32v__DOT__sl, R->tt_um_fesens_r32v__DOT__ep,
             R->tt_um_fesens_r32v__DOT__ph, R->tt_um_fesens_r32v__DOT__j, c, s, m,
             (top.uio_in >> 2) & 1, R->tt_um_fesens_r32v__DOT__cy, R->tt_um_fesens_r32v__DOT__cmp,
             R->tt_um_fesens_r32v__DOT__mem, (unsigned long long)R->tt_um_fesens_r32v__DOT__bfr);
    }
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
      // SPI transactions of this instruction: fetch, register traffic, optional data access
      bool io = (r.maddr >> 31) != 0;
      if (fail.empty() && (r.rmask | r.wmask) && !io) {
        unsigned n = (r.rmask | r.wmask) == 1 ? 1 : (r.rmask | r.wmask) == 3 ? 2 : 4;
        if ((uint32_t)(r.maddr & 0xFFFF) + n > (uint32_t)RF_BASE) {
          snprintf(b, sizeof b, "program accesses the register region at pc %08x", r.pc);
          fail = b;
        }
      }
      if (fail.empty()) {
        if (ram.in_txn()) fail = "SPI transaction still open at retirement";
        std::vector<Txn> all(ram.done.begin(), ram.done.end());
        ram.done.clear();
        std::vector<Txn> ts;
        for (size_t i = 0; i < all.size(); i++) {
          bool reg = in_region(all[i].addr, all[i].data.size());
          if (reg) {
            if (all[i].data.size() != 4 && all[i].data.size() != 2) {
              snprintf(b, sizeof b, "retire %llu: register transaction of %zu bytes", (unsigned long long)retired, all[i].data.size());
              fail = b;
            }
            reg_txns++;
          } else
            ts.push_back(all[i]);
        }
        size_t want = 1;
        bool split = r.wmask == 0xF && !io;  // a RAM SW: two 2-byte WRITEs
        if ((r.rmask | r.wmask) && !io) want = split ? 3 : 2;
        if ((r.pc & 0xFFFFu) >= (uint32_t)RF_BASE && fail.empty()) fail = "program executes in the region";
        if (!fail.empty()) {
        } else if (ts.size() != want) {
          snprintf(b, sizeof b, "retire %llu pc %08x %s: %zu SPI transactions, expected %zu",
                   (unsigned long long)retired, r.pc, iss_disasm(r.insn).c_str(), ts.size(), want);
          fail = b;
        } else {
          const Txn& f = ts[0];
          if (f.write || f.addr != (uint16_t)r.pc || f.data.size() < 4) {
            snprintf(b, sizeof b, "retire %llu: bad fetch transaction", (unsigned long long)retired);
            fail = b;
          }
          if (want >= 2) {
            unsigned n = (r.rmask | r.wmask) == 1 ? 1 : (r.rmask | r.wmask) == 3 ? 2 : 4;
            bool okd = true;
            std::vector<uint8_t> db;
            for (size_t q = 1; q < ts.size(); q++) {
              const Txn& d = ts[q];
              uint16_t ea = (uint16_t)(r.maddr + (split ? 2 * (q - 1) : 0));
              if (d.addr != ea || d.write != (r.wmask != 0) || d.data.size() != (split ? 2u : n)) okd = false;
              db.insert(db.end(), d.data.begin(), d.data.end());
            }
            for (unsigned k = 0; okd && k < n; k++) {
              uint8_t exp = (uint8_t)((r.wmask ? r.mwdata : r.mrdata) >> (8 * k));
              if (db.size() != n || db[k] != exp) okd = false;
            }
            if (!okd) {
              snprintf(b, sizeof b, "retire %llu pc %08x %s: data transaction mismatch",
                       (unsigned long long)retired, r.pc, iss_disasm(r.insn).c_str());
              fail = b;
            }
          }
        }
      }
      // the region holds every register the ISS has written, and the x0 slot the next pc
      if (r.rd) written |= 1u << r.rd;
      if (fail.empty() && !r.trap && region_reg(ram.mem, 0) != r.npc) {
        snprintf(b, sizeof b, "retire %llu pc %08x %s: pc slot = %08x, expected %08x",
                 (unsigned long long)retired, r.pc, iss_disasm(r.insn).c_str(), region_reg(ram.mem, 0), r.npc);
        fail = b;
      }
      for (int q = 1; q < 16 && fail.empty(); q++)
        if (((written >> q) & 1) && region_reg(ram.mem, q) != iss.reg(q)) {
          snprintf(b, sizeof b, "retire %llu pc %08x %s: region x%d = %08x, expected %08x",
                   (unsigned long long)retired, r.pc, iss_disasm(r.insn).c_str(), q,
                   region_reg(ram.mem, q), iss.reg(q));
          fail = b;
        }
      // timing by class
      uint64_t gap = cycle - last_ret;
      if (gap > max_gap) max_gap = gap;
      if (retired == 0) first_gap = gap;  // includes the boot slot; kept out of the class table
      // class name, with the access width for memory classes and the direction for shifts
      std::string cn = iss_class(r);
      unsigned msk = r.rmask | r.wmask;
      if (msk) cn += msk == 1 ? "_b" : msk == 3 ? "_h" : "_w";
      if (cn == "shift") {
        unsigned s = ((r.insn & 0x7F) == 0x13) ? (r.insn >> 20) & 31 : r.rs2v & 31;
        cn += ((r.insn >> 14) & 1) ? "_right" : "_left";
        cn += s == 0 ? "_0" : s <= 8 ? "_1-8" : s <= 16 ? "_9-16" : s <= 24 ? "_17-24" : "_25-31";
      }
      int ci = 0;
      if (retired == 0) cn = "first_after_reset";
      for (; ci < ncls; ci++)
        if (cls_names[ci] == cn) break;
      if (ci == ncls) {
        if (ncls < 47) cls_names[ncls++] = cn;
        else ci = 47;
      }
      cls_cycles[ci] += gap;
      cls_count[ci]++;
      if (gap > cls_max[ci]) cls_max[ci] = gap;
      if (gap < cls_min[ci]) cls_min[ci] = gap;
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
  if (fail.empty() && memcmp(ram.mem, ref.ram(), RF_BASE) != 0) {
    for (int a = 0; a < RF_BASE; a++)
      if (ram.mem[a] != ref.ram()[a]) {
        char b[96];
        snprintf(b, sizeof b, "final RAM differs at %04x: %02x vs %02x", a, ram.mem[a], ref.ram()[a]);
        fail = b;
        break;
      }
  }
  if (rvfi && fail.empty() && retired != ref_ret) fail = "retired count differs from the ISS";
  // final registers (those the program wrote) must be in the region
  {
    uint32_t wr = 1;
    Iss w;
    w.load_image(img);
    uint64_t k = 0, ch = 0;
    uint8_t pv = 0;
    while (!w.halted() && k < 50000000ull) {
      Retire q = w.step(rvfi ? ui_value(o.seed, k) : ui_value(o.seed, ch));
      if (q.reserved) break;
      k++;
      if (w.uo_out() != pv) { pv = w.uo_out(); ch++; }
      if (q.rd && !q.trap) wr |= 1u << q.rd;
    }
    for (int q = 1; q < 16 && fail.empty(); q++)
      if (((wr >> q) & 1) && region_reg(ram.mem, q) != ref.reg(q)) {
        char b[96];
        snprintf(b, sizeof b, "final region x%d = %08x, expected %08x", q, region_reg(ram.mem, q), ref.reg(q));
        fail = b;
      }
  }

  printf("{\"prog\":\"%s\",\"mode\":\"%s\",\"seed\":%u,\"pass\":%s,\"retired\":%llu,\"cycles\":%llu,"
         "\"cpi\":%.1f,\"max_cycles_per_insn\":%llu,\"first_gap\":%llu,\"reg_txns\":%llu,\"error\":\"%s\"}\n",
         o.hex, rvfi ? "rvfi" : "pins", o.seed, fail.empty() ? "true" : "false",
         (unsigned long long)retired, (unsigned long long)cycle,
         retired ? (double)cycle / retired : 0.0, (unsigned long long)max_gap,
         (unsigned long long)first_gap, (unsigned long long)reg_txns, fail.c_str());
  if (rvfi && !o.quiet) {
    for (int i = 0; i < ncls; i++)
      fprintf(stderr, "  %-20s n=%-7llu min=%-5llu avg=%-7.1f max=%llu\n", cls_names[i].c_str(),
              (unsigned long long)cls_count[i], (unsigned long long)cls_min[i],
              (double)cls_cycles[i] / cls_count[i], (unsigned long long)cls_max[i]);
  }
  return fail.empty() ? 0 : 1;
}
