/* The 16-bit timer over its full range, contract v3 section 5.3. The harness loops uo7 (the
 * timer output when ALTFN[7] = 1) back to ui0 (the capture input): expect.json loop_uo7_to_ui0.
 * Every check holds at any core speed from 3 to 2,048 cycles per instruction (the contract-4
 * cap); the comments give the worst case.
 *   A. Registers with bit 15 set: CMP = 0xBEEF and CNT = 0xA5C3 read back (timer off, so CNT
 *      holds); a byte store to byte 1 of CNT and a halfword store to CMP change only their bytes;
 *      TIMER_CTRL's ten bits read back.
 *   B. Wrap at 65,535 -> 0 (P = 4, no auto-reload): with CMP = 0xFFFF the match at 0xFFFF sets
 *      IRQ_PEND.CMP and CNT goes on from 0 (read below 0x0800: 2,048 ticks are 32,768 cycles,
 *      16 instructions at the cap); with CMP = 0x7000 the wrap alone sets no flag.
 *   C. Auto-reload at a high CMP (0xC350 = 50,000, P = 1) in toggle mode, capture on both edges:
 *      for T16_AR_PERIODS reloads, every CNT sample is <= CMP, some sample has bit 15 set (the
 *      count spends 34,466 cycles at or above 0x8000 per period, more than a polling iteration
 *      at the cap), and each capture right after a reload is <= 3 (the loop latency is under
 *      2 ticks).
 *   D. 16-bit captures, free-running toggle mode, P = 3 (a tick outlasts the loop latency, so
 *      CAP = CMP + 1, +-1): rising edge at CMP = 0x8001 (CAP 0x8002), falling edge at 0xC3C3
 *      (CAP 0xC3C4), rising edge at 0xFFFF, where CNT wraps (CAP 0x0000, +-1 mod 2^16).
 *   E. Prescale P = 15 (a tick every 32,768 cycles), timed by a UART frame (ALTFN[0] = 0, so it
 *      stays internal; TX_BUSY reads 1 from the write to the end of the stop bit, contract 5.5):
 *      DIV = 8191 makes the frame 81,920 cycles, up to 90,113 with the start latency, plus at
 *      most about 30,000 cycles of reads and polling at the cap: CNT advances by 2 to 4 ticks
 *      (P = 14 would give at least 5).
 *   F. A TIMER_CNT write restarts the prescaler (P = 15): after a tick, a 26,000-cycle frame
 *      brings the program close to the next tick of the old prescaler phase; it writes
 *      CNT = 0x1234 there, waits a 12,000-cycle frame (at most 27,537 cycles from the write to
 *      the read at the cap, below one tick) and CNT must still be 0x1234. A prescaler that kept
 *      its phase would have ticked in between at typical core speeds. CNT then ticks on.
 * Declared (ruling R13): the outcome depends on the timer and loop timing. */
#include "bonetto.h"
#include "stim.h"

#define WAIT_FLAG(f, id) WAIT_UNTIL(IRQ_PEND & (f), 1u << 22, id)
#define IO_REG16(slot) (*(volatile uint16_t *)(IO_BASE + 4 * (slot)))
#define IO_BYTE1(slot) (*(volatile uint8_t *)(IO_BASE + 4 * (slot) + 1))
#define CAP_NEAR(cap, want) ((((cap) - (want) + 1) & 0xFFFF) <= 2) /* want +-1, mod 2^16 */

/* One UART frame of 10 (div + 1) cycles, internal (ALTFN[0] = 0): returns once TX_BUSY clears. */
static void uart_frame(uint32_t div, uint32_t id) {
  UART_CTRL = div;
  UART_DATA = 0x55;
  WAIT_UNTIL(!(UART_CTRL & UART_TX_BUSY), 1u << 22, id);
}

static void phase_a(void) {
  TIMER_CTRL = 0;
  TIMER_CMP = T16_CMP_A;
  TIMER_CNT = T16_CNT_A;
  CHECK(TIMER_CMP == T16_CMP_A, 0x10);
  CHECK(TIMER_CNT == T16_CNT_A, 0x11);
  IO_BYTE1(SLOT_TIMER_CNT) = 0xFF; /* byte 1 only */
  CHECK(TIMER_CNT == (0xFF00 | (T16_CNT_A & 0xFF)), 0x12);
  IO_REG16(SLOT_TIMER_CMP) = 0x8001;
  CHECK(TIMER_CMP == 0x8001, 0x13);
  CHECK(IO_BYTE1(SLOT_TIMER_CMP) == 0x80, 0x14);
  /* every TIMER_CTRL field but the enable (and output mode 11, which the contract leaves out) */
  const uint32_t ctl = TMR_P(15) | TMR_AUTO | TMR_OUT_PWM | TMR_CAP_BOTH;
  TIMER_CTRL = ctl;
  CHECK(TIMER_CTRL == ctl, 0x15);
  TIMER_CTRL = 0;
  CHECK(TIMER_CTRL == 0, 0x16);
}

