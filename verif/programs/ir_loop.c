/* Timer output looped back to the capture input, contract v3 section 5.3. The harness connects
 * uo7 (the timer output, ALTFN[7] = 1) to ui0 (the capture input): expect.json
 * loop_uo7_to_ui0. A tick is 2^IR_P = 16 cycles, longer than the loop's latency (the output
 * register, the input synchronizer and the capture: a few cycles), so a capture lands in the
 * tick of its edge.
 *   A. One match without auto-reload (CMP = IR_CMP_ABS, toggle mode, capture on rising edges):
 *      the output rises at the match, where CNT becomes CMP + 1, so CAP = CMP + 1 (+-1), the
 *      match set IRQ_PEND.CMP, and ui0 is high. Then the counter stops and the output is driven
 *      low with capture on falling edges: capture works while the counter is disabled, CAP is
 *      the frozen count, and ui0 is low.
 *   B. Auto-reload at CMP = IR_CMP, toggle mode, capture on both edges: an edge every CMP + 1
 *      ticks. For IR_CAPTURES captures: CAP is right after the reload to 0 (CAP <= 2), the
 *      delta between successive captures, (CMP + 1) + CAP[k] - CAP[k-1], is CMP + 1 ticks +-1,
 *      and the live CNT never exceeds CMP.
 *   C. Free-running (no auto-reload), toggle mode, capture on both edges; at each match the
 *      program moves CMP IR_HALF ticks ahead. Each capture is CMP + 1 (+-1) of the match that
 *      made the edge, and successive captures are IR_HALF ticks apart (+-1, mod 2^16).
 * Its outcome depends on the timer and loop timing: expect.json is declared (ruling R13). */
#include "bonetto.h"
#include "stim.h"

#define WAIT_FLAG(f, id) WAIT_UNTIL(IRQ_PEND & (f), 1u << 22, id)

int main(void) {
  /* A */
  TIMER_CTRL = 0;
  ALTFN = ALT_TIMER; /* uo7 = the timer output, low */
  TIMER_CMP = IR_CMP_ABS;
  TIMER_CNT = 0;
  IRQ_PEND = IRQ_CMP | IRQ_CAP;
  TIMER_CTRL = TMR_P(IR_P) | TMR_EN | TMR_OUT_TOGGLE | TMR_CAP_RISE;
  WAIT_FLAG(IRQ_CAP, 0x10);
  uint32_t cap = TIMER_CAP;
  CHECK(IRQ_PEND & IRQ_CMP, 0x11);
  CHECK(cap >= IR_CMP_ABS && cap <= IR_CMP_ABS + 2, 0x12);
  CHECK(GPIO_IN & 1, 0x1c); /* a rising edge was captured: ui0 is high */
  /* A2: stop the counter and drive the output low (mode 00) with capture on falling edges:
   * capture works while the counter is disabled, and takes the frozen count. */
  IRQ_PEND = IRQ_CAP;
  TIMER_CTRL = TMR_P(IR_P) | TMR_OUT_LOW | TMR_CAP_FALL;
  WAIT_FLAG(IRQ_CAP, 0x1d);
  CHECK(TIMER_CAP == TIMER_CNT, 0x1e);
  CHECK(!(GPIO_IN & 1), 0x1f);
  TIMER_CTRL = 0;

  /* B */
  TIMER_CMP = IR_CMP;
  TIMER_CNT = 0;
  IRQ_PEND = IRQ_CMP | IRQ_CAP;
  CHECK(!(IRQ_PEND & IRQ_CAP), 0x13);
  TIMER_CTRL = TMR_P(IR_P) | TMR_EN | TMR_AUTO | TMR_OUT_TOGGLE | TMR_CAP_BOTH;
  uint32_t prev = 0;
  for (int k = 0; k < IR_CAPTURES; k++) {
    WAIT_FLAG(IRQ_CAP, 0x14);
    cap = TIMER_CAP;
    IRQ_PEND = IRQ_CAP | IRQ_CMP;
    CHECK(cap <= 2, 0x15);
    CHECK(TIMER_CNT <= IR_CMP, 0x16);
    if (k > 0) {
      uint32_t delta = (IR_CMP + 1) + cap - prev;
      CHECK(delta >= IR_CMP && delta <= IR_CMP + 2, 0x17);
    }
    prev = cap;
  }
  TIMER_CTRL = 0;
  for (volatile int i = 0; i < 4; i++) {
  }

  /* C */
  uint32_t cmp = IR_HALF;
  TIMER_CMP = cmp;
  TIMER_CNT = 0;
  IRQ_PEND = IRQ_CMP | IRQ_CAP;
  TIMER_CTRL = TMR_P(IR_P) | TMR_EN | TMR_OUT_TOGGLE | TMR_CAP_BOTH;
  for (int k = 0; k < IR_EDGES; k++) {
    WAIT_FLAG(IRQ_CMP, 0x18); /* the match at CNT = cmp toggled the output */
    IRQ_PEND = IRQ_CMP;
    uint32_t matched = cmp;
    cmp = (cmp + IR_HALF) & 0xFFFF;
    TIMER_CMP = cmp; /* the next edge, IR_HALF ticks later */
    WAIT_FLAG(IRQ_CAP, 0x19);
    cap = TIMER_CAP;
    IRQ_PEND = IRQ_CAP;
    CHECK(((cap - (matched + 1) + 1) & 0xFFFF) <= 2, 0x1a); /* CAP = match + 1, +-1 */
    if (k > 0) CHECK(((cap - prev - IR_HALF + 1) & 0xFFFF) <= 2, 0x1b);
    prev = cap;
  }
  TIMER_CTRL = 0;
  return 0;
}
