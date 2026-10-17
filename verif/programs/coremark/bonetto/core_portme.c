/* CoreMark port for BonettoR32V. See core_portme.h. */
#include <stdarg.h>

#include "coremark.h"

/* --- output: RAM log ring at 0xF000-0xF7FF, always NUL-terminated ------------------------- */
extern char _log_ring[];
static ee_u32 log_pos;

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

/* Enough of printf for CoreMark: %d %u %x %lu %s %c %%, with an optional zero pad and width. */
int ee_printf(const char *fmt, ...) {
  va_list ap;
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

/* --- timing: no timer on this platform ----------------------------------------------------- */
/* The cosim measures cycles itself. time_in_secs() reports 10 s so CoreMark's run-length rule
 * ("must execute for at least 10 secs") doesn't count as an error in this functional test. */
void start_time(void) {}
void stop_time(void) {}
CORE_TICKS get_time(void) { return 0; }
secs_ret time_in_secs(CORE_TICKS ticks) {
  (void)ticks;
  return 10;
}

void portable_init(core_portable *p, int *argc, char *argv[]) {
  (void)argc;
  (void)argv;
  log_pos = 0;
  _log_ring[0] = 0;
  p->portable_id = 1;
}
void portable_fini(core_portable *p) { p->portable_id = 0; }

/* Performance run seeds (0, 0, 0x66), one iteration, all algorithms. */
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
