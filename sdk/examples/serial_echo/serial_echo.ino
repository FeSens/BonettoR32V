// Serial echo: 115,200 baud on UO0 (TX) and UI1 (RX), 8N1. Prints a greeting, then sends back
// every byte it receives; a carriage return also prints the number of bytes echoed so far.

uint32_t count = 0;

void setup() {
  Serial.begin(115200);
  Serial.println("BonettoR32V serial echo");
}

void loop() {
  int c = Serial.read();
  if (c < 0) return;
  Serial.write((uint8_t)c);
  count++;
  if (c == '\r') {
    Serial.print("\n[");
    Serial.print(count);
    Serial.println(" bytes]");
  }
}
