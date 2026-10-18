// Unit test for r32v_periph's SPI master (hyp 106), against contract 5.6 rather than one device:
// for every D in 1..7 and many bytes, the slave shows each MISO bit only at the sample edges the
// contract guarantees (the rising SCK edge and the 3 cycles after it) and noise at every other
// edge, so a master that samples outside that window fails. It also checks, every cycle, the
// exact SCK waveform from the store (low 2^D cycles, high 2^D cycles, per bit), MOSI stable
// across each rising edge and set while SCK is low, MOSI MSB first, BUSY from the store through
// the eighth falling edge, SPI_DATA = the slave's byte afterwards, a store while BUSY ignored,
// CS_n = SPI_CTRL[0], the reset values of SPI_DATA and SPI_CTRL, and that D = 0 (unspecified)
// stays live. Flops start random (Verilator --x-initial unique), one reset edge.
//
//   src/test_spi/run.sh   (on hwe-bench via tools/hwe_run.sh: builds with Verilator, runs seeds 1-4)
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <random>

#include "Vr32v_periph.h"
#include "verilated.h"

static Vr32v_periph* m;
static uint64_t t = 0;  // edges so far: the state "after edge t"
static int fails = 0;
static std::mt19937_64 rng;

#define FAIL(...)                                                       \
  do {                                                                  \
    if (fails++ < 20) {                                                 \
      std::printf("FAIL t=%llu: ", (unsigned long long)t);              \
      std::printf(__VA_ARGS__);                                         \
      std::printf("\n");                                                \
    }                                                                   \
  } while (0)

static void edge() {
  m->clk = 0;
  m->eval();
  m->clk = 1;
  m->eval();
  t++;
  m->we = 0;
  m->re = 0;
}
static uint16_t peek(int slot) {  // combinational read of a slot (no side effects: no re)
  m->slot = slot;
  m->off = 0;
  m->size = 1;
  m->eval();
  return m->rdata;
}
static void store(int slot, uint16_t v, int size = 1) {  // the store takes effect at the next edge
  m->slot = slot;
  m->off = 0;
  m->size = size;
  m->wdata = v;
  m->we = 1;
}
static bool sck() { return (m->uo_out >> 5) & 1; }
static bool mosi() { return (m->uo_out >> 6) & 1; }
static bool csn() { return (m->uo_out >> 4) & 1; }

static const int SPI_DATA = 13, SPI_CTRL = 14, ALTFN = 2;

// One transfer of `tx` at divider d with the slave answering `rx`. The store is set up now and
// lands at the next edge t_w. Rising SCK edge k (1..8) is at t_w + h + 2h(k-1), falling edge k at
// t_w + 2hk (h = 2^d). The slave shows bit 7-(k-1) of rx at sample edges rise_k .. rise_k + 3 and
// noise at every other edge. Returns false on any check failure.
static void transfer(int d, uint8_t tx, uint8_t rx, bool poke_busy) {
  const uint64_t h = 1ull << d;
  store(SPI_DATA, tx, 0);
  const uint64_t tw = t + 1;
  const uint64_t end = tw + 16 * h;
  bool prev_sck = false, prev_mosi = false;
  uint8_t got_mosi = 0;
  while (t < end + 3) {
    const uint64_t e = t + 1;  // the edge about to happen
    // MISO for edge e
    bool level = rng() & 1;
    if (e >= tw + h) {
      const uint64_t r = e - (tw + h);  // edges since rising edge 1
      const uint64_t k = r / (2 * h);   // bit index 0..7 (which rising edge this follows)
      if (k < 8 && r % (2 * h) <= 3) level = (rx >> (7 - k)) & 1;
    }
    m->ui_in = (uint8_t)((rng() & 0xF7) | (level << 3));
    if (poke_busy && e == tw + 3) store(SPI_DATA, (uint8_t)~tx, 0);  // ignored: BUSY
    edge();
    const bool s = sck(), mo = mosi();
    // expected SCK after edge t
    bool exp_sck = false;
    if (t >= tw + h && t < end) exp_sck = ((t - tw - h) / h) % 2 == 0;
    if (s != exp_sck) FAIL("d=%d SCK=%d, expected %d (store at %llu)", d, s, exp_sck,
                           (unsigned long long)tw);
    const bool busy = (peek(SPI_CTRL) >> 4) & 1;
    const bool exp_busy = t >= tw && t < end;
    if (busy != exp_busy) FAIL("d=%d BUSY=%d, expected %d (store at %llu)", d, busy, exp_busy,
                               (unsigned long long)tw);
    if (t > tw) {
      if (s && mo != prev_mosi) FAIL("d=%d MOSI changed while SCK high", d);
      if (!prev_sck && s) got_mosi = (uint8_t)((got_mosi << 1) | prev_mosi);  // rising edge
    }
    if (t >= end && mo) FAIL("d=%d MOSI not 0 when idle", d);
    prev_sck = s;
    prev_mosi = mo;
  }
  if (got_mosi != tx) FAIL("d=%d MOSI sent %02x, expected %02x", d, got_mosi, tx);
  const uint16_t data = peek(SPI_DATA);
  if (data != rx) FAIL("d=%d SPI_DATA=%04x, expected %02x (tx %02x)", d, data, rx, tx);
}

