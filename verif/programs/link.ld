/* BonettoR32V test programs, contract v3 (docs/contract-v3.md section 3): code runs from the QSPI
 * Pmod's flash, data lives in its PSRAM. Execution starts at pc = 0 after reset.
 *
 * Flash (CPU 0x0000_0000, read-only; the image is loaded before reset):
 *   0x0000_0000 ..        .text, .rodata, then the load image of .data (and of .window)
 *   0x0001_0000 .. +4 KiB .fwindow: random-stream programs' flash data window (gen_random.py)
 *   0x000F_8000 ..        .farcode: code reached by JAL offsets with J-imm bits 16-20 (selftest)
 * PSRAM (CPU 0x0100_0000 + byte, starts arbitrary: crt0 copies .data and clears .bss):
 *   0x0100_0000 ..        .data (copied from flash by crt0), then .bss (cleared by crt0)
 *   0x0100_8000 .. +1 KiB .window: random-stream programs' data window (copied by their prologue)
 *   0x017F_0000 .. 0x017F_07FF  log ring (CoreMark output; NUL-terminated text)
 *   0x017F_0800 .. 0x017F_FF7F  stack (grows down from _stack_top)
 *   0x017F_FF80 .. 0x017F_FFFF  core region (contract 3): the core's private state; programs
 *                               never touch it
 * IO (CPU 0x8000_0000, slot addr[6:2]): bonetto.h.
 */
OUTPUT_ARCH(riscv)
ENTRY(_start)

SECTIONS
{
  . = 0;
  .text : {
    KEEP(*(.text.start))
    *(.text .text.*)
  }
  .rodata : ALIGN(4) { *(.rodata .rodata.* .srodata .srodata.*) }
  . = ALIGN(4);
  _etext = .;

  /* Random-stream programs (gen_random.py): the flash data window. The load images of .data and
   * .window still start at _etext, below it (asserted). */
  .fwindow 0x00010000 : { KEEP(*(.fwindow)) }

  /* selftest: a JAL target about 1 MiB away (offset bits 16-20 of the J immediate). */
  .farcode 0x000F8000 : { KEEP(*(.farcode)) }

  .data 0x01000000 : AT(_etext) ALIGN(4) {
    _data_start = .;
    *(.data .data.* .sdata .sdata.*)
    . = ALIGN(4);
    _data_end = .;
  }
  _data_lma = LOADADDR(.data);

  .bss (NOLOAD) : ALIGN(4) {
    _bss_start = .;
    *(.bss .bss.* .sbss .sbss.* COMMON)
    . = ALIGN(4);
    _bss_end = .;
  }

  /* Random-stream programs (gen_random.py): the data window in PSRAM, with its initial bytes in
   * flash (the program's prologue copies them). */
  .window 0x01008000 : AT(_data_lma + SIZEOF(.data)) {
    _window_start = .;
    KEEP(*(.window))
    _window_end = .;
  }
  _window_lma = LOADADDR(.window);
  _flash_end = _window_lma + SIZEOF(.window);

  _log_ring = 0x017F0000;
  _log_ring_end = 0x017F0800;
  _stack_top = 0x017FFF80;  /* the core region starts here (contract 3) */

  ASSERT(_bss_end <= _log_ring, "data and bss must end below the log ring (0x017F0000)")
  ASSERT(SIZEOF(.window) == 0 || _bss_end <= 0x01008000, "data and bss overlap the data window")
  ASSERT(SIZEOF(.fwindow) == 0 || _flash_end <= 0x00010000, "flash image overlaps .fwindow")
  ASSERT(SIZEOF(.farcode) == 0 || _flash_end <= 0x000F8000, "flash image overlaps .farcode")
  ASSERT(_flash_end <= 0x01000000, "flash image larger than 16 MiB")

  /DISCARD/ : { *(.comment) *(.note .note.*) *(.eh_frame .eh_frame_hdr) }
}
