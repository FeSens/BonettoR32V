/* GPIO demo: counts on uo_out, reads ui_in and echoes it XOR a pattern, then stores a word, a
 * halfword, two bytes and a misaligned word to RAM, reads them back and shows them on uo_out
 * (so the TT test, which only sees uo_out and the SPI pins, covers WRITE transactions too).
 * Small (well under 2,000 retired instructions): the gate-level test runs it.
 * Expected uo_out sequence: build/programs/gpio_demo.expect.json (pins ui_in schedule). */
#include "bonetto.h"

static volatile uint32_t buf[4];

static uint8_t fold(uint32_t x) { return (uint8_t)(x ^ (x >> 8) ^ (x >> 16) ^ (x >> 24)); }

int main(void) {
  uint8_t acc = 0;
  for (uint8_t i = 1; i <= 20; i++) {
    GPIO = i;                    /* count */
    uint8_t v = GPIO;            /* ui_in */
    GPIO = (uint8_t)(v ^ 0x5A);  /* echo XOR pattern */
    acc = (uint8_t)(acc + v);
  }
  /* RAM: every store width, then loads with zero and sign extension. The values depend on ui_in,
   * so a wrong byte shows on uo_out. Misaligned accesses never trap (contract section 3); asm
   * keeps the compiler from splitting the misaligned word and from replacing lb/lh with
   * lbu/lhu plus shifts. */
  uint32_t x = 0x9E3779B1u * (uint32_t)(acc + 1);
  buf[0] = x;                                            /* sw at +0 */
  ((volatile uint16_t *)buf)[2] = (uint16_t)(x >> 7);    /* sh at +4 */
  ((volatile uint8_t *)buf)[6] = (uint8_t)(x >> 3);      /* sb at +6 */
  ((volatile uint8_t *)buf)[7] = (uint8_t)(0x80 | acc);  /* sb at +7 */
  __asm__ volatile("sw %0, 9(%1)" : : "r"(~x), "r"(buf) : "memory");  /* sw at +9..+12 */
  uint32_t mis, b7, h3;
  __asm__ volatile("lw %0, 9(%1)" : "=r"(mis) : "r"(buf) : "memory");
  __asm__ volatile("lb %0, 7(%1)" : "=r"(b7) : "r"(buf) : "memory");  /* negative byte */
  __asm__ volatile("lh %0, 6(%1)" : "=r"(h3) : "r"(buf) : "memory");  /* negative halfword */
  uint32_t hu = ((volatile uint16_t *)buf)[3];                         /* lhu at +6 */
  GPIO = fold(buf[0]);
  GPIO = fold(buf[1]);
  GPIO = fold(mis ^ buf[2] ^ buf[3]);
  GPIO = fold(b7 ^ (h3 << 3) ^ (hu << 5));
  /* A word read of the GPIO address returns ui_in in byte 0 and zeros above. */
  uint32_t w = GPIO_WORD;
  GPIO = (w >> 8) == 0 ? (uint8_t)(acc ^ 0xC3) : 0xEE;
  return 0;
}
