/* BonettoR32V SDK internals shared by the core files (not part of the sketch API). */
#ifndef R32V_INTERNAL_H
#define R32V_INTERNAL_H

#include "Arduino.h"

/* ---- the timebase: auto-reload every 20 ms, a tick every 2^TB_P cycles ------------------------- */
#define TB_UNIT_MS 20u
#define TB_UNIT_US 20000u
/* TB_P: the smallest prescale with a 20 ms unit of at most 65,536 ticks (4 at 50 MHz) */
#if (F_CPU / 50) <= 65536
#define TB_P 0
#elif (F_CPU / 100) <= 65536
#define TB_P 1
#elif (F_CPU / 200) <= 65536
#define TB_P 2
#elif (F_CPU / 400) <= 65536
#define TB_P 3
#elif (F_CPU / 800) <= 65536
#define TB_P 4
#elif (F_CPU / 1600) <= 65536
#define TB_P 5
#elif (F_CPU / 3200) <= 65536
#define TB_P 6
#elif (F_CPU / 6400) <= 65536
#define TB_P 7
#else
#error "F_CPU is too high for the timebase"
#endif
#define TB_UNIT ((uint32_t)((F_CPU / 50) >> TB_P)) /* ticks per 20 ms (62,500 at 50 MHz) */
#define TB_US_Q16 ((uint32_t)((((uint64_t)TB_UNIT_US << 16) / TB_UNIT) + 1))
#define TICKS_PER_US_Q12 ((uint32_t)((((uint64_t)F_CPU << 12) / 1000000u) >> TB_P))
/* A duration given in tenths of a microsecond, in timer ticks (rounded), at compile time */
#define US10_TICKS(x)                                                                    \
  ((uint32_t)(((((uint64_t)(x) * (uint64_t)F_CPU) / 10000000u) + (1u << TB_P) / 2u) >> \
              TB_P))

uint32_t r32v_us_to_ticks(uint32_t us); /* us < 335,544 at 50 MHz */

/* ---- shared state ------------------------------------------------------------------------------ */
extern volatile uint32_t r32v_pwm_per, r32v_pwm_q; /* the PWM period (pwmMax() = PER + 1) */
extern volatile uint8_t r32v_ir_on;   /* ir_begin(): PWM0 is the carrier, the IR gate is on */
extern volatile uint8_t r32v_spi_on;  /* spi_begin(): uo4-uo6 belong to the SPI */
/* The Serial receive ring, filled by trap.S (which uses these offsets) or by r32v_serial_isr()
 * when the C handler sees the byte first; one slot stays empty, so buf[head] is always free. */
#define R32V_RX_SIZE 64u
struct r32v_rx {
  volatile uint8_t head;          /*  0 */
  volatile uint8_t tail;          /*  1 */
  volatile uint8_t cbdue;         /*  2: trap.S buffered a byte and cb is set: call it */
  uint8_t pad;
  volatile uint32_t lost;         /*  4: bytes lost (UART overrun, or the ring was full) */
  void (*volatile cb)(void);      /*  8: the INT_SERIAL callback */
  volatile uint8_t buf[R32V_RX_SIZE]; /* 12 */
};
extern struct r32v_rx r32v_rx;
void r32v_pwm_apply(void);            /* PWM_CTRL from the shared period, IR gate, enable */
void r32v_pwm_split(uint32_t cycles, uint32_t *per, uint32_t *q); /* a period in PER, Q */

#define R32V_IR_MAX 80 /* mark/space durations in one IR frame */
extern uint16_t r32v_ir_buf[R32V_IR_MAX]; /* ticks; 0 means 65,536; marks at even indices */
extern volatile uint8_t r32v_ir_n;
void r32v_ir_start(void); /* sends r32v_ir_buf[0..r32v_ir_n) (an odd count: it ends with a mark) */

#endif /* R32V_INTERNAL_H */
