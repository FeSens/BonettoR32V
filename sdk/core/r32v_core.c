/* BonettoR32V SDK core: startup, the trap dispatcher, the timer (timebase, tone on UO7, the IR
 * sender's mark/space timing), digital and PWM pins, attachInterrupt.
 *
 * The timer. One 16-bit timer serves everything, in auto-reload mode with a fixed prescale
 * (TB_P: 2^4 = 16 cycles = 0.32 us per tick at 50 MHz). Its compare interrupt fires at the end
 * of every period and adds the period's length to the time (tb_acc, tb_us, tb_ms): the
 * timebase period is 20 ms (62,500 ticks at 50 MHz), so the interrupt costs one entry per
 * 20 ms. tone() on UO7 and the IR sender change the period (half a tone period, or each IR mark
 * and space) and the output mode (toggle), and the same interrupt goes on adding each period,
 * so the time stays exact through them. The counter is never written after startup: a mode
 * change ends the running period early by moving TIMER_CMP (timer_take), so no tick is lost.
 * A UO7 tone interrupts every half period; trap.S handles most of those in a short assembly
 * path (struct r32v_tf), and the C path here takes the rest (unit boundaries, the stop).
 *
 * Time arithmetic: the core has no multiplier (RV32E); conversions are multiplications by
 * constants, which GCC turns into shifts and adds. micros() = tb_us + floor-ish(ticks * 0.32)
 * with a 16-bit fixed-point factor rounded up: never more than 1 us ahead within a 20 ms unit
 * and never decreasing.
 */
#include "r32v_internal.h"

_Static_assert(F_CPU % (50ul << TB_P) == 0, "F_CPU must make 20 ms a whole number of timer ticks");
#define TAKE_LEAD 2048u  /* ticks from a timer_take() to the first edge (655 us at 50 MHz) */
#define TAKE_GUARD 1024u /* the running period must have this many ticks left to be cut short */
#define TB_MIN_PERIOD (4u * (TAKE_LEAD + TAKE_GUARD))
#define TICKS_PER_MS (TB_UNIT / TB_UNIT_MS)
/* tone() on UO7 takes one interrupt per half period: the highest pitch keeps the half period
 * (781 ticks, about 12,500 cycles at 50 MHz) well above what the C path of the interrupt takes: two
 * matches during one interrupt would merge into one and lose a half period of time */
#define TONE7_MAX_HZ 2000u

static inline uint32_t ticks_to_us(uint32_t t) { return (t * TB_US_Q16) >> 16; } /* t < 65,536 */
static inline uint32_t us20k_to_ms(uint32_t us) { return (us * 33555u) >> 25; }  /* us <= 20,000 */
uint32_t r32v_us_to_ticks(uint32_t us) { return (us * TICKS_PER_US_Q12) >> 12; } /* us < 335,544 */

/* ---- timer state ------------------------------------------------------------------------------- */
enum { OWN_TB, OWN_TONE, OWN_IR };
/* Shared with the trap's fast path (trap.S, which uses these offsets). While `on`, a compare
 * interrupt that ends neither a 20 ms unit nor a timed tone is handled there: acc += cur, the
 * tone's countdown and output level, and the flag cleared (about 30 instructions). */
struct r32v_tf {
  volatile uint32_t on;   /*  0: a UO7 tone plays at a steady period (else every one takes C) */
  volatile uint32_t acc;  /*  4: ticks from the last unit boundary to the running period's start */
  volatile uint32_t cur;  /*  8: ticks in the running period (TIMER_CMP + 1) */
  volatile uint32_t left; /* 12: timed UO7 tone: matches to go before it may stop (0: untimed) */
  volatile uint32_t hi;   /* 16: the UO7 tone output after the last match */
  uint32_t unit;          /* 20: TB_UNIT */
};
struct r32v_tf r32v_tf = {0, 0, TB_UNIT, 0, 0, TB_UNIT};
_Static_assert(__builtin_offsetof(struct r32v_tf, unit) == 20, "trap.S uses these offsets");
#define tb_acc r32v_tf.acc
#define tb_cur r32v_tf.cur
static volatile uint32_t tb_us, tb_ms; /* the time at the last 20 ms unit boundary */
static volatile uint32_t tb_period = TB_UNIT; /* the timebase period (INT_TIMER) */
static volatile uint8_t tmr_owner;
static volatile uint32_t tmr_cap;      /* TIMER_CTRL [9:8]: the capture edge */
static void (*volatile timer_cb)(void);
static void (*volatile cap_cb)(void);
static volatile uint32_t cap_ticks, cap_us;