int main(int argc, char** argv) {
  const int seed = argc > 1 ? std::atoi(argv[1]) : 1;
  Verilated::randSeed(seed);
  Verilated::randReset(2);
  rng.seed(seed * 7919u);
  m = new Vr32v_periph;
  m->ui_in = 0;
  m->we = 0;
  m->re = 0;
  m->slot = 0;
  m->off = 0;
  m->size = 0;
  m->wdata = 0;
  m->rst_n = 0;
  edge();  // one reset edge
  m->rst_n = 1;
  if (peek(SPI_CTRL) != 0x0001) FAIL("SPI_CTRL after reset = %04x, expected 0001", peek(SPI_CTRL));
  if (peek(SPI_DATA) != 0x0000) FAIL("SPI_DATA after reset = %04x, expected 0000", peek(SPI_DATA));
  for (int i = 0; i < 5; i++) edge();
  store(ALTFN, 0x70, 0);
  edge();
  for (int d = 1; d <= 7; d++) {
    store(SPI_CTRL, (uint16_t)((d << 1) | (d & 1)), 0);  // odd d: CS_n high, even: low
    edge();
    if (csn() != (d & 1)) FAIL("CS_n=%d, expected %d", csn(), d & 1);
    if ((peek(SPI_CTRL) & 0xF) != ((d << 1) | (d & 1))) FAIL("SPI_CTRL readback");
    const int n = d <= 3 ? 64 : 8;
    for (int i = 0; i < n; i++) {
      const uint8_t tx = (uint8_t)rng(), rx = (uint8_t)rng();
      transfer(d, tx, rx, i == 1);
      for (int g = (int)(rng() % 4); g > 0; g--) edge();  // back to back or a few cycles apart
    }
    transfer(d, 0x00, 0xFF, false);
    transfer(d, 0xFF, 0x00, false);
  }
  // D = 0 is unspecified: only liveness (BUSY clears)
  store(SPI_CTRL, 0, 0);
  edge();
  store(SPI_DATA, 0xA5, 0);
  edge();
  int w = 0;
  while ((peek(SPI_CTRL) >> 4) & 1) {
    edge();
    if (++w > 100) {
      FAIL("D=0 transfer never ends");
      break;
    }
  }
  std::printf("{\"seed\":%d,\"pass\":%s,\"fails\":%d,\"cycles\":%llu}\n", seed,
              fails ? "false" : "true", fails, (unsigned long long)t);
  delete m;
  return fails ? 1 : 0;
}
