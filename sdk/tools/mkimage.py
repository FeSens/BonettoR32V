#!/usr/bin/env python3
"""ELF -> flash images for BonettoR32V (docs/contract.md section 3).

    mkimage.py PROG.elf --bin PROG.bin --hex PROG.hex

Every loadable segment goes to flash at its load address (p_paddr): .text and .rodata where they
run, .data's initial bytes after them (crt0 copies them to PSRAM). The flash is 16 MiB; nothing
may load at or above it.
  .bin  the raw flash image from address 0 to the last loaded byte, gaps 0xFF (erased flash):
        what goes into the QSPI Pmod's W25Q128 at offset 0.
  .hex  the cosim's sparse $readmemh image (verif/programs/elf2hex.py's format): one @address
        record per segment, 16 hex bytes per line.
"""
import argparse
import struct
import sys

FLASH = 1 << 24


def segments(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise SystemExit(f"{path}: not a little-endian ELF32 file")
    phoff, = struct.unpack_from("<I", data, 28)
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    out = []
    for i in range(phnum):
        p_type, p_offset, _v, p_paddr, p_filesz, _m, _f, _a = struct.unpack_from(
            "<8I", data, phoff + i * phentsize)
        if p_type == 1 and p_filesz:
            if p_paddr + p_filesz > FLASH:
                raise SystemExit(f"segment {p_paddr:#x}+{p_filesz:#x} is outside the 16 MiB flash")
            out.append((p_paddr, data[p_offset:p_offset + p_filesz]))
    out.sort()
    for (a, d), (b, _) in zip(out, out[1:]):
        if a + len(d) > b:
            raise SystemExit(f"segments overlap at {b:#x}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("elf")
    ap.add_argument("--bin")
    ap.add_argument("--hex")
    a = ap.parse_args()
    segs = segments(a.elf)
    if a.bin:
        end = max(addr + len(d) for addr, d in segs)
        img = bytearray(b"\xff" * end)
        for addr, d in segs:
            img[addr:addr + len(d)] = d
        with open(a.bin, "wb") as f:
            f.write(img)
    if a.hex:
        lines = []
        for addr, d in segs:
            lines.append(f"@{addr:08x}")
            for i in range(0, len(d), 16):
                lines.append(" ".join(f"{b:02x}" for b in d[i:i + 16]))
        with open(a.hex, "w") as f:
            f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
