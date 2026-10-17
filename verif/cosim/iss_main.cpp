// Standalone runner for the reference ISS.
//
//   iss_main <prog.hex> [--max N] [--log] [--seed S] [--ui rvfi|pins|const:V]
//            [--start-pc A] [--io-ram] [--dump-ram PATH] [--expect PATH] [--check-init]
//
// --log prints one JSON line per retirement. The last stdout line is always a summary
// ({"summary": true, ...}). Exit codes: 0 halted on EBREAK, 2 reserved/unspecified instruction
// (a reserved encoding, a misaligned IO access, or a fetch or RAM access touching the core region
// 0xFF80-0xFFFF: then the summary has "region": true),
// 3 halted on another fatal trap, 4 --max reached, 5 --check-init and a register was read before
// any write, 1 usage or I/O error.
//
// GPIO input schedules (the value an instruction observes on ui_in):
//   rvfi:    instruction i sees ui_value(seed, i)                 (the RVFI cosim's schedule)
//   pins:    instruction i sees ui_value(seed, c), where c is the number of uo_out value changes
//            made by instructions before i                         (pins cosim, gate-level test)
//   const:V  every instruction sees V
// --start-pc and --io-ram are test hooks for the spike cross-check only.
//
// Contract v3 mode (verif/cosim/iss3.h, docs/contract-v3.md):
//
//   iss_main --v3 <prog.hex> [--max N] [--json] [--log] [--vread SLOT=V]... [--irq N]...
//            [--check-init] [--dump-psram PATH]
//
// prog.hex is a flash image in $readmemh form (sparse, @addr records; unset bytes read 0xFF). The
// run prints the retired count, the uo_out GPIO-mode trace (GPIO_OUT & ~ALTFN, one entry per
// change) and the end reason: "selfloop" (jal x0, 0 retired twice in a row at the same pc),
// "max" (N retirements, default 50,000,000), "region" (a fetch or access touched the core region)
// or "unspecified" (any other unspecified step, see iss3.h). Plain text by default; --json makes
// the last stdout line a JSON summary. --log prints one JSON line per retirement first, and one
// per interrupt entry ("irq_entry": 1); a line whose step consulted a volatile value adds
// "vslot", "viss" (the model's value) and "vdut" (the value used).
//   --vread SLOT=V  the next volatile read of SLOT (an IO slot 0-31, or 32 = mip) gets V as the
//                   DUT's value; repeat to queue more. Without a queued value the model's own
//                   value is used.
//   --irq N         an interrupt request from retirement N on, taken (take_interrupt) at the
//                   first instruction boundary from there where irq_allowed(); repeatable, each
//                   request is taken once.
//   --check-init    exit 5 when a register or an arbitrary-reset CSR is read before any write.
//   --dump-psram P  write the final 8 MiB PSRAM image to P (raw bytes from PSRAM byte 0; bytes
//                   no store wrote hold the model's seed pattern), whatever the end reason.
//                   verif/arch-test/run_arch.py --v3 reads the signature from it.
// Exit codes: 0 selfloop, 2 unspecified, 3 region, 4 max, 5 --check-init failed, 1 usage or I/O.
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <map>
#include <string>
#include <vector>

#include "iss.h"
#define ISS3_NAMESPACED  // v2's iss.h owns the global Retire, iss_class and iss_disasm
#include "iss3.h"

