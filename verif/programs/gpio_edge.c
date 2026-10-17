/* GPIO edge flags, contract v3 sections 5.7 and 5.8: rising and falling flags per pin, W1C,
 * enables, and IRQ_PEND.GPIO = OR of GPIO_EPEND. The harness drives ui_in from expect.json
 * gpio_script (stim.h GE_V0..GE_V7, one stage every GE_GAP cycles). GE_H (pins 1, 4, 5, 6) starts
 * high, GE_L (pins 0, 2, 3, 7) low. For each stage the program waits until GPIO_IN shows the new
 * value, lets the flags settle, then checks GPIO_EPEND:
 *   phase 1 (L rising, H falling): V1 L rises -> L; V2 H falls -> H; V3 L falls -> none
 *   phase 2 (L falling, H rising): V4 H rises -> H; V5 L rises -> none; V6 L falls -> L
 *   phase 3 (only pins 0, 2, 4, 6 enabled, rising): V7 all rise -> 0x55
 * Clearing uses W1C one pin at a time (the other flags must stay), all at once, and a byte store;
 * writing 1s to clear flags does nothing. Changing the enables or the polarity is not an edge.
 * ui1 is the UART RX line: the divider is set first so the stray frames stay in spec.
 * Its outcome depends on the stimulus timing: expect.json is declared (ruling R13). */
#include "bonetto.h"
#include "stim.h"

/* More than 3 cycles (each instruction takes at least one): synchronizer and edge detection. */
static void settle(void) { __asm__ volatile("nop\n\tnop\n\tnop\n\tnop" : : : "memory"); }

static void wait_ui(uint32_t v, uint32_t id) {
  WAIT_UNTIL((GPIO_IN & 0xFF) == v, 1u << 22, id);
  settle();
}

static void expect_flags(uint32_t want, uint32_t id) {
  CHECK(GPIO_EPEND == want, id);
  CHECK(((IRQ_PEND & IRQ_GPIO) != 0) == (want != 0), id + 1);
}

/* W1C one flag at a time, lowest first; the others stay set. */
static void clear_one_by_one(uint32_t flags, uint32_t id) {
  GPIO_EPEND = ~flags & 0xFF; /* 1s on clear flags: nothing changes */
  CHECK(GPIO_EPEND == flags, id);
  while (flags) {
    uint32_t b = flags & -flags;
    GPIO_EPEND = b;
    flags &= ~b;
    CHECK(GPIO_EPEND == flags, id + 1);
  }
}

int main(void) {
  UART_CTRL = UART_DIV;
  wait_ui(GE_V0, 0x10);
  GPIO_EDGE = EDGE_EN(0xFF) | EDGE_FALL(GE_H);
  CHECK(GPIO_EDGE == (EDGE_EN(0xFF) | EDGE_FALL(GE_H)), 0x11);
  settle();
  expect_flags(0, 0x12); /* enabling is not an edge */

  wait_ui(GE_V1, 0x14);
  expect_flags(GE_L, 0x15);
  clear_one_by_one(GE_L, 0x17);
  expect_flags(0, 0x19);

  wait_ui(GE_V2, 0x1b);
  expect_flags(GE_H, 0x1c);
  GPIO_EPEND = GE_H; /* all at once */
  expect_flags(0, 0x1e);

  wait_ui(GE_V3, 0x20);
  expect_flags(0, 0x21);

  GPIO_EDGE = EDGE_EN(0xFF) | EDGE_FALL(GE_L);
  settle();
  expect_flags(0, 0x23); /* a polarity change is not an edge */

  wait_ui(GE_V4, 0x25);
  expect_flags(GE_H, 0x26);
  clear_one_by_one(GE_H, 0x28);

  wait_ui(GE_V5, 0x2a);
  expect_flags(0, 0x2b);

  wait_ui(GE_V6, 0x2d);
  expect_flags(GE_L, 0x2e);
  IO_REG8(SLOT_GPIO_EPEND) = GE_L; /* a byte store */
  expect_flags(0, 0x30);

  GPIO_EDGE = EDGE_EN(GE_EN3);
  settle();
  expect_flags(0, 0x32);
  wait_ui(GE_V7, 0x34);
  expect_flags(GE_EN3, 0x35);
  GPIO_EPEND = 0xFF;
  expect_flags(0, 0x37);

  GPIO_EDGE = 0;
  return 0;
}
