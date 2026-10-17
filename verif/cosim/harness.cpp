// Pin-level cosim harness for tt_um_fesens_r32v (Verilator main). See docs/cosim.md.
//
//   harness <prog.hex> [--seed S] [--max-cycles C] [--dump-ram PATH] [--trace PATH] [-v]
//           [--region-zero]   (self-test hook: the core region starts zero instead of random)
//   harness --spi-only N [--seed S] [--dump-ram PATH]       (harness self-test, no ISS)
//
// Contract v2: RAM bytes 0xFF80-0xFFFF are the core region (the core's private state). Every SPI
// transaction is a region transaction or a program transaction (contract section 6); region
// transactions are free, persist in the RAM model (they are the core's registers), start from
// random bytes, and are left out of the final-RAM comparison.
//
// Built twice by build.sh:
//   rvfi mode (RVFI_MODE, design built with RISCV_FORMAL): every retirement is compared field by
//     field with the reference ISS, every program SPI transaction is attributed to the instruction
//     that retires next and checked against it (one exact fetch, load READs, store WRITEs),
//     uo_out timing and the latency cap are enforced.
//   pins mode (plain design): the program SPI write stream, the uo_out change sequence and the
//     final RAM image outside the region are compared with the ISS; no RVFI ports exist.
// Both modes: the contract section 2 waveform rules (spiram.h), region/program classification,
// constant uio_oe / unused outputs, reset values, outputs that only change at rising clk edges,
// noise on MISO while SCK is low and on every unused input, random power-up state (+verilator
// seed) and region contents, and no SPI WRITE after the halt (rvfi) or program WRITE after the
// last store (pins).
//
// The last stdout line is a JSON summary. Exit 0 = pass, 1 = fail, 2 = usage or setup error.
#include <verilated.h>

#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include "Vtt_um_fesens_r32v.h"
#include "iss.h"
#include "spiram.h"

#ifdef HARNESS_TRACE
#include <verilated_fst_c.h>
#endif

namespace {

constexpr uint64_t LAT_CAP = 1024;          // contract section 4
constexpr uint64_t HALT_WINDOW = 4 * LAT_CAP;  // rvfi mode: cycles watched after the halt

struct Opts {
  const char* hex = nullptr;
  uint32_t seed = 1;
  uint64_t max_cycles = 0;  // 0 = 1024 * (ISS retirements + 16): the latency cap's bound
  const char* dump_ram = nullptr;
  const char* trace = nullptr;
  uint64_t spi_only = 0;
  int verbose = 0;
  bool region_zero = false;  // test hook: leave the region as the image has it (zeros)
};

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
  for (char c : s) {
    if (c == '"' || c == '\\') o += '\\', o += c;
    else if (c == '\n') o += "\\n";
    else if ((unsigned char)c < 0x20) o += ' ';
    else o += c;
  }
  return o + "\"";
}

std::string trace_json(const std::vector<uint8_t>& t) {
  std::string s = "[";
  for (size_t i = 0; i < t.size(); i++) s += (i ? "," : "") + std::to_string(t[i]);
  return s + "]";
}

std::string describe(uint64_t n, const Retire& e) {
  return fmt("retirement #%llu (pc 0x%08x, insn 0x%08x %s)", (unsigned long long)n, e.pc, e.insn,
             iss_disasm(e.insn).c_str());
}

// What made the ISS step unspecified (Retire::reserved).
const char* unspecified(const Retire& r) {
  return r.region ? "a fetch or RAM access touching the core region 0xFF80-0xFFFF"
                  : "an unspecified instruction";
}

// Contract v2 section 6: every SPI transaction is a region transaction (every byte it addresses
// is in 0xFF80-0xFFFF, no wrap out) or a program transaction (no region byte). The bytes it
// addresses: READ, every byte with at least one bit clocked; WRITE, every complete byte; a
// transaction with no data byte addresses its start address A.
enum class TxnKind { Region, Program, Mixed };

TxnKind classify(const SpiTxn& x) {
  const size_t n = x.bytes.size();
  if (n == 0) return in_region(x.addr) ? TxnKind::Region : TxnKind::Program;
  bool in = false, out = false;
  for (size_t k = 0; k < n; k++) (in_region(x.at(k)) ? in : out) = true;
  return in && out ? TxnKind::Mixed : in ? TxnKind::Region : TxnKind::Program;
}

std::string txn_where(const SpiTxn& x, uint64_t release_edge) {
  return fmt("transaction at 0x%04x, %zu data byte%s, cycles %llu..%llu", x.addr, x.bytes.size(),
             x.bytes.size() == 1 ? "" : "s", (unsigned long long)(x.start - release_edge),
             (unsigned long long)(x.end - release_edge));
}

// A transaction that is neither kind, as an error message.
std::string mixed_error(const SpiTxn& x, uint64_t release_edge) {
  size_t k = 0;
  const bool first_in = in_region(x.at(0));
  while (k < x.bytes.size() && in_region(x.at(k)) == first_in) k++;
  const char* how = first_in ? "wraps out of the core region 0xFF80-0xFFFF"
                             : "runs from program RAM into the core region 0xFF80-0xFFFF";
  return fmt("SPI %s (%s) %s at byte 0x%04x: a transaction is either a region transaction (every "
             "byte in 0xFF80-0xFFFF, no wrap out) or a program transaction (no region byte) "
             "(contract section 6)",
             x.write ? "WRITE" : "READ", txn_where(x, release_edge).c_str(), how, x.at(k));
}

