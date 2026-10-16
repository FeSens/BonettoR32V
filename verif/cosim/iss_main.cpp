// Standalone runner for the reference ISS.
//
//   iss_main <prog.hex> [--max N] [--log] [--seed S] [--ui rvfi|pins|const:V]
//            [--start-pc A] [--io-ram] [--dump-ram PATH] [--expect PATH] [--check-init]
//
// --log prints one JSON line per retirement. The last stdout line is always a summary
// ({"summary": true, ...}). Exit codes: 0 halted on EBREAK, 2 reserved/unspecified instruction,
// 3 halted on another fatal trap, 4 --max reached, 5 --check-init and a register was read before
// any write, 1 usage or I/O error.
//
// GPIO input schedules (the value an instruction observes on ui_in):
//   rvfi:    instruction i sees ui_value(seed, i)                 (the RVFI cosim's schedule)
//   pins:    instruction i sees ui_value(seed, c), where c is the number of uo_out value changes
//            made by instructions before i                         (pins cosim, gate-level test)
//   const:V  every instruction sees V
// --start-pc and --io-ram are test hooks for the spike cross-check only.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "iss.h"

namespace {

struct RunResult {
  uint64_t retired = 0;
  std::string halt;  // ebreak | ecall | trap | reserved | max
  uint32_t halt_pc = 0, halt_insn = 0;
  std::vector<uint8_t> uo_trace;
  uint8_t uo_final = 0;
  bool uninit = false;
  uint32_t uninit_pc = 0;
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

}  // namespace

int main(int argc, char** argv) {
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
      "\"uo_final\":%u,\"uo_trace\":%s,\"uninit_read\":%s,\"uninit_pc\":%u,\"min_sp\":%u}\n",
      (unsigned long long)res.retired, res.halt.c_str(), res.halt_pc, res.halt_insn, res.uo_final,
      trace_json(res.uo_trace).c_str(), res.uninit ? "true" : "false", res.uninit_pc, res.min_sp);
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
    std::fprintf(stderr, "reserved/unspecified instruction 0x%08x (%s) at pc 0x%08x\n",
                 res.halt_insn, iss_disasm(res.halt_insn).c_str(), res.halt_pc);
    return 2;
  }
  if (res.halt == "max") return 4;
  return 3;
}
