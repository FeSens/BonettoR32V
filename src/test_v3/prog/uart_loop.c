/* uart_loop (run with --loop: ui1 <- uo0): UART TX looped to RX at DIV = 15. Receive, RX_FULL
 * and IRQ_PEND.RX, the clear on reading byte 0 (not byte 1), overrun (the second byte is dropped
 * and RX_OVR set), writes while TX_BUSY ignored. */
#include "bonetto3.h"

static void send(uint8_t b) {
  while (UART_CTRL & UART_TX_BUSY) { }
  UART_DATA = b;
}

int main(void) {
  UART_CTRL = 15;
  ALTFN = 1;                       /* uo0 = UART TX (idle 1) */
  spin(1);
  CHECK(!(UART_CTRL & (UART_RX_FULL | UART_RX_OVR)));   /* ALTFN's 0 -> 1 is a rising edge: no frame */
  send(0x55);
  while (!(UART_CTRL & UART_RX_FULL)) { }
  CHECK(IRQ_PEND & IRQ_RX);
  CHECK(REGB(11, 1) == 0);         /* byte 1: reads 0, doesn't clear */
  CHECK(UART_CTRL & UART_RX_FULL);
  CHECK(REGB(11, 0) == 0x55);      /* byte 0: clears RX_FULL */
  CHECK(!(UART_CTRL & UART_RX_FULL));
  CHECK(!(IRQ_PEND & IRQ_RX));
  CHECK(UART_DATA == 0x55);        /* the last byte stays readable */
  /* overrun */
  send(0xA3);
  while (!(UART_CTRL & UART_RX_FULL)) { }
  send(0x3C);
  while (UART_CTRL & UART_TX_BUSY) { }
  spin(1);
  CHECK((UART_CTRL & (UART_RX_FULL | UART_RX_OVR)) == (UART_RX_FULL | UART_RX_OVR));
  CHECK(UART_DATA == 0xA3);        /* LW covers byte 0: clears both */
  CHECK(!(UART_CTRL & (UART_RX_FULL | UART_RX_OVR)));
  /* a write while busy is ignored (DIV = 1000: a frame outlasts an instruction) */
  UART_CTRL = 1000;
  UART_DATA = 0x11;
  CHECK(UART_CTRL & UART_TX_BUSY);
  UART_DATA = 0x22;
  while (!(UART_CTRL & UART_RX_FULL)) { }
  while (UART_CTRL & UART_TX_BUSY) { }
  spin(3);
  CHECK(!(UART_CTRL & UART_RX_OVR));
  CHECK(REGB(11, 0) == 0x11);
  UART_CTRL = 15;
  /* every byte value round-trips */
  static const uint8_t v[] = {0x00, 0xFF, 0x80, 0x01, 0x7E, 0xC3};
  for (unsigned i = 0; i < sizeof v; i++) {
    send(v[i]);
    while (!(UART_CTRL & UART_RX_FULL)) { }
    CHECK(REGB(11, 0) == v[i]);
  }
  while (UART_CTRL & UART_TX_BUSY) { }
  ALTFN = 0;
  return 0;
}
