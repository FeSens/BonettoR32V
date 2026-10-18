// Button interrupt: a push button from 3.3 V to UI2 (with a 10 kOhm pull-down: the chip has no
// pull-ups), so a press is a rising edge. The interrupt counts presses, ignoring edges within
// 20 ms of the last one (contact bounce); loop() toggles LED_BUILTIN and prints the count.

const uint8_t BUTTON = UI2;
volatile uint32_t presses = 0;
volatile uint32_t last_ms = 0;

void onPress() {
  uint32_t now = millis();  // fine inside an interrupt callback
  if (presses == 0 || now - last_ms >= 20) {
    presses++;
    last_ms = now;
  }
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(115200);
  attachInterrupt(digitalPinToInterrupt(BUTTON), onPress, RISING);
  Serial.println("press the button on UI2");
}

uint32_t shown = 0;

void loop() {
  uint32_t n = presses;
  if (n != shown) {
    shown = n;
    digitalWrite(LED_BUILTIN, n & 1);
    Serial.print("presses: ");
    Serial.println(n);
  }
}
