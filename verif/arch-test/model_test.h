// riscv-arch-test target model for BonettoR32V (docs/arch-test.md).
//
// One ELF serves both simulators. It is linked at 0x10000 (verif/programs/link.ld with
// RAM_BASE = 0x10000), because spike's debug module occupies [0, 0x1000):
//   - spike starts at its reset vector 0x1000, where RVMODEL_BOOT puts a jump to the test;
//   - the DUT starts at pc 0, which is image offset 0, i.e. the test's first instruction, and
//     fetches at pc[15:0]. RVMODEL_BOOT jumps to the test's linked address, so from then on the DUT
//     runs at 0x1xxxx like spike, and AUIPC/JAL results and data addresses agree.
// No CSRs, no trap handler (rvtest_mtrap_routine stays undefined). Registers start unknown on the
// DUT, so RVMODEL_BOOT zeroes x1..x15 (as spike does), since RVTEST_INIT_GPRS leaves x14 and x15
// to the test. RVMODEL_HALT writes tohost (spike's HTIF exit) and executes EBREAK (the DUT's halt;
// on spike it traps, and HTIF exits on the tohost write).
#ifndef BONETTO_MODEL_TEST_H
#define BONETTO_MODEL_TEST_H

#define RVMODEL_DATA_SECTION \
  .align 3; .global tohost; tohost: .dword 0; .global fromhost; fromhost: .dword 0;

// RVMODEL_BOOT appears twice in every test (in the test and in RVTEST_CODE_BEGIN): .ifndef keeps
// the second one empty.
#define RVMODEL_BOOT                                \
  .ifndef rvmodel_boot_done;                        \
  .set rvmodel_boot_done, 1;                        \
  .pushsection .tramp, "ax";                        \
  j rvtest_entry_point;                             \
  .popsection;                                      \
  li x1, 0; li x2, 0; li x3, 0; li x4, 0; li x5, 0; \
  li x6, 0; li x7, 0; li x8, 0; li x9, 0; li x10, 0; \
  li x11, 0; li x12, 0; li x13, 0; li x14, 0;       \
  lui x15, %hi(rvmodel_linked);                     \
  addi x15, x15, %lo(rvmodel_linked);               \
  jalr x0, 0(x15);                                  \
rvmodel_linked:                                     \
  li x15, 0;                                        \
  .endif;

#define RVMODEL_HALT        \
  li x1, 1;                 \
  la x2, tohost;            \
  sw x1, 0(x2);             \
  ebreak;                   \
rvmodel_halt_loop:          \
  j rvmodel_halt_loop;

#define RVMODEL_DATA_BEGIN  \
  RVMODEL_DATA_SECTION      \
  .align 4;                 \
  .global begin_signature;  \
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
