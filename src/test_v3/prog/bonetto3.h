/* Seed unit-test platform header (contract v3 sections 3-5). */
#ifndef BONETTO3_H
#define BONETTO3_H
#include <stdint.h>

#define IO_BASE 0x80000000u
#define REG(n) (*(volatile uint32_t *)(IO_BASE + 4u * (n)))
#define REGB(n, b) (*(volatile uint8_t *)(IO_BASE + 4u * (n) + (b)))
#define REGH(n, h) (*(volatile uint16_t *)(IO_BASE + 4u * (n) + 2u * (h)))
#define REGSB(n, b) (*(volatile int8_t *)(IO_BASE + 4u * (n) + (b)))
#define REGSH(n, h) (*(volatile int16_t *)(IO_BASE + 4u * (n) + 2u * (h)))

#define GPIO_OUT   REG(0)
#define GPIO_IN    REG(1)
#define ALTFN      REG(2)
#define TIMER_CNT  REG(3)
#define TIMER_CMP  REG(4)
#define TIMER_CAP  REG(5)
#define TIMER_CTRL REG(6)
#define PWM_CTRL   REG(7)
#define PWM_DUTY0  REG(8)
#define PWM_DUTY1  REG(9)
#define PWM_DUTY2  REG(10)
#define UART_DATA  REG(11)
#define UART_CTRL  REG(12)
#define SPI_DATA   REG(13)
#define SPI_CTRL   REG(14)
#define IRQ_EN     REG(15)
#define IRQ_PEND   REG(16)
#define GPIO_EDGE  REG(17)
#define GPIO_EPEND REG(18)

#define UART_TX_BUSY (1u << 13)
#define UART_RX_FULL (1u << 14)
#define UART_RX_OVR  (1u << 15)
#define SPI_BUSY     (1u << 4)

#define IRQ_CMP    1u
#define IRQ_CAP    2u
#define IRQ_RX     4u
#define IRQ_TXIDLE 8u
#define IRQ_GPIO   16u

#define csr_read(c) ({ uint32_t __v; __asm__ volatile("csrr %0, " #c : "=r"(__v)); __v; })
#define csr_write(c, v) __asm__ volatile("csrw " #c ", %0" ::"r"(v))
#define csr_set(c, v) __asm__ volatile("csrs " #c ", %0" ::"r"(v))
#define csr_clear(c, v) __asm__ volatile("csrc " #c ", %0" ::"r"(v))

#define MSTATUS_MIE  8u
#define MSTATUS_MPIE 0x80u
#define MIE_MEIE     0x800u

/* crt0: main() returning 0 reports pass (uo_out = 0x5A), anything else fail (0xEE). */
int main(void);
#define CHECK(c) do { if (!(c)) return __LINE__; } while (0)

static inline void spin(int n) { for (volatile int i = 0; i < n; i++) { } }
#endif