/* tone */
static volatile uint32_t tone_half;    /* UO7: ticks per half period */
static volatile uint8_t pwm_tone_pin;  /* UO1..UO3 while a PWM tone plays, else 0 */
static volatile uint32_t pwm_tone_end;
static volatile uint8_t pwm_tone_timed;

/* PWM (the shared period) */
volatile uint32_t r32v_pwm_per = 1022, r32v_pwm_q = 0; /* pwmMax() = PER + 1 = 1023 */
volatile uint8_t r32v_ir_on;

/* IR frame: mark/space durations in ticks (0 means 65,536); ir.c fills them */
uint16_t r32v_ir_buf[R32V_IR_MAX];
volatile uint8_t r32v_ir_n;
static volatile uint8_t ir_i;
static volatile uint8_t ir_active;

static inline uint32_t ctrl(uint32_t mode) {
  return TMR_P(TB_P) | TMR_EN | TMR_AUTO | mode | tmr_cap;
}

/* ms at a period boundary, from the timebase state (in the interrupt) */
static inline uint32_t now_ms_at(uint32_t acc, uint32_t ms) {
  return ms + us20k_to_ms(ticks_to_us(acc));
}

static void tone7_stop(void); /* interrupts off */

/* The compare interrupt, at the end of every timer period (when trap.S's fast path doesn't take
 * it): set the next period (for IR first, since its mark or space has already started and must
 * end at the new TIMER_CMP), then add the period that ended to the time, then the owner's work. */
static inline void timer_isr(void) {
  IRQ_PEND = IRQ_CMP;
  const uint32_t ended = tb_cur;
  const uint8_t own = tmr_owner;
  uint32_t next = tb_period;
  if (own == OWN_IR) {
    const uint8_t i = ir_i;
    if (i < r32v_ir_n) {
      next = r32v_ir_buf[i];
      if (!next) next = 65536;
      ir_i = i + 1;
    } else { /* the last mark ended (the output just went low): back to the timebase */
      TIMER_CTRL = ctrl(TMR_OUT_LOW);
      tmr_owner = OWN_TB;
      ir_active = 0;
    }
    TIMER_CMP = next - 1;
    tb_cur = next;
  }
  uint32_t a = tb_acc + ended, us = tb_us, ms = tb_ms;
  while (a >= TB_UNIT) {
    a -= TB_UNIT;
    us += TB_UNIT_US;
    ms += TB_UNIT_MS;
  }
  tb_acc = a;
  tb_us = us;
  tb_ms = ms;
  if (pwm_tone_timed && (int32_t)(now_ms_at(a, ms) - pwm_tone_end) >= 0) noTone(pwm_tone_pin);
  if (own == OWN_TONE) {
    next = tone_half;
    const uint32_t hi = r32v_tf.hi ^ 1u, left = r32v_tf.left;
    r32v_tf.hi = hi;
    /* a timed tone stops at the first falling edge once its count is done: its last pulse is
     * whole */
    if (left > 1u) {
      r32v_tf.left = left - 1u;
    } else if (left == 1u && !hi) {
      tone7_stop();
      next = tb_period;
    }
    TIMER_CMP = next - 1;
    tb_cur = next;
    r32v_tf.on = tmr_owner == OWN_TONE; /* the period is steady now */
  } else if (own == OWN_TB) {
    TIMER_CMP = next - 1;
    tb_cur = next;
    void (*cb)(void) = timer_cb;
    if (cb) cb();
  }
}

static inline void capture_isr(void) {
  IRQ_PEND = IRQ_CAP;
  const uint32_t cap = TIMER_CAP, now = TIMER_CNT;
  /* the capture came in the running period, or (cap > now) in the one before */
  const uint32_t ago = cap <= now ? now - cap : now + tb_cur - cap;
  cap_ticks = cap;
  cap_us = micros() - ticks_to_us(ago < 65536u ? ago : 65535u);
  void (*cb)(void) = cap_cb;
  if (cb) cb();
}

