# SPDX-FileCopyrightText: 2026 Felipe Sens Bonetto
# SPDX-License-Identifier: Apache-2.0
"""Tiny Tapeout test for the BonettoR32V MCU (contract v3, docs/contract-v3.md) on the TT QSPI Pmod.

The Pmod is the cosim's C++ model (verif/cosim/pmod.h, through pmod_ctypes.py): a W25Q128JV flash
holding the program and an APS6404L PSRAM, with every contract 2.1-2.4 bus rule checked on every
clock. The loop mirrors the cosim harness (verif/cosim/harness.cpp) once per rising clk edge t:
the design's outputs after edge t go to the model (read at the falling edge, when they have
settled, also in a unit-delay netlist), then rst_n, ui_in and uio_in for edge t + 1 are driven
from the model's output, so the design samples the model at the next rising edge. Just before
edge t + 1 the outputs must still be the ones read at the falling edge (they come from posedge
flip-flops, not from clk or an input; the edge where rst_n rises is exempt, as in the harness).

Every test runs on its own copy of the design (tb.v slots), which starts from Icarus X power-up:
nothing is initialized (contract 1, invariant 6). X or Z fails, from the first rising edge on:
  - on uo_out, on uio_oe[0], [3], [6] and [7], and on the three CS_n and SCK, always;
  - on uio_oe[1], [2], [4], [5] while a CS_n is low (idle, they don't matter: contract 1);
  - on a driven SD line at a rising SCK edge (the memory samples it) or the clock before (it must
    be stable across the edge).
Any other X on a driven SD line reaches the model as 0, and an X on an undriven one doesn't
matter. The memories start in the power-up modes of the test's seed (pmod_init_for_seed, shared
with the cosim: seeds 1-4 cover all four of contract 2.4), and the reset lasts as long as the
harness's for that seed (1 to 9 edges).

The programs are copies of build/programs (gpio_demo, uart_echo; verif/programs), so TT's CI
needs no RISC-V compiler. When build/programs exists, the copies must equal it.
  - test_gpio_demo_mode1..4: gpio_demo under the four power-up modes, each with its own constant
    ui_in (bit 1, the UART RX line, high). uo_out (GPIO-mode pins, ruling R24) must go through
    gpio_demo's GPIO_OUT sequence for that ui_in and end on expect.json's result_gpio, then hold
    it while the program ends and its writes drain. Every PSRAM write into expect.json's
    psram_window must write a byte the program stores at that address, and the window's final
    bytes must be the program's (contract 8 and section 4's end compare, as the pins-mode
    harness does with its ISS). The sequence, the stores and the final bytes come from
    tbmodels.py, a model of gpio_demo.c checked here against the ISS's expect.json (ui_in = 0).
    Stack writes, outside the window, are not modeled.
  - test_uart_hello: uart_echo sends "hello" first at expect.json's uart_div (bit time DIV + 1);
    the frames decoded at uo[0] (8N1, every bit exactly DIV + 1 cycles) must spell "hello". The
    run stops there; the echo part is the cosim's.
tbmodels.py's models have their own unit tests: python3 -m pytest test/test_tbmodels.py.
"""
import json
import time
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from pmod_ctypes import Pmod, load_flash_hex
from tbmodels import (GPIO_DEMO_WINDOW, PinError, UartRx, changes, gpio_demo_memory,
                      gpio_demo_shown)

HERE = Path(__file__).resolve().parent
BUILD = HERE.parent / "build" / "programs"
LAT_CAP = 2048     # contract 4: cycles per instruction at most
DRAIN = 2048       # contract 4: cycles after the program end before memory is compared
CLK_NS = 25.0      # contract 7: T for the Pmod's rate and tCEM rules
SIM_PERIOD_US = 10  # the simulated period: long, so unit-delay netlists settle
REGION_LO = 0x7FFF80  # contract 3: the core region, PSRAM 0x7F_FF80-0x7F_FFFF

# uio bits (contract section 1)
CSF, SD0, SD1, SCK, SD2, SD3, CSR, CSB = (1 << i for i in range(8))
SDALL = SD0 | SD1 | SD2 | SD3
FIXED = CSF | SCK | CSR | CSB

Fail = PinError
_images = {}
_slots_used = set()


