# SPDX-FileCopyrightText: 2026 Felipe Sens Bonetto
# SPDX-License-Identifier: Apache-2.0
"""The TT QSPI Pmod model (verif/cosim/pmod.h: flash, PSRAM, the contract v3 section 2 bus rules)
for the cocotb test, through its C API (verif/cosim/pmod_capi.cpp) and ctypes.

The library is build/cosim/libpmod.so: `bash verif/cosim/build.sh` (no arguments) builds it, and
test/Makefile rebuilds it when pmod.h or pmod_capi.cpp is newer. PMOD_LIB=<path> overrides it.

Use it the way the cosim harness (verif/cosim/harness.cpp) does, once per rising clk edge t:
    ok = pmod.edge(t, uio_out, uio_oe)   # the design's outputs after edge t
    uio_in = pmod.uio_in()               # the design's input until edge t + 1
"""
import ctypes
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = Path(os.environ.get("PMOD_LIB", ROOT / "build" / "cosim" / "libpmod.so"))
FLASH_SIZE = 1 << 24

# pmod_init_for_seed (pmod.h): the memories' power-up modes for a seed (contract 2.4)
MODES = ("flash standard, PSRAM SPI not reset", "flash continuous read, PSRAM SPI not reset",
         "flash standard, PSRAM QPI", "flash continuous read, PSRAM QPI")


def mode_of_seed(seed: int) -> str:
    return MODES[(seed - 1) % 4]


_lib = None


def _load():
    global _lib
    if _lib is None:
        if not LIB.is_file():
            raise FileNotFoundError(f"{LIB} is missing: run `bash verif/cosim/build.sh` (no "
                                    f"arguments) or `make -C test`")
        lib = ctypes.CDLL(str(LIB))
        lib.pmod_new_for_seed.restype = ctypes.c_void_p
        lib.pmod_new_for_seed.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_double]
        lib.pmod_edge.restype = ctypes.c_int
        lib.pmod_edge.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint8,
                                  ctypes.c_uint8]
        lib.pmod_uio_in.restype = ctypes.c_uint8
        lib.pmod_uio_in.argtypes = [ctypes.c_void_p]
        lib.pmod_error.restype = ctypes.c_char_p
        lib.pmod_error.argtypes = [ctypes.c_void_p]
        lib.pmod_free.restype = None
        lib.pmod_free.argtypes = [ctypes.c_void_p]
        _lib = lib
    return _lib


def load_flash_hex(path) -> bytes:
    """A sparse $readmemh flash image (verif/programs/elf2hex.py: `@addr` byte addresses, one
    byte per token, `//` comments) as the full 16 MiB, unset bytes erased (0xFF): the same reading
    as iss3.h load_flash_hex."""
    img = bytearray(b"\xff") * FLASH_SIZE
    n = 0
    for lineno, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.split("//", 1)[0]
        for tok in line.split():
            where = f"{path}:{lineno}"
            if tok.startswith("@"):
                n = int(tok[1:], 16)
                if n >= FLASH_SIZE:
                    raise ValueError(f"address outside the 16 MiB flash at {where}")
                continue
            v = int(tok, 16)
            if v > 0xFF:
                raise ValueError(f"token is not a byte at {where}")
            if n >= FLASH_SIZE:
                raise ValueError(f"image runs past the 16 MiB flash at {where}")
            img[n] = v
            n += 1
    return bytes(img)


class Pmod:
    """One QSPI Pmod (flash image + PSRAM) in the power-up modes of `seed`, which is also the
    noise seed (pmod_new_for_seed). clk_ns is the signoff period T for the contract's rate and
    tCEM rules, whatever period the simulation uses."""

    def __init__(self, seed: int, image: bytes, clk_ns: float = 25.0):
        if image is not None and len(image) != FLASH_SIZE:
            raise ValueError(f"the flash image must be exactly {FLASH_SIZE} bytes or None")
        self._lib = _load()
        self.seed = seed
        self.mode = mode_of_seed(seed)
        self._p = self._lib.pmod_new_for_seed(seed, image, clk_ns)
        if not self._p:
            raise RuntimeError("pmod_new_for_seed failed")

    def edge(self, t: int, uio_out: int, uio_oe: int) -> bool:
        return bool(self._lib.pmod_edge(self._p, t, uio_out, uio_oe))

    def uio_in(self) -> int:
        return self._lib.pmod_uio_in(self._p)

    def error(self) -> str:
        return self._lib.pmod_error(self._p).decode()

    def close(self):
        if self._p:
            self._lib.pmod_free(self._p)
            self._p = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
