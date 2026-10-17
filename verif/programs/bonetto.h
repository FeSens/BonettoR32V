/* BonettoR32V platform definitions for C test programs (docs/contract.md section 3). */
#ifndef BONETTO_H
#define BONETTO_H

#include <stdint.h>

/* GPIO: reading the byte at 0x80000000 returns ui_in; writing it sets uo_out.
 * Any address with bit 31 set is IO: bytes at X[1:0] = 0 are the GPIO byte, others read 0. */
#define GPIO_BASE 0x80000000u
#define GPIO (*(volatile uint8_t *)GPIO_BASE)
#define GPIO_WORD (*(volatile uint32_t *)GPIO_BASE)

/* RAM log ring at 0xF000-0xF7FF (NUL-terminated text), extracted by the cosim harness. */
extern char _log_ring[];
extern char _log_ring_end[];

#endif