def program(name):
    """(16 MiB flash image, expect.json) of a test program."""
    if name not in _images:
        for suffix in (".hex", ".expect.json"):
            copy, src = HERE / (name + suffix), BUILD / (name + suffix)
            if src.exists():
                assert src.read_bytes() == copy.read_bytes(), (
                    f"test/{copy.name} differs from build/programs/{src.name}: rebuild with "
                    f"`make -C verif/programs`, or re-copy if the program changed")
        _images[name] = (load_flash_hex(HERE / (name + ".hex")),
                         json.loads((HERE / (name + ".expect.json")).read_text()))
    return _images[name]


def read(sig):
    """(value, X/Z mask) of a vector; X/Z bits read 0 in the value."""
    v = sig.value
    if v.is_resolvable:
        return int(v), 0
    val = x = 0
    for ch in str(v):  # MSB first
        val, x = val << 1, x << 1
        if ch == "1":
            val |= 1
        elif ch != "0":
            x |= 1
    return val, x


def reset_edges_of(seed):
    """verif/cosim/harness.cpp: rst_n low for this many rising edges (seed 1: the minimum, 1)."""
    return 1 + ((seed + 8) * 5) % 9


async def run(dut, slot, name, seed, ui, observe, budget, memory=None):
    """Run program `name` on slot `slot` with the Pmod in seed's power-up modes and ui_in = ui.

    observe(t, uo, where) is called at every edge after the reset (where: the cycle and the run,
    for messages) and returns True to end the run; the run fails when it hasn't ended `budget`
    edges after the reset. memory = (stores, final), {address: {bytes}} and {address: byte} over
    a PSRAM window: every write into the window must write a byte in stores[address], and after
    the run the window holds final."""
    assert slot not in _slots_used, (
        f"slot s{slot} is already used by another test: each test needs its own copy of the "
        f"design (add a slot in tb.v)")
    _slots_used.add(slot)
    s = getattr(dut, f"s{slot}")
    image, _ = program(name)
    resets = reset_edges_of(seed)
    pmod = Pmod(seed, image, CLK_NS)
    tag = f"{name}, seed {seed} ({pmod.mode}), {resets} reset edge(s)"
    dut._log.info(f"slot s{slot}: {tag}, ui_in = {ui:#04x}")
    clock = Clock(s.clk, SIM_PERIOD_US, unit="us")
    wall = time.time()
    t, started, checked = 0, False, 0
    try:
        # Inputs first, then a clock that starts low: rising edge 0 comes half a period later with
        # rst_n low and settled. uio_in before edge 0 is the model's idle-bus noise, as in the
        # harness.
        s.ena.value = 1
        s.ui_in.value = ui
        s.rst_n.value = 0
        s.uio_in.value = pmod.uio_in()
        s.clk.value = 0
        await Timer(1, unit="us")
        clock.start(start_high=False)
        started = True
        prev_sck, prev_x, rst = 0, 0, 0
        while True:
            await FallingEdge(s.clk)  # the outputs after rising edge t
            o, ox = read(s.outs)      # {uo_out, uio_out, uio_oe}
            uo, uio, oe = o >> 16, (o >> 8) & 0xFF, o & 0xFF
            uo_x, uio_x, oe_x = ox >> 16, (ox >> 8) & 0xFF, ox & 0xFF
            where = f"cycle {t} ({tag})"
            selected = (uio & (CSF | CSR)) != CSF | CSR
            if uo_x or (uio_x & FIXED) or (oe_x & FIXED) or (selected and oe_x & SDALL):
                raise Fail(f"{where}: X/Z on an output: uo_out = {s.uo_out.value}, uio_out = "
                           f"{s.uio_out.value}, uio_oe = {s.uio_oe.value} (no power-up values, "
                           f"contract 1: uo_out, the fixed uio_oe bits, CS_n and SCK must be 0 "
                           f"or 1, and the SD uio_oe bits while a CS_n is low)")
            if t < resets and ((uio & (CSF | CSR)) != CSF | CSR or uio & SCK or uo):
                raise Fail(f"{where}: during reset both CS_n must be 1, SCK 0 and uo_out 0 "
                           f"(section 1): uio_out = {uio:#04x}, uo_out = {uo:#04x}")
            sck = uio & SCK
            if sck and not prev_sck and selected:
                bad = (uio_x | prev_x) & oe & SDALL
                if bad:
                    raise Fail(f"{where}: X/Z on driven SD lines (uio bits {bad:#04x}) at a "
                               f"rising SCK edge or the clock before: uio_out = "
                               f"{s.uio_out.value} (rule 2.1.4)")
            if not pmod.edge(t, uio, oe):
                raise Fail(f"{where}: {pmod.error()}")
            if memory and pmod.write_count() > checked:
                for wt, a, d in pmod.writes(checked):
                    checked += 1
                    if a < REGION_LO and a in memory[1] and d not in memory[0].get(a, ()):
                        raise Fail(f"{where}: PSRAM write of {d:#04x} to {a:#08x} (cycle {wt}) "
                                   f"matches no store of the program to that address (contract "
                                   f"8: outside the core region only bytes of stores, at their "
                                   f"addresses, with their data)")
            if t >= resets:
                if observe(t, uo, where):
                    break
                if t - resets >= budget:
                    raise Fail(f"{where}: timeout, {budget} cycles after the reset (the latency "
                               f"cap's bound for this program, {LAT_CAP} cycles per instruction)")
            # inputs for edge t + 1
            rst_change = rst != (t + 1 >= resets)
            if rst_change:
                rst = 1 - rst
                s.rst_n.value = rst
            s.uio_in.value = pmod.uio_in()
            prev_sck, prev_x = sck, uio_x
            await RisingEdge(s.clk)   # outputs just before edge t + 1
            if not rst_change and read(s.outs) != (o, ox):
                raise Fail(f"{where}: outputs changed between rising clk edges (uo_out, uio_out, "
                           f"uio_oe {o:#08x} -> {read(s.outs)[0]:#08x}): they must come from "
                           f"posedge flip-flops, not from clk or an input")
            t += 1
        if memory:
            for a, want in sorted(memory[1].items()):
                got = pmod.psram_byte(a)
                if got != want:
                    raise Fail(f"{where}: final PSRAM byte {a:#08x} is {got:#04x}, the program "
                               f"stored {want:#04x} (contract 4: compared after the program end "
                               f"and the {DRAIN}-cycle drain)")
    finally:
        if started:
            clock.stop()
        pmod.close()
        dt = time.time() - wall
        dut._log.info(f"slot s{slot}: {t + 1} cycles in {dt:.1f} s "
                      f"({1e6 * dt / max(t + 1, 1):.0f} us per cycle)")
    return t