/* ---- GPIO edge interrupts ---------------------------------------------------------------------- */
static void (*volatile gpio_cb[8])(void);
static volatile uint8_t gpio_change; /* pins in CHANGE mode: the polarity follows the level */

static inline void gpio_isr(void) {
  const uint32_t p = GPIO_EPEND & 0xFFu;
  GPIO_EPEND = p;
  if (p & gpio_change) {
    /* CHANGE: wait for the edge away from the level the pin has now */
    const uint32_t lvl = GPIO_IN & p & gpio_change;
    const uint32_t e = GPIO_EDGE;
    GPIO_EDGE = (e & ~((uint32_t)(p & gpio_change) << 8)) | (lvl << 8);
  }
  for (uint32_t i = 0; i < 8; i++)
    if (p & (1u << i)) {
      void (*cb)(void) = gpio_cb[i];
      if (cb) cb();
    }
}

/* ---- the trap ---------------------------------------------------------------------------------- */
void r32v_serial_isr(void) __attribute__((weak));

void __attribute__((weak)) r32v_fault(uint32_t mcause, uint32_t mepc) {
  (void)mcause;
  (void)mepc;
  noInterrupts();
  for (;;) {
  }
}

/* Every trap enters at r32v_trap (trap.S); what its short paths don't finish comes here. */
void __attribute__((interrupt("machine"))) r32v_trap_c(void) {
  const uint32_t cause = csr_read(mcause);
  if (!(cause & 0x80000000u)) {
    r32v_fault(cause, csr_read(mepc));
    return;
  }
  if (r32v_rx.cbdue) { /* trap.S buffered a byte: the INT_SERIAL callback */
    r32v_rx.cbdue = 0;
    void (*cb)(void) = r32v_rx.cb;
    if (cb) cb();
  }
  const uint32_t pend = IRQ_PEND & IRQ_EN;
  if (pend & IRQ_CAP) capture_isr();
  if (pend & IRQ_CMP) timer_isr();
  if ((pend & IRQ_RX) && r32v_serial_isr) r32v_serial_isr();
  if (pend & IRQ_GPIO) gpio_isr();
}

/* ---- startup ----------------------------------------------------------------------------------- */
void r32v_init(void) {
  IRQ_EN = 0;
  csr_write(mie, 0);
  GPIO_OUT = 0;
  ALTFN = 0;
  GPIO_EDGE = 0;
  GPIO_EPEND = 0xFF;
  PWM_CTRL = PWM_PER(r32v_pwm_per) | PWM_Q(r32v_pwm_q);
  PWM_DUTY0 = 0;
  PWM_DUTY1 = 0;
  PWM_DUTY2 = 0;
  SPI_CTRL = SPI_CS_N | SPI_D(1);
  /* the timebase: counter from 0, a 20 ms auto-reload period */
  TIMER_CTRL = 0;
  TIMER_CNT = 0;
  tb_cur = tb_period = TB_UNIT;
  TIMER_CMP = TB_UNIT - 1;
  IRQ_PEND = IRQ_CMP | IRQ_CAP;
  TIMER_CTRL = ctrl(TMR_OUT_LOW);
  IRQ_EN = IRQ_CMP;
  csr_write(mie, MIE_MEIE);
  interrupts();
}

void __attribute__((weak)) yield(void) {}

/* ---- time -------------------------------------------------------------------------------------- */
/* The ticks since the last unit boundary (x) and the time at it, consistently: a match that the
 * interrupt hasn't handled yet (interrupts off, or it is about to run) counts as handled. */
static inline uint32_t tb_read(uint32_t *base, int ms) {
  const uint32_t m = r32v_irq_save();
  uint32_t c = TIMER_CNT, x = tb_acc;
  uint32_t b = ms ? tb_ms : tb_us;
  if (IRQ_PEND & IRQ_CMP) {
    c = TIMER_CNT;
    x += tb_cur;
  }
  r32v_irq_restore(m);
  x += c;
  while (x >= TB_UNIT) {
    x -= TB_UNIT;
    b += ms ? TB_UNIT_MS : TB_UNIT_US;
  }
  *base = b;
  return x;
}

uint32_t micros(void) {
  uint32_t b;
  const uint32_t x = tb_read(&b, 0);
  return b + ticks_to_us(x);
}

