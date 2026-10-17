/* GPIO demo, contract v3: counts on GPIO_OUT, reads GPIO_IN (ui_in) and echoes it XOR a pattern,
 * then stores a word, a halfword, two bytes and a misaligned word to PSRAM, reads them back with
 * every load width and checks them. Every stored value depends on ui_in, so neither the stores
 * nor the checks fold at compile time; the program passes whatever ui_in is. It then shows folds
 * of the PSRAM words on GPIO_OUT and ends with GPIO_OUT = 0x5A (crt0), or 0xEE with the failing
 * check's id before it.
 *
 * Every value it shows before the end is below 0x80, so it never shows the marks 0xA5, 0xC0 or
 * 0xC1, whatever ui_in is. Small (about 450 retired instructions): the gate-level test runs it.
 * The expected uo_out sequence for ui_in = 0 is build/programs/gpio_demo.expect.json. */
#include "bonetto.h"

static volatile uint32_t buf[4];

static uint8_t fold(uint32_t x) { return (uint8_t)((x ^ (x >> 8) ^ (x >> 16) ^ (x >> 24)) & 0x7F); }

int main(void) {
  uint8_t acc = 0;
  for (uint8_t i = 1; i <= 20; i++) {
    GPIO_OUT = i;                               /* count */
    uint8_t v = (uint8_t)GPIO_IN;               /* ui_in */
    GPIO_OUT = (uint8_t)((v ^ 0x3C) & 0x7F);    /* echo */
    acc = (uint8_t)(acc + v);
  }
  /* PSRAM: every store width, then loads with zero and sign extension. Misaligned accesses never
   * trap (contract 3); asm keeps the compiler from splitting the misaligned word and from
   * replacing lb/lh with lbu/lhu plus shifts. */
  uint32_t a1 = (uint32_t)acc + 1;
  uint32_t x = (a1 << 24) ^ (a1 << 13) ^ (a1 << 3) ^ 0x9E3779B1u;
  uint8_t b6 = (uint8_t)(x >> 3), b7 = (uint8_t)(0x80 | acc);
  buf[0] = x;                                              /* sw at +0 */
  ((volatile uint16_t *)buf)[2] = (uint16_t)(x >> 7);      /* sh at +4 */
  ((volatile uint8_t *)buf)[6] = b6;                       /* sb at +6 */
  ((volatile uint8_t *)buf)[7] = b7;                       /* sb at +7 */
  __asm__ volatile("sw %0, 9(%1)" : : "r"(~x), "r"(buf) : "memory"); /* sw at +9..+12 */
  uint32_t mis, lb7, lh6;
  __asm__ volatile("lw %0, 9(%1)" : "=r"(mis) : "r"(buf) : "memory");
  __asm__ volatile("lb %0, 7(%1)" : "=r"(lb7) : "r"(buf) : "memory"); /* negative byte */
  __asm__ volatile("lh %0, 6(%1)" : "=r"(lh6) : "r"(buf) : "memory"); /* negative halfword */
  uint32_t lhu6 = ((volatile uint16_t *)buf)[3];                       /* lhu at +6 */
  uint32_t h67 = ((uint32_t)b7 << 8) | b6;
  CHECK(mis == ~x, 1);
  CHECK(lb7 == (uint32_t)(int32_t)(int8_t)b7, 2);
  CHECK(lh6 == (uint32_t)(int32_t)(int16_t)h67, 3);
  CHECK(lhu6 == h67, 4);
  CHECK(buf[0] == x, 5);
  CHECK(buf[1] == (((x >> 7) & 0xFFFF) | (h67 << 16)), 6);
  CHECK(buf[2] == (~x << 8), 7);  /* byte 8 was 0 (.bss), bytes 9..11 = ~x bytes 0..2 */
  CHECK(buf[3] == (~x >> 24), 8); /* byte 12 = ~x byte 3, bytes 13..15 still 0 */
  GPIO_OUT = fold(buf[0]);
  GPIO_OUT = fold(buf[1]);
  GPIO_OUT = fold(mis ^ buf[2] ^ buf[3]);
  GPIO_OUT = fold(lb7 ^ (lh6 << 3) ^ (lhu6 << 5));
  /* GPIO_IN bits 31:8 read 0 (not implemented). */
  CHECK((GPIO_IN >> 8) == 0, 9);
  return 0;
}
