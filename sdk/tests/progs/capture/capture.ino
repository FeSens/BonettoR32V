// attachInterrupt(INT_CAPTURE): the timer captures rising edges on UI0 (sdk/tests/test_sdk.py
// drives three pulses through the harness's gpio_script and checks the intervals). Prints
// "captures <n> <dt1> <dt2>" in microseconds between the captured edges (captureMicros()), then
// GPIO_OUT = 0x5A with interrupts off and a self-loop: the cosim harness's program end.

static volatile uint32_t caps = 0;
static volatile uint32_t at[3];

void onCapture() {
  if (caps < 3) at[caps] = captureMicros();
  caps++;
}

void setup() {
  Serial.begin(115200);
  attachInterrupt(INT_CAPTURE, onCapture, RISING);
  const uint32_t t0 = millis();
  while (caps < 3 && millis() - t0 < 200) {
  }
  detachInterrupt(INT_CAPTURE);
  Serial.print("captures ");
  Serial.print(caps);
  Serial.print(" ");
  Serial.print(at[1] - at[0]);
  Serial.print(" ");
  Serial.println(at[2] - at[1]);
  serial_flush();
  noInterrupts();
  GPIO_OUT = 0x5A;
  for (;;) {
  }
}

void loop() {}
