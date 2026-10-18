/* BonettoR32V SDK: IR remote sender. The LED pin is UO1: PWM0 makes the carrier (38 kHz for NEC,
 * 36 kHz for RC-5, duty 1/3) and the IR gate (PWM_CTRL[14]) ANDs it with the timer output, which
 * the core drives in toggle mode: high = mark (carrier on), low = space. The CPU only moves
 * TIMER_CMP at each mark/space edge, in the timer interrupt (r32v_core.c); a frame is a list of
 * mark/space durations in timer ticks (0.32 us at 50 MHz).
 *
 * Wiring: UO1 -> resistor -> NPN/MOSFET -> IR LED (940 nm) and its series resistor, from the
 * supply; never drive an IR LED straight from the pin.
 *
 * NEC (IRremote's sendNEC): 9 ms mark, 4.5 ms space, 32 bits LSB first (address, ~address,
 * command, ~command; a 16-bit address replaces the first two bytes), each bit a 562.5 us mark
 * then a 562.5 us (0) or 1,687.5 us (1) space, then a 562.5 us stop mark. Repeats: 9 ms mark,
 * 2.25 ms space, 562.5 us mark, one every 110 ms. RC-5: 14 Manchester bits of 1,778 us. */
#include "r32v_internal.h"

#define NEC_LEAD_MARK US10_TICKS(90000)
#define NEC_LEAD_SPACE US10_TICKS(45000)
#define NEC_REP_SPACE US10_TICKS(22500)
#define NEC_UNIT US10_TICKS(5625)
#define NEC_ONE_SPACE US10_TICKS(16875)
#define NEC_PERIOD_MS 110u
#define RC5_HALF US10_TICKS(8890)
_Static_assert(NEC_LEAD_MARK < 65536u, "NEC timings must fit the 16-bit timer");

static uint32_t carrier_khz;

/* PWM0 = the carrier (duty 1/3), the IR gate on, UO1 = the gated output. */
static void set_carrier(uint32_t khz) {
  if (khz == carrier_khz && r32v_ir_on) return;
  const uint32_t hz = khz * 1000u;
  uint32_t per, q;
  r32v_pwm_split(((uint32_t)F_CPU + hz / 2) / hz, &per, &q);
  const uint32_t m = r32v_irq_save();
  r32v_pwm_per = per;
  r32v_pwm_q = q;
  r32v_ir_on = 1;
  PWM_DUTY0 = (per + 1) / 3;
  r32v_pwm_apply();
  ALTFN = ALTFN | ALT_PWM0; /* the timer output is low between frames: the LED is off */
  r32v_irq_restore(m);
  carrier_khz = khz;
}

void ir_begin(void) { set_carrier(38); }

static void send_frame(uint32_t khz) {
  set_carrier(khz);
  r32v_ir_start();
  while (ir_busy()) {
  }
}

static uint32_t nec_put_bits(uint32_t n, uint32_t data) {
  for (uint32_t i = 0; i < 32; i++) {
    r32v_ir_buf[n++] = (uint16_t)NEC_UNIT;
    r32v_ir_buf[n++] = (uint16_t)((data & 1u) ? NEC_ONE_SPACE : NEC_UNIT);
    data >>= 1;
  }
  return n;
}

static uint32_t nec_t0; /* millis() at the last NEC frame's start, for the repeat spacing */

void ir_send_nec_raw(uint32_t data) {
  while (ir_busy()) {
  }
  uint32_t n = 0;
  r32v_ir_buf[n++] = (uint16_t)NEC_LEAD_MARK;
  r32v_ir_buf[n++] = (uint16_t)NEC_LEAD_SPACE;
  n = nec_put_bits(n, data);
  r32v_ir_buf[n++] = (uint16_t)NEC_UNIT; /* stop mark */
  r32v_ir_n = (uint8_t)n;
  nec_t0 = millis();
  send_frame(38);
}

void ir_send_nec_repeat(void) {
  while (ir_busy()) {
  }
  while ((uint32_t)(millis() - nec_t0) < NEC_PERIOD_MS) {
  }
  r32v_ir_buf[0] = (uint16_t)NEC_LEAD_MARK;
  r32v_ir_buf[1] = (uint16_t)NEC_REP_SPACE;
  r32v_ir_buf[2] = (uint16_t)NEC_UNIT;
  r32v_ir_n = 3;
  nec_t0 = millis();
  send_frame(38);
}

void ir_send_nec(uint16_t address, uint8_t command, uint8_t repeats) {
  uint32_t data;
  if (address > 0xFFu) /* extended NEC: a 16-bit address */
    data = (uint32_t)address;
  else
    data = (uint32_t)address | ((uint32_t)(~address & 0xFFu) << 8);
  data |= ((uint32_t)command << 16) | ((uint32_t)(~command & 0xFFu) << 24);
  ir_send_nec_raw(data);
  while (repeats--) ir_send_nec_repeat();
}

void ir_send_rc5(uint8_t address, uint8_t command, bool toggle) {
  /* S1 = 1, S2 = 1 (or the inverted command bit 6, RC-5X), T, A4..A0, C5..C0, MSB first;
   * Manchester: 1 = space then mark, 0 = mark then space. Leading and trailing spaces merge
   * with the idle line; equal neighbours merge into one 1,778 us mark or space. */
  const uint32_t bits = (1u << 13) | ((command & 0x40u) ? 0u : (1u << 12)) |
                        (toggle ? (1u << 11) : 0u) | ((uint32_t)(address & 0x1Fu) << 6) |
                        (command & 0x3Fu);
  while (ir_busy()) {
  }
  uint32_t n = 0, run = 0;
  int level = 0; /* the idle line: a space */
  for (int i = 13; i >= 0; i--) {
    const int b = (bits >> i) & 1;
    const int halves[2] = {b ? 0 : 1, b ? 1 : 0};
    for (int h = 0; h < 2; h++) {
      if (halves[h] == level) {
        run++;
      } else {
        if (n || level) r32v_ir_buf[n++] = (uint16_t)(run * RC5_HALF); /* not the lead space */
        level = halves[h];
        run = 1;
      }
    }
  }
  if (level) r32v_ir_buf[n++] = (uint16_t)(run * RC5_HALF); /* the last mark; a space merges */
  r32v_ir_n = (uint8_t)n;
  send_frame(36);
}

void ir_send_raw(const uint16_t *us, uint16_t n, uint8_t khz) {
  if (!n) return;
  while (ir_busy()) {
  }
  if (n > R32V_IR_MAX) n = R32V_IR_MAX;
  if (!(n & 1u)) n--; /* a trailing space merges with the idle line */
  for (uint16_t i = 0; i < n; i++) {
    uint32_t t = r32v_us_to_ticks(us[i] < 20000u ? us[i] : 20000u);
    if (t < 64u) t = 64u;
    r32v_ir_buf[i] = (uint16_t)t;
  }
  r32v_ir_n = (uint8_t)n;
  send_frame(khz ? khz : 38);
}
