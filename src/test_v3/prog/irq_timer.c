/* irq_timer: timer compare interrupts (P = 0, CMP = 40000, auto-reload) counted by a machine
 * interrupt handler that checks mcause and clears IRQ_PEND.CMP (W1C). Then MIE masks a pending
 * interrupt (mip.MEIP reads 1) and re-enabling MIE takes it. TIMER_CAP stays 0 (no capture). */
#include "bonetto3.h"

static volatile int count, bad;

void __attribute__((interrupt("machine"), aligned(4))) isr(void) {
  if (csr_read(mcause) != 0x8000000Bu) bad = 1;
  if (csr_read(mstatus) & MSTATUS_MIE) bad = 2;
  if (IRQ_PEND & IRQ_CMP) {
    IRQ_PEND = IRQ_CMP;
    count++;
  } else {
    bad = 3;
  }
}

int main(void) {
  csr_write(mtvec, (uint32_t)isr);
  TIMER_CMP = 40000;
  TIMER_CNT = 0;
  TIMER_CTRL = (1u << 4) | (1u << 5);  /* P = 0, enable, auto-reload */
  IRQ_EN = IRQ_CMP;
  csr_set(mie, MIE_MEIE);
  csr_set(mstatus, MSTATUS_MIE);
  int work = 0;
  while (count < 8) work++;
  CHECK(!bad);
  CHECK(work > 0);
  csr_clear(mstatus, MSTATUS_MIE);
  int c0 = count;
  while (!(IRQ_PEND & IRQ_CMP)) { }
  spin(3);
  CHECK(count == c0);
  CHECK(csr_read(mip) & 0x800);
  csr_set(mstatus, MSTATUS_MIE);
  spin(2);
  CHECK(count > c0);
  CHECK(!bad);
  CHECK(TIMER_CAP == 0);
  csr_clear(mstatus, MSTATUS_MIE);
  TIMER_CTRL = 0;
  IRQ_EN = 0;
  return 0;
}
