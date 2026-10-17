"""Unit tests for the QSPI Pmod chip models (verif/cosim/pmod.h, contract v3 section 2).

build.sh (no arguments) builds build/cosim/test_pmod and build/cosim/libpmod.so. test_pmod runs
hand-built pin waveforms through PmodBus and prints one PASS/FAIL line per case; every line must be
PASS. The C API (pmod_capi.cpp) gets a smoke test through ctypes, the way the gate-level test loads
it.

Run: python3 -m pytest verif/cosim/test_pmod.py -q
"""
import ctypes
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build" / "cosim"

CASES = {
    "test_flash_read03",
    "test_flash_fastread_0b_6b_eb",
    "test_flash_continuous",
    "test_flash_continuous_partial",
    "test_flash_program_ignored",
    "test_psram_requires_reset",
    "test_psram_spi_rw",
    "test_psram_spi_0b",
    "test_psram_quad_and_qpi",
    "test_reset_sequence_from_every_state",
    "test_trst",
    "test_tcem_limit",
    "test_contention",
    "test_waveform_rules",
    "test_partial_command_ignored",
    "test_max_sck",
    "test_write_byte_boundary",
    "test_command_legality",
}

# uio bits (contract section 1)
CSF, SD0, SD1, SCK, CSR, CSB = 0x01, 0x02, 0x04, 0x08, 0x40, 0x80
OE_FIXED = CSF | SCK | CSR | CSB


@pytest.fixture(scope="module")
def built():
    p = subprocess.run(["bash", str(ROOT / "verif/cosim/build.sh")], capture_output=True,
                       text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    return OUT


def test_unit_cases(built):
    p = subprocess.run([str(built / "test_pmod")], capture_output=True, text=True, timeout=600)
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    bad = [ln for ln in lines if not ln.startswith("PASS ")]
    assert not bad, "\n".join(bad) + p.stderr
    assert p.returncode == 0, p.stderr
    assert {ln.split()[1] for ln in lines} == CASES


def test_capi(built):
    lib = ctypes.CDLL(str(built / "libpmod.so"))
    lib.pmod_new.restype = ctypes.c_void_p
    lib.pmod_new.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                             ctypes.c_double]
    lib.pmod_new_for_seed.restype = ctypes.c_void_p
    lib.pmod_new_for_seed.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_double]
    lib.pmod_edge.restype = ctypes.c_int
    lib.pmod_edge.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint8, ctypes.c_uint8]
    lib.pmod_uio_in.restype = ctypes.c_uint8
    lib.pmod_uio_in.argtypes = [ctypes.c_void_p]
    lib.pmod_error.restype = ctypes.c_char_p
    lib.pmod_error.argtypes = [ctypes.c_void_p]
    lib.pmod_free.argtypes = [ctypes.c_void_p]

    img = bytearray(1 << 24)
    img[0x123456] = 0xC5
    img = bytes(img)
    idle = CSF | CSR | CSB
    sel = idle & ~CSF

    def flash_read03(h, addr):
        """One flash 0x03 byte at SCK = clk/2. Returns (byte, None) or (None, error)."""
        t = 0
        byte = 0
        seq = [(sel, OE_FIXED)]  # flash CS_n falls
        for bit in [(0x03 >> i) & 1 for i in range(7, -1, -1)] + \
                   [(addr >> i) & 1 for i in range(23, -1, -1)]:
            d = SD0 if bit else 0
            seq += [(sel | d, OE_FIXED | SD0), (sel | SCK | d, OE_FIXED | SD0)]
        for k in range(8):
            seq += [(sel, OE_FIXED | SD0), (sel | SCK, OE_FIXED | SD0)]
        seq += [(sel, OE_FIXED), (idle, OE_FIXED)]
        first_data_high = 1 + 2 * 32 + 1  # index of the first data clock's high edge
        for i, (out, oe) in enumerate(seq):
            t += 1
            if lib.pmod_edge(h, t, out, oe) != 1:
                return None, lib.pmod_error(h).decode()
            k = i - first_data_high
            if k >= 0 and k % 2 == 0 and k < 16:
                byte = byte << 1 | (lib.pmod_uio_in(h) >> 2 & 1)
        assert lib.pmod_error(h) == b""
        return byte, None

    assert lib.pmod_new(3, img, 0, 3, 25.0) is None  # out-of-range PSRAM mode

    h = lib.pmod_new(3, img, 0, 0, 25.0)  # flash standard, PSRAM SPI not reset
    assert flash_read03(h, 0x123456) == (0xC5, None)
    assert lib.pmod_edge(h, 1000, idle & ~CSF & ~CSR, OE_FIXED) == 0  # both CS_n low
    assert b"both CS_n low" in lib.pmod_error(h)
    lib.pmod_free(h)

    # pmod_new_for_seed: seeds 1 and 3 power up the flash in standard mode (3: PSRAM in QPI)
    h = lib.pmod_new_for_seed(1, img, 25.0)
    assert flash_read03(h, 0x123456) == (0xC5, None)
    lib.pmod_free(h)
    h = lib.pmod_new_for_seed(3, None, 25.0)  # NULL image: erased flash
    assert flash_read03(h, 0x123456) == (0xFF, None)
    lib.pmod_free(h)
    # seed 2 powers up in continuous read: the 0x03 bits become a quad address, M and dummy
    # edges, and the core's SD0 drive then collides with the flash's quad data
    h = lib.pmod_new_for_seed(2, img, 25.0)
    byte, err = flash_read03(h, 0x123456)
    assert byte is None and "contention" in err, err
    lib.pmod_free(h)