uint32_t millis(void) {
  uint32_t b;
  const uint32_t x = tb_read(&b, 1);
  return b + us20k_to_ms(ticks_to_us(x));
}

/* counts whole milliseconds of micros() from the call, like Arduino's: exact to a micros() call */
void delay(uint32_t ms) {
  uint32_t start = micros();
  while (ms) {
    yield();
    while (ms && (uint32_t)(micros() - start) >= 1000u) {
      ms--;
      start += 1000u;
    }
  }
}

void delayMicroseconds(uint32_t us) {
  while (us >= 300000u) { /* r32v_us_to_ticks() covers up to 335 ms */
    delayMicroseconds(250000u);
    us -= 250000u;
  }
  const uint32_t ticks = r32v_us_to_ticks(us);
  uint32_t prev = TIMER_CNT, elapsed = 0;
  while (elapsed < ticks) {
    const uint32_t c = TIMER_CNT;
    elapsed += c >= prev ? c - prev : c + tb_cur - prev; /* one reload per iteration at most */
    prev = c;
  }
}

void timerPeriodMicros(uint32_t us) {
  uint32_t t = us >= 300000u ? 65536u : r32v_us_to_ticks(us);
  if (t > 65536u) t = 65536u;
  if (t < TB_MIN_PERIOD) t = TB_MIN_PERIOD;
  tb_period = t; /* from the next timebase period on */
}

/* Hand the timer to `owner` (toggle output, starting low): cut the running period short so the
 * owner's first period ends TAKE_LEAD ticks from now, or wait for the boundary if the running
 * period ends sooner than that anyway. Never writes TIMER_CNT, so the timebase stays exact. */
static void timer_take(uint8_t owner) {
  for (;;) {
    const uint32_t m = r32v_irq_save();
    if (IRQ_PEND & IRQ_CMP) timer_isr(); /* a boundary is due: take it first */
    const uint32_t c = TIMER_CNT;
    if (c + TAKE_LEAD + TAKE_GUARD < tb_cur) {
      TIMER_CMP = c + TAKE_LEAD;
      tb_cur = c + TAKE_LEAD + 1;
      tmr_owner = owner;
      r32v_tf.on = 0; /* this period isn't steady: its end takes the C path */
      r32v_tf.hi = 0;
      TIMER_CTRL = ctrl(TMR_OUT_TOGGLE); /* the toggle output restarts at 0 */
      r32v_irq_restore(m);
      return;
    }
    r32v_irq_restore(m);
  }
}

/* ---- digital pins ------------------------------------------------------------------------------ */
volatile uint8_t r32v_spi_on; /* spi.c: spi_begin() gave uo4-uo6 to the SPI */

void pinMode(uint8_t pin, uint8_t mode) {
  if (pin <= UO7 && mode == OUTPUT) {
    const uint32_t m = r32v_irq_save();
    ALTFN = ALTFN & ~(1u << pin);
    r32v_irq_restore(m);
  }
}

void digitalWrite(uint8_t pin, uint8_t val) {
  if (pin > UO7) return;
  const uint32_t bit = 1u << pin;
  const uint32_t m = r32v_irq_save();
  if (pin == SS && r32v_spi_on) { /* SPI owns uo4: drive its CS_n level */
    SPI_CTRL = (SPI_CTRL & 0xEu) | (val ? SPI_CS_N : 0u);
  } else {
    GPIO_OUT = val ? (GPIO_OUT | bit) : (GPIO_OUT & ~bit);
    ALTFN = ALTFN & ~bit;
  }
  r32v_irq_restore(m);
}

int digitalRead(uint8_t pin) {
  if (pin >= UI0 && pin <= UI7) return (int)((GPIO_IN >> (pin - UI0)) & 1u);
  if (pin <= UO7) return (int)((GPIO_OUT >> pin) & 1u);
  return LOW;
}

/* ---- PWM --------------------------------------------------------------------------------------- */
uint32_t pwmMax(void) { return r32v_pwm_per + 1; }

void r32v_pwm_apply(void) {
  PWM_CTRL = PWM_PER(r32v_pwm_per) | PWM_Q(r32v_pwm_q) | PWM_EN | (r32v_ir_on ? PWM_IRGATE : 0u);
}

