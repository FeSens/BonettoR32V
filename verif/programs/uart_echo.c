/* UART echo, contract v3 section 5.5 (Review Focus 4). 1 Mbaud: DIV = UART_DIV = 39 (40 cycles
 * per bit at 40 MHz).
 *
 *   1. Sends UART_HELLO ("hello"), before any byte is received: the gate-level test decodes it.
 *      Before each send it polls until TX_BUSY = 0 (a write while busy is ignored). TX_BUSY reads
 *      1 only until the stop bit ends (contract 5.5), so a read after a short frame may see 0:
 *      the program doesn't check it right after the write; the harness judges TX_BUSY reads.
 *   2. Echoes the UART_N bytes the harness sends (expect.json uart_rx), checking each against
 *      UART_MSG and that none was dropped. uart_echo receives them at +1 % of the bit time,
 *      uart_echo_fast at -1 % (the same program; only uart_rate_ppm differs).
 *   3. Overrun: it stops reading. UART_KEEP arrives (RX_FULL = 1), then UART_DROP arrives while
 *      RX_FULL = 1: it is dropped and RX_OVR = 1. Reading byte 0 of UART_DATA returns UART_KEEP and
 *      clears RX_FULL and RX_OVR. It echoes UART_KEEP.
 *   4. Side effects only for the byte named (contract 5): reading byte 1 of UART_DATA during the
 *      overrun keeps RX_FULL and RX_OVR; at the end, with the divider at its maximum (a frame
 *      would keep TX_BUSY at 1 for about 82k cycles), writing byte 1 and the halfword at byte 2
 *      sends nothing.
 * uart_tx_expect = UART_HELLO + UART_MSG + UART_KEEP.
 *
 * It ends with GPIO_OUT = 0x5A and ALTFN = ALT_UART_TX: uo0 stays the idle UART line (high), so
 * the end makes no false start bit. A failed check ends with GPIO_OUT = 0xEE (crt0: ALTFN = 0).
 * Its outcome depends on the harness's UART timing: expect.json is declared (ruling R13). */
#include "bonetto.h"
#include "stim.h"

static const char msg[] = UART_MSG;
static const char hello[] = UART_HELLO;

static void send(uint32_t b) {
  WAIT_UNTIL(!(UART_CTRL & UART_TX_BUSY), 1u << 20, 0x10);
  UART_DATA = b; /* writing byte 0 starts the frame */
}

int main(void) {
  UART_CTRL = UART_DIV;
  CHECK((UART_CTRL & UART_DIV_MASK) == UART_DIV, 0x12);
  ALTFN = ALT_UART_TX;
  for (const char *s = hello; *s; s++) send((uint8_t)*s);

  for (int i = 0; i < UART_N; i++) {
    WAIT_UNTIL(UART_CTRL & UART_RX_FULL, 1u << 22, 0x13);
    CHECK(!(UART_CTRL & UART_RX_OVR), 0x14);
    uint8_t b = IO_REG8(SLOT_UART_DATA); /* reading byte 0 clears RX_FULL */
    CHECK(!(UART_CTRL & UART_RX_FULL), 0x15);
    CHECK(b == (uint8_t)msg[i], 0x16);
    send(b);
  }

  /* overrun: stop reading */
  WAIT_UNTIL(UART_CTRL & UART_RX_FULL, 1u << 22, 0x17);
  WAIT_UNTIL(UART_CTRL & UART_RX_OVR, 1u << 22, 0x18);
  CHECK(UART_CTRL & UART_RX_FULL, 0x19);
  CHECK(IRQ_PEND & IRQ_RX, 0x1a);
  CHECK(*(volatile uint8_t *)(IO_BASE + OFS_UART_DATA + 1) == 0, 0x1f); /* byte 1: no side effect */
  CHECK((UART_CTRL & (UART_RX_FULL | UART_RX_OVR)) == (UART_RX_FULL | UART_RX_OVR), 0x20);
  uint8_t k = IO_REG8(SLOT_UART_DATA);
  CHECK(k == UART_KEEP, 0x1b); /* the dropped UART_DROP didn't replace it */
  CHECK(!(UART_CTRL & (UART_RX_FULL | UART_RX_OVR)), 0x1c);
  CHECK(!(IRQ_PEND & IRQ_RX), 0x1d);
  send(k);
  WAIT_UNTIL(!(UART_CTRL & UART_TX_BUSY), 1u << 20, 0x1e); /* the last frame is out */

  /* Writing bytes 1-3 of UART_DATA sends nothing (only byte 0 starts a frame). */
  UART_CTRL = UART_DIV_MASK;
  *(volatile uint8_t *)(IO_BASE + OFS_UART_DATA + 1) = 0x55;
  CHECK(!(UART_CTRL & UART_TX_BUSY), 0x21);
  *(volatile uint16_t *)(IO_BASE + OFS_UART_DATA + 2) = 0x5555;
  CHECK(!(UART_CTRL & UART_TX_BUSY), 0x22);

  /* The end without crt0's _pass, which would hand uo0 back to GPIO (low). */
  GPIO_OUT = RESULT_PASS;
  for (;;) {
  }
}
