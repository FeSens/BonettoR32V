/* BonettoR32V test programs: one 64 KB SPI RAM, execution starts at pc = 0.
 *
 *   0x0000 .. _end     code, rodata, data, bss
 *   0xF000 .. 0xF7FF   RAM log ring (CoreMark output; NUL-terminated text)
 *   0xF800 .. 0xFF7F   stack (grows down from 0xFF80)
 *   0xFF80 .. 0xFFFF   core region (contract v2 section 3): the core's private state; programs
 *                      never touch it
 *   0x80000000         GPIO (bonetto.h)
 *
 * RAM_BASE (default 0) relocates the whole layout. Only the spike cross-check uses it
 * (RAM_BASE = 0x10000), because spike's debug module occupies [0, 0x1000).
 */
OUTPUT_ARCH(riscv)
ENTRY(_start)

RAM_BASE = DEFINED(RAM_BASE) ? RAM_BASE : 0;

SECTIONS
{
  . = RAM_BASE;
  .text : {
    KEEP(*(.text.start))
    *(.text .text.*)
  }
  .rodata : ALIGN(4) { *(.rodata .rodata.* .srodata .srodata.*) }
  .data : ALIGN(4) { *(.data .data.* .sdata .sdata.*) }
  .bss (NOLOAD) : ALIGN(4) {
    _bss_start = .;
    *(.bss .bss.* .sbss .sbss.* COMMON)
    . = ALIGN(4);
    _bss_end = .;
  }
  _end = .;

  /* Random-stream programs (gen_random.py) preload their data window 0x8000-0x8FFF here.
   * ld reports an overlap if the program itself reaches 0x8000. */
  .window (RAM_BASE + 0x8000) : { KEEP(*(.window)) }

  _log_ring = RAM_BASE + 0xF000;
  _log_ring_end = RAM_BASE + 0xF800;
  _stack_top = RAM_BASE + 0xFF80;  /* the core region starts here (contract v2 section 3) */

  ASSERT(_end <= _log_ring, "program too large: it must end below 0xF000 (log ring)")

  /DISCARD/ : { *(.comment) *(.note .note.*) *(.eh_frame .eh_frame_hdr) }
}
