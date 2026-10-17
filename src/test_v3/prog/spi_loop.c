/* spi_loop (run with --loop: ui3 <- uo6, ui6 <- uo4): SPI master MOSI looped to MISO at every
 * D (0-7); each received byte equals the byte sent; BUSY; CS_n on uo4 follows SPI_CTRL[0]. */
#include "bonetto3.h"

static uint8_t xfer(uint8_t b) {
  SPI_DATA = b;
  while (SPI_CTRL & SPI_BUSY) { }
  return (uint8_t)SPI_DATA;
}

int main(void) {
  ALTFN = 0x70;                    /* uo4 CS_n, uo5 SCK, uo6 MOSI */
  SPI_CTRL = (2u << 1) | 1u;
  spin(1);
  CHECK(GPIO_IN & (1u << 6));      /* CS_n = 1 */
  SPI_CTRL = (2u << 1);
  spin(1);
  CHECK(!(GPIO_IN & (1u << 6)));
  static const uint8_t v[] = {0x00, 0xFF, 0xA5, 0x5A, 0x01, 0x80, 0x3C, 0xC3};
  for (unsigned i = 0; i < sizeof v; i++) CHECK(xfer(v[i]) == v[i]);
  /* every divider: MISO is the pin during the high phase, whatever the synchronizer delay */
  for (unsigned d = 0; d < 8; d++) {
    SPI_CTRL = d << 1;
    CHECK(xfer((uint8_t)(0x96 ^ d)) == (uint8_t)(0x96 ^ d));
    CHECK(xfer((uint8_t)(0x69 + d)) == (uint8_t)(0x69 + d));
  }
  SPI_CTRL = (7u << 1);            /* a transfer outlasts an instruction */
  SPI_DATA = 0x11;
  SPI_DATA = 0x22;                 /* ignored: busy (one instruction later) */
  CHECK(SPI_CTRL & SPI_BUSY);
  while (SPI_CTRL & SPI_BUSY) { }
  CHECK(SPI_DATA == 0x11);
  SPI_CTRL = 1;
  ALTFN = 0;
  return 0;
}
