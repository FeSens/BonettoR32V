"""Tests of the BonettoR32V SDK (sdk/). Entry point: make -C sdk test.

1. Every example builds (make -C sdk); its .bin is the flash image from address 0 and holds the
   same bytes as its .hex (the cosim's format); the startup code is at address 0.
2. The NEC decoder (nec_decode.py) on synthetic waveforms: frames, repeats, wrong timings.
3. On the RTL (src/, the cosim harness in pins mode and, for the IR remote and the serial echo,
   in rvfi mode too, where the reference ISS checks every retirement): each example runs with a
   stimulus (buttons on ui pins, UART bytes) and the harness's pin dump (--dump-pins), and the
   test checks the pins with cycle stamps: blink and c_blink intervals (delay(), millis()), the
   fade's 10-bit duties, Serial bytes, the button count, tone periods, and the IR remote's NEC
   frames DECODED from uo[1] (leader, 32 bits, address and command, repeats, carrier). The
   examples loop forever, so those runs end at their cycle budget (--max-cycles); a run that
   stops earlier failed a harness check. tests/progs/overhead measures the timebase and tone
   interrupt cost and writes build/overhead.json.
   These need Verilator: run them on hwe-bench (tools/hwe_run.sh -- make -C sdk test). On macOS
   they are skipped (ruling R11: no Verilator on the Mac).
"""
import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SDK = Path(__file__).resolve().parents[1]
ROOT = SDK.parent
BUILD = SDK / "build"
sys.path.insert(0, str(SDK / "tests"))
import nec_decode  # noqa: E402

F_CPU = 50_000_000
CYC_US = F_CPU // 1_000_000
UART_DIV = round(F_CPU / 115200) - 1  # 433: 434 cycles per bit
EXAMPLES = ["blink", "fade", "serial_echo", "button", "tone", "ir_remote", "c_blink"]
RTL = platform.system() != "Darwin" and shutil.which("verilator") is not None
needs_rtl = pytest.mark.skipif(not RTL, reason="RTL cosim needs Verilator: run on hwe-bench "
                               "(tools/hwe_run.sh -- make -C sdk test)")


@pytest.fixture(scope="session")
def built():
    for args in (["examples"], ["SKETCH=tests/progs/overhead"]):
        p = subprocess.run(["make", "-s", "-C", str(SDK), *args], capture_output=True, text=True)
        assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    return BUILD


# ---- 1. images ------------------------------------------------------------------------------------
def read_hex(path):
    mem, addr = {}, None
    for ln in Path(path).read_text().split():
        if ln.startswith("@"):
            addr = int(ln[1:], 16)
        else:
            mem[addr] = int(ln, 16)
            addr += 1
    return mem


@pytest.mark.parametrize("name", EXAMPLES + ["overhead"])
def test_images(built, name):
    d = built / name
    for ext in ("elf", "bin", "hex", "lst"):
        assert (d / f"{name}.{ext}").is_file(), ext
    img = (d / f"{name}.bin").read_bytes()
    mem = read_hex(d / f"{name}.hex")
    assert max(mem) + 1 == len(img), "the .bin ends at the last loaded byte"
    assert all(img[a] == v for a, v in mem.items()), ".bin and .hex disagree"
    assert all(img[a] == 0xFF for a in range(len(img)) if a not in mem), "gaps are erased flash"
    assert img[:4] == bytes.fromhex("93000000"), "crt0 (li x1, 0) at address 0"
    assert len(img) < 16 << 20


def test_no_division_or_multiply_calls_in_hot_paths(built):
    """micros(), millis() and the trap handler use shifts and adds only (RV32E has no M)."""
    lst = (built / "ir_remote" / "ir_remote.lst").read_text()
    for fn in ("micros", "millis", "r32v_trap", "timer_isr", "delayMicroseconds"):
        m = re.search(rf"^[0-9a-f]+ <{fn}>:\n(.*?)\n\n", lst, re.S | re.M)
        if m is None:
            continue  # inlined
        assert not re.search(r"<__(mul|div|udiv|mod|umod)\w*>", m.group(1)), fn


