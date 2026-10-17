// C API over PmodBus (pmod.h) for ctypes: built by build.sh (no arguments) as
// build/cosim/libpmod.so.
//
//   img: the flash image, exactly 16 MiB (copied at creation), or NULL for an erased flash (all
//        0xFF).
//   pmod_new: flash_mode 0 Standard, 1 Continuous; psram_mode 0 SpiUninit, 1 Spi, 2 Qpi. Returns
//        NULL for an out-of-range mode.
//   pmod_new_for_seed: the power-up modes of pmod_init_for_seed(seed) (contract 2.4), with seed
//        also as the noise seed.
//   pmod_edge returns 1, or 0 on the first violation (pmod_error says why; it is "" until then).
#include "pmod.h"

extern "C" {

void* pmod_new(uint32_t seed, const uint8_t* img, int flash_mode, int psram_mode, double clk_ns) {
  if (flash_mode < 0 || flash_mode > 1 || psram_mode < 0 || psram_mode > 2) return nullptr;
  PmodInit init{(FlashMode)flash_mode, (PsramMode)psram_mode};
  return new PmodBus(seed, img, init, clk_ns);
}

void* pmod_new_for_seed(uint32_t seed, const uint8_t* img, double clk_ns) {
  return new PmodBus(seed, img, pmod_init_for_seed(seed), clk_ns);
}

int pmod_edge(void* p, uint64_t t, uint8_t out, uint8_t oe) {
  return static_cast<PmodBus*>(p)->edge(t, out, oe) ? 1 : 0;
}

uint8_t pmod_uio_in(void* p) { return static_cast<PmodBus*>(p)->uio_in(); }

const char* pmod_error(void* p) { return static_cast<PmodBus*>(p)->error().c_str(); }

void pmod_free(void* p) { delete static_cast<PmodBus*>(p); }

}  // extern "C"
