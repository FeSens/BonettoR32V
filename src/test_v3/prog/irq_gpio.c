/* irq_gpio (run with --loop): GPIO edge interrupts on ui2 (<- uo1), raised by the main loop's
 * own GPIO writes in a stream of taken branches, loads and stores. The handler checks mcause,
 * that mepc lies inside the main loop, clears GPIO_EPEND (W1C) and counts. Then WFI (a no-op or
 * a stall until MEIP & MEIE) with MIE = 0 falls through on a pending edge, and an MRET-adjacent
 * re-enable takes a pending interrupt. */
#include "bonetto3.h"

static volatile int count, bad;
static volatile uint32_t buf[8];
extern char loop_start[], loop_end[];

void __attribute__((interrupt("machine"), aligned(4))) isr(void) {
  uint32_t epc = csr_read(mepc);
  if (csr_read(mcause) != 0x8000000Bu) bad = 1;
  if (epc < (uint32_t)loop_start || epc >= (uint32_t)loop_end) bad = 2;
  if (!(GPIO_EPEND & (1u << 2))) bad = 3;
  GPIO_EPEND = 1u << 2;
  count++;
}

int main(void) {
  csr_write(mtvec, (uint32_t)isr);
  GPIO_EDGE = 1u << 2;          /* ui2 rising */
  IRQ_EN = IRQ_GPIO;
  csr_set(mie, MIE_MEIE);
  csr_set(mstatus, MSTATUS_MIE);
  uint32_t acc = 0;
  __asm__ volatile(".global loop_start\nloop_start:");
  for (int i = 0; i < 12; i++) {
    GPIO_OUT = 2;
    for (int k = 0; k < 3; k++) {
      buf[k] = acc + (uint32_t)k;
      acc += buf[(k + 1) & 7];
      if (acc & 1) acc ^= 0x55;
    }
    GPIO_OUT = 0;
  }
  __asm__ volatile(".global loop_end\nloop_end:");
  CHECK(!bad);
  CHECK(count == 12);
  /* WFI with MIE = 0 falls through on a pending edge; the interrupt then waits for MIE */
  csr_clear(mstatus, MSTATUS_MIE);
  GPIO_OUT = 2;
  __asm__ volatile("wfi");
  spin(1);
  CHECK(count == 12);
  CHECK(csr_read(mip) & 0x800);
  GPIO_OUT = 0;
  csr_set(mstatus, MSTATUS_MIE);
  __asm__ volatile("nop; nop; nop");
  CHECK(count == 13);
  csr_clear(mstatus, MSTATUS_MIE);
  IRQ_EN = 0;
  GPIO_EDGE = 0;
  return 0;
}