// Per-instruction SPI bookkeeping (rvfi mode): the program transactions of the instruction that
// retires next (contract section 6). Exactly one fetch READ of exactly its 4 bytes; for a RAM
// load, READs of only its bytes that together read all of them; for a RAM store, WRITEs of only
// its bytes, each byte exactly once and with the store's value; nothing else.
struct Expect {
  Retire e;
  uint16_t pc16 = 0;
  bool fetched = false;  // its fetch READ was seen
  int nload = 0;
  uint16_t load[4];
  bool load_read[4];
  int nstore = 0;
  uint16_t store[4];
  uint8_t store_val[4];
  bool store_written[4];

  void set(const Retire& r) {
    e = r;
    pc16 = (uint16_t)r.pc;
    fetched = false;
    nload = nstore = 0;
    bool io = r.maddr >> 31;
    if (r.rmask && !io) {
      nload = __builtin_popcount(r.rmask);
      for (int k = 0; k < nload; k++) load[k] = (uint16_t)(r.maddr + k), load_read[k] = false;
    }
    if (r.wmask && !io) {
      nstore = __builtin_popcount(r.wmask);
      for (int k = 0; k < nstore; k++) {
        store[k] = (uint16_t)(r.maddr + k);
        store_val[k] = (uint8_t)(r.mwdata >> (8 * k));
        store_written[k] = false;
      }
    }
  }
  int load_index(uint16_t a) const {
    for (int k = 0; k < nload; k++)
      if (load[k] == a) return k;
    return -1;
  }
  int store_index(uint16_t a) const {
    for (int k = 0; k < nstore; k++)
      if (store[k] == a) return k;
    return -1;
  }
  std::string allowed() const {
    std::string s = fmt("fetch 0x%04x..0x%04x%s", pc16, (uint16_t)(pc16 + 3),
                        fetched ? " (done)" : "");
    if (nload) s += fmt(", load 0x%04x+%d", load[0], nload);
    if (nstore) {
      s += ", store";
      for (int k = 0; k < nstore; k++) s += fmt(" 0x%04x=0x%02x", store[k], store_val[k]);
    }
    return s + ", and region transactions";
  }
  // One completed program READ. Returns "" or what is wrong with it (who: the instruction,
  // where: the transaction). The first READ of exactly pc[15:0] + k (k < 4) is the fetch; a READ
  // whose every byte is a load byte is a load READ (its complete bytes count for the load), so a
  // load of the instruction's own bytes still needs a READ of its own.
  std::string read_txn(const SpiTxn& x, const std::string& who, const std::string& where) {
    const size_t n = x.bytes.size(), full = x.full_bytes();
    const bool exact_fetch = x.addr == pc16 && n == 4 && full == 4;
    if (exact_fetch && !fetched) {
      fetched = true;
      return "";
    }
    bool only_load = n > 0;
    for (size_t k = 0; k < n; k++) only_load = only_load && load_index(x.at(k)) >= 0;
    if (only_load) {
      for (size_t k = 0; k < full; k++) load_read[load_index(x.at(k))] = true;
      return "";
    }
    if (n == 0)
      return fmt("a program SPI READ (%s) read no data byte: it is neither %s's fetch nor a load "
                 "READ (contract section 6)",
                 where.c_str(), who.c_str());
    if (!fetched && x.addr == pc16) {
      if (n > 4)
        return fmt("the fetch READ of %s (%s) read beyond the instruction's 4 bytes (byte "
                   "0x%04x): a fetch reads exactly that instruction's bytes, and a load reads its "
                   "bytes with a READ of its own (contract section 6)",
                   who.c_str(), where.c_str(), x.at(4));
      return fmt("the fetch READ of %s (%s) read %u data bits: a fetch reads exactly the "
                 "instruction's 4 bytes, 32 bits (contract section 6)",
                 who.c_str(), where.c_str(), x.data_bits);
    }
    if (exact_fetch)
      return fmt("a second fetch READ of %s (%s): exactly one fetch per instruction (contract "
                 "section 6)",
                 who.c_str(), where.c_str());
    size_t k = 0;
    while (load_index(x.at(k)) >= 0) k++;
    return fmt("SPI READ of byte 0x%04x (%s) is not part of %s; allowed: %s", x.at(k),
               where.c_str(), who.c_str(), allowed().c_str());
  }
  // One completed program WRITE: only the store's bytes, each exactly once, with its value.
  std::string write_txn(const SpiTxn& x, const std::string& who, const std::string& where) {
    for (size_t k = 0; k < x.bytes.size(); k++) {
      const int j = store_index(x.at(k));
      if (j < 0)
        return fmt("SPI WRITE of byte 0x%04x = 0x%02x (%s) is not part of %s; allowed: %s",
                   x.at(k), x.bytes[k], where.c_str(), who.c_str(), allowed().c_str());
      if (store_written[j])
        return fmt("SPI WRITE of store byte 0x%04x (%s) a second time for %s: a store writes "
                   "each of its bytes exactly once (contract section 6)",
                   x.at(k), where.c_str(), who.c_str());
      if (x.bytes[k] != store_val[j])
        return fmt("SPI WRITE of byte 0x%04x = 0x%02x (%s) for %s: the store writes 0x%02x there",
                   x.at(k), x.bytes[k], where.c_str(), who.c_str(), store_val[j]);
      store_written[j] = true;
    }
    return "";
  }
};

