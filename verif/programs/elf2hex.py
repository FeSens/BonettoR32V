#!/usr/bin/env python3
"""Convert an RV32 ELF into the 64 KB RAM image read by $readmemh (one byte per line).

Usage: elf2hex.py IN.elf OUT.hex [--base ADDR]

Loadable segments inside [base, base + 64 KB) are placed at (vaddr - base); unused bytes are 0.
Segments entirely outside that range are ignored (the spike cross-check build's trampoline at
0x1000); a segment that straddles the range is an error.
"""
import argparse
import struct
import sys

RAM = 1 << 16


def load_segments(path: str):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise SystemExit(f"{path}: not a little-endian ELF32 file")
    phoff, = struct.unpack_from("<I", data, 28)
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    for i in range(phnum):
        p_type, p_offset, p_vaddr, _paddr, p_filesz, _memsz, _flags, _align = struct.unpack_from(
            "<8I", data, phoff + i * phentsize)
        if p_type == 1 and p_filesz:
            yield p_vaddr, data[p_offset:p_offset + p_filesz]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("elf")
    ap.add_argument("hex")
    ap.add_argument("--base", type=lambda s: int(s, 0), default=0)
    a = ap.parse_args()
    img = bytearray(RAM)
    for vaddr, seg in load_segments(a.elf):
        lo, hi = vaddr, vaddr + len(seg)
        if hi <= a.base or lo >= a.base + RAM:
            continue
        if lo < a.base or hi > a.base + RAM:
            raise SystemExit(f"{a.elf}: segment {lo:#x}-{hi:#x} straddles the 64 KB RAM")
        img[lo - a.base:hi - a.base] = seg
    with open(a.hex, "w") as f:
        f.write("".join(f"{b:02x}\n" for b in img))
    return 0


if __name__ == "__main__":
    sys.exit(main())
