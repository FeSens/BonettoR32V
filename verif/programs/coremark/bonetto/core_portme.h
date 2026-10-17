/* CoreMark port for BonettoR32V, contract v3 (code in the QSPI Pmod's flash, data and stack in
 * its PSRAM; see verif/programs/link.ld). Output goes to the PSRAM log ring at CPU
 * 0x017F0000-0x017F07FF (NUL-terminated), which the cosim can read from the final PSRAM image.
 * The timed iterations are bracketed by GPIO_OUT = 0xC0 / 0xC1 (core_portme.c). Built with
 * ITERATIONS from the Makefile (CM_ITERATIONS, 1) and TOTAL_DATA_SIZE=2000. */
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

/* Static data area in .bss (PSRAM). The stack grows down from 0x017FFF80, the core region's
 * start (contract v3 section 3), toward the log ring's end at 0x017F0800. */
#define MEM_METHOD MEM_STATIC
#define MEM_LOCATION "STATIC"
#define SEED_METHOD SEED_VOLATILE
#define MULTITHREAD 1
#define default_num_contexts 1

#ifndef COMPILER_VERSION
#define COMPILER_VERSION "riscv gcc " __VERSION__
#endif
#ifndef COMPILER_FLAGS
#define COMPILER_FLAGS "-O2 -march=rv32e_zicsr -mabi=ilp32e"
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
