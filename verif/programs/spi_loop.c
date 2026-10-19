/* SPI master loop, contract v3 section 5.6: mode 0, MSB first, D = SPI_DIV_D = 1 (SCK low 2
 * cycles, high 2 cycles). The harness's echo device returns the previous byte it received.
 * With CS_n low (SPI_CTRL[0] = 0 on uo4), it sends 0x00..0x0F and then 0x10, and checks that
 * each transfer after the first returns the byte sent before it (expect.json spi_tx_expect lists
 * the MOSI bytes). After each write it polls until BUSY clears (after the eighth high phase).
 * Then, with D at its maximum (a transfer would keep BUSY at 1 for 2,048 cycles), writing byte 1
 * and the halfword at byte 2 of SPI_DATA starts nothing (contract 5: byte 0 only).
 * uo4 (CS_n) never pulses low outside the transfers: GPIO_OUT = 0x10 (uo4 high, uo5 = SCK low)
 * while the pins are GPIO, before ALTFN hands them to the SPI and again before returning, so
 * crt0's ALTFN = 0 keeps uo4 high until GPIO_OUT = 0x5A (bit 4 = 1, bit 5 = 0).
 * Its outcome depends on the harness's SPI device: expect.json is declared (ruling R13). */
#include "bonetto.h"
#include "stim.h"

int main(void) {
  GPIO_OUT = 0x10; /* CS_n high, SCK low while the pins are GPIO */
  SPI_CTRL = SPI_CS_N | SPI_D(SPI_DIV_D);
  CHECK((SPI_CTRL & 0xF) == (SPI_CS_N | SPI_D(SPI_DIV_D)), 0x10);
  ALTFN = ALT_SPI_CS | ALT_SPI_SCK | ALT_SPI_MOSI;
  SPI_CTRL = SPI_D(SPI_DIV_D); /* CS_n low */
  for (uint32_t i = 0; i <= SPI_N; i++) {
    IO_REG8(SLOT_SPI_DATA) = (uint8_t)i; /* writing byte 0 starts the transfer */
    WAIT_UNTIL(!(SPI_CTRL & SPI_BUSY), 1u << 20, 0x11);
    uint32_t rx = SPI_DATA;
    CHECK(rx >> 8 == 0, 0x12);
    if (i > 0) CHECK(rx == i - 1, 0x13); /* the echo of the previous byte */
  }
  SPI_CTRL = SPI_D(7); /* CS_n low, the slowest SCK */
  *(volatile uint8_t *)(IO_BASE + OFS_SPI_DATA + 1) = 0xA5;
  CHECK(!(SPI_CTRL & SPI_BUSY), 0x15);
  *(volatile uint16_t *)(IO_BASE + OFS_SPI_DATA + 2) = 0xA5A5;
  CHECK(!(SPI_CTRL & SPI_BUSY), 0x16);
  SPI_CTRL = SPI_CS_N | SPI_D(SPI_DIV_D); /* CS_n high */
  CHECK((SPI_CTRL & 0x1F) == (SPI_CS_N | SPI_D(SPI_DIV_D)), 0x14);
  GPIO_OUT = 0x10; /* crt0's ALTFN = 0 then keeps CS_n high */
  return 0;
}
