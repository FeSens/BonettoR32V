/* Stimulus constants of the peripheral test programs, shared by the programs (C and assembly) and
 * by mkexpect.py, which parses the `#define NAME value` lines below to write expect.json (the
 * harness's stimulus and expectations) and <name>.stim (progsim's). One value per line: a decimal
 * or hex integer, or a string literal.
 *
 * Times are clock cycles (40 MHz, contract 7) counted from the first retirement. Every schedule
 * leaves room for the slowest legal core: 2,048 cycles per instruction (the contract-4 cap). The
 * seed takes about 800.
 *
 * ui[1] is the UART RX line: scripts keep it high (idle) except in gpio_edge, which toggles every
 * pin and sets the UART divider first so stray frames stay in spec.
 */
#ifndef STIM_H
#define STIM_H

/* ---- uart_echo, uart_echo_fast: 1 Mbaud, 16 echoed bytes, then an overrun ------------------ */
#define UART_DIV 39               /* bit time 40 cycles: 1 Mbaud at 40 MHz */
#define UART_T0 400000            /* first RX frame starts here (after "hello") */
#define UART_GAP 90000            /* between RX frame starts: one echo is ~33 instructions */
#define UART_N 16
#define UART_MSG "Hello, BonettoR!"
#define UART_OVR_T 2000000        /* UART_KEEP arrives here, UART_DROP UART_GAP later */
#define UART_KEEP 0x4B            /* 'K': received while RX_FULL = 0, read after the overrun */
#define UART_DROP 0x44            /* 'D': arrives while RX_FULL = 1, dropped (RX_OVR = 1) */
#define UART_PPM 10000            /* uart_echo: +1 % bit time; uart_echo_fast: -1 % */
#define UART_HELLO "hello"        /* sent first, before any RX: the gate-level test decodes it */

/* ---- spi_loop: echo device (returns the previous byte), D = 1 ------------------------------- */
#define SPI_DIV_D 1
#define SPI_N 16                  /* bytes 0x00..0x0F, then one more to read 0x0F back */

/* ---- pwm: three channels and the timer in PWM mode ----------------------------------------- */
#define PWM_PERIOD 99
#define PWM_PRESCALE 1
#define PWM_DUTY_0 0
#define PWM_DUTY_1 50
#define PWM_DUTY_2 120            /* above PER: always 1 */
#define PWM_TMR_P 0
#define PWM_TMR_CMP 300           /* uo7 high while CNT < 300, of every 65,536 cycles */
#define PWM_HOLD 4                /* timer compare matches the program waits after MARK_PWM */

/* ---- ir_loop: timer toggle output on uo7 looped back to ui0, capture --------------------- */
#define IR_P 4                    /* a tick every 16 cycles */
#define IR_CMP_ABS 500            /* phase A: one match, no auto-reload: CAP = CMP + 1 (+-1) */
#define IR_CMP 2999               /* phase B: auto-reload, an edge every 3,000 ticks */
#define IR_CAPTURES 5             /* phase B captures: 4 deltas of CMP + 1 ticks (+-1) */
#define IR_HALF 6000              /* phase C: free-running, CMP advanced by IR_HALF at each match */
#define IR_EDGES 5                /* phase C captures: 4 deltas of IR_HALF ticks (+-1) */

/* ---- irq_timer ------------------------------------------------------------------------- */
#define IT_CMP 499                /* auto-reload at 499: a match every 500 cycles (P = 0) */
#define IT_COUNT 20

/* ---- gpio_edge: per-pin rising / falling flags, W1C ---------------------------------------- */
#define GE_T0 300000              /* stage 1 (GE_V1); stage k at GE_T0 + (k - 1) * GE_GAP */
#define GE_GAP 250000             /* a stage takes up to ~100 instructions */
#define GE_H 0x72                 /* pins high at the start (1, 4, 5, 6) */
#define GE_L 0x8D                 /* pins low at the start (0, 2, 3, 7) */
#define GE_V0 0x72                /* ui at cycle 0: GE_H */
#define GE_V1 0xFF                /* phase 1 (L rising, H falling): L rises -> flags GE_L */
#define GE_V2 0x8D                /* H falls -> flags GE_H */
#define GE_V3 0x00                /* L falls (rising polarity) -> no flag */
#define GE_V4 0x72                /* phase 2 (L falling, H rising): H rises -> flags GE_H */
#define GE_V5 0xFF                /* L rises (falling polarity) -> no flag */
#define GE_V6 0x00                /* L falls -> flags GE_L; H falls (rising polarity) -> none */
#define GE_V7 0xFF                /* phase 3 (enables 0x55, rising): flags 0x55 */
#define GE_EN3 0x55

/* ---- wfi: pulses on ui4 ------------------------------------------------------------------ */
#define WFI_PIN 4
#define WFI_T0 200000             /* first rising edge; then one every WFI_PERIOD */
#define WFI_PERIOD 100000         /* high for half of it */
#define WFI_PULSES 16

#endif /* STIM_H */
