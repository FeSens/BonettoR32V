/* pwm (harness --pwm 1:99:1:0,2:99:1:50,3:99:1:120,7:65535:0:300): PER = 99, Q = 1, duties
 * 0 / 50 / 120 on PWM0-2 (uo1-uo3), and the timer in PWM mode on uo7 (P = 0, CMP = 300, no
 * auto-reload: high 300 of every 65,536 cycles). uo0 (GPIO) high marks the measurement window. */
#include "bonetto3.h"

int main(void) {
  PWM_DUTY0 = 0;
  PWM_DUTY1 = 50;
  PWM_DUTY2 = 120;
  PWM_CTRL = 99u | (1u << 10) | (1u << 13);
  TIMER_CMP = 300;
  TIMER_CNT = 0;
  TIMER_CTRL = (2u << 6) | (1u << 4);
  ALTFN = 0x8E;
  spin(40);                 /* settle (more than one period of each) */
  GPIO_OUT = 1;             /* marker on */
  spin(100);                /* several timer periods */
  GPIO_OUT = 0;             /* marker off */
  ALTFN = 0;
  PWM_CTRL = 0;
  TIMER_CTRL = 0;
  return 0;
}