@pytest.mark.parametrize("name", EXAMPLES + ["overhead"])
def test_gp_tp_reserved_for_the_trap(built, name):
    """trap.S uses gp and tp as scratch without saving them: no other code may touch them
    (crt0 only zeroes them)."""
    fn, bad = None, []
    for ln in (built / name / f"{name}.lst").read_text().splitlines():
        m = re.match(r"^[0-9a-f]+ <(\S+)>:$", ln)
        if m:
            fn = m.group(1)
            continue
        m = re.match(r"^\s*[0-9a-f]+:\s+[0-9a-f]+\s+(\S+)\s*(\S*)", ln)
        if m and fn not in ("_start", "r32v_trap"):
            if {"gp", "tp"} & set(re.split(r"[,()]", m.group(2))):
                bad.append((fn, ln.strip()))
    assert bad == [], bad[:5]
    assert "__global_pointer$" not in (built / name / f"{name}.map").read_text()


# ---- 2. the NEC decoder on synthetic waveforms ----------------------------------------------------
def synth(frames_us, period=1316, high=438, t0=100_000):
    """A pin dump: frames_us = list of (start_us, [mark, space, mark, ...] in us) on uo[1]."""
    rows = [(1, 0)]
    for start, seq in frames_us:
        t = t0 + start * CYC_US
        for k, d in enumerate(seq):
            n = round(d * CYC_US)
            if k % 2 == 0:  # a mark: carrier pulses from t, cut at t + n
                c = t
                while c < t + n:
                    rows.append((c, 0x02))
                    rows.append((min(c + high, t + n), 0x00))
                    c += period
            t += n
    return rows


def nec_seq(data, spaces=None):
    seq = [9000, 4500]
    for i in range(32):
        seq += [562.5, 1687.5 if (data >> i) & 1 else 562.5]
    seq.append(562.5)
    if spaces:
        for i, s in spaces.items():
            seq[i] = s
    return seq


def gate_errors(frame, nominal_us, slack=20):
    """[(index, nominal cycles, [lo, hi])] for each mark or space of a decoded frame whose
    nominal length lies outside the gate bounds (nec_decode: the gate's own timing from the
    carrier pulses), with `slack` cycles of margin (the SDK rounds to 16-cycle timer ticks)."""
    out = []
    for i, (us, (lo, hi)) in enumerate(zip(nominal_us, frame["bounds"])):
        n = round(us * CYC_US)
        if not lo - slack <= n <= hi + slack:
            out.append((i, n, [lo, hi]))
    return out


def test_nec_decoder_synthetic():
    data = 0x04 | (0xFB << 8) | (0x08 << 16) | (0xF7 << 24)
    rows = synth([(0, nec_seq(data)), (110_000, [9000, 2250, 562.5])])
    frames, car = nec_decode.decode(rows)
    assert [f["kind"] for f in frames] == ["nec", "repeat"], frames
    f = frames[0]
    assert (f["address"], f["command"], f["address_inv_ok"], f["command_inv_ok"]) == \
        (0x04, 0x08, True, True)
    assert car["period_cycles"] == 1316 and car["high_cycles"] == 438
    assert gate_errors(f, nec_seq(data)) == [] and gate_errors(frames[1], [9000, 2250, 562.5]) == []
    # a 16-bit address, and timings 8 % off still decode; 15 % off does not
    rows = synth([(0, nec_seq(0x8877 | (0x12 << 16) | (0xED << 24)))])
    assert nec_decode.decode(rows)[0][0]["address16"] == 0x8877
    slow = nec_seq(data, {2: 562.5 * 1.08, 7: 1687.5 * 0.92})  # bit 0's mark, bit 2's space (a 1)
    f = nec_decode.decode(synth([(0, slow)]))[0][0]
    assert f["command"] == 0x08 and 0.079 < f["worst_timing_error"] < 0.081
    assert [e[0] for e in gate_errors(f, nec_seq(data))] == [2, 7]
    bad = nec_seq(data, {5: 562.5 * 1.15})
    assert nec_decode.decode(synth([(0, bad)]))[0][0]["kind"] == "junk"
    assert nec_decode.decode(synth([(0, [9000 * 1.2, 4500] + nec_seq(data)[2:])]))[0][0]["kind"] \
        == "junk"


