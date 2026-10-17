/* PWM and timer PWM outputs, contract v3 sections 5.3 and 5.4.
 *   - PWM: PER = 99, Q = 1 (period (99 + 1) * 2 = 200 cycles), duties 0 / 50 / 120 on channels
 *     0..2 (uo1..uo3): always 0, high 100 of 200 cycles, always 1 (duty above PER).
 *   - Timer in PWM mode on uo7: P = 0, CMP = 300, no auto-reload: high while CNT < 300, i.e. 300
 *     of every 65,536 cycles.
 * The program sets everything up, hands the pins to their functions (ALTFN), reads the settings
 * back, then writes GPIO_OUT = MARK_PWM (0xA5): the harness measures the four outputs after
 * that (expect.json pwm). It keeps running for PWM_HOLD more timer compare matches (one per
 * 65,536 cycles, polled through IRQ_PEND.CMP), then stops both and ends with GPIO_OUT = 0x5A.
 * The pins in GPIO mode show 0xA5 & ~ALTFN = 0x21 at the mark.
 * The checks are the harness's (pin timing): expect.json is declared (ruling R13). */
#include "bonetto.h"
#include "stim.h"

#define PWM_CFG (PWM_PER(PWM_PERIOD) | PWM_Q(PWM_PRESCALE))
#define TMR_CFG (TMR_P(PWM_TMR_P) | TMR_EN | TMR_OUT_PWM | TMR_CAP_OFF)

int main(void) {
  PWM_CTRL = PWM_CFG;
  PWM_DUTY(0) = PWM_DUTY_0;
  PWM_DUTY(1) = PWM_DUTY_1;
  PWM_DUTY(2) = PWM_DUTY_2;
  PWM_CTRL = PWM_CFG | PWM_EN;
  TIMER_CMP = PWM_TMR_CMP;
  TIMER_CNT = 0;
  TIMER_CTRL = TMR_CFG;
  ALTFN = ALT_PWM0 | ALT_PWM1 | ALT_PWM2 | ALT_TIMER;
  CHECK(PWM_CTRL == (PWM_CFG | PWM_EN), 0x10);
  CHECK(PWM_DUTY(0) == PWM_DUTY_0, 0x11);
  CHECK(PWM_DUTY(1) == PWM_DUTY_1, 0x12);
  CHECK(PWM_DUTY(2) == PWM_DUTY_2, 0x13);
  CHECK(TIMER_CMP == PWM_TMR_CMP, 0x14);
  CHECK(TIMER_CTRL == TMR_CFG, 0x15);
  CHECK(ALTFN == (ALT_PWM0 | ALT_PWM1 | ALT_PWM2 | ALT_TIMER), 0x16);
  GPIO_OUT = MARK_PWM;
  IRQ_PEND = IRQ_CMP;
  for (int k = 0; k < PWM_HOLD; k++) {
    WAIT_UNTIL(IRQ_PEND & IRQ_CMP, 1u << 22, 0x17); /* CNT passed CMP */
    IRQ_PEND = IRQ_CMP;
  }
  TIMER_CTRL = 0;
  PWM_CTRL = 0;
  return 0;
}