// One RAM store of the reference run, in program order (pins mode).
struct StoreGroup {
  uint64_t index;  // retirement index
  int n;
  uint16_t addr[4];
  uint8_t val[4];
};

class Harness {
 public:
  explicit Harness(const Opts& o) : o_(o), spi_(o.seed) {}

  int run(const uint8_t* img);

 private:
  Opts o_;
  VerilatedContext* ctx_ = nullptr;
  Vtt_um_fesens_r32v* top_ = nullptr;
#ifdef HARNESS_TRACE
  VerilatedFstC* tfp_ = nullptr;
#endif
  SpiRam spi_;
  std::string error_;
  uint64_t t_ = 0;          // rising clk edges so far
  uint64_t reset_edges_ = 1;  // edges 0 .. reset_edges_-1 have rst_n = 0
  uint8_t ui_ = 0;          // ui_in, driven at every falling edge
  bool ui_pend_ = false;    // rvfi mode: ui_next_ goes on ui_in at the falling edge after ui_at_
  uint64_t ui_at_ = 0;
  uint8_t ui_next_ = 0;
  std::vector<uint8_t> uo_seen_;
  uint8_t uo_last_ = 0;

  // results
  uint64_t retired_ = 0;
  uint64_t cycles_ = 0;
  uint64_t max_gap_ = 0, first_gap_ = 0;
  std::map<std::string, uint64_t> per_class_;
  bool halted_ = false;
  uint64_t halt_edge_ = 0;

  uint64_t since_release(uint64_t t) const { return t - (reset_edges_ - 1); }

  // Cycles are counted from reset release: cycle n is the n-th rising edge with rst_n high.
  bool fail(const std::string& why) {
    if (error_.empty())
      error_ = (t_ < reset_edges_ ? fmt("reset edge %llu: ", (unsigned long long)t_)
                                  : fmt("cycle %llu: ", (unsigned long long)since_release(t_))) +
               why;
    return false;
  }

  void tick_trace(uint64_t time) {
#ifdef HARNESS_TRACE
    if (tfp_) tfp_->dump(time);
#else
    (void)time;
#endif
  }

  uint32_t outputs() const {
    return (uint32_t)top_->uo_out | (uint32_t)top_->uio_out << 8 | (uint32_t)top_->uio_oe << 16;
  }

  // rvfi mode, contract section 3: the cosim changes ui_in within 2 cycles after a retirement.
  // After retirement edge t the new value goes on ui_in at the falling edge after edge t + d,
  // d in {0, 1} chosen per retirement from the seed, so the core's flops first see it at edge
  // t + 1 or t + 2. An IO load that samples ui_in before its own fetch has completed can then
  // read the previous value.
  void schedule_ui(uint8_t v, uint64_t index) {
    if (ui_pend_) ui_ = ui_next_;
    ui_next_ = v;
    ui_at_ = t_ + ((ui_value(o_.seed ^ 0xA5A5A5A5u, index) >> 4) & 1);
    ui_pend_ = true;
  }
};

