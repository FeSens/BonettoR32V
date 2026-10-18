/* C blink: the same API from plain C (C99, GNU dialect). Toggles UO6 every 250 ms and prints
 * millis() on Serial (115,200 baud) each time. */
#include "Arduino.h"

void setup(void) {
  pinMode(UO6, OUTPUT);
  serial_begin(115200);
  serial_print("c_blink\r\n");
}

void loop(void) {
  digitalWrite(UO6, !digitalRead(UO6));
  serial_print_uint(millis(), DEC);
  serial_print("\r\n");
  delay(250);
}