# ---- 3. the examples on the RTL ---------------------------------------------------------------------
def run_rtl(tmp, name, expect, max_cycles, mode="pins", seed=1):
    sys.path.insert(0, str(ROOT / "verif" / "cosim"))
    import run_cosim
    d = tmp / f"{name}-{mode}"
    d.mkdir()
    hexf = d / f"{name}.hex"
    shutil.copy(BUILD / name / f"{name}.hex", hexf)
    (d / f"{name}.expect.json").write_text(json.dumps(expect))
    dump = d / "pins.txt"
    summ, res = run_cosim.run_one(mode, hexf, seed, max_cycles, dump_pins=dump, clk_ns=20.0)
    rows, end = nec_decode.read_dump(dump)
    return summ, res, rows, end


def ran_to_budget(summ, max_cycles):
    """A forever-looping example passed every harness check up to its cycle budget."""
    err = summ["error"] or ""
    assert not summ["pass"] and re.search(rf"timeout after {max_cycles + 1} cycles \(--max-cycles\)",
                                          err), summ


def pin_edges(rows, pin):
    return nec_decode.edges(rows, pin)


def periods(ed):
    """[(rise, period, high)] for consecutive rising edges."""
    rises = [(c, i) for i, (c, lv) in enumerate(ed) if lv]
    out = []
    for (r0, i0), (r1, _) in zip(rises, rises[1:]):
        f = ed[i0 + 1][0] if i0 + 1 < len(ed) else None
        out.append((r0, r1 - r0, f - r0 if f else None))
    return out


@needs_rtl
def test_blink_on_rtl(built, tmp_path):
    mc = 56_000_000
    summ, _, rows, _ = run_rtl(tmp_path, "blink", {"result_gpio": 0x5A}, mc)
    ran_to_budget(summ, mc)
    ed = [e for e in pin_edges(rows, 7) if e[0] > 1]
    assert [lv for _, lv in ed] == [1, 0, 1], ed
    for (a, _), (b, _) in zip(ed, ed[1:]):
        assert abs((b - a) - 25_000_000) <= 50_000, (b - a)  # 500 ms within 1 ms


@needs_rtl
def test_c_blink_on_rtl(built, tmp_path):
    mc = 40_000_000
    summ, res, rows, _ = run_rtl(tmp_path, "c_blink", {"result_gpio": 0x5A, "uart_div": UART_DIV},
                                 mc)
    ran_to_budget(summ, mc)
    text = res["uart_tx"]
    assert text.startswith("c_blink\r\n"), text
    ms = [int(x) for x in text.split("\r\n")[1:-1]]  # complete lines only
    assert len(ms) >= 3 and ms[0] <= 5, ms
    assert all(250 <= b - a <= 252 for a, b in zip(ms, ms[1:])), ms
    ed = [c for c, _ in pin_edges(rows, 6)[1:]]
    assert len(ed) >= 3 and all(abs((b - a) - 12_500_000) <= 100_000 for a, b in zip(ed, ed[1:]))


@needs_rtl
def test_fade_on_rtl(built, tmp_path):
    mc = 12_000_000
    summ, _, rows, _ = run_rtl(tmp_path, "fade", {"result_gpio": 0x5A}, mc)
    ran_to_budget(summ, mc)
    runs = []  # (start, high) for runs of at least 3 identical full periods of 1,023 cycles
    for r, p, h in periods(pin_edges(rows, 2)):
        if p == 1023 and h is not None:
            if runs and runs[-1][1] == h:
                runs[-1][2] += 1
            else:
                runs.append([r, h, 1])
    steps = [(r, h) for r, h, n in runs if n >= 3]
    highs = [h for _, h in steps]
    assert len(highs) >= 15 and highs == [31 * (k + 1) for k in range(len(highs))], highs
    # a step every 10 ms: delay(10) plus the loop's own instructions (analogWrite() and the
    # duty arithmetic, at about 100 cycles each), under 0.4 ms
    gaps = [b - a for (a, _), (b, _) in zip(steps, steps[1:])]
    assert all(500_000 <= g <= 520_000 for g in gaps), gaps


