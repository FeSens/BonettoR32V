// Pin timing probe for hypothesis 113 (PSRAM tCPH, the boot's continuous-read entry read).
//
// Runs the plain design against the QSPI Pmod models of verif/cosim/pmod.h (which check the
// contract-2 rules in force) and measures, over the whole run:
//   - the CS_n high time between two consecutive transactions of the same chip, per chip
//     (minimum and a histogram, in cycles);
//   - the first transactions after reset: chip, edges, the nibble driven at each edge and the
//     edges clocked with SD0-SD3 released (uio_oe = 0), and the cycle the release starts.
// Observation only; the gates do not run it.
//
//   tb <prog.hex> [--seed S] [--cycles N] [--clk-ns 20] [--log K]
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "Vtt_um_fesens_r32v.h"
#include "verilated.h"
#include "iss3.h"
#include "pmod.h"

using namespace pmod_detail;

struct Txn {
  bool flash = false;
  uint64_t start = 0, end = 0, release = 0;
  int edges = 0, released_edges = 0;
  std::string nibs;
};

int main(int argc, char** argv) {
  const char* hex = nullptr;
  uint32_t seed = 1;
  uint64_t cycles = 2000000;
  double clk_ns = 20.0;
  int log_n = 6;
  for (int i = 1; i < argc; i++) {
    std::string a = argv[i];
    if (a == "--seed") seed = (uint32_t)std::strtoul(argv[++i], nullptr, 0);
    else if (a == "--cycles") cycles = std::strtoull(argv[++i], nullptr, 0);
    else if (a == "--clk-ns") clk_ns = std::atof(argv[++i]);
    else if (a == "--log") log_n = std::atoi(argv[++i]);
    else hex = argv[i];
  }
  if (!hex) {
    std::fprintf(stderr, "usage: tb <prog.hex> [--seed S] [--cycles N] [--clk-ns 20] [--log K]\n");
    return 2;
  }
  std::vector<uint8_t> img(v3::FLASH_SIZE);
  std::string err;
  if (!load_flash_hex(hex, img.data(), &err)) {
    std::fprintf(stderr, "%s\n", err.c_str());
    return 2;
  }
  auto ctx = std::make_unique<VerilatedContext>();
  ctx->randReset(2);
  ctx->randSeed((int)seed);
  auto top = std::make_unique<Vtt_um_fesens_r32v>(ctx.get());
  PmodBus pmod(seed, img.data(), pmod_init_for_seed(seed), clk_ns);

  const uint64_t reset_edges = 1;
  uint64_t hist[2][10] = {};
  uint64_t min_hi[2] = {~0ull, ~0ull}, n_txn[2] = {}, rise_t[2] = {};
  bool seen_rise[2] = {false, false};
  bool pcs[2] = {true, true}, psck = false;
  std::vector<Txn> log;
  Txn cur;
  bool in_txn = false;
  std::string error;

  top->clk = 0;
  top->rst_n = 0;
  top->ena = 1;
  top->ui_in = 0;
  top->uio_in = pmod.uio_in();
  top->eval();
  uint64_t t = 0;
  for (; t < cycles; t++) {
    top->clk = 1;
    top->eval();
    const uint8_t out = top->uio_out, oe = top->uio_oe;
    if (!pmod.edge(t, out, oe)) {
      error = pmod.error();
      break;
    }
    if (t >= reset_edges) {
      const bool cs[2] = {(out & CSF) != 0, (out & CSR) != 0};  // [0] flash, [1] PSRAM
      const bool s = (out & SCK) != 0;
      for (int k = 0; k < 2; k++) {
        if (!pcs[k] && cs[k]) {  // CS_n rose at t: high from t on
          rise_t[k] = t;
          seen_rise[k] = true;
          if (in_txn && (int)log.size() < log_n) {
            cur.end = t;
            log.push_back(cur);
          }
          in_txn = false;
        }
        if (pcs[k] && !cs[k]) {  // CS_n fell at t
          n_txn[k]++;
          if (seen_rise[k]) {
            const uint64_t hi = t - rise_t[k];
            if (hi < min_hi[k]) min_hi[k] = hi;
            hist[k][hi < 9 ? hi : 9]++;
          }
          cur = Txn();
          cur.flash = (k == 0);
          cur.start = t;
          in_txn = true;
        }
      }
      if (in_txn) {
        if (!psck && s) {  // rising SCK edge
          cur.edges++;
          if ((oe & SDALL) == 0) cur.released_edges++;
          if ((oe & SDALL) == SDALL) {
            char b[4];
            std::snprintf(b, sizeof b, "%X", uio_to_nib(out));
            cur.nibs += b;
          } else {
            cur.nibs += (oe & SDALL) ? "?" : "z";
          }
        }
        if ((oe & SDALL) == 0 && cur.release == 0) cur.release = t;
        if ((oe & SDALL) != 0) cur.release = 0;  // released only if it stays released
      }
      pcs[0] = cs[0];
      pcs[1] = cs[1];
      psck = s;
    }
    top->clk = 0;
    top->rst_n = t + 1 >= reset_edges;
    top->uio_in = pmod.uio_in();
    top->eval();
  }

  std::printf("{\"prog\":\"%s\",\"seed\":%u,\"cycles\":%llu,\"pass\":%s,\"error\":\"%s\"}\n", hex,
              seed, (unsigned long long)t, error.empty() ? "true" : "false", error.c_str());
  const char* name[2] = {"flash", "psram"};
  for (int k = 0; k < 2; k++) {
    std::printf("%s: %llu transactions, min CS_n high between two of them %llu cycles; high-time "
                "histogram (cycles:count)",
                name[k], (unsigned long long)n_txn[k],
                (unsigned long long)(min_hi[k] == ~0ull ? 0 : min_hi[k]));
    for (int h = 0; h < 10; h++)
      if (hist[k][h]) std::printf(" %d%s:%llu", h, h == 9 ? "+" : "", (unsigned long long)hist[k][h]);
    std::printf("\n");
  }
  for (size_t i = 0; i < log.size(); i++) {
    const Txn& x = log[i];
    std::printf("txn %zu %s cycles %llu..%llu (CS_n low %llu): %d edges, %d with SD0-3 released "
                "(released from cycle %llu); nibbles %s\n",
                i, x.flash ? "flash" : "psram", (unsigned long long)x.start,
                (unsigned long long)x.end - 1, (unsigned long long)(x.end - x.start), x.edges,
                x.released_edges, (unsigned long long)x.release, x.nibs.c_str());
  }
  return error.empty() ? 0 : 1;
}
