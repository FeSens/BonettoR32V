/* PWM over the full 10-bit range, and a mid-run settings change, contract v3 section 5.4; the
 * timer's PWM mode with CMP bit 15 set, section 5.3.
 *   1. The PWM runs with PER = P10_PRE_PER (99), Q = 0, duty P10_PRE_DUTY on all three channels,
 *      the pins already handed to their functions (ALTFN).
 *   2. Mid-run, without stopping it: PER = 1023 (bits 8-9 set), Q = 0 (period 1,024 cycles) and
 *      duties 1, 512 (bit 9) and 1023 (= PER: high 1,023 of 1,024 cycles) on channels 0..2
 *      (uo1..uo3). The old period (100 cycles) is shorter than the new one, so "within one
 *      period" (5.4) holds for either reading before the harness measures (one settling period).
 *   3. The timer in PWM mode on uo7: P = 0, CMP = 0xC000, no auto-reload: high while
 *      CNT < 0xC000, i.e. 49,152 of every 65,536 cycles.
 * Right after the change it writes GPIO_OUT = MARK_PWM (0xA5): the harness measures the four
 * outputs exactly after that (expect.json pwm). It reads the settings back, keeps running for
 * P10_HOLD timer compare matches (one per 65,536 cycles), then stops both and ends with
 * GPIO_OUT = 0x5A. Declared (ruling R13): the checks are the harness's pin timing. */
#include "bonetto.h"
#include "stim.h"

#define PRE_CFG (PWM_PER(P10_PRE_PER) | PWM_Q(0) | PWM_EN)
#define PWM_CFG (PWM_PER(P10_PER) | PWM_Q(P10_Q) | PWM_EN)
#define TMR_CFG (TMR_P(P10_TMR_P) | TMR_EN | TMR_OUT_PWM | TMR_CAP_OFF)

int main(void) {
  for (int i = 0; i < 3; i++) PWM_DUTY(i) = P10_PRE_DUTY;
  PWM_CTRL = PRE_CFG;
  TIMER_CMP = P10_TMR_CMP;
  TIMER_CNT = 0;
  TIMER_CTRL = TMR_CFG;
  ALTFN = ALT_PWM0 | ALT_PWM1 | ALT_PWM2 | ALT_TIMER;
  CHECK(PWM_CTRL == PRE_CFG, 0x10);
  /* the change, mid-run */
  PWM_CTRL = PWM_CFG;
  PWM_DUTY(0) = P10_DUTY_0;
  PWM_DUTY(1) = P10_DUTY_1;
  PWM_DUTY(2) = P10_DUTY_2;
  GPIO_OUT = MARK_PWM;
  IRQ_PEND = IRQ_CMP;
  CHECK(PWM_CTRL == PWM_CFG, 0x11);
  CHECK(PWM_DUTY(0) == P10_DUTY_0, 0x12);
  CHECK(PWM_DUTY(1) == P10_DUTY_1, 0x13);
  CHECK(PWM_DUTY(2) == P10_DUTY_2, 0x14);
  CHECK(TIMER_CMP == P10_TMR_CMP, 0x15);
  CHECK(TIMER_CTRL == TMR_CFG, 0x16);
  for (int k = 0; k < P10_HOLD; k++) {
    WAIT_UNTIL(IRQ_PEND & IRQ_CMP, 1u << 22, 0x18); /* CNT passed CMP */
    IRQ_PEND = IRQ_CMP;
  }
  TIMER_CTRL = 0;
  PWM_CTRL = 0;
  CHECK(PWM_CTRL == 0, 0x19);
  return 0;
}
