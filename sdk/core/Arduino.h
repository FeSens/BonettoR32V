/* BonettoR32V SDK: an Arduino-style API for the BonettoR32V microcontroller (RV32E, code in the
 * QSPI Pmod's flash, data in its PSRAM). docs/programming-guide.md is the manual.
 *
 * A sketch defines setup() and loop() (or main()); the SDK's startup initializes the hardware,
 * starts the timebase and enables interrupts first. C sketches use the C API below; C++ sketches
 * (.ino, .cpp) also get Serial, SPI and IrSender objects.
 *
 * Pins: outputs UO0..UO7 (uo_out), inputs UI0..UI7 (ui_in). Fixed functions (contract 5.2):
 *   UO0 Serial TX, UI1 Serial RX; UO1 PWM0 / IR LED; UO2, UO3 PWM1, PWM2; UO4 SS, UO5 SCK,
 *   UO6 MOSI, UI3 MISO; UO7 timer output (tone); UI0 timer capture.
 *
 * Resources (one timer, one PWM block):
 *   - The timer is the timebase: prescale 2^TB_P cycles per tick, auto-reload every 20 ms; its
 *     compare interrupt (once per 20 ms) keeps millis()/micros(). tone() on UO7 and the IR sender
 *     borrow it: they change its period, and the same interrupt keeps counting time through them,
 *     so millis() never stops. INT_TIMER callbacks run only while nothing borrows it. A UO7 tone
 *     costs an interrupt per half period (2 kHz at most); a PWM tone (UO1-UO3) costs nothing.
 *   - The PWM block has one period for UO1-UO3. analogWriteFrequency() and tone() on a PWM pin
 *     change it for all three. IrSender.begin() sets it to the IR carrier (38 kHz) for good.
 *   - Interrupt callbacks run with interrupts off. Keep them short (under ~100 instructions,
 *     about 0.2 ms): while an IR frame is being sent, a longer callback breaks its timing.
 *   - gp and tp (x3, x4) belong to the trap handler (trap.S): assembly in a sketch must not
 *     use them.
 */
#ifndef ARDUINO_H
#define ARDUINO_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "r32v.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- pins and constants ---------------------------------------------------------------------- */
enum { UO0 = 0, UO1, UO2, UO3, UO4, UO5, UO6, UO7, UI0, UI1, UI2, UI3, UI4, UI5, UI6, UI7 };
#define LED_BUILTIN UO7  /* the decimal point of the TT demo board's 7-segment display */
#define TX UO0
#define RX UI1
#define IR_SEND_PIN UO1
#define TONE_PIN UO7
#define SS UO4
#define SCK UO5
#define MOSI UO6
#define MISO UI3
#define CAPTURE_PIN UI0

#define LOW 0
#define HIGH 1
#define INPUT 0
#define OUTPUT 1
#define INPUT_PULLUP 2 /* accepted; the chip has no pull-ups */
#define RISING 1
#define FALLING 2
#define CHANGE 3
#define BIN 2
#define OCT 8
#define DEC 10
#define HEX 16

/* attachInterrupt() sources: digitalPinToInterrupt(UI0..UI7) = 0..7, plus: */
#define INT_TIMER 8    /* every timebase period (20 ms; timerPeriodMicros() changes it) */
#define INT_CAPTURE 9  /* timer capture on UI0 (mode RISING, FALLING or CHANGE); captureMicros() */
#define INT_SERIAL 10  /* a byte arrived (after Serial has buffered it) */
#define NOT_AN_INTERRUPT (-1)
#define digitalPinToInterrupt(p) ((p) >= UI0 && (p) <= UI7 ? (int)(p) - UI0 : NOT_AN_INTERRUPT)

/* ---- digital and PWM --------------------------------------------------------------------------- */
void pinMode(uint8_t pin, uint8_t mode);   /* UOx OUTPUT: back to GPIO; inputs need nothing */
void digitalWrite(uint8_t pin, uint8_t val);
int digitalRead(uint8_t pin);              /* UIx: the input; UOx: the GPIO_OUT bit */
/* 10-bit PWM on UO1-UO3: duty 0 = off ... pwmMax() = always on (1023 by default, 48.9 kHz at
 * 50 MHz). Other pins: HIGH from half of pwmMax(). */
