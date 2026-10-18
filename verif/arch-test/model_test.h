// riscv-arch-test target model for BonettoR32V, contract v3 (docs/contract.md; docs/arch-test.md).
// The contract v2 model (EBREAK halt, 64 KB RAM at 0x10000) was deleted at the integration swap
// (plan Task 9, ruling R26).
//
// One ELF serves both simulators, linked by verif/programs/link.ld with .text moved to 0x1000 plus
// run_arch.py's .signature script:
//   flash 0x0000_1000 ..  code (.text.init, rvtest_entry_point first), then the load images of
//                         .data and .signature
//   PSRAM 0x0100_0000 ..  .data: arch_test.h's trap save areas, the test's input data, tohost
//   PSRAM 0x0101_0000 ..  .signature: begin_signature .. end_signature
//   - spike (--disable-dtb) starts at its reset vector 0x1000 = rvtest_entry_point; its debug
//     module owns [0, 0x1000), so no code can sit at flash 0 in the ELF;
//   - the DUT starts at pc 0: run_arch.py adds `jal x0, 0x1000` at flash byte 0 of the DUT image
//     (the only bytes of the image that aren't the ELF's), so from there both run the same code
//     at the same addresses.
// RVMODEL_BOOT copies .data and .signature from flash (PSRAM starts arbitrary, contract 3),
// initializes the CSRs with arbitrary reset values that arch_test.h's trap prolog reads (mtvec and
// mscratch; mstatus, so MPIE is known), then zeroes x1..x15 (registers start unknown on the DUT).
// mtvec starts at rvmodel_trap: a trap the test doesn't expect writes tohost = 3 (spike exits
// with 1) and GPIO_OUT = 0xEE, then self-loops.
// RVMODEL_HALT writes tohost = 1 (spike's HTIF exit) and GPIO_OUT = 0x5A (the end marker: the
// pins-mode cosim sees the program reach its end at the pins), then ends the program with
// `jal x0, 0` retired twice at the same pc (contract 4). spike maps a scratch page at 0x8000_0000
// so the GPIO store is a plain store there.
// mtval reads 0 for every cause (contract 4), so SET_REL_TVAL_MSK is 0: arch_test.h's handler then
// stores 0 as each trap's tval signature word, on spike and on the DUT alike (the hook
// arch_test.h provides for implementations that leave xtval zero). mcause and the relocated mepc
// are still compared.
#ifndef BONETTO_MODEL_TEST_H
#define BONETTO_MODEL_TEST_H

#define RVMODEL_DATA_SECTION \
  .align 3; .global tohost; tohost: .dword 0; .global fromhost; fromhost: .dword 0;

#define SET_REL_TVAL_MSK 0

// RVMODEL_BOOT appears twice in every test (in the test and in RVTEST_CODE_BEGIN): .ifndef keeps
// the second one empty. The first one runs before arch_test.h's trap prolog.
#define RVMODEL_BOOT                                 \
  .ifndef rvmodel_boot_done;                         \
  .set rvmodel_boot_done, 1;                         \
  la x1, _data_lma;                                  \
  la x2, _data_start;                                \
  la x3, _data_end;                                  \
rvmodel_copy_data:                                   \
  bgeu x2, x3, rvmodel_copy_data_done;               \
  lw x4, 0(x1);                                      \
  sw x4, 0(x2);                                      \
  addi x1, x1, 4;                                    \
  addi x2, x2, 4;                                    \
  j rvmodel_copy_data;                               \
rvmodel_copy_data_done:                              \
  la x1, _sig_lma;                                   \
  la x2, _sig_start;                                 \
  la x3, _sig_end;                                   \
rvmodel_copy_sig:                                    \
  bgeu x2, x3, rvmodel_copy_sig_done;                \
  lw x4, 0(x1);                                      \
  sw x4, 0(x2);                                      \
  addi x1, x1, 4;                                    \
  addi x2, x2, 4;                                    \
  j rvmodel_copy_sig;                                \
rvmodel_copy_sig_done:                               \
  la x1, rvmodel_trap;                               \
  csrw mtvec, x1;                                    \
  csrw mscratch, x0;                                 \
  csrw mstatus, x0;                                  \
  li x1, 0; li x2, 0; li x3, 0; li x4, 0; li x5, 0;  \
  li x6, 0; li x7, 0; li x8, 0; li x9, 0; li x10, 0; \
  li x11, 0; li x12, 0; li x13, 0; li x14, 0;        \
  li x15, 0;                                         \
  .endif;

#define RVMODEL_HALT        \
  li x1, 1;                 \
  la x2, tohost;            \
  sw x1, 0(x2);             \
  lui x1, 0x80000;          \
  li x2, 0x5A;              \
  sw x2, 0(x1);             \
rvmodel_halt_loop:          \
  j rvmodel_halt_loop;      \
  .align 2;                 \
rvmodel_trap:               \
  li x1, 3;                 \
  la x2, tohost;            \
  sw x1, 0(x2);             \
  lui x1, 0x80000;          \
  li x2, 0xEE;              \
  sw x2, 0(x1);             \
rvmodel_trap_loop:          \
  j rvmodel_trap_loop;

#define RVMODEL_DATA_BEGIN               \
  RVMODEL_DATA_SECTION                   \
  .section .signature, "aw", @progbits;  \
  .align 4;                              \
  .global begin_signature;               \
begin_signature:

#define RVMODEL_DATA_END    \
  .align 4;                 \
  .global end_signature;    \
end_signature:

#define RVMODEL_IO_INIT
#define RVMODEL_IO_WRITE_STR(_R, _STR)
#define RVMODEL_IO_CHECK()
#define RVMODEL_IO_ASSERT_GPR_EQ(_S, _R, _I)
#define RVMODEL_IO_ASSERT_SFPR_EQ(_F, _R, _I)
#define RVMODEL_IO_ASSERT_DFPR_EQ(_D, _R, _I)

// No interrupts.
#define RVMODEL_SET_MSW_INT
#define RVMODEL_CLEAR_MSW_INT
#define RVMODEL_CLEAR_MTIMER_INT
#define RVMODEL_CLEAR_MEXT_INT
#define RVMODEL_SET_SSW_INT
#define RVMODEL_CLEAR_SSW_INT
#define RVMODEL_CLEAR_STIMER_INT
#define RVMODEL_CLEAR_SEXT_INT
#define RVMODEL_SET_VSW_INT
#define RVMODEL_CLEAR_VSW_INT
#define RVMODEL_CLEAR_VTIMER_INT
#define RVMODEL_CLEAR_VEXT_INT

#endif
