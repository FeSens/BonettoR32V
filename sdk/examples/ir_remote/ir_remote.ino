// IR TV remote: three buttons send NEC codes through an IR LED on UO1.
//   UI2 power, UI4 volume up, UI5 volume down: push buttons from 3.3 V with 10 kOhm pull-downs,
//   so a press is a rising edge. Holding a volume button sends NEC repeat codes every 110 ms.
//   UO1 -> 1 kOhm -> NPN base (e.g. 2N2222); the IR LED (940 nm) and a ~33 Ohm resistor go
//   from the supply to the collector. LED_BUILTIN lights while a code goes out, and Serial
//   (115,200 baud) reports each one.
// The codes are the ones commonly published for LG TVs (NEC address 0x04: power 0x08, volume up
// 0x02, volume down 0x03). Other brands use other codes: read yours with any IR receiver
// sketch (for example IRremote's ReceiveDump on an Arduino) and change them here.

const uint16_t TV_ADDRESS = 0x04;
const uint8_t CMD_POWER = 0x08;
const uint8_t CMD_VOL_UP = 0x02;
const uint8_t CMD_VOL_DOWN = 0x03;

const uint8_t BTN_POWER = UI2;
const uint8_t BTN_VOL_UP = UI4;
const uint8_t BTN_VOL_DOWN = UI5;

volatile uint8_t pressed = 0;  // one bit per button, set by the interrupts

void onPower() { pressed |= 1; }
void onVolUp() { pressed |= 2; }
void onVolDown() { pressed |= 4; }

void send(uint8_t command, uint8_t button, bool repeat_while_held, const char *name) {
  digitalWrite(LED_BUILTIN, HIGH);
  IrSender.sendNEC(TV_ADDRESS, command, 0);
  uint32_t repeats = 0;
  while (repeat_while_held && digitalRead(button) == HIGH) {
    IrSender.sendNECRepeat();  // 110 ms after the previous frame
    repeats++;
  }
  digitalWrite(LED_BUILTIN, LOW);
  Serial.print("sent ");
  Serial.print(name);
  Serial.print(" (0x");
  Serial.print(command, HEX);
  Serial.print("), repeats ");
  Serial.println(repeats);
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(115200);
  IrSender.begin();  // UO1: 38 kHz carrier, gated by the timer
  attachInterrupt(digitalPinToInterrupt(BTN_POWER), onPower, RISING);
  attachInterrupt(digitalPinToInterrupt(BTN_VOL_UP), onVolUp, RISING);
  attachInterrupt(digitalPinToInterrupt(BTN_VOL_DOWN), onVolDown, RISING);
  Serial.println("IR remote ready");
}

void loop() {
  noInterrupts();
  uint8_t p = pressed;
  pressed = 0;
  interrupts();
  if (p & 1) send(CMD_POWER, BTN_POWER, false, "power");
  if (p & 2) send(CMD_VOL_UP, BTN_VOL_UP, true, "volume up");
  if (p & 4) send(CMD_VOL_DOWN, BTN_VOL_DOWN, true, "volume down");
}