int Harness::run(const uint8_t* img) {
  ctx_ = new VerilatedContext;
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
  spi_.load(img);
  // Reset length depends on the seed; seed 1 uses the minimum of one edge.
  reset_edges_ = 1 + ((o_.seed + 8) * 5) % 9;
  const bool spi_only = o_.spi_only != 0;
  // Contract section 3: the core region's contents at reset are arbitrary, like on-die registers.
  if (!spi_only && !o_.region_zero)
    for (uint32_t a = REGION_LO; a < SpiRam::SIZE; a++)
      spi_.poke((uint16_t)a, ui_value(o_.seed * 0x2545F491u + 0x5EED, a));

  // ---- reference -----------------------------------------------------------------------------
  Iss iss;
  iss.load_image(img);
#ifdef RVFI_MODE
  Expect pend;
  // uo_out: the ISS's change sequence up to and including the pending instruction. The DUT's
  // uo_out must make exactly these changes, in order: each one no earlier than the previous
  // retirement and no later than one cycle after its IO store retires.
  std::vector<uint8_t> uo_ref_seq;
  uint8_t iss_uo_last = 0;
  size_t uo_dut_n = 0;       // changes the DUT has made
  auto note_iss_uo = [&]() {
    if (iss.uo_out() != iss_uo_last) iss_uo_last = iss.uo_out(), uo_ref_seq.push_back(iss_uo_last);
  };
  uint64_t last_ret = reset_edges_ - 1;
  uint64_t uo_deadline = 0;
  size_t uo_deadline_n = 0;  // changes due by uo_deadline
  uint64_t budget = 0;
  if (!spi_only) {
    // Pre-run once to size the cycle budget.
    Iss probe;
    probe.load_image(img);
    uint64_t n = 0;
    while (!probe.halted() && n < 100000000ULL) {
      Retire r = probe.step(ui_value(o_.seed, n));
      if (r.reserved) break;
      n++;
    }
    budget = o_.max_cycles ? o_.max_cycles : LAT_CAP * (n + 16);
    ui_ = ui_value(o_.seed, 0);
    Retire r = iss.step(ui_);
    if (r.reserved) {
      fail(std::string("test program bug: the ISS hit ") + unspecified(r) + " at " +
           describe(0, r));
    }
    note_iss_uo();
    pend.set(r);
  }
#else
  // Pins mode: run the reference to the end first with the pins ui_in schedule.
  std::vector<StoreGroup> stores;
  std::vector<uint8_t> uo_ref;
  uint64_t ref_retired = 0, last_obs = 0;
  std::vector<uint8_t> ref_ram;
  uint64_t budget = 0;
  if (!spi_only) {
    uint64_t changes = 0;
    uint8_t uo = 0;
    while (!iss.halted()) {
      Retire r = iss.step(ui_value(o_.seed, changes));
      if (r.reserved) {
        fail(std::string("test program bug: the ISS hit ") + unspecified(r) + " at " +
             describe(ref_retired, r));
        break;
      }
      if (r.wmask && !(r.maddr >> 31)) {
        StoreGroup g;
        g.index = ref_retired;
        g.n = __builtin_popcount(r.wmask);
        for (int k = 0; k < g.n; k++) {
          g.addr[k] = (uint16_t)(r.maddr + k);
          g.val[k] = (uint8_t)(r.mwdata >> (8 * k));
        }
        stores.push_back(g);
        last_obs = ref_retired;
      }
      if (iss.uo_out() != uo) {
        uo = iss.uo_out();
        uo_ref.push_back(uo);
        changes++;
        last_obs = ref_retired;
      }
      ref_retired++;
      if (ref_retired > 100000000ULL) {
        fail("test program bug: the ISS ran 100M instructions without halting");
        break;
      }
    }
    ref_ram.assign(iss.ram(), iss.ram() + Iss::RAM_SIZE);
    budget = o_.max_cycles ? o_.max_cycles : LAT_CAP * (ref_retired + 16);
    ui_ = ui_value(o_.seed, 0);
  }
  size_t st_idx = 0;         // store group being matched
  bool st_marks[4] = {false, false, false, false};
  bool st_started = false;   // some byte of stores[st_idx] was seen
  size_t uo_idx = 0;
  bool all_seen = stores.empty() && uo_ref.empty();
  uint64_t all_seen_edge = reset_edges_ - 1;
  uint64_t quiet_until = 0;
  uint64_t last_txn_end = reset_edges_ - 1;
  // instructions after the last observable effect (store or uo_out change), halt included
  const uint64_t tail = ref_retired ? ref_retired - 1 - last_obs : 0;
  if (all_seen) quiet_until = reset_edges_ - 1 + (ref_retired + 8) * LAT_CAP;
#endif
  if (spi_only) budget = o_.spi_only;

  // ---- simulation ----------------------------------------------------------------------------
  top_->clk = 0;
  top_->rst_n = 0;
  top_->ena = 1;
  top_->ui_in = ui_;
  top_->uio_in = (uint8_t)(spi_.noise() & ~4u) | (spi_.miso() << 2);
  top_->eval();
  tick_trace(0);
  bool done = !error_.empty();
  bool passed = false;
  auto t0 = std::chrono::steady_clock::now();

  for (t_ = 0; !done; t_++) {
    // rising edge t
    top_->clk = 1;
    top_->eval();
    tick_trace(2 * t_ + 1);
    const uint32_t out = outputs();
    const uint8_t uo = top_->uo_out;
    const bool cs_n = top_->uio_out & 1, mosi = (top_->uio_out >> 1) & 1,
               sck = (top_->uio_out >> 3) & 1;
    const bool in_reset = t_ < reset_edges_;

    if (top_->uio_oe != 0x0B) {
      fail(fmt("uio_oe = 0x%02x, must be constant 0x0b", top_->uio_oe));
      break;
    }
    if (top_->uio_out & 0xF4) {
      fail(fmt("uio_out = 0x%02x: bits [7:4] and [2] must be 0", top_->uio_out));
      break;
    }
    if (in_reset && (!cs_n || sck || uo)) {
      fail(fmt("during reset CS_n = %d, SCK = %d, uo_out = 0x%02x (must be 1, 0, 0)", cs_n, sck,
               uo));
      break;
    }
#ifdef RVFI_MODE
    if (in_reset && top_->rvfi_valid) {
      fail("rvfi_valid during reset");
      break;
    }
#endif
    if (!spi_.edge(t_, cs_n, sck, mosi)) {
      fail(spi_.error());
      break;
    }
    if (spi_only) {
      if (!in_reset && uo != uo_last_) uo_seen_.push_back(uo), uo_last_ = uo;
      if (!in_reset && since_release(t_) >= budget) {
        passed = true;
        break;
      }
    } else if (!in_reset) {
#ifdef RVFI_MODE
      // SPI transactions completed so far belong to the instruction that retires next. Region
      // transactions are free (any number, READ or WRITE, any order); program transactions must
      // be that instruction's fetch, load READs or store WRITEs (contract section 6).
      for (const SpiTxn& x : spi_.done()) {
        const TxnKind kind = classify(x);
        if (kind == TxnKind::Mixed) {
          fail(mixed_error(x, reset_edges_ - 1));
        } else if (halted_) {
          if (x.write) fail("SPI WRITE after the halt");
        } else if (kind == TxnKind::Region) {
          // the core's own state: any number, READ or WRITE, in any order
        } else {
          const std::string who = describe(retired_, pend.e);
          const std::string where = txn_where(x, reset_edges_ - 1);
          const std::string err = x.write ? pend.write_txn(x, who, where)
                                          : pend.read_txn(x, who, where);
          if (!err.empty()) fail(err);
        }
        if (!error_.empty()) break;
      }
      spi_.done().clear();
      if (!error_.empty()) break;
      if (halted_ && spi_.write_started && spi_.write_started_at > halt_edge_) {
        fail("SPI WRITE started after the halt");
        break;
      }

      if (top_->rvfi_valid) {
        if (halted_) {
          fail(fmt("retirement after the halt (rvfi_valid %llu cycles after the trap)",
                   (unsigned long long)(t_ - halt_edge_)));
          break;
        }
        const Retire& e = pend.e;
        const std::string who = describe(retired_, e);
        // latency cap (contract section 4): at most LAT_CAP edges since the previous retirement
        // (or since reset release, the last edge with rst_n low)
        const uint64_t gap = t_ - last_ret;
        if (gap > LAT_CAP) {
          fail(fmt("latency cap: %s came %llu cycles after %s (limit %llu)", who.c_str(),
                   (unsigned long long)gap,
                   retired_ ? "the previous retirement" : "reset release",
                   (unsigned long long)LAT_CAP));
          break;
        }
        if (!cs_n) {
          fail("SPI transaction still open at " + who +
               " (all of an instruction's SPI traffic must finish before its rvfi_valid cycle)");
          break;
        }
        // The instruction's own program SPI traffic must be complete (checked before the RVFI
        // fields, so a bus bug is named as one even when it also corrupts a value).
        if (!pend.fetched)
          fail(fmt("%s retired without its fetch: an SPI READ of exactly its 4 bytes "
                   "0x%04x..0x%04x",
                   who.c_str(), pend.pc16, (uint16_t)(pend.pc16 + 3)));
        for (int k = 0; k < pend.nload && error_.empty(); k++)
          if (!pend.load_read[k])
            fail(fmt("%s retired without reading load byte 0x%04x over SPI", who.c_str(),
                     pend.load[k]));
        for (int k = 0; k < pend.nstore && error_.empty(); k++)
          if (!pend.store_written[k])
            fail(fmt("%s retired without writing store byte 0x%04x over SPI", who.c_str(),
                     pend.store[k]));
        // A fatal trap has no memory write on the pins (contract section 4): the ISS reports no
        // store for it, so any program WRITE already failed above. Region WRITEs of the trapping
        // instruction are allowed (section 6: any number per instruction; the region is the
        // core's private state, not program memory). After its retirement no WRITE at all may
        // start (checked above).
        if (!error_.empty()) break;
        // field-by-field comparison
        struct F {
          const char* name;
          uint64_t got, exp;
          bool on_trap;  // compared on a trapping retirement too (contract section 6)
        } fields[] = {
            // channel conventions, every pulse
            {"rvfi_order", top_->rvfi_order, retired_, true},
            {"rvfi_intr", top_->rvfi_intr, 0, true},
            {"rvfi_mode", top_->rvfi_mode, 3, true},
            {"rvfi_ixl", top_->rvfi_ixl, 1, true},
            // a trapping retirement: trap, halt, insn, pc_rdata, and no side effects
            {"rvfi_insn", top_->rvfi_insn, e.insn, true},
            {"rvfi_pc_rdata", top_->rvfi_pc_rdata, e.pc, true},
            {"rvfi_trap", top_->rvfi_trap, e.trap, true},
            {"rvfi_halt", top_->rvfi_halt, e.halt, true},
            {"rvfi_rd_addr", top_->rvfi_rd_addr, e.rd, true},
            {"rvfi_mem_wmask", top_->rvfi_mem_wmask, e.wmask, true},
            // everything else only on a normal retirement
            {"rvfi_rs1_addr", top_->rvfi_rs1_addr, e.rs1, false},
            {"rvfi_rs1_rdata", top_->rvfi_rs1_rdata, e.rs1v, false},
            {"rvfi_rs2_addr", top_->rvfi_rs2_addr, e.rs2, false},
            {"rvfi_rs2_rdata", top_->rvfi_rs2_rdata, e.rs2v, false},
            {"rvfi_rd_wdata", top_->rvfi_rd_wdata, e.rdv, false},
            {"rvfi_pc_wdata", top_->rvfi_pc_wdata, e.npc, false},
            {"rvfi_mem_rmask", top_->rvfi_mem_rmask, e.rmask, false},
            {"rvfi_mem_addr", top_->rvfi_mem_addr,
             (e.rmask | e.wmask) ? e.maddr : top_->rvfi_mem_addr, false},
        };
        for (const F& f : fields) {
          if ((f.on_trap || !e.trap) && f.got != f.exp) {
            fail(fmt("RVFI mismatch at %s: %s = 0x%llx, expected 0x%llx", who.c_str(), f.name,
                     (unsigned long long)f.got, (unsigned long long)f.exp));
            break;
          }
        }
        if (!error_.empty()) break;
        uint32_t rl = 0, wl = 0;
        for (int k = 0; k < 4; k++) {
          if ((e.rmask >> k) & 1) rl |= 0xFFu << (8 * k);
          if ((e.wmask >> k) & 1) wl |= 0xFFu << (8 * k);
        }
        if ((top_->rvfi_mem_rdata & rl) != (e.mrdata & rl)) {
          fail(fmt("RVFI mismatch at %s: rvfi_mem_rdata = 0x%08x, expected 0x%08x (lanes 0x%08x)",
                   who.c_str(), top_->rvfi_mem_rdata, e.mrdata, rl));
          break;
        }
        if ((top_->rvfi_mem_wdata & wl) != (e.mwdata & wl)) {
          fail(fmt("RVFI mismatch at %s: rvfi_mem_wdata = 0x%08x, expected 0x%08x (lanes 0x%08x)",
                   who.c_str(), top_->rvfi_mem_wdata, e.mwdata, wl));
          break;
        }
        // latency statistics
        if (retired_ == 0) first_gap_ = gap;
        if (gap > max_gap_) max_gap_ = gap;
        uint64_t& pc = per_class_[iss_class(e)];
        if (gap > pc) pc = gap;
        last_ret = t_;
        // uo_out: the retired instruction's change (if any) must be visible one cycle later
        if (uo_dut_n < uo_ref_seq.size()) {
          uo_deadline = t_ + 1;
          uo_deadline_n = uo_ref_seq.size();
        }
        retired_++;
        if (e.halt) {
          halted_ = true;
          halt_edge_ = t_;
          cycles_ = since_release(t_);
          spi_.write_started = false;
        } else {
          const uint8_t nu = ui_value(o_.seed, retired_);
          schedule_ui(nu, retired_);
          Retire r = iss.step(nu);
          if (r.reserved) {
            fail(std::string("test program bug: the ISS hit ") + unspecified(r) + " at " +
                 describe(retired_, r));
            break;
          }
          note_iss_uo();
          pend.set(r);
        }
      }
      // uo_out makes exactly the reference's changes, in order; nothing else changes it, not
      // even a glitch that returns to the old value before the store retires
      if (uo != uo_last_) {
        if (uo_dut_n >= uo_ref_seq.size() || uo != uo_ref_seq[uo_dut_n]) {
          fail(fmt("uo_out changed to 0x%02x; %s (only an IO store writing byte 0 changes it, "
                   "once; next retirement %s)",
                   uo,
                   uo_dut_n < uo_ref_seq.size()
                       ? fmt("the next change due is to 0x%02x", uo_ref_seq[uo_dut_n]).c_str()
                       : fmt("no change is due (uo_out stays 0x%02x)", uo_last_).c_str(),
                   halted_ ? "none (halted)" : describe(retired_, pend.e).c_str()));
          break;
        }
        uo_dut_n++;
        uo_seen_.push_back(uo);
        uo_last_ = uo;
      }
      if (uo_deadline && t_ == uo_deadline) {
        if (uo_dut_n < uo_deadline_n) {
          fail(fmt("uo_out = 0x%02x one cycle after the IO store retired; expected 0x%02x", uo,
                   uo_ref_seq[uo_deadline_n - 1]));
          break;
        }
        uo_deadline = 0;
      }
      if (!halted_ && t_ - last_ret >= LAT_CAP + 1) {
        fail(fmt("latency cap: no retirement within %llu cycles of %s (next: %s)",
                 (unsigned long long)LAT_CAP,
                 retired_ ? "the previous retirement" : "reset release",
                 describe(retired_, pend.e).c_str()));
        break;
      }
      if (halted_ && t_ - halt_edge_ >= HALT_WINDOW) {
        passed = true;
        break;
      }
#else
      // SPI idle watchdog. Every instruction is fetched over SPI before it retires (rvfi mode
      // checks that) and retires within LAT_CAP cycles of the previous one, so while the program
      // runs, transactions end at most 2 * LAT_CAP cycles apart (LAT_CAP for the first).
      if (!spi_.done().empty()) last_txn_end = t_;
      if (!all_seen && t_ - last_txn_end > (last_txn_end == reset_edges_ - 1 ? 1 : 2) * LAT_CAP) {
        fail(fmt("no SPI transaction for %llu cycles while the program still has stores or GPIO "
                 "writes to do: the core stopped or hung",
                 (unsigned long long)(t_ - last_txn_end)));
        break;
      }
      // Program WRITE bytes must follow the reference run's stores, in order: only a store's
      // bytes, each exactly once, with its value (a store may be split over several WRITEs).
      // Region transactions are the core's own (contract section 6).
      for (const SpiTxn& x : spi_.done()) {
        const TxnKind kind = classify(x);
        if (kind == TxnKind::Mixed) {
          fail(mixed_error(x, reset_edges_ - 1));
          break;
        }
        if (!x.write || kind == TxnKind::Region) continue;
        for (size_t k = 0; k < x.bytes.size() && error_.empty(); k++) {
          uint16_t a = x.at(k);
          uint8_t v = x.bytes[k];
          auto in = [&](size_t i) -> int {  // index of address a in store i, or -1
            if (i >= stores.size()) return -1;
            for (int j = 0; j < stores[i].n; j++)
              if (stores[i].addr[j] == a) return j;
            return -1;
          };
          auto mismatch = [&](size_t i, bool cur_full) {
            const StoreGroup& g = stores[i];
            std::string want;
            for (int q = 0; q < g.n; q++) want += fmt(" 0x%04x=0x%02x", g.addr[q], g.val[q]);
            fail(fmt("SPI WRITE of 0x%02x to 0x%04x (transaction at cycle %llu) doesn't match the "
                     "reference store #%zu of %zu (retirement #%llu):%s%s",
                     v, a, (unsigned long long)since_release(x.start), i, stores.size(),
                     (unsigned long long)g.index, want.c_str(),
                     cur_full ? " (complete; the next store didn't match either)" : ""));
          };
          int j = in(st_idx);
          bool cur_full = st_started;
          for (int q = 0; q < (st_idx < stores.size() ? stores[st_idx].n : 0); q++)
            cur_full = cur_full && st_marks[q];
          int jn = cur_full ? in(st_idx + 1) : -1;
          if (jn >= 0) {  // the next store starts (preferred once the current one is complete)
            if (stores[st_idx + 1].val[jn] != v) {
              mismatch(st_idx + 1, false);
            } else {
              st_idx++;
              for (bool& m : st_marks) m = false;
              st_marks[jn] = true;
              st_started = true;
            }
          } else if (j >= 0 && st_marks[j]) {
            fail(fmt("SPI WRITE of store byte 0x%04x (transaction at cycle %llu) a second time for "
                     "reference store #%zu (retirement #%llu): a store writes each of its bytes "
                     "exactly once (contract section 6)",
                     a, (unsigned long long)since_release(x.start), st_idx,
                     (unsigned long long)stores[st_idx].index));
          } else if (j >= 0 && stores[st_idx].val[j] == v) {
            st_marks[j] = true;
            st_started = true;
          } else if (st_idx < stores.size()) {
            mismatch(st_idx, cur_full);
          } else {
            fail(fmt("SPI WRITE of 0x%02x to 0x%04x after the reference program's last store", v,
                     a));
          }
        }
        if (!error_.empty()) break;
      }
      spi_.done().clear();
      if (!error_.empty()) break;
      if (uo != uo_last_) {
        if (uo_idx >= uo_ref.size() || uo != uo_ref[uo_idx]) {
          fail(fmt("uo_out changed to 0x%02x; the reference's change #%zu is %s", uo, uo_idx,
                   uo_idx < uo_ref.size() ? fmt("0x%02x", uo_ref[uo_idx]).c_str()
                                          : "none (no more changes)"));
          break;
        }
        uo_seen_.push_back(uo);
        uo_last_ = uo;
        uo_idx++;
        ui_ = ui_value(o_.seed, uo_idx);
      }
      if (!all_seen) {
        bool st_done = stores.empty() || st_idx == stores.size() - 1;
        if (st_done && !stores.empty()) {
          st_done = st_started;
          for (int q = 0; q < stores[st_idx].n; q++) st_done = st_done && st_marks[q];
        }
        if (st_done && uo_idx == uo_ref.size()) {
          all_seen = true;
          all_seen_edge = t_;
          quiet_until = t_ + (tail + 8) * LAT_CAP;
          spi_.write_addr_seen = false;
          cycles_ = since_release(t_);
        }
      }
      // Region WRITEs go on (the core's registers); a program WRITE may not start.
      if (all_seen && spi_.write_addr_seen && !in_region(spi_.write_addr)) {
        fail(fmt("SPI WRITE to 0x%04x started after the reference program's last store",
                 spi_.write_addr));
        break;
      }
      if (all_seen && t_ >= quiet_until) {
        passed = true;
        break;
      }
#endif
      // The budget bounds the program itself (to the halt in rvfi mode, to the last observable
      // effect in pins mode); the observation windows after it don't count against it.
#ifdef RVFI_MODE
      const bool program_done = halted_;
#else
      const bool program_done = all_seen;
#endif
      if (!passed && error_.empty() && !program_done && since_release(t_) > budget) {
        fail(fmt("timeout after %llu cycles (the latency cap bounds a correct run to %llu)",
                 (unsigned long long)since_release(t_), (unsigned long long)budget));
        break;
      }
    }
    if (!error_.empty()) break;

    // falling edge: outputs must not change between rising edges
    top_->clk = 0;
    const bool rst_change = (t_ + 1 == reset_edges_);
    top_->rst_n = t_ + 1 >= reset_edges_;
    if (ui_pend_ && t_ >= ui_at_) ui_ = ui_next_, ui_pend_ = false;
    top_->ui_in = ui_;
    top_->uio_in = (uint8_t)(spi_.noise() & ~4u) | (spi_.miso() << 2);
    top_->eval();
    tick_trace(2 * t_ + 2);
    if (!rst_change && outputs() != out) {
      fail(fmt("outputs changed between rising clk edges (uo_out/uio_out/uio_oe 0x%06x -> 0x%06x): "
               "they must come from posedge flip-flops, not from clk or an input",
               out, outputs()));
      break;
    }
  }
  double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();

  // The final RAM compare leaves out the core region (contract section 3).
#ifndef RVFI_MODE
  if (passed && !spi_only) {
    const uint8_t* m = spi_.mem();
    for (uint32_t a = 0; a < REGION_LO; a++)
      if (m[a] != ref_ram[a]) {
        passed = false;
        fail(fmt("final RAM differs from the reference at 0x%04x: 0x%02x, expected 0x%02x", a, m[a],
                 ref_ram[a]));
        break;
      }
    retired_ = ref_retired;
    (void)all_seen_edge;
  }
#else
  if (passed && !spi_only) {
    const uint8_t* m = spi_.mem();
    for (uint32_t a = 0; a < REGION_LO; a++)
      if (m[a] != iss.ram()[a]) {
        passed = false;
        fail(fmt("final RAM differs from the ISS at 0x%04x: 0x%02x, expected 0x%02x", a, m[a],
                 iss.ram()[a]));
        break;
      }
  }
#endif
  if (!error_.empty()) passed = false;

  if (o_.dump_ram) {
    FILE* f = std::fopen(o_.dump_ram, "wb");
    if (f) {
      std::fwrite(spi_.mem(), 1, SpiRam::SIZE, f);
      std::fclose(f);
    }
  }
#ifdef HARNESS_TRACE
  if (tfp_) tfp_->close();
#endif
  top_->final();

  std::string pcj = "{";
  for (auto& kv : per_class_)
    pcj += (pcj.size() > 1 ? "," : "") + json_str(kv.first) + ":" + std::to_string(kv.second);
  pcj += "}";
#ifdef RVFI_MODE
  const char* mode = "rvfi";
  bool timed = retired_ > 0;
#else
  const char* mode = "pins";
  bool timed = false;
#endif
  std::printf(
      "{\"pass\":%s,\"mode\":\"%s\",\"retired\":%llu,\"cycles\":%llu,\"cpi\":%s,"
      "\"max_cycles_per_insn\":%s,\"first_retire_cycle\":%s,\"per_class\":%s,\"uo_trace\":%s,"
      "\"reset_edges\":%llu,\"sim_cycles\":%llu,\"sim_seconds\":%.3f,\"error\":%s}\n",
      passed ? "true" : "false", spi_only ? "spi-only" : mode, (unsigned long long)retired_,
      (unsigned long long)cycles_,
      timed && cycles_ ? fmt("%.3f", (double)cycles_ / (double)retired_).c_str() : "null",
      timed ? std::to_string(max_gap_).c_str() : "null",
      timed ? std::to_string(first_gap_).c_str() : "null", timed ? pcj.c_str() : "null",
      trace_json(uo_seen_).c_str(), (unsigned long long)reset_edges_, (unsigned long long)t_, secs,
      error_.empty() ? "null" : json_str(error_).c_str());
  std::fflush(stdout);
  return passed ? 0 : 1;
}

