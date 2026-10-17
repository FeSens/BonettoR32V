#!/usr/bin/env python3
"""Convert an RV32 ELF into a sparse 16 MiB flash image for $readmemh (contract v3 section 3).

Usage: elf2hex.py IN.elf OUT.hex

Every loadable segment goes to flash at its load address (p_paddr: .data and .window sit in PSRAM
at run time, but their initial bytes are loaded into flash after the code, and crt0 / the random
prologue copies them). The output has one `@hhhhhhhh` record per segment (a byte address)
followed by its bytes, 16 hex bytes per line. Bytes the file doesn't set are erased flash (the
readers make them 0xFF). A segment outside the 16 MiB flash (load address bit 24 or above set)
is an error: the test loads only flash before reset.
"""
import argparse
import struct
import sys

FLASH = 1 << 24


def load_segments(path: str):
    """Yield (load address, bytes) for every PT_LOAD segment with file data."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise SystemExit(f"{path}: not a little-endian ELF32 file")
    phoff, = struct.unpack_from("<I", data, 28)
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    for i in range(phnum):
        p_type, p_offset, _vaddr, p_paddr, p_filesz, _memsz, _flags, _align = struct.unpack_from(
            "<8I", data, phoff + i * phentsize)
        if p_type == 1 and p_filesz:
            yield p_paddr, data[p_offset:p_offset + p_filesz]


def to_hex(segments) -> str:
    out = []
    used = []
    for addr, seg in sorted(segments):
        if addr + len(seg) > FLASH:
            raise SystemExit(f"segment {addr:#x}-{addr + len(seg):#x} is outside the 16 MiB flash")
        for lo, hi in used:
            if addr < hi and lo < addr + len(seg):
                raise SystemExit(f"segments overlap at {max(lo, addr):#x}")
        used.append((addr, addr + len(seg)))
        out.append(f"@{addr:08x}")
        for i in range(0, len(seg), 16):
            out.append(" ".join(f"{b:02x}" for b in seg[i:i + 16]))
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("elf")
    ap.add_argument("hex")
    a = ap.parse_args()
    text = to_hex(list(load_segments(a.elf)))
    with open(a.hex, "w") as f:
        f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
