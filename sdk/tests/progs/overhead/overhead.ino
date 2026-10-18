// Measures what the SDK's interrupts cost the CPU on the RTL (sdk/tests/test_sdk.py times it from
// the cosim pin dump). The same busy loop runs four times:
//   phase 1 (GPIO_OUT 0x04): interrupts off: the reference
//   phase 2 (0x08): interrupts on, only the timebase (one timer interrupt per 20 ms)
//   phase 3 (0x0C): tone(UO7, 1000): one timer interrupt per half period (2,000 per second)
//   phase 4 (0x10): tone(UO7, 2000), the highest UO7 pitch: 4,000 per second
// Each phase starts and ends with a GPIO_OUT store (uo2..uo4 show the phase number), and Serial
// prints micros() around it ("p<phase> <us at start> <us at end>"): the test compares those with
// the cycles, so a lost timer match would show. Then GPIO_OUT = 0x5A with interrupts off and a
// self-loop: the cosim harness's program end.

static const uint32_t N = 16000;  // about 8 M cycles on the RTL
static volatile uint32_t sink;

static void busy() {
  for (uint32_t i = 0; i < N; i++) sink = i;
}

static void phase(uint8_t p) {
  // interrupts off between each micros() and its GPIO_OUT store, so no interrupt lands in the
  // part the test can't see from the pins
  noInterrupts();
  uint32_t m0 = micros();
  GPIO_OUT = (uint32_t)p << 2;
  interrupts();
  busy();
  noInterrupts();
  GPIO_OUT = 0;
  uint32_t m1 = micros();
  interrupts();
  Serial.print("p");
  Serial.print(p);
  Serial.print(" ");
  Serial.print(m0);
  Serial.print(" ");
  Serial.println(m1);
}

void setup() {
  Serial.begin(115200);
  noInterrupts();
  GPIO_OUT = 1u << 2;
  busy();
  GPIO_OUT = 0;
  interrupts();
  Serial.println("p1");
  phase(2);
  tone(TONE_PIN, 1000);
  delay(5);
  phase(3);
  tone(TONE_PIN, 2000);
  delay(5);
  phase(4);
  noTone(TONE_PIN);
  serial_flush();
  noInterrupts();
  GPIO_OUT = 0x5A;
  for (;;) {
  }
}

void loop() {}
