/* Seed unit-test programs (contract v3 section 3): code and rodata in flash at 0, .data copied
 * by crt0 from flash to PSRAM 0x0100_0000, .bss after it, stack top 0x017F_FF80 (the core
 * region 0x017F_FF80-0x017F_FFFF is the core's). */
OUTPUT_ARCH(riscv)
ENTRY(_start)
MEMORY {
  FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 16M
  PSRAM (rwx) : ORIGIN = 0x01000000, LENGTH = 0x7F0000
}
SECTIONS
{
  .text : { KEEP(*(.text.start)) *(.text .text.*) } > FLASH
  .rodata : ALIGN(4) { *(.rodata .rodata.* .srodata .srodata.*) } > FLASH
  .data : ALIGN(4) {
    _data_start = .;
    *(.data .data.* .sdata .sdata.*)
    . = ALIGN(4);
    _data_end = .;
  } > PSRAM AT > FLASH
  _data_load = LOADADDR(.data);
  .bss (NOLOAD) : ALIGN(4) {
    _bss_start = .;
    *(.bss .bss.* .sbss .sbss.* COMMON)
    . = ALIGN(4);
    _bss_end = .;
  } > PSRAM
  _stack_top = 0x017FFF80;
  /DISCARD/ : { *(.comment) *(.note .note.*) *(.eh_frame .eh_frame_hdr) *(.riscv.attributes) }
}