SERIAL_RX = [[300_000, ord("h")], [360_000, ord("i")], [420_000, ord("\r")]]


@needs_rtl
@pytest.mark.parametrize("mode", ["pins", "rvfi"])
def test_serial_echo_on_rtl(built, tmp_path, mode):
    mc = 1_200_000
    exp = {"result_gpio": 0x5A, "uart_div": UART_DIV, "uart_rx": SERIAL_RX}
    summ, res, _, _ = run_rtl(tmp_path, "serial_echo", exp, mc, mode)
    ran_to_budget(summ, mc)
    assert res["uart_tx"] == "BonettoR32V serial echo\r\nhi\r\n[3 bytes]\r\n", res["uart_tx"]


@needs_rtl
def test_serial_burst_on_rtl(built, tmp_path):
    """17 bytes back to back at 115,200 baud (a frame every 4,350 cycles, 10 cycles of idle
    between frames): trap.S must take each byte before the next one lands (the UART holds one),
    and the ring buffer holds them while the sketch echoes."""
    mc = 2_200_000
    msg = "0123456789abcdef\r"
    rx = [[300_000 + i * 4_350, ord(c)] for i, c in enumerate(msg)]
    exp = {"result_gpio": 0x5A, "uart_div": UART_DIV, "uart_rx": rx}
    summ, res, _, _ = run_rtl(tmp_path, "serial_echo", exp, mc)
    ran_to_budget(summ, mc)
    assert res["uart_tx"] == "BonettoR32V serial echo\r\n" + msg + "\n[17 bytes]\r\n", res["uart_tx"]


@needs_rtl
def test_button_on_rtl(built, tmp_path):
    mc = 3_500_000
    script = [[0, 0x02], [400_000, 0x06], [400_400, 0x02], [400_800, 0x06], [500_000, 0x02],
              [2_500_000, 0x06], [2_600_000, 0x02]]  # ui1 (RX) stays high; ui2 bounces once
    exp = {"result_gpio": 0x5A, "uart_div": UART_DIV, "gpio_script": script}
    summ, res, rows, _ = run_rtl(tmp_path, "button", exp, mc)
    ran_to_budget(summ, mc)
    assert res["uart_tx"] == "press the button on UI2\r\npresses: 1\r\npresses: 2\r\n", res
    assert [lv for _, lv in pin_edges(rows, 7)] == [0, 1, 0]