namespace {

struct RunResult {
  uint64_t retired = 0;
  std::string halt;  // ebreak | ecall | trap | reserved | max
  uint32_t halt_pc = 0, halt_insn = 0;
  std::vector<uint8_t> uo_trace;
  uint8_t uo_final = 0;
  bool uninit = false;
  uint32_t uninit_pc = 0;
  bool region = false;  // the unspecified step was a fetch or RAM access touching 0xFF80-0xFFFF
  uint32_t min_sp = 0xFFFFFFFFu;
};

enum class UiMode { Rvfi, Pins, Const };

RunResult run(const uint8_t* img, uint64_t max, UiMode mode, uint32_t seed, uint8_t ui_const,
              bool set_pc, uint32_t start_pc, bool io_ram, bool log, Iss* keep) {
  Iss iss;
  iss.load_image(img);
  if (set_pc) iss.set_pc(start_pc);
  if (io_ram) iss.set_io_ram_compat(true);
  RunResult res;
  uint8_t uo_prev = 0;
  uint64_t changes = 0;
  for (uint64_t i = 0; i < max; i++) {
    uint8_t ui = mode == UiMode::Rvfi ? ui_value(seed, i)
                 : mode == UiMode::Pins ? ui_value(seed, changes)
                                        : ui_const;
    Retire r = iss.step(ui);
    if (r.reserved) {
      res.halt = "reserved";
      res.region = r.region;
      res.halt_pc = r.pc;
      res.halt_insn = r.insn;
      break;
    }
    res.retired++;
    if (iss.reg(2) && iss.reg(2) < res.min_sp) res.min_sp = iss.reg(2);
    if (iss.uo_out() != uo_prev) {
      uo_prev = iss.uo_out();
      res.uo_trace.push_back(uo_prev);
      changes++;
    }
    if (log) {
      std::printf(
          "{\"i\":%llu,\"pc\":%u,\"npc\":%u,\"insn\":%u,\"trap\":%d,\"halt\":%d,"
          "\"rs1\":%u,\"rs1v\":%u,\"rs2\":%u,\"rs2v\":%u,\"rd\":%u,\"rdv\":%u,"
          "\"maddr\":%u,\"rmask\":%u,\"wmask\":%u,\"mrdata\":%u,\"mwdata\":%u,\"ui\":%u,\"uo\":%u}\n",
          (unsigned long long)i, r.pc, r.npc, r.insn, r.trap, r.halt, r.rs1, r.rs1v, r.rs2, r.rs2v,
          r.rd, r.rdv, r.maddr, r.rmask, r.wmask, r.mrdata, r.mwdata, ui, iss.uo_out());
    }
    if (r.halt) {
      res.halt = r.insn == 0x00100073u ? "ebreak" : r.insn == 0x00000073u ? "ecall" : "trap";
      res.halt_pc = r.pc;
      res.halt_insn = r.insn;
      break;
    }
  }
  if (res.halt.empty()) {
    res.halt = "max";
    res.halt_pc = iss.pc();
  }
  res.uo_final = iss.uo_out();
  res.uninit = iss.uninit_read();
  res.uninit_pc = iss.uninit_pc();
  if (keep) *keep = iss;
  return res;
}

std::string trace_json(const std::vector<uint8_t>& t) {
  std::string s = "[";
  for (size_t i = 0; i < t.size(); i++) {
    if (i) s += ",";
    s += std::to_string(t[i]);
  }
  return s + "]";
}

int usage() {
  std::fprintf(stderr,
               "usage: iss_main <prog.hex> [--max N] [--log] [--seed S] [--ui rvfi|pins|const:V]\n"
               "                [--start-pc A] [--io-ram] [--dump-ram PATH] [--expect PATH] "
               "[--check-init]\n");
  return 1;
}

// ---- contract v3 mode ---------------------------------------------------------------------------
int usage_v3() {
  std::fprintf(stderr,
               "usage: iss_main --v3 <prog.hex> [--max N] [--json] [--log] [--vread SLOT=V]... "
               "[--irq N]... [--check-init] [--dump-psram PATH]\n");
  return 1;
}

void log_v3(const v3::Retire& r, uint64_t i, const v3::Iss3& iss, bool entry, bool vused,
            uint32_t vslot, uint32_t viss, uint32_t vdut) {
  std::printf(
      "{\"i\":%llu,%s\"pc\":%u,\"npc\":%u,\"insn\":%u,\"intr\":%d,\"trap\":%d,"
      "\"rs1\":%u,\"rs1v\":%u,\"rs2\":%u,\"rs2v\":%u,\"rd\":%u,\"rdv\":%u,"
      "\"maddr\":%u,\"rmask\":%u,\"wmask\":%u,\"mrdata\":%u,\"mwdata\":%u,"
      "\"mcause\":%u,\"mstatus\":%u,\"mepc\":%u,\"uo\":%u",
      (unsigned long long)i, entry ? "\"irq_entry\":1," : "", r.pc, r.npc, r.insn, r.intr, r.trap,
      r.rs1, r.rs1v, r.rs2, r.rs2v, r.rd, r.rdv, r.maddr, r.rmask, r.wmask, r.mrdata, r.mwdata,
      r.mcause_after, iss.csr_peek(0x300), iss.csr_peek(0x341), iss.uo_gpio());
  if (vused) std::printf(",\"vslot\":%u,\"viss\":%u,\"vdut\":%u", vslot, viss, vdut);
  std::printf("}\n");
}

int main_v3(int argc, char** argv) {
  const char* hex = nullptr;
  uint64_t max = 50000000ULL;
  bool json = false, log = false, check_init = false;
  const char* dump_psram = nullptr;
  std::map<uint32_t, std::deque<uint32_t>> vq;  // --vread values per slot
  std::vector<uint64_t> irqs;                   // --irq requests
  for (int i = 1; i < argc; i++) {
    std::string a = argv[i];
    auto next = [&]() -> const char* {
      if (i + 1 >= argc) {
        std::fprintf(stderr, "missing value for %s\n", a.c_str());
        std::exit(1);
      }
      return argv[++i];
    };
    if (a == "--v3") continue;
    if (a == "--max") max = std::strtoull(next(), nullptr, 0);
    else if (a == "--json") json = true;
    else if (a == "--log") log = true;
    else if (a == "--check-init") check_init = true;
    else if (a == "--dump-psram") dump_psram = next();
    else if (a == "--irq") irqs.push_back(std::strtoull(next(), nullptr, 0));
    else if (a == "--vread") {
      const char* s = next();
      char* end = nullptr;
      unsigned long slot = std::strtoul(s, &end, 0);
      if (*end != '=' || slot > v3::VSLOT_MIP) return usage_v3();
      vq[(uint32_t)slot].push_back((uint32_t)std::strtoul(end + 1, nullptr, 0));
    } else if (a[0] == '-') return usage_v3();
    else hex = argv[i];
  }
  if (!hex) return usage_v3();
  std::vector<uint8_t> img(v3::FLASH_SIZE);
  std::string err;
  if (!v3::load_flash_hex(hex, img.data(), &err)) {
    std::fprintf(stderr, "%s\n", err.c_str());
    return 1;
  }
  std::sort(irqs.begin(), irqs.end());

  v3::Iss3 iss(img.data());
  bool vused = false;
  uint32_t vslot = 0, viss = 0, vdut = 0;
  v3::IoHooks hooks;
  hooks.volatile_read = [&](uint32_t slot, uint32_t iss_value) {
    uint32_t v = iss_value;
    auto it = vq.find(slot);
    if (it != vq.end() && !it->second.empty()) {
      v = it->second.front();
      it->second.pop_front();
    }
    vused = true;
    vslot = slot, viss = iss_value, vdut = v;
    return v;
  };

  std::string end;
  uint32_t end_pc = 0, end_insn = 0, min_sp = 0xFFFFFFFFu;
  std::vector<uint8_t> uo_trace;
  uint8_t uo_prev = iss.uo_gpio();
  size_t next_irq = 0;
  uint64_t pending = 0;
  for (;;) {
    if (iss.retired() >= max) {
      end = "max";
      end_pc = iss.pc();
      break;
    }
    while (next_irq < irqs.size() && irqs[next_irq] <= iss.retired()) pending++, next_irq++;
    if (pending && iss.irq_allowed()) {
      pending--;
      v3::Retire e = iss.take_interrupt();
      if (log) log_v3(e, iss.retired(), iss, true, false, 0, 0, 0);
    }
    vused = false;
    const uint64_t idx = iss.retired();
    v3::Retire r = iss.step(hooks);
    if (r.reserved) {
      end = r.region ? "region" : "unspecified";
      end_pc = r.pc;
      end_insn = r.insn;
      break;
    }
    if (iss.reg(2) && iss.reg(2) < min_sp) min_sp = iss.reg(2);
    if (iss.uo_gpio() != uo_prev) uo_trace.push_back(uo_prev = iss.uo_gpio());
    if (log) log_v3(r, idx, iss, false, vused, vslot, viss, vdut);
    if (iss.selfloop()) {
      end = "selfloop";
      end_pc = r.pc;
      end_insn = r.insn;
      break;
    }
  }

  if (dump_psram) {
    std::vector<uint8_t> m(v3::PSRAM_SIZE);
    for (uint32_t a = 0; a < v3::PSRAM_SIZE; a++) m[a] = iss.psram(a);
    FILE* f = std::fopen(dump_psram, "wb");
    const bool ok = f && std::fwrite(m.data(), 1, m.size(), f) == m.size();
    if (!f || std::fclose(f) != 0 || !ok) {
      std::fprintf(stderr, "cannot write %s\n", dump_psram);
      return 1;
    }
  }
  if (json) {
    std::printf(
        "{\"summary\":true,\"retired\":%llu,\"end\":\"%s\",\"end_pc\":%u,\"end_insn\":%u,"
        "\"uo_trace\":%s,\"gpio_out\":%u,\"altfn\":%u,\"traps\":%llu,\"interrupts\":%llu,"
        "\"touched_region\":%s,\"uninit_read\":%s,\"uninit_pc\":%u,\"min_sp\":%u}\n",
        (unsigned long long)iss.retired(), end.c_str(), end_pc, end_insn,
        trace_json(uo_trace).c_str(), iss.io().gpio_out(), iss.io().altfn(),
        (unsigned long long)iss.traps(), (unsigned long long)iss.interrupts(),
        iss.touched_region ? "true" : "false", iss.uninit_read() ? "true" : "false",
        iss.uninit_pc(), min_sp);
  } else {
    std::printf("retired %llu\nend %s at pc 0x%08x\nuo_trace", (unsigned long long)iss.retired(),
                end.c_str(), end_pc);
    for (uint8_t v : uo_trace) std::printf(" %02x", v);
    std::printf("\ngpio_out %02x altfn %02x\n", iss.io().gpio_out(), iss.io().altfn());
  }
  if (end == "unspecified" || end == "region")
    std::fprintf(stderr, "%s: 0x%08x (%s) at pc 0x%08x (contract v3 sections 3-4)\n", end.c_str(),
                 end_insn, v3::iss_disasm(end_insn).c_str(), end_pc);
  if (check_init && iss.uninit_read()) {
    std::fprintf(stderr, "register or CSR read before any write at pc 0x%08x\n", iss.uninit_pc());
    return 5;
  }
  if (end == "selfloop") return 0;
  if (end == "unspecified") return 2;
  if (end == "region") return 3;
  return 4;
}

}  // namespace

