/* Timer compare interrupt, contract v3 sections 4, 5.3 and 5.8.
 *
 *   1. The timer matches every IT_CMP + 1 = 500 cycles (P = 0, auto-reload) and interrupts
 *      through IRQ_PEND.CMP; the handler checks mcause and the source, clears CMP (W1C) and
 *      counts. At IT_COUNT (20) entries it disables the source, so main runs again even on a core
 *      so slow that a new match is always pending when the handler returns.
 *   2. TIMER_CAP is unused: capture is off, so CAP is still 0 and IRQ_PEND.CAP clear.
 *   3. mstatus.MIE masks: with MIE = 0 a pending match (mip.MEIP = 1) is not taken; once MIE = 1
 *      it is taken before more than one further instruction retires (contract 4).
 *   4. mie.MEIE masks the same way.
 * Ends with GPIO_OUT = 0x5A (crt0), or 0xEE after the failing check's id.
 * Its outcome depends on interrupt timing: expect.json is declared (ruling R13). */
#include "bonetto.h"
#include "stim.h"

static volatile uint32_t count, limit;

void __attribute__((interrupt("machine"), aligned(4))) irq_handler(void) {
  if (CSR_READ(mcause) != MCAUSE_EXT_IRQ) _fail(0x30);
  if (!(IRQ_PEND & IRQ_CMP)) _fail(0x31); /* the only enabled source */
  IRQ_PEND = IRQ_CMP;                      /* W1C */
  if (++count >= limit) IRQ_EN = 0;
}

int main(void) {
  CSR_WRITE(mtvec, (uint32_t)irq_handler);
  TIMER_CMP = IT_CMP;
  TIMER_CNT = 0;
  IRQ_PEND = IRQ_CMP | IRQ_CAP;
  limit = IT_COUNT;
  IRQ_EN = IRQ_CMP;
  CSR_SET(mie, MIE_MEIE);
  TIMER_CTRL = TMR_P(0) | TMR_EN | TMR_AUTO | TMR_OUT_LOW | TMR_CAP_OFF;
  IRQ_ON();
  WAIT_UNTIL(count >= IT_COUNT, 1u << 22, 1);
  IRQ_OFF();
  CHECK(count == IT_COUNT, 2);

  /* TIMER_CAP unused */
  CHECK(TIMER_CAP == 0, 3);
  CHECK(!(IRQ_PEND & IRQ_CAP), 4);

  /* mstatus.MIE masks */
  limit = IT_COUNT + 1;
  IRQ_PEND = IRQ_CMP;
  IRQ_EN = IRQ_CMP;
  WAIT_UNTIL(CSR_READ(mip) & MIP_MEIP, 1u << 22, 5); /* the next match */
  for (volatile int i = 0; i < 4; i++) {
  }
  CHECK(count == IT_COUNT, 6);
  IRQ_ON();
  NOP(); /* at most one more instruction retires before the interrupt */
  CHECK(count == IT_COUNT + 1, 7);

  /* mie.MEIE masks */
  IRQ_OFF();
  limit = IT_COUNT + 2;
  CSR_CLEAR(mie, MIE_MEIE);
  IRQ_PEND = IRQ_CMP;
  IRQ_EN = IRQ_CMP;
  IRQ_ON();
  WAIT_UNTIL(CSR_READ(mip) & MIP_MEIP, 1u << 22, 8);
  for (volatile int i = 0; i < 4; i++) {
  }
  CHECK(count == IT_COUNT + 1, 9);
  CSR_SET(mie, MIE_MEIE);
  NOP();
  CHECK(count == IT_COUNT + 2, 10);

  IRQ_OFF();
  TIMER_CTRL = 0;
  IRQ_EN = 0;
  IRQ_PEND = IRQ_CMP;
  CSR_WRITE(mie, 0);
  return 0;
}