# ---- gpio_demo ----------------------------------------------------------------------------------
# Per mode: its own ui_in, bit 1 (UART RX) high; together every other bit is seen at 0 and at 1.
GPIO_UI = {1: 0x5B, 2: 0xA7, 3: 0x0F, 4: 0xF2}
# gpio_demo ends with GPIO_OUT = 0x5A, then retires its self-loop twice (each within the cap), then
# the drain: (2 + 2) x LAT_CAP, the harness's (tail + 2) x LAT_CAP.
GPIO_END = 4 * LAT_CAP


async def gpio_demo(dut, mode):
    _, ex = program("gpio_demo")
    assert changes(gpio_demo_shown(0), ~ex.get("altfn", 0) & 0xFF) == ex["uo_trace"], (
        "tbmodels.gpio_demo_shown() no longer matches the ISS (gpio_demo.expect.json uo_trace, "
        "ui_in = 0): update it to verif/programs/gpio_demo.c")
    assert ex["psram_window"] == GPIO_DEMO_WINDOW, (
        "gpio_demo.expect.json psram_window changed: update tbmodels.gpio_demo_memory()")
    mask = ~ex.get("final_altfn", 0) & 0xFF  # GPIO-mode pins (ruling R24)
    ui = GPIO_UI[mode]
    want = changes(gpio_demo_shown(ui), mask)
    assert want[-1] == ex["result_gpio"] & mask
    stores, final = gpio_demo_memory(ui)
    seen, st = [], {"last": 0, "done": None}

    def observe(t, uo, where):
        g = uo & mask
        if g != st["last"]:
            n = len(seen)
            if n >= len(want):
                raise Fail(f"{where}: uo_out changed to {uo:#04x} after the final "
                           f"{want[-1]:#04x}")
            if g != want[n]:
                raise Fail(f"{where}: uo_out change #{n} is {g:#04x}, expected {want[n]:#04x} "
                           f"(seen so far {[hex(v) for v in seen]})")
            seen.append(g)
            st["last"] = g
            if len(seen) == len(want):
                st["done"] = t
                dut._log.info(f"all {len(want)} uo_out changes seen by cycle {t}")
        return st["done"] is not None and t - st["done"] >= GPIO_END

    # every instruction within the latency cap: (retired + 16) x LAT_CAP, as the harness
    await run(dut, mode - 1, "gpio_demo", mode, ui, observe, LAT_CAP * (ex["retired"] + 16),
              memory=(stores, final))
    assert seen == want and st["last"] == ex["result_gpio"] & mask