void analogWrite(uint8_t pin, uint32_t duty) {
  if (pin < UO1 || pin > UO3) {
    digitalWrite(pin, duty * 2 >= pwmMax() ? HIGH : LOW);
    return;
  }
  if (pin == UO1 && r32v_ir_on) return; /* UO1 is the IR LED */
  if (pin == pwm_tone_pin) return;      /* a tone plays there */
  if (duty > 1023u) duty = 1023u;
  const uint32_t m = r32v_irq_save();
  PWM_DUTY(pin - UO1) = duty;           /* duty > PER: always on */
  r32v_pwm_apply();
  ALTFN = ALTFN | (1u << pin);
  r32v_irq_restore(m);
}

/* PER and Q for a period of `cycles` clock cycles: the smallest Q that fits the period in 1,023
 * steps (PER <= 1022, so a 10-bit duty of 1023 is always above PER: fully on) */
void r32v_pwm_split(uint32_t cycles, uint32_t *per, uint32_t *q) {
  uint32_t qq = 0;
  while (qq < 7 && (cycles >> qq) > 1023u) qq++;
  uint32_t steps = cycles >> qq;
  if (steps > 1023u) steps = 1023u;
  if (steps < 2u) steps = 2u;
  *per = steps - 1;
  *q = qq;
}

uint32_t analogWriteFrequency(uint32_t hz) {
  if (!hz || r32v_ir_on) return pwmMax(); /* the IR carrier owns the PWM period */
  uint32_t per, q;
  r32v_pwm_split((uint32_t)F_CPU / hz, &per, &q);
  const uint32_t m = r32v_irq_save();
  r32v_pwm_per = per;
  r32v_pwm_q = q;
  if (PWM_CTRL & PWM_EN) r32v_pwm_apply();
  r32v_irq_restore(m);
  return per + 1;
}

/* ---- tone -------------------------------------------------------------------------------------- */
static volatile uint8_t tone_hid_ir; /* tone on UO7 hid the IR LED pin (its gate would follow) */

static void tone7_stop(void) {
  TIMER_CTRL = ctrl(TMR_OUT_LOW);
  tmr_owner = OWN_TB; /* the running half period ends, then timebase periods */
  r32v_tf.on = 0;
  r32v_tf.left = 0;
  ALTFN = ALTFN & ~ALT_TIMER;
  GPIO_OUT = GPIO_OUT & ~(1u << UO7);
  if (tone_hid_ir) {
    ALTFN = ALTFN | ALT_PWM0;
    tone_hid_ir = 0;
  }
}

void tone(uint8_t pin, uint32_t hz, uint32_t ms) {
  if (!hz) return;
  if (pin == UO7) {
    while (ir_active) {
    }
    if (hz > TONE7_MAX_HZ) hz = TONE7_MAX_HZ;
    uint32_t half = ((uint32_t)F_CPU >> (TB_P + 1)) / hz;
    if (half > 65536u) half = 65536u;
    /* a timed tone: the match that starts it, then one per half period (the interrupt stops it
     * at the first falling edge once the count is done, so it plays ms within a half period) */
    uint32_t left = 0;
    if (ms) {
      if (ms > 1000000u) ms = 1000000u;
      left = (ms * TICKS_PER_MS + half / 2u) / half + 1u;
    }
    uint32_t m = r32v_irq_save();
    tone_half = half;
    r32v_tf.left = left;
    r32v_tf.on = 0; /* the next match takes the C path, which applies the new half period */
    if (tmr_owner == OWN_TONE) { /* a new frequency from the next half period */
      r32v_irq_restore(m);
      return;
    }
    if (r32v_ir_on && (ALTFN & ALT_PWM0)) {
      ALTFN = ALTFN & ~ALT_PWM0;
      GPIO_OUT = GPIO_OUT & ~(1u << UO1);
      tone_hid_ir = 1;
    }
    ALTFN = ALTFN | ALT_TIMER;
    r32v_irq_restore(m);
    timer_take(OWN_TONE);
  } else if (pin >= UO1 && pin <= UO3) {
    if (r32v_ir_on) return; /* the IR carrier owns the PWM period */
    uint32_t per, q;
    r32v_pwm_split((uint32_t)F_CPU / hz, &per, &q);
    const uint32_t m = r32v_irq_save();
    pwm_tone_pin = pin;
    pwm_tone_timed = 0;
    PWM_DUTY(pin - UO1) = (per + 1) / 2;
    PWM_CTRL = PWM_PER(per) | PWM_Q(q) | PWM_EN;
    ALTFN = ALTFN | (1u << pin);
    r32v_irq_restore(m);
    if (ms) {
      pwm_tone_end = millis() + ms;
      pwm_tone_timed = 1;
    }
  }
}