int main(int argc, char** argv) {
  for (int i = 1; i < argc; i++)
    if (std::strcmp(argv[i], "--v3") == 0) return main_v3(argc, argv);
  if (argc < 2) return usage();
  const char* hex = nullptr;
  uint64_t max = 50000000ULL;
  bool log = false, io_ram = false, set_pc = false, check_init = false;
  uint32_t seed = 1, start_pc = 0;
  UiMode mode = UiMode::Pins;
  uint8_t ui_const = 0;
  const char* dump = nullptr;
  const char* expect = nullptr;
  for (int i = 1; i < argc; i++) {
    std::string a = argv[i];
    auto next = [&]() -> const char* {
      if (i + 1 >= argc) {
        std::fprintf(stderr, "missing value for %s\n", a.c_str());
        std::exit(1);
      }
      return argv[++i];
    };
    if (a == "--max") max = std::strtoull(next(), nullptr, 0);
    else if (a == "--log") log = true;
    else if (a == "--seed") seed = (uint32_t)std::strtoul(next(), nullptr, 0);
    else if (a == "--ui") {
      std::string m = next();
      if (m == "rvfi") mode = UiMode::Rvfi;
      else if (m == "pins") mode = UiMode::Pins;
      else if (m.rfind("const:", 0) == 0) {
        mode = UiMode::Const;
        ui_const = (uint8_t)std::strtoul(m.c_str() + 6, nullptr, 0);
      } else return usage();
    } else if (a == "--start-pc") {
      set_pc = true;
      start_pc = (uint32_t)std::strtoul(next(), nullptr, 0);
    } else if (a == "--io-ram") io_ram = true;
    else if (a == "--dump-ram") dump = next();
    else if (a == "--expect") expect = next();
    else if (a == "--check-init") check_init = true;
    else if (a[0] == '-') return usage();
    else hex = argv[i];
  }
  if (!hex) return usage();
  static uint8_t img[Iss::RAM_SIZE];
  std::string err;
  if (!load_hex_image(hex, img, &err)) {
    std::fprintf(stderr, "%s\n", err.c_str());
    return 1;
  }
  static Iss final_iss;
  RunResult res = run(img, max, mode, seed, ui_const, set_pc, start_pc, io_ram, log, &final_iss);
  std::printf(
      "{\"summary\":true,\"retired\":%llu,\"halt\":\"%s\",\"halt_pc\":%u,\"halt_insn\":%u,"
      "\"uo_final\":%u,\"uo_trace\":%s,\"uninit_read\":%s,\"uninit_pc\":%u,\"min_sp\":%u,"
      "\"region\":%s}\n",
      (unsigned long long)res.retired, res.halt.c_str(), res.halt_pc, res.halt_insn, res.uo_final,
      trace_json(res.uo_trace).c_str(), res.uninit ? "true" : "false", res.uninit_pc, res.min_sp,
      res.region ? "true" : "false");
  if (dump) {
    FILE* f = std::fopen(dump, "wb");
    if (!f || std::fwrite(final_iss.ram(), 1, Iss::RAM_SIZE, f) != Iss::RAM_SIZE) {
      std::fprintf(stderr, "cannot write %s\n", dump);
      return 1;
    }
    std::fclose(f);
  }
  if (expect) {
    // The gate-level test and the pins cosim use the pins schedule; record the RVFI schedule too.
    RunResult rv = run(img, max, UiMode::Rvfi, seed, 0, false, 0, false, false, nullptr);
    RunResult pn = mode == UiMode::Pins && !set_pc && !io_ram
                       ? res
                       : run(img, max, UiMode::Pins, seed, 0, false, 0, false, false, nullptr);
    FILE* f = std::fopen(expect, "w");
    if (!f) {
      std::fprintf(stderr, "cannot write %s\n", expect);
      return 1;
    }
    std::fprintf(f,
                 "{\"seed\": %u, \"ui_schedule\": \"pins\", \"retired\": %llu, \"halt\": \"%s\", "
                 "\"halt_pc\": %u, \"halt_insn\": %u, \"uo_final\": %u, \"uo_trace\": %s,\n"
                 " \"rvfi\": {\"retired\": %llu, \"halt\": \"%s\", \"uo_final\": %u, \"uo_trace\": "
                 "%s}}\n",
                 seed, (unsigned long long)pn.retired, pn.halt.c_str(), pn.halt_pc, pn.halt_insn,
                 pn.uo_final, trace_json(pn.uo_trace).c_str(), (unsigned long long)rv.retired,
                 rv.halt.c_str(), rv.uo_final, trace_json(rv.uo_trace).c_str());
    std::fclose(f);
  }
  if (check_init && res.uninit) {
    std::fprintf(stderr, "register read before any write at pc 0x%08x\n", res.uninit_pc);
    return 5;
  }
  if (res.halt == "ebreak") return 0;
  if (res.halt == "reserved") {
    if (res.region)
      std::fprintf(stderr,
                   "unspecified: 0x%08x (%s) at pc 0x%08x touches the core region 0xFF80-0xFFFF "
                   "(contract section 3; test programs never do)\n",
                   res.halt_insn, iss_disasm(res.halt_insn).c_str(), res.halt_pc);
    else
      std::fprintf(stderr, "reserved/unspecified instruction 0x%08x (%s) at pc 0x%08x\n",
                   res.halt_insn, iss_disasm(res.halt_insn).c_str(), res.halt_pc);
    return 2;
  }
  if (res.halt == "max") return 4;
  return 3;
}