@needs_rtl
def test_tone_on_rtl(built, tmp_path):
    mc = 54_000_000
    summ, _, rows, _ = run_rtl(tmp_path, "tone", {"result_gpio": 0x5A}, mc)
    ran_to_budget(summ, mc)
    ed = pin_edges(rows, 7)[1:]
    # bursts of toggling separated by silences of more than 1 ms
    bursts, cur = [], [ed[0][0]] if ed else []
    for (a, _), (b, _) in zip(ed, ed[1:]):
        if b - a > CYC_US * 1000:
            bursts.append(cur)
            cur = []
        cur.append(b)
    bursts.append(cur)
    notes = [523, 659, 784, 1047]
    assert len(bursts) >= 4, [len(b) for b in bursts]
    for hz, b in zip(notes, bursts):
        half = (F_CPU >> 5) // hz * 16  # TB_P = 4: a half period of (F_CPU / 32 / hz) ticks
        gaps = {y - x for x, y in zip(b, b[1:])}
        assert gaps == {half}, (hz, sorted(gaps)[:5], half)
        dur_ms = (b[-1] - b[0]) / F_CPU * 1000
        assert 150 - 1000 / hz <= dur_ms <= 150 + 1000 / hz, (hz, dur_ms)
    for x, y in zip(bursts, bursts[1:4]):  # a note every 200 ms
        assert abs((y[0] - x[0]) - 10_000_000) <= 200_000, (y[0] - x[0])
    # the PWM beep: 1 kHz from PER and Q (49,984 cycles), half on; the first period after the
    # tone() call may be cut, since a new PWM setting takes effect within a period (contract 5.4)
    pw = [(p, h) for _, p, h in periods(pin_edges(rows, 2)) if h is not None]
    assert len(pw) > 150 and pw[0][0] <= 49_984 and pw[0][1] <= 24_960, pw[:2]
    assert {p for p, _ in pw[1:]} == {49_984} and {h for _, h in pw[1:]} == {24_960}, pw[:4]
    beep = [c for c, _ in pin_edges(rows, 2)[1:]]
    assert abs((beep[-1] - beep[0]) - 10_000_000) <= 150_000, beep[-1] - beep[0]


IR_SCRIPT = [[0, 0x02], [1_000_000, 0x06], [1_500_000, 0x02],      # power: a short press
             [6_000_000, 0x12], [21_000_000, 0x02],                # volume up: held 300 ms
             [30_000_000, 0x22], [30_500_000, 0x02]]               # volume down: short


@needs_rtl
@pytest.mark.parametrize("mode", ["pins", "rvfi"])
def test_ir_remote_on_rtl(built, tmp_path, mode):
    mc = 36_000_000
    exp = {"result_gpio": 0x5A, "uart_div": UART_DIV, "gpio_script": IR_SCRIPT}
    summ, res, rows, _ = run_rtl(tmp_path, "ir_remote", exp, mc, mode)
    ran_to_budget(summ, mc)
    frames, car = nec_decode.decode(rows, pin=1, fcpu=F_CPU, tol=0.10)
    kinds = [f["kind"] for f in frames]
    nec = [f for f in frames if f["kind"] == "nec"]
    reps = [i for i, k in enumerate(kinds) if k == "repeat"]
    assert "junk" not in kinds and len(nec) == 3, frames
    assert [(f["address"], f["command"]) for f in nec] == [(0x04, 0x08), (0x04, 0x02), (0x04, 0x03)]
    assert all(f["address_inv_ok"] and f["command_inv_ok"] for f in nec)
    # the gate's own timing (the timer edges, before the carrier quantizes them) is exact to a
    # timer tick for every mark and space of every frame and repeat
    for f in nec:
        bits = [1687.5 if (f["data"] >> i) & 1 else 562.5 for i in range(32)]
        nominal = [9000, 4500] + [x for b in bits for x in (562.5, b)] + [562.5]
        assert gate_errors(f, nominal) == [], (f["start"], gate_errors(f, nominal))
    for i in reps:
        assert gate_errors(frames[i], [9000, 2250, 562.5]) == [], frames[i]
    assert all(abs(f["lead_mark_us"] - 9000) <= 90 and abs(f["lead_space_us"] - 4500) <= 45
               for f in nec)
    # repeats only after volume up (held 300 ms), every 110 ms, the last one started while held
    assert kinds[:2] == ["nec", "nec"] and kinds[-1] == "nec" and len(reps) >= 2, kinds
    assert reps == list(range(2, 2 + len(reps)))
    starts = [frames[i]["start"] for i in [1] + reps]
    for a, b in zip(starts, starts[1:]):
        assert 109.5 <= (b - a) / F_CPU * 1000 <= 111.5, (b - a)
    # the sketch checks the button after each frame ends (a frame lasts 68.1 ms, a repeat
    # 11.8 ms): every repeat was decided while the button was held (released at cycle 21 M),
    # and the check after the last one saw it released
    release = 21_000_000
    ends = [starts[0] + 68_063 * CYC_US] + [s + 11_813 * CYC_US for s in starts[1:]]
    assert all(e <= release + 50_000 for e in ends[:-1]) and ends[-1] > release - 50_000, (
        starts, ends)
    assert len(reps) == 3, reps  # decisions at about 188, 243, 353 ms; 463 ms is past 420 ms
    # the carrier: 37.99 kHz (PER 657, Q 1: 1,316 cycles), duty 1/3 (438 cycles)
    assert car["period_min"] == car["period_max"] == 1316 and car["high_cycles"] == 438, car
    assert car["phase_ok"], car
    # LED_BUILTIN (uo7) is high around each frame; Serial reports the same frames
    led = pin_edges(rows, 7)[1:]
    assert [lv for _, lv in led] == [1, 0] * 3, led
    for f in nec:
        assert any(r <= f["start"] <= fl for (r, _), (fl, _) in zip(led[::2], led[1::2]))
    want = ("IR remote ready\r\nsent power (0x8), repeats 0\r\n"
            f"sent volume up (0x2), repeats {len(reps)}\r\nsent volume down (0x3), repeats 0\r\n")
    assert res["uart_tx"] == want, res["uart_tx"]


