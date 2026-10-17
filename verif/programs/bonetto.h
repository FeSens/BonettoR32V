/* BonettoR32V platform definitions for the test programs (docs/contract-v3.md sections 3-5).
 * Usable from C and from assembly (.S files include it too: only #defines outside the C part).
 *
 * IO: 32-bit slots at 0x8000_0000 + 4n (slot = addr[6:2], other address bits ignored). A load
 * returns the whole slot on its byte lanes; a store writes only the bytes it covers. Accesses
 * that aren't naturally aligned are UNSPECIFIED (a halfword at slot byte 2 is fine).
 *
 * Program end (contract 4): the program writes GPIO_OUT = RESULT_PASS (0x5A) and retires
 * `jal x0, 0` twice at the same pc. A failed check or an unexpected trap writes RESULT_FAIL
 * (0xEE) instead, then self-loops too. crt0.S provides _pass, _fail and _default_trap.
 */
#ifndef BONETTO_H
#define BONETTO_H

#define IO_BASE 0x80000000
#define RESULT_PASS 0x5A
#define RESULT_FAIL 0xEE
#define MARK_PWM 0xA5      /* pwm: the harness measures the PWM pins after this GPIO_OUT value */
#define MARK_CM_START 0xC0 /* coremark: timed iterations start */
#define MARK_CM_STOP 0xC1  /* coremark: timed iterations end */

/* Slot numbers (contract 5.1) */
#define SLOT_GPIO_OUT 0
#define SLOT_GPIO_IN 1
#define SLOT_ALTFN 2
#define SLOT_TIMER_CNT 3
#define SLOT_TIMER_CMP 4
#define SLOT_TIMER_CAP 5
#define SLOT_TIMER_CTRL 6
#define SLOT_PWM_CTRL 7
#define SLOT_PWM_DUTY0 8
#define SLOT_PWM_DUTY1 9
#define SLOT_PWM_DUTY2 10
#define SLOT_UART_DATA 11
#define SLOT_UART_CTRL 12
#define SLOT_SPI_DATA 13
#define SLOT_SPI_CTRL 14
#define SLOT_IRQ_EN 15
#define SLOT_IRQ_PEND 16
#define SLOT_GPIO_EDGE 17
#define SLOT_GPIO_EPEND 18

/* Byte offsets from IO_BASE (for `lw t0, OFS_x(base)` in assembly) */
#define OFS_GPIO_OUT (4 * SLOT_GPIO_OUT)
#define OFS_GPIO_IN (4 * SLOT_GPIO_IN)
#define OFS_ALTFN (4 * SLOT_ALTFN)
#define OFS_TIMER_CNT (4 * SLOT_TIMER_CNT)
#define OFS_TIMER_CMP (4 * SLOT_TIMER_CMP)
#define OFS_TIMER_CAP (4 * SLOT_TIMER_CAP)
#define OFS_TIMER_CTRL (4 * SLOT_TIMER_CTRL)
#define OFS_PWM_CTRL (4 * SLOT_PWM_CTRL)
#define OFS_PWM_DUTY0 (4 * SLOT_PWM_DUTY0)
#define OFS_PWM_DUTY1 (4 * SLOT_PWM_DUTY1)
#define OFS_PWM_DUTY2 (4 * SLOT_PWM_DUTY2)
#define OFS_UART_DATA (4 * SLOT_UART_DATA)
#define OFS_UART_CTRL (4 * SLOT_UART_CTRL)
#define OFS_SPI_DATA (4 * SLOT_SPI_DATA)
#define OFS_SPI_CTRL (4 * SLOT_SPI_CTRL)
#define OFS_IRQ_EN (4 * SLOT_IRQ_EN)
#define OFS_IRQ_PEND (4 * SLOT_IRQ_PEND)
#define OFS_GPIO_EDGE (4 * SLOT_GPIO_EDGE)
#define OFS_GPIO_EPEND (4 * SLOT_GPIO_EPEND)

/* TIMER_CTRL fields */
#define TMR_P(p) (p)               /* [3:0] prescale: a tick every 2^P cycles */
#define TMR_EN (1 << 4)
#define TMR_AUTO (1 << 5)          /* auto-reload: CNT = 0 after the match */
#define TMR_OUT_LOW (0 << 6)
#define TMR_OUT_TOGGLE (1 << 6)
#define TMR_OUT_PWM (2 << 6)       /* high while CNT < CMP */
#define TMR_CAP_OFF (0 << 8)
#define TMR_CAP_RISE (1 << 8)
#define TMR_CAP_FALL (2 << 8)
#define TMR_CAP_BOTH (3 << 8)

/* PWM_CTRL fields */
#define PWM_PER(n) (n)             /* [9:0] */
#define PWM_Q(q) ((q) << 10)       /* [12:10] */
#define PWM_EN (1 << 13)
#define PWM_IRGATE (1 << 14)

/* UART_CTRL and SPI_CTRL bits */
#define UART_DIV_MASK 0x1FFF
#define UART_TX_BUSY (1 << 13)
#define UART_RX_FULL (1 << 14)
#define UART_RX_OVR (1 << 15)
#define SPI_CS_N 1
#define SPI_D(d) ((d) << 1)        /* [3:1]: SCK low 2^D cycles, then high 2^D cycles */
#define SPI_BUSY (1 << 4)

