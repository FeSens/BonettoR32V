/* BonettoR32V SDK memory map (docs/contract.md section 3):
 *   Flash (CPU 0x0000_0000, 16 MiB, read-only): .text, .rodata, .init_array, then the load image
 *     of .data. The flash image (.bin) starts at flash byte 0, where the core starts after reset.
 *   PSRAM (CPU 0x0100_0000, 8 MiB): .data (copied by crt0), .bss (cleared by crt0), free space,
 *     then the stack growing down from 0x017F_FF80. PSRAM 0x7F_FF80-0x7F_FFFF (CPU 0x017F_FF80
 *     and up) is the core region: the CPU keeps its registers there; programs never touch it.
 *   IO (CPU 0x8000_0000): sdk/core/r32v.h.
 * No __global_pointer$: gp and tp belong to the trap handler (trap.S), so the linker must never
 * relax accesses to gp-relative ones.
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
  .init_array : ALIGN(4) {
    __init_array_start = .;
    KEEP(*(SORT(.init_array.*)))
    KEEP(*(.init_array .ctors))
    __init_array_end = .;
  }
  . = ALIGN(4);
  _etext = .;

  .data 0x01000000 : AT(_etext) ALIGN(4) {
    _data_start = .;
    *(.data .data.* .sdata .sdata.*)
    . = ALIGN(4);
    _data_end = .;
  }
  _data_lma = LOADADDR(.data);
  _flash_end = _data_lma + SIZEOF(.data);

  .bss (NOLOAD) : ALIGN(4) {
    _bss_start = .;
    *(.bss .bss.* .sbss .sbss.* COMMON)
    . = ALIGN(4);
    _bss_end = .;
  }

  _stack_top = 0x017FFF80;  /* the core region starts here */
  ASSERT(_bss_end <= 0x017F0000, "data and bss must leave room for the stack")
  ASSERT(_flash_end <= 0x01000000, "flash image larger than 16 MiB")

  /DISCARD/ : { *(.comment) *(.note .note.*) *(.eh_frame .eh_frame_hdr) }
}
