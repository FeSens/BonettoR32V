/* periph_regs (run with --loop): every slot's reset value, read/write masks, byte lanes and
 * sign extension, address aliasing, read-only slots, W1C bits (IRQ_PEND.CMP/CAP,
 * GPIO_EPEND), unimplemented slots, and the immediate BUSY flags. */
#include "bonetto3.h"

static const uint32_t reset_val[32] = {
    [14] = 1,      /* SPI_CTRL: CS_n = 1 */
    [16] = 8,      /* IRQ_PEND: TXIDLE = !TX_BUSY */
};

#define ALIAS(n, a) (*(volatile uint32_t *)(0x80000000u + (a) + 4u * (n)))

int main(void) {
  /* reset values (crt0 touches no IO) */
  for (int n = 0; n < 32; n++) CHECK(REG(n) == reset_val[n]);

  /* read/write masks */
  GPIO_OUT = 0xFFFFFFFF; CHECK(GPIO_OUT == 0xFF); GPIO_OUT = 0; CHECK(GPIO_OUT == 0);
  ALTFN = 0xFFFFFFFF;    CHECK(ALTFN == 0xFF);    ALTFN = 0;    CHECK(ALTFN == 0);
  TIMER_CNT = 0xFFFFFFFF; CHECK(TIMER_CNT == 0xFFFF); TIMER_CNT = 0x1234; CHECK(TIMER_CNT == 0x1234);
  TIMER_CMP = 0xFFFFFFFF; CHECK(TIMER_CMP == 0xFFFF);
  TIMER_CTRL = ~(1u << 4); CHECK(TIMER_CTRL == 0x3EF); TIMER_CTRL = 0; CHECK(TIMER_CTRL == 0);
  PWM_CTRL = ~(1u << 13); CHECK(PWM_CTRL == 0x5FFF); PWM_CTRL = 0;
  PWM_DUTY0 = 0xFFFFFFFF; CHECK(PWM_DUTY0 == 0x3FF);
  PWM_DUTY1 = 0xFFFFFFFF; CHECK(PWM_DUTY1 == 0x3FF);
  PWM_DUTY2 = 0xFFFFFFFF; CHECK(PWM_DUTY2 == 0x3FF);
  UART_CTRL = 0xFFFFFFFF; CHECK(UART_CTRL == 0x1FFF);
  SPI_CTRL = 0xFFFFFFFE; CHECK(SPI_CTRL == 0xE);
  SPI_CTRL = 0xFFFFFFFF; CHECK(SPI_CTRL == 0xF);
  IRQ_EN = 0xFFFFFFFF; CHECK(IRQ_EN == 0x1F); IRQ_EN = 0;
  GPIO_EDGE = 0xFFFFFFFF; CHECK(GPIO_EDGE == 0xFFFF); GPIO_EDGE = 0;
  /* read-only slots ignore writes */
  GPIO_IN = 0xFFFFFFFF; CHECK(GPIO_IN == 0);
  TIMER_CAP = 0xFFFFFFFF; CHECK(TIMER_CAP == 0);
  GPIO_EPEND = 0xFFFFFFFF; CHECK(GPIO_EPEND == 0);
  IRQ_PEND = 0xFFFFFFFF; CHECK(IRQ_PEND == 8);
  /* unimplemented slots */
  for (int n = 19; n < 32; n++) { REG(n) = 0xFFFFFFFF; CHECK(REG(n) == 0); }

  /* byte lanes */
  TIMER_CMP = 0x1234;
  REGB(4, 1) = 0xAB; CHECK(TIMER_CMP == 0xAB34);
  REGB(4, 0) = 0xCD; CHECK(TIMER_CMP == 0xABCD);
  REGB(4, 2) = 0xEE; CHECK(TIMER_CMP == 0xABCD);
  REGH(4, 0) = 0x80FF; CHECK(TIMER_CMP == 0x80FF);
  REGH(4, 1) = 0x5555; CHECK(TIMER_CMP == 0x80FF);
  CHECK(REGSB(4, 0) == -1);
  CHECK(REGB(4, 0) == 0xFF);
  CHECK(REGSB(4, 1) == (int8_t)0x80);
  CHECK(REGB(4, 1) == 0x80);
  CHECK(REGSB(4, 2) == 0);
  CHECK(REGSH(4, 0) == (int16_t)0x80FF);
  CHECK(REGH(4, 0) == 0x80FF);
  CHECK(REGH(4, 1) == 0);
  UART_CTRL = 0;
  REGB(12, 1) = 0xFF; CHECK(UART_CTRL == 0x1F00);
  REGB(12, 0) = 0x27; CHECK(UART_CTRL == 0x1F27);
  REGB(7, 1) = 0x7F; CHECK(PWM_CTRL == 0x7F00);   /* bits 14:8 */
  PWM_CTRL = 0;
  REGB(17, 1) = 0xA5; CHECK(GPIO_EDGE == 0xA500);
  REGB(17, 0) = 0x3C; CHECK(GPIO_EDGE == 0xA53C);
  GPIO_EDGE = 0;

  /* aliasing: address bits outside [6:2] (and above bit 31's IO select) are ignored */
  ALIAS(4, 0x100) = 0x4321; CHECK(TIMER_CMP == 0x4321);
  CHECK(ALIAS(4, 0x3F000080) == 0x4321);
  CHECK(*(volatile uint32_t *)0xC0000010u == 0x4321);

  /* IRQ_PEND.CMP: set on a compare tick, W1C */
  TIMER_CMP = 5;
  TIMER_CNT = 0;
  TIMER_CTRL = (1u << 4) | (1u << 5);   /* enable, auto-reload, P = 0 */
  CHECK(IRQ_PEND & IRQ_CMP);
  CHECK(TIMER_CNT <= 5);
  TIMER_CTRL = 0;
  IRQ_PEND = 0; CHECK(IRQ_PEND & IRQ_CMP);          /* writing 0 doesn't clear */
  IRQ_PEND = IRQ_CAP; CHECK(IRQ_PEND & IRQ_CMP);    /* nor does a 1 elsewhere */
  REGB(16, 1) = 0xFF; CHECK(IRQ_PEND & IRQ_CMP);    /* nor byte 1 */
  IRQ_PEND = IRQ_CMP; CHECK(!(IRQ_PEND & IRQ_CMP));

  /* IRQ_PEND.CAP: capture on ui0 (looped from uo7, GPIO mode) rising edge */
  TIMER_CNT = 0x777;
  TIMER_CTRL = 1u << 8;                  /* capture rising, counter disabled */
  GPIO_OUT = 0x80;
  spin(1);
  CHECK(IRQ_PEND & IRQ_CAP);
  CHECK(TIMER_CAP == 0x777);
  IRQ_PEND = IRQ_CAP; CHECK(!(IRQ_PEND & IRQ_CAP));
  GPIO_OUT = 0;                          /* falling edge: not selected */
  spin(1);
  CHECK(!(IRQ_PEND & IRQ_CAP));
  TIMER_CTRL = 2u << 8;                  /* falling */
  TIMER_CNT = 0x999;
  GPIO_OUT = 0x80; spin(1); CHECK(!(IRQ_PEND & IRQ_CAP));
  GPIO_OUT = 0; spin(1); CHECK(IRQ_PEND & IRQ_CAP); CHECK(TIMER_CAP == 0x999);
  IRQ_PEND = IRQ_CAP;
  TIMER_CTRL = 0;

  /* GPIO edges: ui2 <- uo1 (rising enabled), ui4 <- uo2 (falling enabled), ui5 <- uo3 (off) */
  GPIO_EDGE = (1u << 2) | (1u << 4) | (1u << (8 + 4));
  GPIO_OUT = 0x0E;                       /* uo1, uo2, uo3 rise */
  spin(1);
  CHECK(GPIO_EPEND == (1u << 2));
  CHECK(IRQ_PEND & IRQ_GPIO);
  GPIO_OUT = 0;                          /* fall */
  spin(1);
  CHECK(GPIO_EPEND == ((1u << 2) | (1u << 4)));
  GPIO_EPEND = 1u << 4; CHECK(GPIO_EPEND == (1u << 2));
  GPIO_EPEND = 0; CHECK(GPIO_EPEND == (1u << 2));
  GPIO_EPEND = 0xFF; CHECK(GPIO_EPEND == 0);
  CHECK(!(IRQ_PEND & IRQ_GPIO));
  CHECK(GPIO_IN == 0);
  GPIO_OUT = 0x12; spin(1);              /* uo1 -> ui2, uo4 -> ui6 */
  CHECK(GPIO_IN == ((1u << 2) | (1u << 6)));
  GPIO_OUT = 0;
  GPIO_EDGE = 0;
  GPIO_EPEND = 0xFF;

  /* UART_DATA and SPI_DATA writes set BUSY for the next instruction */
  UART_CTRL = 1000;                /* a frame outlasts an instruction */
  UART_DATA = 0x41;
  CHECK(UART_CTRL & UART_TX_BUSY);
  CHECK(!(IRQ_PEND & IRQ_TXIDLE));
  while (UART_CTRL & UART_TX_BUSY) { }
  CHECK(IRQ_PEND & IRQ_TXIDLE);
  SPI_CTRL = (7u << 1) | 1u;
  SPI_DATA = 0x5A;
  CHECK(SPI_CTRL & SPI_BUSY);
  while (SPI_CTRL & SPI_BUSY) { }
  CHECK((SPI_CTRL & SPI_BUSY) == 0);

  for (int n = 0; n < 32; n++)
    if (n != 3 && n != 4 && n != 5 && n != 11 && n != 12 && n != 13 && n != 14 && n != 16 && n < 19) REG(n) = 0;
  return 0;
}