static void phase_b(void) {
  IRQ_PEND = IRQ_CMP | IRQ_CAP;
  TIMER_CMP = 0xFFFF;
  TIMER_CNT = 0xFFF0;
  TIMER_CTRL = TMR_P(4) | TMR_EN; /* free-running, output low */
  WAIT_FLAG(IRQ_CMP, 0x17);       /* the match at 0xFFFF */
  uint32_t v = TIMER_CNT;
  CHECK(v < 0x0800, 0x18); /* wrapped to 0 and counting */
  WAIT_UNTIL(TIMER_CNT >= v + 2, 1u << 22, 0x19);
  CHECK(TIMER_CNT < 0x8000, 0x1a);
  TIMER_CTRL = 0;
  IRQ_PEND = IRQ_CMP;
  TIMER_CMP = 0x7000;
  TIMER_CNT = 0xFFF0;
  TIMER_CTRL = TMR_P(4) | TMR_EN;
  WAIT_UNTIL(TIMER_CNT < 0x8000, 1u << 22, 0x1b); /* wrapped */
  CHECK(!(IRQ_PEND & IRQ_CMP), 0x1c);              /* a wrap is no match */
  TIMER_CTRL = 0;
}

static void phase_c(void) {
  ALTFN = ALT_TIMER; /* uo7 = the timer output, looped back to ui0 */
  TIMER_CMP = T16_CMP_AR;
  TIMER_CNT = T16_AR_START;
  IRQ_PEND = IRQ_CMP | IRQ_CAP;
  TIMER_CTRL = TMR_P(1) | TMR_EN | TMR_AUTO | TMR_OUT_TOGGLE | TMR_CAP_BOTH;
  uint32_t high = 0;
  for (int k = 0; k < T16_AR_PERIODS; k++) {
    uint32_t n = 1u << 22;
    while (!(IRQ_PEND & IRQ_CMP)) {
      uint32_t c = TIMER_CNT;
      CHECK(c <= T16_CMP_AR, 0x1d);
      if (c >= 0x8000) high++;
      if (--n == 0) _fail(0x1e);
    }
    IRQ_PEND = IRQ_CMP;
    WAIT_FLAG(IRQ_CAP, 0x1f); /* the toggle came back on ui0 */
    uint32_t cap = TIMER_CAP;
    IRQ_PEND = IRQ_CAP;
    CHECK(cap <= 3, 0x20); /* right after the reload to 0 */
  }
  CHECK(high > 0, 0x21);
  TIMER_CTRL = 0;
}

static void phase_d(void) {
  TIMER_CMP = T16_CAP_CMP1;
  TIMER_CNT = T16_CAP_CNT0;
  IRQ_PEND = IRQ_CMP | IRQ_CAP;
  TIMER_CTRL = TMR_P(T16_CAP_P) | TMR_EN | TMR_OUT_TOGGLE | TMR_CAP_BOTH;
  WAIT_FLAG(IRQ_CAP, 0x22); /* rising edge */
  uint32_t cap = TIMER_CAP;
  CHECK(IRQ_PEND & IRQ_CMP, 0x23);
  CHECK(CAP_NEAR(cap, T16_CAP_CMP1 + 1), 0x24);
  CHECK(GPIO_IN & 1, 0x25);
  IRQ_PEND = IRQ_CMP | IRQ_CAP;
  TIMER_CMP = T16_CAP_CMP2;
  WAIT_FLAG(IRQ_CAP, 0x26); /* falling edge */
  cap = TIMER_CAP;
  CHECK(CAP_NEAR(cap, T16_CAP_CMP2 + 1), 0x27);
  CHECK(!(GPIO_IN & 1), 0x28);
  IRQ_PEND = IRQ_CMP | IRQ_CAP;
  TIMER_CMP = 0xFFFF;
  WAIT_FLAG(IRQ_CAP, 0x29); /* rising edge at the wrap */
  cap = TIMER_CAP;
  CHECK(CAP_NEAR(cap, 0x0000), 0x2a);
  CHECK(TIMER_CNT < 0x8000, 0x2b);
  TIMER_CTRL = 0;
  ALTFN = 0;
  IRQ_PEND = IRQ_CMP | IRQ_CAP;
}

static void phase_e(void) {
  TIMER_CMP = 0xFFFF;
  TIMER_CNT = 0;
  TIMER_CTRL = TMR_P(15) | TMR_EN;
  uint32_t c0 = TIMER_CNT;
  uart_frame(T16_P15_DIV, 0x2c);
  uint32_t d = (TIMER_CNT - c0) & 0xFFFF;
  CHECK(d >= 2 && d <= 4, 0x2d);
}

static void phase_f(void) {
  UART_CTRL = T16_RS_DIV_A;
  TIMER_CNT = T16_RS_CNT0; /* P = 15 still */
  WAIT_UNTIL(TIMER_CNT != T16_RS_CNT0, 1u << 22, 0x2e); /* a tick: the prescaler phase is known */
  UART_DATA = 0x55;
  WAIT_UNTIL(!(UART_CTRL & UART_TX_BUSY), 1u << 22, 0x2f);
  UART_CTRL = T16_RS_DIV_B;
  /* from the CNT write to the CNT read: 1 store, the frame (at most 13,201 cycles), at most one
   * polling iteration and the load */
  TIMER_CNT = T16_RS_CNT;
  UART_DATA = 0x55;
  while (UART_CTRL & UART_TX_BUSY) {
  }
  CHECK(TIMER_CNT == T16_RS_CNT, 0x30);
  WAIT_UNTIL(TIMER_CNT == T16_RS_CNT + 1, 1u << 22, 0x31); /* and it ticks on */
  TIMER_CTRL = 0;
}

int main(void) {
  phase_a();
  phase_b();
  phase_c();
  phase_d();
  phase_e();
  phase_f();
  return 0;
}
