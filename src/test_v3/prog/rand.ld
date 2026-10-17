/* v2 random streams (verif/programs/gen_random.py) on the v3 map: code at 0 and the data window
 * at 0x8000, both in flash; accesses whose address bit 24 is set land in PSRAM. */
OUTPUT_ARCH(riscv)
ENTRY(_start)
SECTIONS
{
  . = 0;
  .text : { KEEP(*(.text.start)) *(.text .text.*) }
  .window 0x8000 : { KEEP(*(.window)) }
  /DISCARD/ : { *(.comment) *(.note .note.*) *(.riscv.attributes) }
}
