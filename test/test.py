# SPDX-FileCopyrightText: 2026 Felipe Sens Bonetto
# SPDX-License-Identifier: Apache-2.0
"""Tiny Tapeout test for BonettoR32V: gpio_demo on the core with a 23LC512 SPI RAM model.

gpio_demo.hex is the 64 KB RAM image of verif/programs/gpio_demo.c and gpio_demo.expect.json the
reference ISS's result for the pins ui_in schedule with seed 1 (both copied from build/programs,
so TT's CI needs no compiler). ui_in follows the schedule of the pins-mode cosim: ui_value(1, c)
after the c-th uo_out change, applied at the falling clk edge after the change. The test passes
when the uo_out change sequence equals the expected one and nothing changes afterwards.

Contract v2 (docs/contract.md sections 3 and 6): the core may keep x1..x15, the pc and scratch in
the RAM's core region 0xFF80-0xFFFF. The RAM model serves that traffic as plain memory and fills
the region with random bytes at start (its reset contents are arbitrary); gpio_demo's stack starts
at 0xFF80, below the region, and the program never touches it. The model also checks the section 6
transaction classes the pins show: each transaction is all region or all program (a null READ,
header only, touches nothing), a program READ addresses at most 4 bytes, and program WRITEs
between two program READs write at most 4 bytes, none twice.
"""
import json
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, Timer

HERE = Path(__file__).resolve().parent
EXPECT = json.loads((HERE / "gpio_demo.expect.json").read_text())
LAT_CAP = 1024   # contract section 4: cycles per instruction at most
QUIET = 16 * LAT_CAP  # after the last expected change: the program's tail, EBREAK, its guard
M32 = 0xFFFFFFFF


def ui_value(seed: int, k: int) -> int:
    """verif/cosim/iss.h ui_value()."""
    x = ((k * 0x9E3779B1) ^ ((seed + 0x7F4A7C15) * 0x85EBCA6B)) & M32
    x ^= x >> 16
    x = (x * 0x7FEB352D) & M32
    x ^= x >> 15
    x = (x * 0x846CA68B) & M32
    x ^= x >> 16
    return x & 0xFF


def check_copies():
    """When the program build exists (a local checkout), the committed copies must match it."""
    build = HERE.parent / "build" / "programs"
    for name in ("gpio_demo.hex", "gpio_demo.expect.json"):
        src = build / name
        if src.exists():
            assert src.read_bytes() == (HERE / name).read_bytes(), (
                f"test/{name} differs from build/programs/{name}: copy it again")


def val(sig, what):
    v = sig.value
    assert v.is_resolvable, f"{what} = {v} (X or Z)"
    return int(v)


def check_uio_out(sig):
    """CS_n (bit 0) and SCK (bit 3) must be 0 or 1, bits 7:4 and 2 must be 0. MOSI (bit 1) is
    left alone: the contract constrains it only at rising SCK edges (the RAM model checks it
    there), so X on it elsewhere is legal."""
    s = str(sig.value)  # MSB first: s[7 - i] is bit i
    assert len(s) == 8, s
    for i in (0, 3):
        assert s[7 - i] in "01", f"uio_out = {s}: bit {i} is X or Z"
    for i in (2, 4, 5, 6, 7):
        assert s[7 - i] == "0", f"uio_out = {s}: bits [7:4] and [2] must be 0"


@cocotb.test()
async def test_gpio_demo(dut):
    """gpio_demo's uo_out sequence (pins ui_in schedule, seed 1) equals the reference."""
    check_copies()
    assert EXPECT["ui_schedule"] == "pins" and EXPECT["halt"] == "ebreak"
    seed, want = EXPECT["seed"], EXPECT["uo_trace"]
    assert want and want[-1] == EXPECT["uo_final"]
    budget = LAT_CAP * (EXPECT["retired"] + 16)  # the latency cap's bound on the whole run

    # 20 MHz is the signoff clock; the period doesn't matter to a zero/unit-delay simulation, so
    # keep TT's 10 us, which leaves unit-delay gate-level paths plenty of time to settle.
    # Inputs first, then a clock that starts low: the first rising edge comes half a period later,
    # with rst_n low and settled (in a unit-delay netlist too), and the model checks the reset
    # values from that first edge on.
    dut.ena.value = 1
    dut.ui_in.value = ui_value(seed, 0)
    dut.rst_n.value = 0
    dut.clk.value = 0
    await Timer(1, unit="us")
    cocotb.start_soon(Clock(dut.clk, 10, unit="us").start(start_high=False))
    await ClockCycles(dut.clk, 10)
    await FallingEdge(dut.clk)
    assert val(dut.uo_out, "uo_out during reset") == 0
    dut.rst_n.value = 1

    seen, last, cycle, done_at = [], 0, 0, None
    while True:
        await FallingEdge(dut.clk)  # outputs have settled half a period after the rising edge
        cycle += 1
        assert val(dut.uio_oe, "uio_oe") == 0x0B, "uio_oe must be 0x0b"
        check_uio_out(dut.uio_out)
        uo = val(dut.uo_out, "uo_out")
        if uo != last:
            n = len(seen)
            assert n < len(want), (
                f"cycle {cycle}: uo_out changed to {uo:#04x} after the last expected change")
            assert uo == want[n], (
                f"cycle {cycle}: uo_out change #{n} is {uo:#04x}, expected {want[n]:#04x} "
                f"(seen so far {[hex(x) for x in seen]})")
            seen.append(uo)
            last = uo
            dut.ui_in.value = ui_value(seed, len(seen))
            if len(seen) == len(want):
                done_at = cycle
                dut._log.info(f"all {len(want)} uo_out changes seen by cycle {cycle}")
        if done_at is None:
            assert cycle <= budget, (
                f"timeout: {len(seen)} of {len(want)} uo_out changes after {cycle} cycles")
        elif cycle - done_at >= QUIET:
            break
    assert seen == want
