/* BonettoR32V SDK: Serial (the UART: TX on UO0, RX on UI1, 8N1, LSB first; contract 5.5).
 * Receive is interrupt-driven into a 64-byte ring buffer (one interrupt per byte, handled in
 * trap.S's short assembly path, so back-to-back bytes at 115,200 baud keep up); transmit waits
 * for the transmitter (TX_BUSY) and writes the byte, so a byte costs no interrupt.
 * Numbers print without division instructions (the core has none): base 10 uses a shift-and-add
 * divide by 10, bases 2, 8 and 16 use shifts. */
#include "r32v_internal.h"

#define RX_SIZE R32V_RX_SIZE
#define rx r32v_rx /* the ring (r32v_internal.h); trap.S fills it as bytes arrive */

/* The C handler's receive path (r32v_trap_c calls it when it sees IRQ_PEND.RX, which happens
 * only when a byte completes after trap.S has looked): the same work as trap.S. */
void r32v_serial_isr(void) {
  if (UART_CTRL & UART_RX_OVR) rx.lost++; /* the hardware dropped a byte */
  const uint8_t b = UART_DATA8;            /* reading byte 0 clears RX_FULL and RX_OVR */
  const uint8_t h = rx.head, n = (uint8_t)((h + 1u) & (RX_SIZE - 1u));
  if (n != rx.tail) {
    rx.buf[h] = b;
    rx.head = n;
  } else {
    rx.lost++;
  }
  void (*cb)(void) = rx.cb;
  if (cb) cb();
}

void serial_begin(uint32_t baud) {
  if (!baud) return;
  uint32_t div = ((uint32_t)F_CPU + baud / 2) / baud; /* cycles per bit */
  if (div < 16u) div = 16u;                           /* DIV >= 15 (contract 5.5) */
  if (div > 8192u) div = 8192u;
  const uint32_t m = r32v_irq_save();
  UART_CTRL = div - 1;
  (void)UART_DATA8; /* drop a stale byte: clears RX_FULL and RX_OVR */
  rx.head = rx.tail = 0;
  ALTFN = ALTFN | ALT_UART_TX; /* uo0 = TX (idles high) */
  IRQ_EN = IRQ_EN | IRQ_RX;
  r32v_irq_restore(m);
}

void serial_end(void) {
  serial_flush();
  const uint32_t m = r32v_irq_save();
  IRQ_EN = IRQ_EN & ~IRQ_RX;
  GPIO_OUT = GPIO_OUT | (1u << UO0); /* the line stays high (idle) as GPIO */
  ALTFN = ALTFN & ~ALT_UART_TX;
  r32v_irq_restore(m);
}

int serial_available(void) { return (int)((rx.head - rx.tail) & (RX_SIZE - 1u)); }

int serial_peek(void) {
  if (rx.head == rx.tail) return -1;
  return rx.buf[rx.tail];
}

int serial_read(void) {
  const uint8_t t = rx.tail;
  if (rx.head == t) return -1;
  const uint8_t b = rx.buf[t];
  rx.tail = (uint8_t)((t + 1u) & (RX_SIZE - 1u));
  return b;
}

uint32_t serial_overruns(void) { return rx.lost; }

void serial_write(uint8_t b) {
  while (UART_CTRL & UART_TX_BUSY) {
  }
  UART_DATA = b; /* a write while busy would be ignored: we waited */
}

void serial_flush(void) {
  while (UART_CTRL & UART_TX_BUSY) {
  }
}

void serial_print(const char *s) {
  while (*s) serial_write((uint8_t)*s++);
}

/* n / 10 and n % 10 with shifts and adds (Hacker's Delight, divu10) */
static uint32_t divu10(uint32_t n, uint32_t *rem) {
  uint32_t q = (n >> 1) + (n >> 2);
  q += q >> 4;
  q += q >> 8;
  q += q >> 16;
  q >>= 3;
  uint32_t r = n - ((q << 3) + (q << 1));
  const uint32_t t = (r + 6u) >> 4; /* 1 if r >= 10 */
  q += t;
  r -= (t << 3) + (t << 1);
  *rem = r;
  return q;
}

void serial_print_uint(uint32_t v, int base) {
  char buf[33];
  int i = 32;
  buf[i] = 0;
  if (base == 16 || base == 8 || base == 2) {
    const uint32_t sh = base == 16 ? 4u : base == 8 ? 3u : 1u, mask = (uint32_t)base - 1u;
    do {
      const uint32_t d = v & mask;
      buf[--i] = (char)(d < 10 ? '0' + d : 'A' + d - 10);
      v >>= sh;
    } while (v);
  } else if (base == 10 || base < 2 || base > 36) {
    do {
      uint32_t d;
      v = divu10(v, &d);
      buf[--i] = (char)('0' + d);
    } while (v);
  } else { /* other bases: the library division (slow) */
    do {
      const uint32_t d = v % (uint32_t)base;
      buf[--i] = (char)(d < 10 ? '0' + d : 'A' + d - 10);
      v /= (uint32_t)base;
    } while (v);
  }
  serial_print(&buf[i]);
}

void serial_print_int(int32_t v, int base) {
  if (v < 0 && (base == 10 || base < 2 || base > 36)) {
    serial_write('-');
    serial_print_uint(0u - (uint32_t)v, 10);
  } else {
    serial_print_uint((uint32_t)v, base);
  }
}
