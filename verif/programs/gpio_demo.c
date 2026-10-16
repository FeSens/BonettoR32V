/* GPIO demo: counts on uo_out, reads ui_in and echoes it XOR a pattern.
 * Small (well under 2,000 retired instructions): the gate-level test runs it.
 * Expected uo_out sequence: build/programs/gpio_demo.expect.json (pins ui_in schedule). */
#include "bonetto.h"

int main(void) {
  uint8_t acc = 0;
  for (uint8_t i = 1; i <= 20; i++) {
    GPIO = i;                    /* count */
    uint8_t v = GPIO;            /* ui_in */
    GPIO = (uint8_t)(v ^ 0x5A);  /* echo XOR pattern */
    acc = (uint8_t)(acc + v);
  }
  /* A word read of the GPIO address returns ui_in in byte 0 and zeros above. */
  uint32_t w = GPIO_WORD;
  GPIO = (w >> 8) == 0 ? (uint8_t)(acc ^ 0xC3) : 0xEE;
  return 0;
}