void analogWrite(uint8_t pin, uint32_t duty);
uint32_t analogWriteFrequency(uint32_t hz); /* PWM period for UO1-UO3; returns the new pwmMax() */
uint32_t pwmMax(void);

/* ---- time -------------------------------------------------------------------------------------- */
uint32_t millis(void);  /* ms since startup (wraps after 49.7 days) */
uint32_t micros(void);  /* us since startup (wraps after 71.6 minutes) */
void delay(uint32_t ms);
void delayMicroseconds(uint32_t us); /* busy-waits on the timer, plus the call's own instructions */
/* INT_TIMER period: 12,288 to 65,536 timer ticks (3,932 us to 20,971 us at 50 MHz; 20 ms default) */
void timerPeriodMicros(uint32_t us);
void yield(void);                    /* called while delay() waits (weak, does nothing) */

/* ---- interrupts -------------------------------------------------------------------------------- */
void attachInterrupt(int irq, void (*fn)(void), int mode);
void detachInterrupt(int irq);
uint32_t captureMicros(void);   /* micros() at the last INT_CAPTURE edge */
uint32_t captureTicks(void);    /* raw TIMER_CAP of the last INT_CAPTURE edge */
static inline void interrupts(void) { __asm__ volatile("csrsi mstatus, 8" : : : "memory"); }
static inline void noInterrupts(void) { __asm__ volatile("csrci mstatus, 8" : : : "memory"); }

/* ---- tone -------------------------------------------------------------------------------------- */
/* UO7: the timer, 24 Hz (F_CPU / 2^(TB_P+17)) to 2,000 Hz (higher requests play 2,000 Hz); one
 * interrupt per half period (docs/programming-guide.md has the measured cost); a timed tone stops
 * within half a period of ms. UO1-UO3: the PWM block, from F_CPU / 130,944 Hz (382 Hz at 50 MHz);
 * no CPU cost; takes over the PWM period of all three pins; a timed tone stops at the first
 * timebase interrupt after ms (up to 20 ms late). ms = 0: until noTone(). */
void tone(uint8_t pin, uint32_t hz, uint32_t ms);
void noTone(uint8_t pin);

/* ---- Serial (UART on UO0 / UI1, 8N1) --------------------------------------------------------- */
void serial_begin(uint32_t baud);   /* 3,125,000 baud at most at 50 MHz (DIV >= 15) */
void serial_end(void);
int serial_available(void);
int serial_read(void);              /* -1 if nothing */
int serial_peek(void);
void serial_write(uint8_t b);       /* blocks while the transmitter is busy */
void serial_print(const char *s);
void serial_print_uint(uint32_t v, int base);
void serial_print_int(int32_t v, int base);
void serial_flush(void);            /* waits until the last byte is out */
uint32_t serial_overruns(void);     /* bytes lost (buffer full or a hardware overrun) */

/* ---- SPI (mode 0, MSB first: UO4 SS, UO5 SCK, UO6 MOSI, UI3 MISO) ---------------------------- */
void spi_begin(void);
void spi_end(void);
uint32_t spi_set_clock(uint32_t hz); /* SCK = F_CPU / 2^(D+1), D = 1..7; returns the SCK used */
uint8_t spi_transfer(uint8_t b);

/* ---- IR remote sender (UO1: carrier from PWM0, gated by the timer) --------------------------- */
void ir_begin(void);                 /* 38 kHz carrier, duty 1/3; UO1 becomes the IR LED pin */
void ir_send_nec(uint16_t address, uint8_t command, uint8_t repeats); /* blocks until sent */
void ir_send_nec_raw(uint32_t data); /* 32 bits, LSB first (address, ~address, cmd, ~cmd) */
void ir_send_nec_repeat(void);
void ir_send_rc5(uint8_t address, uint8_t command, bool toggle); /* 36 kHz, Manchester */
void ir_send_raw(const uint16_t *us, uint16_t n, uint8_t khz); /* mark, space, mark, ... in us */
bool ir_busy(void);

