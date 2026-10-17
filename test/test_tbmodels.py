# SPDX-FileCopyrightText: 2026 Felipe Sens Bonetto
# SPDX-License-Identifier: Apache-2.0
"""Unit tests of tbmodels.py, the cocotb test's reference models, without a simulator.

Run: python3 -m pytest test/test_tbmodels.py -q
"""
import json
from pathlib import Path

import pytest

from tbmodels import PinError, UartRx, changes, gpio_demo_memory, gpio_demo_shown

HERE = Path(__file__).resolve().parent
DIV = 39  # uart_echo's DIV: 40 cycles per bit
B = DIV + 1


def frame_levels(byte, bit_time=B, stop=1, msb_first=False):
    """The line levels of one 8N1 frame, one per cycle."""
    bits = [(byte >> (7 - k if msb_first else k)) & 1 for k in range(8)]
    out = []
    for level in [0] + bits + [stop]:
        out += [level] * bit_time
    return out


def decode(levels, div=DIV, idle=8):
    """Feed idle-high cycles, then the levels, then idle high again; return the frames."""
    rx = UartRx(div)
    t = 0
    for level in [1] * idle + list(levels) + [1] * (2 * B):
        rx.step(t, level, f"cycle {t}")
        t += 1
    return rx.frames


def test_uart_decodes_frames_back_to_back():
    levels = []
    for c in b"hello":
        levels += frame_levels(c)
    assert bytes(decode(levels)) == b"hello"


def test_uart_not_armed_while_low_before_altfn():
    """uo[0] is GPIO 0 until ALTFN is set: low from the start is no start bit."""
    rx = UartRx(DIV)
    for t in range(3 * B):
        rx.step(t, 0)
    assert rx.frames == [] and rx.t0 is None
    levels = [1] * 5 + frame_levels(ord("h")) + [1] * (2 * B)
    for t, level in enumerate(levels, 3 * B):
        rx.step(t, level)
    assert bytes(rx.frames) == b"h"


@pytest.mark.parametrize("bit_time", [B - 1, B + 1])
def test_uart_rejects_wrong_bit_time(bit_time):
    with pytest.raises(PinError, match="not on a bit boundary"):
        decode(frame_levels(0x55, bit_time=bit_time))


def test_uart_rejects_stop_bit_0():
    with pytest.raises(PinError, match="stop bit"):
        decode(frame_levels(ord("h"), stop=0) + [0] * B)


def test_uart_msb_first_is_another_byte():
    """The decoder is LSB first (contract 5.5): an MSB-first 'h' decodes as 0x16, which the test's
    compare with "hello" rejects."""
    got = decode(frame_levels(ord("h"), msb_first=True))
    assert got == [0x16] and bytes(got) != b"h"


def test_changes_masks_before_dedupe():
    # 0x01 -> 0x81 is no change on the low 7 pins; masking after the dedupe would list it twice
    assert changes([0x01, 0x81, 0x02], 0x7F) == [0x01, 0x02]
    assert changes([0x00, 0x05], 0xFF) == [0x05]  # from the reset value 0


def test_gpio_demo_model_matches_the_iss_trace():
    e = json.loads((HERE / "gpio_demo.expect.json").read_text())
    assert changes(gpio_demo_shown(0), 0xFF) == e["uo_trace"]
    assert gpio_demo_shown(0)[-1] == e["result_gpio"]


@pytest.mark.parametrize("bit", range(8))
def test_gpio_demo_shows_every_ui_bit(bit):
    """A stuck GPIO_IN bit changes the uo_out sequence for every ui_in, bit 7 included."""
    for u in range(256):
        assert changes(gpio_demo_shown(u), 0xFF) != changes(gpio_demo_shown(u ^ (1 << bit)), 0xFF)


def test_gpio_demo_memory_window():
    stores, final = gpio_demo_memory(0x5B)
    assert sorted(final) == list(range(16))
    assert all(final[a] in stores[a] for a in final)
    assert all(0 in stores[a] for a in range(16))  # crt0 clears .bss
    assert [final[a] for a in (8, 13, 14, 15)] == [0, 0, 0, 0]  # never stored by main
