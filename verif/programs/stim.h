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

/* ---- pwm10: the full 10-bit range, Q = 0, after a mid-run change ---------------------------- */
#define P10_PRE_PER 99            /* running before the change: period 100 cycles (<= the new one) */
#define P10_PRE_DUTY 50
#define P10_PER 1023              /* PER bits 8-9 set: period 1,024 cycles */
#define P10_Q 0
#define P10_DUTY_0 1              /* high 1 cycle of 1,024 */
#define P10_DUTY_1 512            /* duty bit 9 */
#define P10_DUTY_2 1023           /* = PER: high 1,023 of 1,024 */
#define P10_TMR_P 0
#define P10_TMR_CMP 0xC000        /* timer PWM mode, CMP bit 15: high 49,152 of 65,536 cycles */
#define P10_HOLD 6                /* timer compare matches the program waits after MARK_PWM */

/* ---- pwm10q: Q > 0, duty above PER and = PER, timer auto-reload at a high CMP ------------- */
#define P10Q_PRE_PER 1023         /* running before the change: Q = 1, period 2,048 cycles */
#define P10Q_PRE_Q 1
#define P10Q_PER 1000             /* PER decreases mid-run: period 1,001 x 8 = 8,008 cycles */
#define P10Q_Q 3
#define P10Q_DUTY_0 1023          /* above PER: always 1 */
#define P10Q_DUTY_1 1000          /* = PER: high 8,000 of 8,008 */
#define P10Q_DUTY_2 0x155         /* 341: bit 8 set, high 2,728 of 8,008 */
#define P10Q_TMR_P 1
#define P10Q_TMR_CMP 0xA000       /* PWM mode, auto-reload at 40,960: high 40,960 of 40,961 ticks */
#define P10Q_HOLD 6

/* ---- timer16: the 16-bit timer over its full range (uo7 looped back to ui0) ---------------- */
#define T16_CMP_A 0xBEEF          /* register readback with bit 15 set */
#define T16_CNT_A 0xA5C3
#define T16_CMP_AR 0xC350         /* auto-reload at 50,000 (P = 1) */
#define T16_AR_START 0xC300       /* the first reload 80 ticks after the start */
#define T16_AR_PERIODS 3
#define T16_CAP_P 3               /* 16-bit captures: a tick (8 cycles) outlasts the loop latency */
#define T16_CAP_CNT0 0x8000
#define T16_CAP_CMP1 0x8001       /* rising edge: CAP = 0x8002 (+-1) */
#define T16_CAP_CMP2 0xC3C3       /* falling edge: CAP = 0xC3C4 (+-1) */
#define T16_P15_DIV 8191          /* UART stopwatch: one frame = 81,920 cycles = 2.5 ticks at P = 15 */
#define T16_RS_DIV_A 2599         /* restart test: a 26,000-cycle frame after a tick ... */
#define T16_RS_DIV_B 1199         /* ... then a 12,000-cycle frame after the CNT write */
#define T16_RS_CNT0 0x7FF0
#define T16_RS_CNT 0x1234

/* ---- ir_gate: a 38 kHz carrier on PWM0, gated by the timer's toggle output (IR remote) ----- */
#define IRG_PER 657               /* Q = 1: period 1,316 cycles, 37.99 kHz at 50 MHz */
#define IRG_Q 1
#define IRG_DUTY 219              /* high 438 cycles: duty 1/3 */
#define IRG_P 8                   /* timer: a tick every 256 cycles */
#define IRG_LEAD 400              /* ticks from the start to the first mark (102,400 cycles) */
#define IRG_MARK 300              /* mark: 300 ticks = 76,800 cycles (58 carrier periods) */
#define IRG_SPACE0 300            /* spaces alternate 300 and 900 ticks (NEC-like 1:1 and 1:3) */
#define IRG_SPACE1 900
#define IRG_MARKS 8               /* marks the harness checks (expect.json ir_gate) */
#define IRG_EDGES 24              /* timer edges the program makes in all (12 marks) */
#define IRG_WITHIN 3000000        /* cycles after MARK_IR by which the 8 marks have passed (the
                                     8th ends 6,700 ticks = 1,715,200 cycles after the start) */
#define IRG_HOLD 4                /* edges with the gate off after MARK_PWM (pwm check of PWM0) */

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
