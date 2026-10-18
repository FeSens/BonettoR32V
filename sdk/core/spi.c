/* BonettoR32V SDK: SPI master (contract 5.6): mode 0, MSB first, SCK on UO5, MOSI on UO6, MISO on
 * UI3, CS_n on UO4 (SS). While SPI owns UO4, digitalWrite(SS, LOW/HIGH) drives CS_n through
 * SPI_CTRL[0], so Arduino code that toggles SS works unchanged. A byte takes 16 x 2^D cycles. */
#include "r32v_internal.h"

static uint32_t spi_d = 2; /* SCK = F_CPU / 8 by default (6.25 MHz at 50 MHz) */

void spi_begin(void) {
  const uint32_t m = r32v_irq_save();
  SPI_CTRL = SPI_CS_N | SPI_D(spi_d);
  GPIO_OUT = GPIO_OUT | (1u << SS); /* if the pins go back to GPIO, CS_n stays high */
  ALTFN = ALTFN | ALT_SPI_CS | ALT_SPI_SCK | ALT_SPI_MOSI;
  r32v_spi_on = 1;
  r32v_irq_restore(m);
}

void spi_end(void) {
  while (SPI_CTRL & SPI_BUSY) {
  }
  const uint32_t m = r32v_irq_save();
  SPI_CTRL = SPI_CS_N | SPI_D(spi_d);
  ALTFN = ALTFN & ~(ALT_SPI_CS | ALT_SPI_SCK | ALT_SPI_MOSI);
  GPIO_OUT = (GPIO_OUT | (1u << SS)) & ~((1u << SCK) | (1u << MOSI));
  r32v_spi_on = 0;
  r32v_irq_restore(m);
}

uint32_t spi_set_clock(uint32_t hz) {
  uint32_t d = 1; /* D >= 1 (contract 5.6) */
  while (d < 7 && ((uint32_t)F_CPU >> (d + 1)) > hz) d++;
  spi_d = d;
  const uint32_t m = r32v_irq_save();
  SPI_CTRL = (SPI_CTRL & SPI_CS_N) | SPI_D(d);
  r32v_irq_restore(m);
  return (uint32_t)F_CPU >> (d + 1);
}

uint8_t spi_transfer(uint8_t b) {
  SPI_DATA8 = b; /* writing byte 0 starts the transfer */
  while (SPI_CTRL & SPI_BUSY) {
  }
  return SPI_DATA8;
}
