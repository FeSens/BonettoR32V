// Tone: a rising arpeggio on UO7 (the timer output; drive a piezo or speaker through a
// transistor), then a 1 kHz beep on UO2 (a PWM pin: no CPU cost while it plays), once a second.
// tone(pin, hz, ms) returns at once and stops by itself after ms; tone(pin, hz) plays until
// noTone(pin).

const uint16_t NOTES[] = {523, 659, 784, 1047};  // C5 E5 G5 C6

void setup() {}

void loop() {
  for (unsigned i = 0; i < sizeof NOTES / sizeof NOTES[0]; i++) {
    tone(TONE_PIN, NOTES[i], 150);
    delay(200);
  }
  tone(UO2, 1000);
  delay(200);
  noTone(UO2);
  delay(1000);
}
