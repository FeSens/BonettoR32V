/* CoreMark port for BonettoR32V, contract v3. See core_portme.h. */
#include <stdarg.h>

#include "bonetto.h"
#include "coremark.h"

/* The result window for the pins-mode PSRAM comparison (mkexpect.py): the whole log ring, which
 * portable_init clears, so every byte of it is written. CPU address pairs, ended by 0, 0. */
__attribute__((used)) const uint32_t _psram_window[] = {0x017F0000u, 0x017F0800u, 0, 0};

/* --- output: PSRAM log ring at 0x017F0000-0x017F07FF, always NUL-terminated ---------------- */
static ee_u32 log_pos;
static int validated, errors;

static void log_putc(char c) {
  _log_ring[log_pos] = c;
  log_pos = (log_pos + 1) & 0x7FF;
  _log_ring[log_pos] = 0;
}

static void log_puts(const char *s) {
  while (*s) log_putc(*s++);
}

static void log_num(ee_u32 v, unsigned base, int width, char pad) {
  char buf[12];
  int i = 0;
  do {
    unsigned d = v % base;
    buf[i++] = (char)(d < 10 ? '0' + d : 'a' + d - 10);
    v /= base;
  } while (v);
  while (i < width) buf[i++] = pad;
  while (i > 0) log_putc(buf[--i]);
}

static int starts_with(const char *s, const char *prefix) {
  while (*prefix)
    if (*s++ != *prefix++) return 0;
  return 1;
}

/* Enough of printf for CoreMark: %d %u %x %lu %s %c %%, with an optional zero pad and width.
 * It also notes CoreMark's verdict lines, for portable_fini. */
int ee_printf(const char *fmt, ...) {
  va_list ap;
  if (starts_with(fmt, "Correct operation validated")) validated = 1;
  if (starts_with(fmt, "Errors detected") || starts_with(fmt, "Cannot validate") ||
      starts_with(fmt, "ERROR"))
    errors = 1;
  va_start(ap, fmt);
  while (*fmt) {
    char c = *fmt++;
    if (c != '%') {
      log_putc(c);
      continue;
    }
    char pad = ' ';
    int width = 0;
    if (*fmt == '0') {
      pad = '0';
      fmt++;
    }
    while (*fmt >= '0' && *fmt <= '9') width = width * 10 + (*fmt++ - '0');
    while (*fmt == 'l') fmt++;
    c = *fmt;
    if (c) fmt++;
    switch (c) {
      case 'd': {
        ee_s32 v = va_arg(ap, ee_s32);
        if (v < 0) {
          log_putc('-');
          log_num((ee_u32)(-v), 10, width, pad);
        } else {
          log_num((ee_u32)v, 10, width, pad);
        }
        break;
      }
      case 'u': log_num(va_arg(ap, ee_u32), 10, width, pad); break;
      case 'x': log_num(va_arg(ap, ee_u32), 16, width, pad); break;
      case 's': log_puts(va_arg(ap, const char *)); break;
      case 'c': log_putc((char)va_arg(ap, int)); break;
      case '%': log_putc('%'); break;
      default:
        log_putc('%');
        if (c) log_putc(c);
        break;
    }
  }
  va_end(ap);
  return 0;
}

/* --- timing: the cosim measures cycles at the pins ----------------------------------------- */
/* start_time / stop_time bracket the timed iterations with GPIO_OUT = 0xC0 / 0xC1 (ALTFN = 0);
 * the harness's coremark_cycles_per_iter is the cycles between those two uo_out changes divided
 * by expect.json's coremark_iters (ruling R21). time_in_secs() reports 10 s so CoreMark's
 * run-length rule ("must execute for at least 10 secs") doesn't count as an error here. */
void start_time(void) { GPIO_OUT = MARK_CM_START; }
void stop_time(void) { GPIO_OUT = MARK_CM_STOP; }
CORE_TICKS get_time(void) { return 0; }
secs_ret time_in_secs(CORE_TICKS ticks) {
  (void)ticks;
  return 10;
}

void portable_init(core_portable *p, int *argc, char *argv[]) {
  (void)argc;
  (void)argv;
  for (ee_u32 *w = (ee_u32 *)_log_ring; w < (ee_u32 *)_log_ring_end; w++) *w = 0;
  log_pos = 0;
  validated = errors = 0;
  p->portable_id = 1;
}

/* The verdict: main returns to crt0, which writes GPIO_OUT = 0x5A; a run CoreMark didn't
 * validate fails with id 0x20 (GPIO_OUT = 0x20, then 0xEE). */
void portable_fini(core_portable *p) {
  p->portable_id = 0;
  if (!validated || errors) _fail(0x20);
}

/* Performance run seeds (0, 0, 0x66), ITERATIONS iterations, all algorithms. */
volatile ee_s32 seed1_volatile = 0x0;
volatile ee_s32 seed2_volatile = 0x0;
volatile ee_s32 seed3_volatile = 0x66;
volatile ee_s32 seed4_volatile = ITERATIONS;
volatile ee_s32 seed5_volatile = 0;

/* gcc may emit calls to these; there's no libc. */
void *memset(void *d, int c, size_t n) {
  unsigned char *p = d;
  while (n--) *p++ = (unsigned char)c;
  return d;
}
void *memcpy(void *d, const void *s, size_t n) {
  unsigned char *p = d;
  const unsigned char *q = s;
  while (n--) *p++ = *q++;
  return d;
}
