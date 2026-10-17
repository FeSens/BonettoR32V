/* CoreMark port for BonettoR32V (RV32E, 64 KB SPI RAM, no timer, no UART).
 * Output goes to the RAM log ring at 0xF000-0xF7FF (NUL-terminated), which the cosim harness
 * extracts from the final RAM image. Built with ITERATIONS=1 and TOTAL_DATA_SIZE=2000. */
#ifndef CORE_PORTME_H
#define CORE_PORTME_H

#include <stddef.h>
#include <stdint.h>

#ifndef ITERATIONS
#define ITERATIONS 1
#endif

#define HAS_FLOAT 0
#define HAS_TIME_H 0
#define USE_CLOCK 0
#define HAS_STDIO 0
#define HAS_PRINTF 0
#define MAIN_HAS_NOARGC 1
#define MAIN_HAS_NORETURN 0

/* Static data area: the stack sits in the 2 KB above the log ring and only holds call frames. */
#define MEM_METHOD MEM_STATIC
#define MEM_LOCATION "STATIC"
#define SEED_METHOD SEED_VOLATILE
#define MULTITHREAD 1
#define default_num_contexts 1

#ifndef COMPILER_VERSION
#define COMPILER_VERSION "riscv gcc " __VERSION__
#endif
#ifndef COMPILER_FLAGS
#define COMPILER_FLAGS "-O2 -march=rv32e -mabi=ilp32e"
#endif

typedef int16_t ee_s16;
typedef uint16_t ee_u16;
typedef int32_t ee_s32;
typedef uint8_t ee_u8;
typedef uint32_t ee_u32;
typedef int32_t ee_ptr_int;
typedef size_t ee_size_t;
typedef uint32_t CORE_TICKS;
#define NULL_PTR ((void *)0)

#define align_mem(x) (void *)(4 + (((ee_ptr_int)(x)-1) & ~3))

#define CORETIMETYPE ee_u32
#define GETMYTIME(_t) (*(_t) = 0)
#define MYTIMEDIFF(fin, ini) ((fin) - (ini))
#define TIMER_RES_DIVIDER 1
#define SAMPLE_TIME_IMPLEMENTATION 1
#define EE_TICKS_PER_SEC 1

typedef struct CORE_PORTABLE_S {
  ee_u8 portable_id;
} core_portable;

void portable_init(core_portable *p, int *argc, char *argv[]);
void portable_fini(core_portable *p);
int ee_printf(const char *fmt, ...);

#endif /* CORE_PORTME_H */
