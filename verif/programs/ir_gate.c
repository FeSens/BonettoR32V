/* The IR gate, contract v3 sections 5.2-5.4: uo1 = PWM0 AND the timer output while
 * PWM_CTRL[14] is set, the way an IR remote modulates its 38 kHz carrier.
 *   1. Carrier on PWM0: PER = 657, Q = 1 (period 1,316 cycles: 37.99 kHz at 50 MHz), duty 219
 *      (high 438 cycles, 1/3), the IR gate on.
 *   2. Marks and spaces from the timer in toggle mode, free-running (P = 8: a tick every 256
 *      cycles), with the CPU moving CMP ahead at each match (the SDK's IR sender does the same):
 *      the first mark starts IRG_LEAD ticks after the start, marks last IRG_MARK ticks, spaces
 *      alternate IRG_SPACE0 / IRG_SPACE1 (NEC-like 1:1 and 1:3). ALTFN = PWM0 | timer, so uo7
 *      shows the timer output. The harness also loops uo7 back to ui0, and the program checks
 *      each edge's capture: CAP = the match + 1 (+-1), so every edge came at its CMP.
 *   3. GPIO_OUT = MARK_IR (0xA6) right after the start: the harness's IR gate meter (expect.json
 *      ir_gate) checks every cycle of the next IRG_MARKS marks: uo1 = carrier AND uo7, the carrier
 *      exact (period, high time) and phase-locked across marks, nothing in the spaces.
 *   4. After IRG_EDGES edges: the gate off (PWM_CTRL[14] = 0) with the timer still toggling, then
 *      GPIO_OUT = MARK_PWM (0xA5): the harness's PWM meter checks that uo1 is the plain carrier
 *      again (expect.json pwm, ch 0) while the program makes IRG_HOLD more edges.
 * Ends with GPIO_OUT = 0x5A. A shortest interval of 300 ticks is 76,800 cycles, 37 instructions
 * at the contract-4 cap of 2,048 cycles per instruction; one edge takes about 25 (the CMP write
 * comes a polling iteration, the flag clear and a load after the edge).
 * Declared (ruling R13). */
#include "bonetto.h"
#include "stim.h"

#define PWM_CFG (PWM_PER(IRG_PER) | PWM_Q(IRG_Q) | PWM_EN)
#define TMR_CFG (TMR_P(IRG_P) | TMR_EN | TMR_OUT_TOGGLE | TMR_CAP_BOTH)

static uint32_t cmp;

/* The interval after edge k: edge 0 starts a mark; odd edges start spaces. */
static uint32_t gap(uint32_t k) {
  if ((k & 1) == 0) return IRG_MARK;
  return (k & 2) ? IRG_SPACE1 : IRG_SPACE0;
}

/* Wait for edge k (the match at cmp toggles the output; its capture comes back on ui0 a few
 * cycles later), clear both flags at once (the next edge sets them again), read the capture, move
 * CMP to the next edge, then check. From the edge to the CMP write: a polling iteration, the
 * clear, a load and the store, about 15 instructions. */
static void edge(uint32_t k, uint32_t id) {
  volatile uint32_t* const io = (volatile uint32_t*)IO_BASE;
  const uint32_t matched = cmp;
  uint32_t next = (matched + gap(k)) & 0xFFFF;
  __asm__ volatile("" : "+r"(next)); /* computed before the edge, not after */
  uint32_t pend, n = 1u << 22;
  while (!((pend = io[SLOT_IRQ_PEND]) & IRQ_CAP))
    if (--n == 0) _fail(id);
  io[SLOT_IRQ_PEND] = IRQ_CMP | IRQ_CAP;
  const uint32_t cap = io[SLOT_TIMER_CAP];
  io[SLOT_TIMER_CMP] = next;
  cmp = next;
  CHECK(pend & IRQ_CMP, id + 1); /* the match that made the edge */
  CHECK(((cap - (matched + 1) + 1) & 0xFFFF) <= 2, id + 2);
}

int main(void) {
  PWM_DUTY(0) = IRG_DUTY;
  PWM_CTRL = PWM_CFG | PWM_IRGATE;
  CHECK(PWM_CTRL == (PWM_CFG | PWM_IRGATE), 0x10);
  TIMER_CTRL = 0;
  cmp = IRG_LEAD;
  TIMER_CMP = cmp;
  TIMER_CNT = 0;
  IRQ_PEND = IRQ_CMP | IRQ_CAP;
  ALTFN = ALT_PWM0 | ALT_TIMER;
  TIMER_CTRL = TMR_CFG; /* toggle mode starts at 0: a space */
  GPIO_OUT = MARK_IR;
  uint32_t k = 0;
  for (; k < IRG_EDGES; k++) edge(k, 0x20);
  /* the gate off: uo1 = PWM0, whatever the timer output */
  PWM_CTRL = PWM_CFG;
  GPIO_OUT = MARK_PWM;
  CHECK(PWM_CTRL == PWM_CFG, 0x11);
  for (; k < IRG_EDGES + IRG_HOLD; k++) edge(k, 0x30);
  TIMER_CTRL = 0;
  PWM_CTRL = 0;
  return 0;
}
