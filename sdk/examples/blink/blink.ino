// Blink: LED_BUILTIN (UO7, the decimal point of the TT demo board's 7-segment display) on for
// 500 ms, off for 500 ms.

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
  digitalWrite(LED_BUILTIN, HIGH);
  delay(500);
  digitalWrite(LED_BUILTIN, LOW);
  delay(500);
}
