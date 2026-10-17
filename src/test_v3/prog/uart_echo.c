/* uart_echo (harness drives ui1 with --uart-rx at DIV = 39, +-1 %): echo 8 bytes on TX (uo0). */
#include "bonetto3.h"

int main(void) {
  UART_CTRL = 39;
  ALTFN = 1;
  for (int i = 0; i < 8; i++) {
    while (!(UART_CTRL & UART_RX_FULL)) { }
    uint8_t b = (uint8_t)UART_DATA;
    while (UART_CTRL & UART_TX_BUSY) { }
    UART_DATA = b;
  }
  while (UART_CTRL & UART_TX_BUSY) { }
  CHECK(!(UART_CTRL & UART_RX_OVR));
  ALTFN = 0;
  return 0;
}
