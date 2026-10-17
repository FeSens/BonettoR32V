/* capture (run with --loop: ui0 <- uo7): the timer's toggle output on uo7 feeds its own capture
 * input on both edges. P = 0, CMP = 1000, no auto-reload: the output toggles when CNT passes
 * 1000 (every 65,536 cycles) and each capture sees CNT a few ticks later. Also the output
 * mode 00 (low) and the toggle restart at 0 when the mode is written. */
#include "bonetto3.h"

int main(void) {
  TIMER_CMP = 1000;
  TIMER_CNT = 0;
  ALTFN = 0x80;
  TIMER_CTRL = (3u << 8) | (1u << 6) | (1u << 4);   /* both edges, toggle, enable */
  int level = 0;
  for (int i = 0; i < 3; i++) {
    while (!(IRQ_PEND & IRQ_CAP)) { }
    uint32_t cap = TIMER_CAP;
    IRQ_PEND = IRQ_CAP;
    CHECK(cap > 1000 && cap < 1010);
    level ^= 1;
    CHECK(((GPIO_IN & 1u) != 0) == level);
    CHECK(IRQ_PEND & IRQ_CMP);
    IRQ_PEND = IRQ_CMP;
  }
  /* the output is 1 after three toggles; a mode write restarts the toggle at 0 */
  CHECK(GPIO_IN & 1u);
  TIMER_CTRL = (1u << 6);
  spin(1);
  CHECK(!(GPIO_IN & 1u));
  IRQ_PEND = IRQ_CAP;
  TIMER_CTRL = 0;
  ALTFN = 0;
  IRQ_PEND = IRQ_CAP | IRQ_CMP;
  return 0;
}