@needs_rtl
def test_interrupt_overhead_on_rtl(built, tmp_path):
    """The timebase interrupt (one per 20 ms) and tone()'s (one per half period): cycles per
    interrupt from the pin dump, written to build/overhead.json."""
    mc = 80_000_000
    exp = {"result_gpio": 0x5A, "final_altfn": 1, "uart_div": UART_DIV}
    summ, res, rows, _ = run_rtl(tmp_path, "overhead", exp, mc)
    assert summ["pass"], (summ["error"], res.get("uart_tx"))
    seg = []  # changes of the phase field uo[4:2] (uo0 is the UART line, uo7 the tone)
    for c, v in rows:
        if not seg or seg[-1][1] != (v >> 2) & 7:
            seg.append((c, (v >> 2) & 7))
    t = {p: c1 - c0 for (c0, p), (c1, _) in zip(seg, seg[1:]) if 1 <= p <= 4}
    assert sorted(t) == [1, 2, 3, 4], seg
    us = {int(m[1]): (int(m[2]), int(m[3]))
          for m in re.finditer(r"p(\d) (\d+) (\d+)", res["uart_tx"])}
    # micros() across each phase minus the phase's length from the pins: the instructions
    # between each micros() read and its GPIO_OUT store (interrupts off there) make it a little
    # positive and the same in every phase; a timer match lost during a tone would take half a
    # tone period off it (500 us at 1 kHz, 250 us at 2 kHz)
    drift = {p: round((us[p][1] - us[p][0]) - t[p] / CYC_US, 1) for p in (2, 3, 4)}
    n_tb = us[2][1] // 20_000 - us[2][0] // 20_000
    out = {"busy_loop_cycles_irq_off": t[1], "timebase": {"interrupts": n_tb}}
    out["timebase"]["cycles_per_interrupt"] = round((t[2] - t[1]) / n_tb)
    out["timebase"]["cpu_percent"] = round(100 * (t[2] - t[1]) / t[2], 3)
    for p, hz in ((3, 1000), (4, 2000)):
        per_irq = (F_CPU >> 5) // hz * 16
        n = t[p] / per_irq
        out[f"tone_{hz}hz"] = {"interrupts": round(n),
                               "cycles_per_interrupt": round((t[p] - t[1]) / n),
                               "cpu_percent": round(100 * (t[p] - t[1]) / t[p], 2)}
    out["micros_minus_pins_us"] = drift
    (BUILD / "overhead.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out))
    assert n_tb >= 5 and out["timebase"]["cpu_percent"] < 2.0, out
    assert out["tone_1000hz"]["cpu_percent"] < 25 and out["tone_2000hz"]["cpu_percent"] < 50, out
    assert 0 <= drift[2] <= 200 and all(abs(drift[p] - drift[2]) <= 50 for p in (3, 4)), drift
