# SPDX-FileCopyrightText: 2026 Felipe Sens Bonetto
# SPDX-License-Identifier: Apache-2.0
"""Pure-Python reference models for the cocotb test (test.py), with no simulator dependency, so
test_tbmodels.py unit-tests them with plain pytest (python3 -m pytest test/test_tbmodels.py).

- gpio_demo_shown / gpio_demo_memory: verif/programs/gpio_demo.c's GPIO_OUT writes, PSRAM stores
  and final result bytes while ui_in holds one value. test.py checks them against the ISS's
  expect.json (ui_in = 0) before using them.
- changes: a GPIO-mode uo_out trace (mask first, then one entry per change), as iss_main lists
  uo_trace.
- UartRx: an 8N1 decoder for UART TX on uo[0] (contract 5.5), as verif/cosim/periph_models.h
  UartTerm::tx_step decodes it.
"""
M32 = 0xFFFFFFFF


class PinError(AssertionError):
    """A check failed at the pins."""


def changes(values, mask, start=0):
    """The trace of a pin group: each value masked, then one entry per change from `start` (the
    reset value)."""
    trace, last = [], start & mask
    for v in values:
        v &= mask
        if v != last:
            trace.append(v)
            last = v
    return trace


def _sext(v, bits):
    return (v - (1 << bits) if v >> (bits - 1) & 1 else v) & M32


def _fold(x):
    return (x ^ (x >> 8) ^ (x >> 16) ^ (x >> 24)) & 0x7F


def _gpio_demo_run(u):
    """(GPIO_OUT writes, [(PSRAM byte address, byte) of every store, in order]) of gpio_demo with
    ui_in = u. buf is the whole .bss (PSRAM bytes 0-15); crt0 clears it with word stores."""
    shown, acc = [], 0
    for i in range(1, 21):
        shown += [i, ((u ^ 0x3C) & 0x7F) ^ (u >> 7)]
        acc = (acc + u) & 0xFF
    a1 = acc + 1
    x = ((a1 << 24) ^ (a1 << 13) ^ (a1 << 3) ^ 0x9E3779B1) & M32
    b6, b7 = (x >> 3) & 0xFF, 0x80 | acc
    stores = [(a, 0) for a in range(16)]                                  # crt0: clear .bss
    stores += list(enumerate(x.to_bytes(4, "little")))                    # sw at +0
    stores += [(4 + k, b) for k, b in enumerate(((x >> 7) & 0xFFFF).to_bytes(2, "little"))]
    stores += [(6, b6), (7, b7)]                                          # sb at +6, +7
    stores += [(9 + k, b) for k, b in enumerate((~x & M32).to_bytes(4, "little"))]  # sw at +9
    buf = bytearray(16)
    for a, b in stores:
        buf[a] = b

    def word(o):
        return int.from_bytes(buf[o:o + 4], "little")

    mis, lb7 = word(9), _sext(buf[7], 8)
    lh6, lhu6 = _sext(buf[6] | buf[7] << 8, 16), buf[6] | buf[7] << 8
    shown += [_fold(word(0)), _fold(word(4)), _fold(mis ^ word(8) ^ word(12)),
              _fold((lb7 ^ (lh6 << 3) ^ (lhu6 << 5)) & M32), 0x5A]
    return shown, stores


def gpio_demo_shown(u):
    """Every value gpio_demo writes to GPIO_OUT with ui_in = u, in order (ALTFN stays 0). The
    program passes whatever ui_in is, so this sequence is what checks the GPIO_IN path."""
    return _gpio_demo_run(u)[0]


GPIO_DEMO_WINDOW = [[0, 16]]  # its expect.json psram_window: buf, the whole .bss


def gpio_demo_memory(u):
    """(store set {PSRAM address: {bytes stored there}}, final bytes {PSRAM address: byte}) of
    gpio_demo with ui_in = u, over its psram_window (GPIO_DEMO_WINDOW). Stores outside it (the
    stack) are not modeled."""
    sset, final = {}, {}
    for a, b in _gpio_demo_run(u)[1]:
        sset.setdefault(a, set()).add(b)
        final[a] = b
    assert sorted(final) == [a for lo, hi in GPIO_DEMO_WINDOW for a in range(lo, hi)]
    return sset, final


class UartRx:
    """8N1 frames at uo[0] (contract 5.5), as verif/cosim/periph_models.h UartTerm decodes them:
    armed once the line is high (it is GPIO 0 until ALTFN is set), a frame starts at a falling
    edge, the line changes only on bit boundaries (bit time B = DIV + 1), data bit k (LSB first)
    is the level at k x B, the stop bit must be 1, and the frame ends at 10 x B."""

    def __init__(self, div):
        self.b = div + 1
        self.armed, self.t0, self.cur, self.prev = False, None, 0, 0
        self.frames = []

    def step(self, t, line, where=""):
        """The line level after edge t. Raises PinError on a framing or bit-time violation."""
        b = self.b
        line = 1 if line else 0
        if self.t0 is None:
            if not self.armed:
                self.armed = bool(line)
            elif not line:
                self.t0, self.cur = t, 0
            self.prev = line
            return
        r = t - self.t0
        if line != self.prev and r % b and r < 10 * b:
            raise PinError(f"{where}: UART TX changed {r} cycles into the frame that started at "
                           f"cycle {self.t0}, not on a bit boundary (bit time DIV + 1 = {b}, "
                           f"contract 5.5)")
        if r % b == 0 and b <= r <= 8 * b:
            self.cur |= line << (r // b - 1)
        if r == 9 * b and not line:
            raise PinError(f"{where}: UART TX framing error: the stop bit of the frame that "
                           f"started at cycle {self.t0} is 0")
        if r == 10 * b:
            self.frames.append(self.cur)
            self.t0 = None
            if not line:  # the next start bit right after the stop bit
                self.t0, self.cur = t, 0
        self.prev = line