/* ---- the sketch, and SDK hooks ---------------------------------------------------------------- */
void setup(void);
void loop(void);
void r32v_init(void);                         /* called by the startup code before main() */
void r32v_fault(uint32_t mcause, uint32_t mepc); /* weak: an exception (ecall, ebreak, illegal) */

#ifdef __cplusplus
} /* extern "C" */

/* ---- C++ veneer: stateless objects over the C API (no constructors run, no libstdc++) -------- */
inline void tone(uint8_t pin, uint32_t hz) { tone(pin, hz, 0); }

class HardwareSerial {
 public:
  void begin(uint32_t baud) const { serial_begin(baud); }
  void end() const { serial_end(); }
  int available() const { return serial_available(); }
  int read() const { return serial_read(); }
  int peek() const { return serial_peek(); }
  void flush() const { serial_flush(); }
  size_t write(uint8_t b) const { serial_write(b); return 1; }
  size_t write(const char *s) const { serial_print(s); return 1; }
  size_t print(const char *s) const { serial_print(s); return 1; }
  size_t print(char c) const { serial_write((uint8_t)c); return 1; }
  size_t print(unsigned char v, int base = DEC) const { serial_print_uint(v, base); return 1; }
  size_t print(int v, int base = DEC) const { serial_print_int(v, base); return 1; }
  size_t print(unsigned int v, int base = DEC) const { serial_print_uint(v, base); return 1; }
  size_t print(long v, int base = DEC) const { serial_print_int(v, base); return 1; }
  size_t print(unsigned long v, int base = DEC) const { serial_print_uint(v, base); return 1; }
  size_t println() const { serial_print("\r\n"); return 2; }
  template <typename T>
  size_t println(T v) const { size_t n = print(v); return n + println(); }
  template <typename T>
  size_t println(T v, int base) const { size_t n = print(v, base); return n + println(); }
  explicit operator bool() const { return true; }
};

class SPISettings {
 public:
  constexpr SPISettings(uint32_t hz = 1000000, int bit_order = 1, int mode = 0)
      : hz_(hz) { (void)bit_order; (void)mode; }
  uint32_t hz_;
};
#define MSBFIRST 1
#define SPI_MODE0 0

class SPIClass {
 public:
  void begin() const { spi_begin(); }
  void end() const { spi_end(); }
  void beginTransaction(const SPISettings &s) const { spi_set_clock(s.hz_); }
  void endTransaction() const {}
  uint8_t transfer(uint8_t b) const { return spi_transfer(b); }
  void transfer(void *buf, size_t n) const {
    uint8_t *p = (uint8_t *)buf;
    for (size_t i = 0; i < n; i++) p[i] = spi_transfer(p[i]);
  }
  void setClock(uint32_t hz) const { spi_set_clock(hz); }
};

class IRsend {
 public:
  void begin(uint8_t pin = IR_SEND_PIN) const { (void)pin; ir_begin(); }
  void sendNEC(uint16_t address, uint8_t command, uint8_t repeats = 0) const {
    ir_send_nec(address, command, repeats);
  }
  void sendNECRaw(uint32_t data) const { ir_send_nec_raw(data); }
  void sendNECRepeat() const { ir_send_nec_repeat(); }
  void sendRC5(uint8_t address, uint8_t command, bool toggle = false) const {
    ir_send_rc5(address, command, toggle);
  }
  void sendRaw(const uint16_t *us, uint16_t n, uint8_t khz) const { ir_send_raw(us, n, khz); }
  bool isBusy() const { return ir_busy(); }
};

static const HardwareSerial Serial __attribute__((unused)) = HardwareSerial();
static const SPIClass SPI __attribute__((unused)) = SPIClass();
static const IRsend IrSender __attribute__((unused)) = IRsend();
#endif /* __cplusplus */

#endif /* ARDUINO_H */