@cocotb.test()
async def test_gpio_demo_mode1(dut):
    """gpio_demo, flash standard, PSRAM SPI not reset (seed 1, 1 reset edge)."""
    await gpio_demo(dut, 1)


@cocotb.test()
async def test_gpio_demo_mode2(dut):
    """gpio_demo, flash continuous read, PSRAM SPI not reset (seed 2)."""
    await gpio_demo(dut, 2)


@cocotb.test()
async def test_gpio_demo_mode3(dut):
    """gpio_demo, flash standard, PSRAM QPI (seed 3)."""
    await gpio_demo(dut, 3)


@cocotb.test()
async def test_gpio_demo_mode4(dut):
    """gpio_demo, flash continuous read, PSRAM QPI (seed 4)."""
    await gpio_demo(dut, 4)


# ---- uart_echo: "hello" ------------------------------------------------------------------------
HELLO = b"hello"  # verif/programs/stim.h UART_HELLO
UART_SEED = 8     # flash continuous read, PSRAM QPI; 9 reset edges
UART_UI = 0x02    # UART RX (ui[1]) idle high, nothing else driven


def hello_budget(div):
    """uart_echo retires 57 instructions up to its first UART_DATA store (ISS), then about 12 per
    byte plus one 5-instruction poll iteration after each frame: (57 + 5 x 17 + 16) x LAT_CAP for
    the instructions and 5 frames of 11 bit times (DIV + 1 cycles each) bound a compliant design;
    doubled."""
    return 2 * ((57 + 5 * 17 + 16) * LAT_CAP + 5 * 11 * (div + 1))


@cocotb.test()
async def test_uart_hello(dut):
    """uart_echo's first UART TX bytes, decoded at uo[0], are "hello"."""
    _, ex = program("uart_echo")
    assert ex["uart_tx_expect"].encode().startswith(HELLO)
    alt = ex.get("final_altfn", 0)
    assert alt & 1, "uart_echo must end with uo[0] on the UART (ALTFN bit 0)"
    gpio_mask = ~alt & 0xFF
    rx = UartRx(ex["uart_div"])
    late = {}  # a UART line error, held while the GPIO pins may still show a failed check

    def observe(t, uo, where):
        if uo & gpio_mask:
            # crt0 _fail: ALTFN = 0 (uo[0] drops to GPIO 0, which looks like a start bit), then
            # GPIO_OUT = the check's id, then 0xEE
            raise Fail(f"{where}: GPIO-mode pins show {uo & gpio_mask:#04x} (uo_out = "
                       f"{uo:#04x}): uart_echo writes GPIO_OUT only at its end (0x5A) or on a "
                       f"failed check (crt0 _fail: the check's id, then 0xEE; ids in "
                       f"verif/programs/uart_echo.c)"
                       + (f"; before that: {late['err']}" if late else ""))
        if late:
            if t - late["t"] > 4 * LAT_CAP:
                raise Fail(late["err"])
            return False
        n = len(rx.frames)
        try:
            rx.step(t, uo & 1, where)
        except PinError as e:
            late.update(t=t, err=str(e))
            return False
        if len(rx.frames) > n:
            got = bytes(rx.frames)
            dut._log.info(f"cycle {t}: UART TX byte {got[-1]:#04x} {chr(got[-1])!r}")
            if got != HELLO[:len(got)]:
                raise Fail(f"{where}: UART TX sent {got!r}, expected {HELLO!r} first")
        return len(rx.frames) == len(HELLO)

    await run(dut, 4, "uart_echo", UART_SEED, UART_UI, observe, hello_budget(ex["uart_div"]))
    assert bytes(rx.frames) == HELLO
