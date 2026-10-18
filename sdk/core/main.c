/* BonettoR32V SDK: the default main() (in the library, so a sketch may define its own main()
 * instead of setup() and loop()). The startup code has already called r32v_init(). */
#include "Arduino.h"

int main(void) {
  setup();
  for (;;) loop();
}
