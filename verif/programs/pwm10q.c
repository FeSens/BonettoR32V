/* PWM with a prescaler (Q > 0), a duty above PER and a duty equal to PER, PER decreasing
 * mid-run, contract v3 section 5.4; the timer's PWM mode with auto-reload at a high CMP, 5.3.
 *   1. The PWM runs with PER = 1023, Q = 1 (period 2,048 cycles), duties 100 / 900 / 0.
 *   2. Mid-run: PER = 1000, Q = 3 (period 1,001 x 8 = 8,008 cycles). The counter may be above
 *      the new PER when it changes. Duties 1023 (above PER: always 1), 1000 (= PER: high 8,000
 *      of 8,008 cycles) and 0x155 = 341 (bit 8: high 2,728 cycles) on channels 0..2. The old
 *      period is shorter than the new one (see pwm10.c).
 *   3. The timer in PWM mode with auto-reload: P = 1, CMP = 0xA000. CNT runs 0..CMP and
 *      reloads at the match, the output is CNT < CMP: period (CMP + 1) x 2 = 81,922 cycles, high
 *      CMP x 2 = 81,920 (expect.json ch 7 with per = duty = CMP).
 * Then GPIO_OUT = MARK_PWM (0xA5) and the harness measures the outputs; the program reads the
 * settings back (bytes and halfwords: PER and the duties through byte 1), holds for P10Q_HOLD
 * timer matches, stops both and ends with GPIO_OUT = 0x5A. Declared (ruling R13). */
#include "bonetto.h"
#include "stim.h"

#define PRE_CFG (PWM_PER(P10Q_PRE_PER) | PWM_Q(P10Q_PRE_Q) | PWM_EN)
#define PWM_CFG (PWM_PER(P10Q_PER) | PWM_Q(P10Q_Q) | PWM_EN)
#define TMR_CFG (TMR_P(P10Q_TMR_P) | TMR_EN | TMR_AUTO | TMR_OUT_PWM | TMR_CAP_OFF)
#define IO_REG16(slot) (*(volatile uint16_t *)(IO_BASE + 4 * (slot)))
#define IO_BYTE1(slot) (*(volatile uint8_t *)(IO_BASE + 4 * (slot) + 1))

int main(void) {
  PWM_DUTY(0) = 100;
  PWM_DUTY(1) = 900;
  PWM_DUTY(2) = 0;
  PWM_CTRL = PRE_CFG;
  TIMER_CMP = P10Q_TMR_CMP;
  TIMER_CNT = 0;
  TIMER_CTRL = TMR_CFG;
  ALTFN = ALT_PWM0 | ALT_PWM1 | ALT_PWM2 | ALT_TIMER;
  CHECK(PWM_CTRL == PRE_CFG, 0x10);
  /* the change, mid-run */
  PWM_CTRL = PWM_CFG;
  PWM_DUTY(0) = P10Q_DUTY_0;
  PWM_DUTY(1) = P10Q_DUTY_1;
  PWM_DUTY(2) = P10Q_DUTY_2;
  GPIO_OUT = MARK_PWM;
  IRQ_PEND = IRQ_CMP;
  CHECK(PWM_CTRL == PWM_CFG, 0x11);
  CHECK(IO_REG16(SLOT_PWM_CTRL) == PWM_CFG, 0x12);
  CHECK(IO_BYTE1(SLOT_PWM_CTRL) == (PWM_CFG >> 8), 0x13);
  CHECK(PWM_DUTY(0) == P10Q_DUTY_0, 0x14);
  CHECK(IO_BYTE1(SLOT_PWM_DUTY0 + 1) == (P10Q_DUTY_1 >> 8), 0x15);
  CHECK(IO_REG16(SLOT_PWM_DUTY0 + 2) == P10Q_DUTY_2, 0x16);
  CHECK(TIMER_CMP == P10Q_TMR_CMP, 0x17);
  CHECK(TIMER_CTRL == TMR_CFG, 0x18);
  for (int k = 0; k < P10Q_HOLD; k++) {
    WAIT_UNTIL(IRQ_PEND & IRQ_CMP, 1u << 22, 0x19); /* one match per auto-reload period */
    IRQ_PEND = IRQ_CMP;
    CHECK(TIMER_CNT <= P10Q_TMR_CMP, 0x1a);
  }
  TIMER_CTRL = 0;
  PWM_CTRL = 0;
  return 0;
}