void noTone(uint8_t pin) {
  const uint32_t m = r32v_irq_save();
  if (pin == UO7) {
    if (tmr_owner == OWN_TONE) tone7_stop();
  } else if (pin >= UO1 && pin <= UO3 && pin == pwm_tone_pin) {
    pwm_tone_pin = 0;
    pwm_tone_timed = 0;
    PWM_DUTY(pin - UO1) = 0;
    ALTFN = ALTFN & ~(1u << pin);
    GPIO_OUT = GPIO_OUT & ~(1u << pin);
    r32v_pwm_apply(); /* the PWM period of analogWrite() again */
  }
  r32v_irq_restore(m);
}

/* ---- the IR frame (ir.c fills r32v_ir_buf and calls this) -------------------------------------- */
void r32v_ir_start(void) {
  while (ir_active) {
  }
  noTone(UO7);
  ir_i = 0;
  ir_active = 1;
  timer_take(OWN_IR);
}

bool ir_busy(void) { return ir_active != 0; }

/* ---- interrupts -------------------------------------------------------------------------------- */
struct r32v_rx r32v_rx; /* here, not in serial.c: trap.S uses it in every image */
_Static_assert(__builtin_offsetof(struct r32v_rx, buf) == 12, "trap.S uses these offsets");

void attachInterrupt(int irq, void (*fn)(void), int mode) {
  const uint32_t m = r32v_irq_save();
  if (irq >= 0 && irq <= 7) {
    const uint32_t bit = 1u << irq;
    gpio_cb[irq] = fn;
    uint32_t pol;
    if (mode == CHANGE) {
      gpio_change |= bit;
      pol = GPIO_IN & bit; /* high now: wait for the falling edge */
    } else {
      gpio_change &= ~bit;
      pol = mode == FALLING ? bit : 0u;
    }
    const uint32_t e = GPIO_EDGE;
    GPIO_EDGE = (e & ~(bit << 8)) | (pol << 8) | bit;
    GPIO_EPEND = bit;
    IRQ_EN = IRQ_EN | IRQ_GPIO;
  } else if (irq == INT_TIMER) {
    timer_cb = fn;
  } else if (irq == INT_CAPTURE) {
    cap_cb = fn;
    tmr_cap = ((uint32_t)mode & 3u) << 8;
    TIMER_CTRL_B1 = (uint8_t)(tmr_cap >> 8); /* byte 1 only: the toggle output stays */
    IRQ_PEND = IRQ_CAP;
    IRQ_EN = IRQ_EN | IRQ_CAP;
  } else if (irq == INT_SERIAL) {
    r32v_rx.cb = fn;
  }
  r32v_irq_restore(m);
}

void detachInterrupt(int irq) {
  const uint32_t m = r32v_irq_save();
  if (irq >= 0 && irq <= 7) {
    const uint32_t bit = 1u << irq;
    GPIO_EDGE = GPIO_EDGE & ~bit;
    GPIO_EPEND = bit;
    gpio_cb[irq] = 0;
    gpio_change &= ~bit;
    if (!(GPIO_EDGE & 0xFFu)) IRQ_EN = IRQ_EN & ~IRQ_GPIO;
  } else if (irq == INT_TIMER) {
    timer_cb = 0;
  } else if (irq == INT_CAPTURE) {
    IRQ_EN = IRQ_EN & ~IRQ_CAP;
    tmr_cap = 0;
    TIMER_CTRL_B1 = 0;
    cap_cb = 0;
  } else if (irq == INT_SERIAL) {
    r32v_rx.cb = 0;
  }
  r32v_irq_restore(m);
}

uint32_t captureMicros(void) { return cap_us; }
uint32_t captureTicks(void) { return cap_ticks; }
