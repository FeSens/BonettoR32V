/* BonettoR32V hardware registers (docs/contract.md sections 3-5; docs/programming-guide.md).
 *
 * Memory map: flash (code, read-only) at 0x0000_0000, PSRAM (data, stack) at 0x0100_0000, IO at
 * 0x8000_0000: 32-bit slots at IO_BASE + 4n. A load returns the whole slot; a store writes only
 * the bytes it covers. Reading byte 0 of UART_DATA pops the received byte; W1C bits clear when 1
 * is written to them.
 */
#ifndef R32V_H
#define R32V_H

#include <stdint.h>

#ifndef F_CPU
#define F_CPU 50000000UL /* the clock the chip runs at (TT demo board setting); see the Makefile */
#endif

#define R32V_IO_BASE 0x80000000u
#define R32V_REG(slot) (*(volatile uint32_t *)(R32V_IO_BASE + 4u * (slot)))
#define R32V_REG8(slot) (*(volatile uint8_t *)(R32V_IO_BASE + 4u * (slot)))

/* Slots (contract 5.1) */
#define GPIO_OUT R32V_REG(0)    /* [7:0] uo pins whose ALTFN bit is 0 */
#define GPIO_IN R32V_REG(1)     /* [7:0] ui pins (synchronized), read-only */
#define ALTFN R32V_REG(2)       /* [7:0] 1 = the uo pin carries its peripheral function */
#define TIMER_CNT R32V_REG(3)   /* [15:0] counter; a write sets it and restarts the prescaler */
#define TIMER_CMP R32V_REG(4)   /* [15:0] compare value */
#define TIMER_CAP R32V_REG(5)   /* [15:0] captured count, read-only */
#define TIMER_CTRL R32V_REG(6)  /* see TMR_* */
#define PWM_CTRL R32V_REG(7)    /* see PWM_* */
#define PWM_DUTY0 R32V_REG(8)   /* [9:0] */
#define PWM_DUTY1 R32V_REG(9)
#define PWM_DUTY2 R32V_REG(10)
#define PWM_DUTY(i) R32V_REG(8 + (i))
#define UART_DATA R32V_REG(11)  /* write [7:0]: send; read [7:0]: last received byte */
#define UART_DATA8 R32V_REG8(11) /* byte 0: reading it clears RX_FULL and RX_OVR */
#define UART_CTRL R32V_REG(12)  /* [12:0] DIV (bit time DIV+1 cycles), see UART_* */
#define SPI_DATA R32V_REG(13)   /* write [7:0]: transfer; read [7:0]: last received byte */
#define SPI_DATA8 R32V_REG8(13)
#define SPI_CTRL R32V_REG(14)   /* see SPI_* */
#define IRQ_EN R32V_REG(15)     /* [4:0] source enables, see IRQ_* */
#define IRQ_PEND R32V_REG(16)   /* [4:0] pending sources */
#define GPIO_EDGE R32V_REG(17)  /* [7:0] per-pin edge enable, [15:8] polarity (1 = falling) */
#define GPIO_EPEND R32V_REG(18) /* [7:0] per-pin edge flags (W1C) */

/* TIMER_CTRL */
#define TMR_P(p) ((p) & 15u)      /* [3:0] prescale: a tick every 2^P cycles */
#define TMR_EN (1u << 4)
#define TMR_AUTO (1u << 5)        /* CNT = 0 at the match */
#define TMR_OUT_LOW (0u << 6)
#define TMR_OUT_TOGGLE (1u << 6)  /* toggle at each match; starts at 0 when the mode is written */
#define TMR_OUT_PWM (2u << 6)     /* high while CNT < CMP */
#define TMR_CAP_OFF (0u << 8)
#define TMR_CAP_RISE (1u << 8)    /* capture edge on ui0 */
#define TMR_CAP_FALL (2u << 8)
#define TMR_CAP_BOTH (3u << 8)
#define TMR_CAP_MASK (3u << 8)

/* PWM_CTRL */
#define PWM_PER(n) ((n) & 1023u)  /* [9:0] period PER: the counter runs 0..PER */
#define PWM_Q(q) (((q) & 7u) << 10) /* [12:10] prescale: a step every 2^Q cycles */
#define PWM_EN (1u << 13)
#define PWM_IRGATE (1u << 14)     /* uo1 = PWM0 AND the timer output */

/* UART_CTRL */
#define UART_DIV_MASK 0x1FFFu
#define UART_TX_BUSY (1u << 13)
#define UART_RX_FULL (1u << 14)
#define UART_RX_OVR (1u << 15)

/* SPI_CTRL */
#define SPI_CS_N 1u               /* [0] the level of uo4 (CS_n) while ALTFN[4] = 1 */
#define SPI_D(d) (((d) & 7u) << 1) /* [3:1] SCK low 2^D cycles, then high 2^D cycles; D >= 1 */
#define SPI_BUSY (1u << 4)

/* IRQ_EN / IRQ_PEND */
#define IRQ_CMP (1u << 0)         /* timer match (W1C in IRQ_PEND) */
#define IRQ_CAP (1u << 1)         /* timer capture (W1C) */
#define IRQ_RX (1u << 2)          /* = UART RX_FULL */
#define IRQ_TXIDLE (1u << 3)      /* = !UART TX_BUSY */
#define IRQ_GPIO (1u << 4)        /* = OR of GPIO_EPEND */

/* ALTFN bits (contract 5.2) */
#define ALT_UART_TX (1u << 0)
#define ALT_PWM0 (1u << 1)        /* gated by the timer output when PWM_IRGATE is set */
#define ALT_PWM1 (1u << 2)
#define ALT_PWM2 (1u << 3)
#define ALT_SPI_CS (1u << 4)
#define ALT_SPI_SCK (1u << 5)
#define ALT_SPI_MOSI (1u << 6)
#define ALT_TIMER (1u << 7)

/* CSRs (contract 4) */
#define MSTATUS_MIE (1u << 3)
#define MIE_MEIE (1u << 11)
#define csr_read(csr)                                           \
  ({                                                            \
    uint32_t v_;                                                \
    __asm__ volatile("csrr %0, " #csr : "=r"(v_) : : "memory"); \
    v_;                                                         \
  })
#define csr_write(csr, v) __asm__ volatile("csrw " #csr ", %0" : : "r"((uint32_t)(v)) : "memory")
#define csr_set(csr, v) __asm__ volatile("csrs " #csr ", %0" : : "r"((uint32_t)(v)) : "memory")
#define csr_clear(csr, v) __asm__ volatile("csrc " #csr ", %0" : : "r"((uint32_t)(v)) : "memory")

/* Interrupts off, returning whether they were on; and back. */
static inline uint32_t r32v_irq_save(void) {
  uint32_t m;
  __asm__ volatile("csrrci %0, mstatus, 8" : "=r"(m) : : "memory");
  return m & MSTATUS_MIE;
}
static inline void r32v_irq_restore(uint32_t m) {
  if (m) __asm__ volatile("csrsi mstatus, 8" : : : "memory");
}

#endif /* R32V_H */