/* IRQ_EN / IRQ_PEND sources */
#define IRQ_CMP (1 << 0)           /* W1C in IRQ_PEND */
#define IRQ_CAP (1 << 1)           /* W1C in IRQ_PEND */
#define IRQ_RX (1 << 2)            /* = RX_FULL */
#define IRQ_TXIDLE (1 << 3)        /* = !TX_BUSY */
#define IRQ_GPIO (1 << 4)          /* = OR of GPIO_EPEND */

/* ALTFN pins (contract 5.2) */
#define ALT_UART_TX (1 << 0)
#define ALT_PWM0 (1 << 1)
#define ALT_PWM1 (1 << 2)
#define ALT_PWM2 (1 << 3)
#define ALT_SPI_CS (1 << 4)
#define ALT_SPI_SCK (1 << 5)
#define ALT_SPI_MOSI (1 << 6)
#define ALT_TIMER (1 << 7)

/* GPIO_EDGE: [7:0] enables, [15:8] polarity (1 = falling) */
#define EDGE_EN(pins) (pins)
#define EDGE_FALL(pins) ((pins) << 8)

/* CSRs (contract 4) */
#define MSTATUS_MIE (1 << 3)
#define MSTATUS_MPIE (1 << 7)
#define MSTATUS_MPP (3 << 11)
#define MIE_MEIE (1 << 11)
#define MIP_MEIP (1 << 11)
#define MCAUSE_EXT_IRQ 0x8000000B
#define MCAUSE_MISALIGNED_FETCH 0
#define MCAUSE_ILLEGAL 2
#define MCAUSE_BREAKPOINT 3
#define MCAUSE_ECALL 11

#ifndef __ASSEMBLER__
#include <stdint.h>

#define IO_REG(slot) (*(volatile uint32_t *)(IO_BASE + 4 * (slot)))
#define GPIO_OUT IO_REG(SLOT_GPIO_OUT)
#define GPIO_IN IO_REG(SLOT_GPIO_IN)
#define ALTFN IO_REG(SLOT_ALTFN)
#define TIMER_CNT IO_REG(SLOT_TIMER_CNT)
#define TIMER_CMP IO_REG(SLOT_TIMER_CMP)
#define TIMER_CAP IO_REG(SLOT_TIMER_CAP)
#define TIMER_CTRL IO_REG(SLOT_TIMER_CTRL)
#define PWM_CTRL IO_REG(SLOT_PWM_CTRL)
#define PWM_DUTY(i) IO_REG(SLOT_PWM_DUTY0 + (i))
#define UART_DATA IO_REG(SLOT_UART_DATA)
#define UART_CTRL IO_REG(SLOT_UART_CTRL)
#define SPI_DATA IO_REG(SLOT_SPI_DATA)
#define SPI_CTRL IO_REG(SLOT_SPI_CTRL)
#define IRQ_EN IO_REG(SLOT_IRQ_EN)
#define IRQ_PEND IO_REG(SLOT_IRQ_PEND)
#define GPIO_EDGE IO_REG(SLOT_GPIO_EDGE)
#define GPIO_EPEND IO_REG(SLOT_GPIO_EPEND)
/* Byte 0 of a slot (a byte access; reading byte 0 of UART_DATA clears RX_FULL and RX_OVR). */
#define IO_REG8(slot) (*(volatile uint8_t *)(IO_BASE + 4 * (slot)))

#define CSR_READ(csr)                                          \
  ({                                                           \
    uint32_t v_;                                               \
    __asm__ volatile("csrr %0, " #csr : "=r"(v_) : : "memory"); \
    v_;                                                        \
  })
#define CSR_WRITE(csr, v) __asm__ volatile("csrw " #csr ", %0" : : "r"((uint32_t)(v)) : "memory")
#define CSR_SET(csr, v) __asm__ volatile("csrs " #csr ", %0" : : "r"((uint32_t)(v)) : "memory")
#define CSR_CLEAR(csr, v) __asm__ volatile("csrc " #csr ", %0" : : "r"((uint32_t)(v)) : "memory")
/* mstatus.MIE on / off with the immediate forms (one instruction each). */
#define IRQ_ON() __asm__ volatile("csrsi mstatus, 8" : : : "memory")
#define IRQ_OFF() __asm__ volatile("csrci mstatus, 8" : : : "memory")
#define WFI() __asm__ volatile("wfi" : : : "memory")
#define NOP() __asm__ volatile("nop")

/* crt0.S: fail with an id (GPIO_OUT = id, then RESULT_FAIL, then a self-loop). */
void _fail(uint32_t id) __attribute__((noreturn));
#define CHECK(cond, id)      \
  do {                       \
    if (!(cond)) _fail(id);  \
  } while (0)

/* Poll until cond holds, at most n times, else fail with id. */
#define WAIT_UNTIL(cond, n, id)                    \
  do {                                             \
    uint32_t w_ = (n);                             \
    while (!(cond)) {                              \
      if (--w_ == 0) _fail(id);                    \
    }                                              \
  } while (0)

/* PSRAM log ring at 0x017F0000-0x017F07FF (NUL-terminated text). */
extern char _log_ring[];
extern char _log_ring_end[];
#endif /* __ASSEMBLER__ */

#endif /* BONETTO_H */