int usage() {
  std::fprintf(stderr,
               "usage: harness <prog.hex> [--seed S] [--max-cycles C] [--dump-ram PATH] "
               "[--trace PATH] [-v] [--region-zero]\n"
               "       harness --spi-only N [--seed S] [--dump-ram PATH]\n");
  return 2;
}

}  // namespace

int main(int argc, char** argv) {
  Opts o;
  for (int i = 1; i < argc; i++) {
    std::string a = argv[i];
    auto next = [&]() -> const char* {
      if (i + 1 >= argc) std::exit(usage());
      return argv[++i];
    };
    if (a == "--seed") o.seed = (uint32_t)std::strtoul(next(), nullptr, 0);
    else if (a == "--max-cycles") o.max_cycles = std::strtoull(next(), nullptr, 0);
    else if (a == "--dump-ram") o.dump_ram = next();
    else if (a == "--trace") o.trace = next();
    else if (a == "--spi-only") o.spi_only = std::strtoull(next(), nullptr, 0);
    else if (a == "-v") o.verbose++;
    else if (a == "--region-zero") o.region_zero = true;
    else if (a.rfind("+verilator", 0) == 0) continue;
    else if (a[0] == '-') return usage();
    else o.hex = argv[i];
  }
  static uint8_t img[Iss::RAM_SIZE];
  if (o.hex) {
    std::string err;
    if (!load_hex_image(o.hex, img, &err)) {
      std::fprintf(stderr, "%s\n", err.c_str());
      return 2;
    }
  } else if (!o.spi_only) {
    return usage();
  }
  if (o.seed == 0) {
    std::fprintf(stderr, "--seed must be nonzero\n");
    return 2;
  }
  Harness h(o);
  return h.run(img);
}
