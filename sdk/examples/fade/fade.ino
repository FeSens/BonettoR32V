// Fade: a 10-bit PWM fade on UO2 (PWM1). analogWrite() takes 0 (off) to 1023 (fully on); the
// PWM runs at 48.9 kHz at 50 MHz (1,023 cycles per period). The duty climbs from 0 to 1023 in
// 33 steps of 31, one every 10 ms, then falls back.

const uint8_t LED = UO2;
const int STEP = 31;
int duty = 0;
int step = STEP;

void setup() {
  analogWrite(LED, 0);
}

void loop() {
  analogWrite(LED, duty);
  delay(10);
  duty += step;
  if (duty >= 1023 || duty <= 0) {
    duty = duty >= 1023 ? 1023 : 0;
    step = -step;
  }
}
