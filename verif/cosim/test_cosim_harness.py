"""Tests for the cosim harness (contract v3: verif/cosim/harness.cpp, periph_models.h,
run_cosim.py). The contract v2 harness and its tests were deleted at the integration swap (plan
Task 9, ruling R26).

1. The peripheral pin models (periph_models.h) through their unit-test binary test_periph:
   UART RX bit edges at round(k (DIV+1) (1 +- 1%)), UART TX exact bit times, the SPI echo device
   (mode 0, MISO held through 3 cycles after each rising edge), the PWM meter.
2. The harness on hand-written stub designs. test_fixtures/playback replays a pin script and an
   RVFI script given as plusargs (one Verilator build per mode); the scripts come from tiny
   hand-assembled programs run on the reference ISS (iss_main --log), so every retirement
   matches the ISS unless a test changes it on purpose.
3. The tcem_stall mutant (ruling R12), run_cosim.py and its program list (ruling R18), the
   liveness fuzz.

Every program here is hand-encoded (no RISC-V compiler needed), so the suite runs on hwe-bench.
Run: pytest verif/cosim/test_cosim_harness.py -v
"""
import json
import subprocess
import sys
from fractions import Fraction
from math import floor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COSIM = ROOT / "verif" / "cosim"
TOOLS = ROOT / "build" / "cosim"
PLAYBACK = COSIM / "test_fixtures" / "playback"
TCEM = COSIM / "mutants" / "tcem_stall"
sys.path.insert(0, str(COSIM))
import run_cosim  # noqa: E402


@pytest.fixture(scope="session")
def tools():
    p = subprocess.run(["bash", str(COSIM / "build.sh")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-3000:]
    return TOOLS


# ------------------------------------------------------------------------------------------------
# a tiny RV32 encoder and the flash image
def _i(op, f3, rd, rs1, imm):
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op


def _s(f3, rs2, imm, rs1):
    return (((imm >> 5) & 0x7F) << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | \
        ((imm & 0x1F) << 7) | 0x23


def lui(rd, imm20): return ((imm20 & 0xFFFFF) << 12) | (rd << 7) | 0x37
def addi(rd, rs1, imm): return _i(0x13, 0, rd, rs1, imm)
def lw(rd, imm, rs1): return _i(0x03, 2, rd, rs1, imm)
def sb(rs2, imm, rs1): return _s(0, rs2, imm, rs1)
def sw(rs2, imm, rs1): return _s(2, rs2, imm, rs1)
def csrrw(rd, csr, rs1): return (csr << 20) | (rs1 << 15) | (1 << 12) | (rd << 7) | 0x73
def csrrs(rd, csr, rs1): return (csr << 20) | (rs1 << 15) | (2 << 12) | (rd << 7) | 0x73
def csrrsi(rd, csr, zimm): return (csr << 20) | (zimm << 15) | (6 << 12) | (rd << 7) | 0x73


ECALL = 0x00000073
WFI = 0x10500073
SELF = 0x0000006F  # jal x0, 0: the program end (retired twice)
GPIO_OUT, ALTFN, UART_DATA, UART_CTRL, SPI_DATA, SPI_CTRL = 0, 8, 44, 48, 52, 56  # slot offsets


def flash_hex(path: Path, segments: dict) -> Path:
    """A sparse $readmemh flash image: {byte address: [32-bit words]}."""
    lines = []
    for addr in sorted(segments):
        lines.append(f"@{addr:x}")
        lines += [" ".join(f"{(w >> (8 * b)) & 0xFF:02x}" for b in range(4))
                  for w in segments[addr]]
    path.write_text("\n".join(lines) + "\n")
    return path


def iss_log(hexf: Path, *args):
    """The ISS's retirements for a program (iss_main --log), interrupt entries left out."""
    p = subprocess.run([str(TOOLS / "iss_main"), str(hexf), "--log", "--json", "--max",
                        "1000", *args], capture_output=True, text=True)
    rows = [json.loads(ln) for ln in p.stdout.splitlines() if ln.startswith("{")]
    assert rows and rows[-1].get("summary") and rows[-1]["end"] == "selfloop", (p.stdout, p.stderr)
    return [r for r in rows[:-1] if not r.get("irq_entry")]


# ------------------------------------------------------------------------------------------------
# the playback stub's scripts
SD0, SD1, SCK, SD2, SD3, CSF, CSR = 0x02, 0x04, 0x08, 0x10, 0x20, 0x01, 0x40
IDLE_UIO, IDLE_OE = 0xC1, 0xC9  # both CS_n high, PSRAM B CS_n high, SCK low; fixed outputs


def nib_uio(n):
    return ((n & 1) << 1) | (((n >> 1) & 1) << 2) | (((n >> 2) & 1) << 4) | (((n >> 3) & 1) << 5)


def single(value, n):
    return [(SD0 if (value >> (n - 1 - i)) & 1 else 0, IDLE_OE | SD0) for i in range(n)]


def quad(value, n):
    return [(nib_uio((value >> (4 * (n - 1 - i))) & 0xF), 0xFF) for i in range(n)]


class Pins:
    """A pin script: uo_out and the Pmod pins, each held from the cycle it is set."""

    def __init__(self):
        self.uo, self.bus = {}, {}

    def set_uo(self, c, v):
        self.uo[c] = v
        return self

    def txn(self, start, cs, edges):
        """One transaction with chip select bit cs; edges: (uio data bits, uio_oe) per rising SCK
        edge, or ("pause", n) to hold SCK low n cycles. SCK = clk/2. Returns the first cycle
        after it with CS_n high again (one cycle of tCPH)."""
        low, t, last = IDLE_UIO & ~cs, start, (0, IDLE_OE)
        for e in edges:
            if e[0] == "pause":
                t += e[1]
                continue
            self.bus[t] = (low | e[0], e[1])
            self.bus[t + 1] = (low | e[0] | SCK, e[1])
            last, t = e, t + 2
        self.bus[t] = (low | last[0], last[1])
        self.bus[t + 1] = (IDLE_UIO, IDLE_OE)
        return t + 2

    def psram_reset(self, start):
        """Contract 2.4: QPI-form 0xF5, then 0x66 and 0x99 in SPI form; tRST after."""
        t = self.txn(start, CSR, quad(0xF5, 2))
        t = self.txn(t + 1, CSR, single(0x66, 8))
        t = self.txn(t + 1, CSR, single(0x99, 8))
        return t + 3

    def psram_write(self, start, addr, data):
        edges = single(0x02, 8) + single(addr, 24)
        for b in data:
            edges += single(b, 8)
        return self.txn(start, CSR, edges)

    def uart_frame(self, start, byte, bit, base):
        """8N1 on uo[0], LSB first; base: the other uo bits."""
        bits = [0] + [(byte >> k) & 1 for k in range(8)] + [1]
        for k, b in enumerate(bits):
            self.set_uo(start + k * bit, base | b)
        return start + 10 * bit

    def spi_xfer(self, start, byte, half, base, glitch_at=None):
        """SPI mode 0 on uo[5] SCK, uo[6] MOSI (MSB first), each phase `half` cycles; base: the
        other uo bits (uo[4] = CS_n). glitch_at: flip MOSI at that cycle."""
        t = start
        for k in range(7, -1, -1):
            m = ((byte >> k) & 1) << 6
            self.set_uo(t, base | m)
            self.set_uo(t + half, base | m | 0x20)
            t += 2 * half
        self.set_uo(t, base | m)
        if glitch_at is not None:
            prev = max(c for c in self.uo if c <= glitch_at)
            self.set_uo(glitch_at, self.uo[prev] ^ 0x40)
            self.set_uo(glitch_at + 1, self.uo[prev])
        return t

    def write(self, path: Path):
        words, uo, uio, oe = [], 0, IDLE_UIO, IDLE_OE
        for c in sorted(set(self.uo) | set(self.bus)):
            uo = self.uo.get(c, uo)
            uio, oe = self.bus.get(c, (uio, oe))
            words += [c, (oe << 16) | (uio << 8) | uo]
        words += [0xFFFFFFFF, 0]
        path.write_text("".join(f"{w:08x}\n" for w in words))


def rvfi_rom(path: Path, rets, cycles):
    words = []
    for order, (r, c) in enumerate(zip(rets, cycles)):
        flags = (r["trap"] & 1) | ((r["intr"] & 1) << 2) | (3 << 4) | (1 << 6)
        words += [c, order & 0xFFFFFFFF, order >> 32, r["insn"], flags, r["rs1"], r["rs2"],
                  r["rs1v"], r["rs2v"], r["rd"], r["rdv"], r["pc"], r["npc"], r["maddr"],
                  r["rmask"], r["wmask"], r["mrdata"], r["mwdata"]]
    words += [0xFFFFFFFF] + [0] * 17
    path.write_text("".join(f"{w & 0xFFFFFFFF:08x}\n" for w in words))


_n = [0]


def stub(mode, hexf, tmp, pins=None, rets=None, cycles=None, expect=None, seed=1, extra=(),
         src=PLAYBACK):
    """Run the harness on the playback stub. Returns the JSON summary."""
    exe = run_cosim.ensure_built(mode, src)
    _n[0] += 1
    cmd = [str(exe), str(hexf), "--seed", str(seed), *extra]
    if expect is not None:
        ep = tmp / f"expect{_n[0]}.json"
        ep.write_text(json.dumps(expect))
        cmd += ["--expect", str(ep)]
    if pins is not None:
        pp = tmp / f"pins{_n[0]}.hex"
        pins.write(pp)
        cmd.append(f"+PINS={pp}")
    if rets is not None and mode == "rvfi":
        rp = tmp / f"rvfi{_n[0]}.hex"
        rvfi_rom(rp, rets, cycles)
        cmd.append(f"+RVFI={rp}")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    lines = p.stdout.strip().splitlines()
    assert lines, (p.returncode, p.stderr[-3000:])
    res = json.loads(lines[-1])
    assert p.returncode == (0 if res["pass"] else 1), (p.returncode, res, p.stderr[-2000:])
    return res


def stub_setup_error(mode, hexf, tmp, expect):
    """A harness run that must stop as a setup error (exit 2, no JSON). Returns its stderr."""
    exe = run_cosim.ensure_built(mode, PLAYBACK)
    _n[0] += 1
    ep = tmp / f"expect{_n[0]}.json"
    ep.write_text(json.dumps(expect))
    p = subprocess.run([str(exe), str(hexf), "--expect", str(ep)], capture_output=True, text=True,
                       timeout=300)
    assert p.returncode == 2, (p.returncode, p.stdout[-2000:], p.stderr[-2000:])
    return p.stderr


def every(n, step=100, first=100):
    return [first + step * k for k in range(n)]


def wave(pins, lo, hi, f):
    """Set uo for every cycle in [lo, hi) from f(cycle), recording only the changes."""
    prev = None
    for c in range(lo, hi):
        v = f(c)
        if v != prev:
            pins.set_uo(c, v)
            prev = v
    return pins


# ------------------------------------------------------------------------------------------------
# 1. the peripheral pin models (test_periph.cpp)
@pytest.fixture(scope="session")
def periph(tools):
    p = subprocess.run([str(tools / "test_periph")], capture_output=True, text=True)
    return p.stdout.splitlines()


def case(lines, name):
    hit = [ln for ln in lines if ln.split(":")[0] in (f"PASS {name}", f"FAIL {name}")]
    assert hit == [f"PASS {name}"], hit or lines


def test_uart_rx_rate_and_overrun(periph):
    """The UartTerm RX driver puts bit edge k (0 = start, 9 = stop) at
    round(k (DIV+1) (1 + ppm / 10^6)) after the frame start: +1% and -1% here, recomputed with
    exact fractions. A second byte queued while the first is on the line starts right at the end
    of the first's stop bit (the overrun case; the DUT-side check is Task 4's uart_echo)."""
    case(periph, "uart_rx_edges")
    case(periph, "uart_rx_back_to_back")
    seen = set()
    for ln in periph:
        if not ln.startswith("EDGES uart_rx "):
            continue
        head, edges = ln[len("EDGES uart_rx "):].split(":")
        div, ppm = (int(x) for x in head.split())
        got = [int(x) for x in edges.split()]
        want = [floor(Fraction(k * (div + 1)) * (1 + Fraction(ppm, 10**6)) + Fraction(1, 2))
                for k in range(10)]
        assert got == want, (div, ppm, got, want)
        seen.add(ppm)
    assert {10000, -10000, 0} <= seen


def test_uart_tx_decoder(periph):
    case(periph, "uart_tx_exact")
    case(periph, "uart_tx_framing")


def test_spi_device_mode0(periph):
    """The echo device returns the previous byte (0xFF first), each MISO bit held from its rising
    SCK edge through 3 cycles after it; MOSI changing while SCK is high (or at the rising edge)
    fails, and so do high or low phases other than 2^D."""
    case(periph, "spi_device_mode0")
    case(periph, "spi_device_errors")


def test_pwm_meter(periph):
    """Period (PER+1) 2^Q and high time min(DUTY, PER+1) 2^Q, measured exactly after one settling
    period (duty 0: always 0; duty above PER: always 1); the timer's PWM mode (period 65,536)."""
    case(periph, "pwm_meter")
    case(periph, "gpio_stim_and_loop")


# ------------------------------------------------------------------------------------------------
# 2. the harness on stubs
PROG_LOOP = [SELF]
PROG_ST = [lui(1, 0x1000), addi(2, 0, 0x5A), sb(2, 0, 1), SELF]  # PSRAM[0] = 0x5A


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_uart_tx_exact_bit_time(tools, tmp_path, mode):
    """A stub sending 'U' with a bit time of 17 cycles passes with uart_div = 16 and fails with
    uart_div = 15 (contract 5.5: each bit exactly DIV+1 cycles)."""
    prog = [lui(1, 0x80000), addi(2, 0, 1), sw(2, ALTFN, 1),      # ALTFN = 1: uo[0] is UART TX
            addi(2, 0, 16), sw(2, UART_CTRL, 1),                 # DIV = 16
            addi(2, 0, 0x55), sw(2, UART_DATA, 1),               # send 'U' (retires at 700)
            addi(3, 0, 0x5A), sw(3, GPIO_OUT, 1), sw(0, ALTFN, 1),  # result, ALTFN = 0
            SELF]
    hexf = flash_hex(tmp_path / "uart.hex", {0: prog})
    rets = iss_log(hexf)
    assert len(rets) == 12
    pins = Pins().set_uo(301, 0x01)                 # ALTFN = 1: the TX line idles high
    assert pins.uart_frame(705, 0x55, 17, 0x00) == 875
    pins.set_uo(901, 0x5B).set_uo(1001, 0x5A)       # GPIO_OUT = 0x5A, then ALTFN = 0
    exp = {"uart_div": 16, "uart_tx_expect": "U", "result_gpio": 0x5A}
    r = stub(mode, hexf, tmp_path, pins, rets, every(12), exp)
    assert r["pass"], r["error"]
    assert r["uart_tx"] == "U" and r["end"] == "selfloop"
    r = stub(mode, hexf, tmp_path, pins, rets, every(12), dict(exp, uart_div=15))
    assert not r["pass"] and "not on a bit boundary" in r["error"], r["error"]
    # R28 (contract 5.5, 7c5740e): the start bit begins within DIV+2 cycles after the UART_DATA
    # store retires (cycle 700): a start at 718 passes, at 719 fails (rvfi mode sees the store)
    for start, ok in ((718, True), (719, False)):
        late = Pins().set_uo(301, 0x01)
        late.uart_frame(start, 0x55, 17, 0x00)
        late.set_uo(901, 0x5B).set_uo(1001, 0x5A)
        r = stub(mode, hexf, tmp_path, late, rets, every(12), exp)
        if mode == "pins" or ok:
            assert r["pass"], (start, r["error"])
        else:
            assert not r["pass"] and "no UART start bit within DIV+2" in r["error"], r["error"]
            assert "cycle 719:" in r["error"], r["error"]


def test_uart_busy_read_follows_pins(tools, tmp_path):
    """R20 (contract 5.5 as of c7d9910): a TX_BUSY read must be 1 while the pins show the frame in
    progress over the whole reading instruction, 0 when no frame is in progress or pending, either
    when the frame ends during the instruction."""
    prog = [lui(1, 0x80000), addi(2, 0, 1), sw(2, ALTFN, 1), addi(2, 0, 16), sw(2, UART_CTRL, 1),
            addi(2, 0, 0x55), sw(2, UART_DATA, 1),   # #6 retires at 700, frame 705..875
            addi(5, 0, 0),                           # #7
            lw(4, UART_CTRL, 1),                     # #8 reads TX_BUSY
            addi(3, 0, 0x5A), sw(3, GPIO_OUT, 1), sw(0, ALTFN, 1), SELF]
    hexf = flash_hex(tmp_path / "busy.hex", {0: prog})

    def run(busy, t7, t8):
        rets = iss_log(hexf, "--vread", f"12={0x2010 if busy else 0x0010}")
        assert len(rets) == 14 and rets[8]["mrdata"] == (0x2010 if busy else 0x0010)
        cycles = every(7) + [t7, t8] + [1100, 1200, 1300, 1400, 1500]
        pins = Pins().set_uo(301, 0x01)
        pins.uart_frame(705, 0x55, 17, 0x00)
        pins.set_uo(1201, 0x5B).set_uo(1301, 0x5A)
        return stub("rvfi", hexf, tmp_path, pins, rets, cycles,
                    {"uart_div": 16, "uart_tx_expect": "U", "result_gpio": 0x5A})

    assert run(True, 720, 760)["pass"]                         # inside the frame: 1
    r = run(False, 720, 760)
    assert not r["pass"] and "TX_BUSY (UART_CTRL[13]) read 0" in r["error"], r["error"]
    assert run(False, 900, 1000)["pass"]                       # after the frame: 0
    r = run(True, 900, 1000)
    assert not r["pass"] and "TX_BUSY (UART_CTRL[13]) read 1" in r["error"], r["error"]
    assert run(True, 860, 880)["pass"] and run(False, 860, 880)["pass"]  # the frame ends inside


def test_rvfi_intr_follow(tools, tmp_path):
    """rvfi mode: a retirement with rvfi_intr = 1 that doesn't follow a trap is an interrupt entry
    (Iss3::take_interrupt); it fails with "interrupt while masked" unless mstatus.MIE and mie.MEIE
    are set. A trap handler's first instruction (rvfi_intr = 1 after a trap) is no interrupt."""
    prog = {0: [addi(1, 0, 0x100), csrrw(0, 0x305, 1),          # mtvec = 0x100
                lui(2, 1), addi(2, 2, -2048), csrrs(0, 0x304, 2),  # mie.MEIE = 1 (#4)
                csrrsi(0, 0x300, 8),                               # mstatus.MIE = 1 (#5)
                SELF],
            0x100: [SELF]}
    hexf = flash_hex(tmp_path / "irq.hex", prog)
    taken = iss_log(hexf, "--irq", "6")
    assert len(taken) == 8 and taken[6]["intr"] == 1 and taken[6]["pc"] == 0x100
    r = stub("rvfi", hexf, tmp_path, Pins(), taken, every(8))
    assert r["pass"] and r["interrupts"] == 1, r["error"]
    plain = iss_log(hexf)
    masked = plain[:5] + taken[6:]  # the interrupt after #4: MEIE = 1 but MIE = 0
    r = stub("rvfi", hexf, tmp_path, Pins(), masked, every(7))
    assert not r["pass"] and "interrupt while masked" in r["error"], r["error"]
    assert "retirement #5" in r["error"] and "mstatus.MIE = 0" in r["error"], r["error"]
    trap = {0: [addi(1, 0, 0x100), csrrw(0, 0x305, 1), ECALL], 0x100: [SELF]}
    hext = flash_hex(tmp_path / "trap.hex", trap)
    rets = iss_log(hext)
    assert [x["intr"] for x in rets] == [0, 0, 0, 1, 0] and rets[2]["trap"] == 1
    r = stub("rvfi", hext, tmp_path, Pins(), rets, every(5))
    assert r["pass"] and r["interrupts"] == 0, r["error"]


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_psram_write_attribution(tools, tmp_path, mode):
    """Contract 8: outside the core region, every PSRAM write writes a byte of a (retired / the
    reference run's) store at its address with its data. Core-region writes are free."""
    hexf = flash_hex(tmp_path / "st.hex", {0: PROG_ST})
    rets = iss_log(hexf)

    def run(writes):
        pins = Pins()
        t = pins.psram_reset(10)
        for addr, data in writes:
            t = pins.psram_write(max(t, 150), addr, data)
        return stub(mode, hexf, tmp_path, pins, rets, every(5))

    r = run([(0, b"\x5a")])
    assert r["pass"] and r["psram_writes"] == 1, r["error"]
    for bad in ([(0, b"\x5b")], [(1, b"\x5a")], [(0, b"\x5a\x5a")]):
        r = run(bad)
        assert not r["pass"] and "matches no" in r["error"] and "contract 8" in r["error"], (
            bad, r["error"])
    r = run([(0, b"\x5a"), (0x7FFF80, b"\x01\x02\x03")])  # the core region: the core's own
    assert r["pass"], r["error"]


def test_latency_cap_2048(tools, tmp_path):
    """Contract 4: a retirement 2,048 cycles after the previous one passes, 2,049 fails; the first
    retirement at edge 2,048 after the last reset edge passes and 2,049 fails (seed 2 has a 6-edge
    reset); with no retirement the watchdog reports it at cycle 2,049 after the last one."""
    hexf = flash_hex(tmp_path / "loop.hex", {0: PROG_LOOP})
    rets = iss_log(hexf)
    assert len(rets) == 2
    for seed in (1, 2):
        r = stub("rvfi", hexf, tmp_path, Pins(), rets, [2048, 4096], seed=seed)
        assert r["pass"], r["error"]
        assert r["first_retire_cycle"] == 2048 and r["max_cycles_per_insn"] == 2048
        r = stub("rvfi", hexf, tmp_path, Pins(), rets, [2049, 2100], seed=seed)
        assert not r["pass"] and "latency cap: retirement #0 " in r["error"], r["error"]
        assert "came 2049 cycles after reset release" in r["error"], r["error"]
    assert stub("rvfi", hexf, tmp_path, Pins(), rets, [100, 2148])["pass"]
    r = stub("rvfi", hexf, tmp_path, Pins(), rets, [100, 2149])
    assert not r["pass"] and "retirement #1 " in r["error"], r["error"]
    assert "came 2049 cycles after the previous retirement" in r["error"], r["error"]
    r = stub("rvfi", hexf, tmp_path, Pins(), rets[:1], [100])
    assert r["error"].startswith("cycle 2149: latency cap: no retirement within 2048 cycles of "
                                 "the previous retirement"), r["error"]
    r = stub("rvfi", hexf, tmp_path, Pins(), [], [])
    assert r["error"].startswith("cycle 2049: latency cap: no retirement within 2048 cycles of "
                                 "reset release"), r["error"]


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_selfloop_drain(tools, tmp_path, mode):
    """Contract 4: memory is compared 2,048 cycles after the self-loop (rvfi mode sees it: the
    second jal x0, 0 retires at cycle 500, so the compare is at 2,548); a PSRAM write landing in
    that window still counts, one landing after it is missing. pins mode can't see the self-loop
    and waits (tail + 2) x 2,048 cycles after the pins reach the reference's final state (tail:
    the reference's retirements after its last GPIO change, all 5 here; pins at it from cycle 1)."""
    hexf = flash_hex(tmp_path / "st.hex", {0: PROG_ST})
    rets = iss_log(hexf)

    def run(start):
        pins = Pins()
        pins.psram_reset(10)
        assert pins.psram_write(start, 0, b"\x5a") == start + 82
        return stub(mode, hexf, tmp_path, pins, rets, every(5))

    r = run(2400)  # the write's last data edge at 2,479
    assert r["pass"], r["error"]
    late = 2600 if mode == "rvfi" else 1 + (5 + 2) * 2048 + 100  # tail: all 5 retirements
    r = run(late)
    assert not r["pass"] and "final PSRAM byte 0x000000" in r["error"], r["error"]
    assert "the program stored 0x5a" in r["error"], r["error"]
    if mode == "rvfi":
        assert "compared 2048 cycles after the program end" in r["error"], r["error"]


def test_gpio_one_cycle_rule(tools, tmp_path):
    """Contract 6, rvfi mode: GPIO pins take a stored value no later than 1 cycle after the store
    retires (they may change earlier, while the store executes) and change for no other reason."""
    prog = [lui(1, 0x80000), addi(2, 0, 0x5A), sw(2, GPIO_OUT, 1), SELF]  # the store retires at 300
    hexf = flash_hex(tmp_path / "g.hex", {0: prog})
    rets = iss_log(hexf)

    def run(*changes):
        pins = Pins()
        for c, v in changes:
            pins.set_uo(c, v)
        return stub("rvfi", hexf, tmp_path, pins, rets, every(5), {"result_gpio": 0x5A})

    assert run((301, 0x5A))["pass"]
    assert run((250, 0x5A))["pass"]
    r = run((302, 0x5A))
    assert not r["pass"] and "one cycle after the store" in r["error"], r["error"]
    r = run((150, 0x5A))
    assert not r["pass"] and "uo_out changed to 0x5a" in r["error"], r["error"]
    r = run((301, 0x5A), (400, 0x00), (401, 0x5A))
    assert not r["pass"] and "uo_out changed to 0x00" in r["error"], r["error"]


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_coremark_cycles_per_iter(tools, tmp_path, mode):
    """R21: coremark_cycles_per_iter = cycles between uo_out changing to 0xC0 and to 0xC1, divided
    by expect.json coremark_iters; null without coremark_iters; the marks are required with it."""
    prog = [lui(1, 0x80000), addi(2, 0, 0xC0), sw(2, GPIO_OUT, 1), addi(2, 0, 0xC1),
            sw(2, GPIO_OUT, 1), addi(2, 0, 0x5A), sw(2, GPIO_OUT, 1), SELF]
    hexf = flash_hex(tmp_path / "cm.hex", {0: prog})
    rets = iss_log(hexf)
    pins = Pins().set_uo(301, 0xC0).set_uo(501, 0xC1).set_uo(701, 0x5A)
    r = stub(mode, hexf, tmp_path, pins, rets, every(9), {"coremark_iters": 4, "result_gpio": 0x5A})
    assert r["pass"] and r["coremark_cycles_per_iter"] == 50, r
    r = stub(mode, hexf, tmp_path, pins, rets, every(9), {"coremark_iters": 3})
    assert r["pass"] and abs(r["coremark_cycles_per_iter"] - 200 / 3) < 1e-5, r
    r = stub(mode, hexf, tmp_path, pins, rets, every(9), {"result_gpio": 0x5A})
    assert r["pass"] and r["coremark_cycles_per_iter"] is None, r
    hexs = flash_hex(tmp_path / "st.hex", {0: PROG_ST})
    pst = Pins()
    pst.psram_reset(10)
    pst.psram_write(150, 0, b"\x5a")
    r = stub(mode, hexs, tmp_path, pst, iss_log(hexs), every(5), {"coremark_iters": 1})
    assert not r["pass"] and "coremark_iters" in r["error"], r["error"]
    assert r["coremark_cycles_per_iter"] is None


def test_pins_mode_judges_volatile_programs_by_result(tools, tmp_path):
    """A program that reads a volatile register (here GPIO_IN) can't run on the ISS alone, so pins
    mode judges it by its self-check: uo_out = result_gpio, held for 3 x 2,048 cycles; 0xEE fails
    at once; without result_gpio pins mode can't judge it."""
    prog = [lui(1, 0x80000), lw(2, 4, 1), addi(3, 0, 0x5A), sw(3, GPIO_OUT, 1), SELF]
    hexf = flash_hex(tmp_path / "vol.hex", {0: prog})
    exp = {"result_gpio": 0x5A}
    r = stub("pins", hexf, tmp_path, Pins().set_uo(5000, 0x5A), expect=exp)
    assert r["pass"] and r["end"] == "result_gpio" and r["deterministic"] is False, r
    assert r["retired"] is None and r["cycles"] == 5000
    r = stub("pins", hexf, tmp_path, Pins().set_uo(5000, 0x5A).set_uo(6000, 0x01)
             .set_uo(9000, 0x5A), expect=exp)
    assert r["pass"] and r["cycles"] == 9000, r
    r = stub("pins", hexf, tmp_path, Pins().set_uo(5000, 0xEE), expect=exp)
    assert not r["pass"] and "self-check failed" in r["error"], r["error"]
    r = stub("pins", hexf, tmp_path, Pins().set_uo(5000, 0x5A))
    assert not r["pass"] and "can't judge" in r["error"], r["error"]
    r = stub("pins", hexf, tmp_path, Pins(), expect=exp, extra=("--max-cycles", "20000"))
    assert not r["pass"] and "timeout after 20001 cycles" in r["error"], r["error"]
    # enabling interrupts (mie.MEIE and mstatus.MIE) makes a program volatile too, even if it
    # never reads an IO register: an interrupt-driven program may spin on memory forever alone
    irq = [lui(2, 1), addi(2, 2, -2048), csrrs(0, 0x304, 2), csrrsi(0, 0x300, 8), lui(1, 0x80000),
           addi(3, 0, 0x5A), sw(3, GPIO_OUT, 1), SELF]
    hexi = flash_hex(tmp_path / "irqen.hex", {0: irq})
    r = stub("pins", hexi, tmp_path, Pins().set_uo(5000, 0x5A), expect=exp)
    assert r["pass"] and r["end"] == "result_gpio" and r["deterministic"] is False, r


PROG_SPI = [lui(1, 0x80000), addi(2, 0, 0x70), sw(2, ALTFN, 1),    # uo[4..6]: CS_n, SCK, MOSI
            addi(2, 0, 2), sw(2, SPI_CTRL, 1),                     # CS_n = 0, D = 1
            addi(2, 0, 0xA5), sw(2, SPI_DATA, 1),                  # transfer 0xA5 (#6 at 700)
            sw(0, ALTFN, 1), addi(3, 0, 0x5A), sw(3, GPIO_OUT, 1), SELF]


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_spi_master_pins(tools, tmp_path, mode):
    """Contract 5.6 at the pins: MOSI changes only while SCK is low, phases are 2^D cycles, and the
    byte on MOSI is the byte the program wrote to SPI_DATA (rvfi mode: the retired store; pins
    mode, deterministic programs: the reference run's write, R33), and spi_tx_expect when given."""
    hexf = flash_hex(tmp_path / "spi.hex", {0: PROG_SPI})
    rets = iss_log(hexf)
    exp = {"spi_div_d": 1, "result_gpio": 0x5A}

    def run(byte, glitch=None, expect=exp):
        pins = Pins().set_uo(301, 0x10).set_uo(501, 0x00)  # CS_n high (reset 1), then low
        assert pins.spi_xfer(702, byte, 2, 0x00, glitch) == 734
        pins.set_uo(801, 0x00).set_uo(1001, 0x5A)
        return stub(mode, hexf, tmp_path, pins, rets, every(12), expect)

    r = run(0xA5)
    assert r["pass"], r["error"]
    r = run(0xA5, glitch=705)
    assert not r["pass"] and "MOSI (uo[6]) changed" in r["error"], r["error"]
    r = run(0xA4)
    assert not r["pass"] and "SPI MOSI sent 0xa4" in r["error"], r["error"]
    if mode == "pins":
        assert "reference run's SPI_DATA write #0 is 0xa5" in r["error"], r["error"]
    r = run(0xA5, expect=dict(exp, spi_tx_expect=[0xA5]))
    assert r["pass"], r["error"]
    r = run(0xA5, expect=dict(exp, spi_tx_expect=[0xA6]))
    assert not r["pass"] and "spi_tx_expect" in r["error"], r["error"]


def test_spi_div_d_must_be_at_least_1(tools, tmp_path):
    """R29 (contract 5.6, 7c5740e): D = 0 is UNSPECIFIED, so spi_div_d 0 is a setup error."""
    hexf = flash_hex(tmp_path / "spi.hex", {0: PROG_SPI})
    err = stub_setup_error("pins", hexf, tmp_path, {"spi_div_d": 0, "result_gpio": 0x5A})
    assert "spi_div_d must be an integer 1..7" in err, err


def test_spi_data_read_matches_miso(tools, tmp_path):
    """rvfi mode: an SPI_DATA read after a transfer returns the byte the echo device drove on MISO
    (0xFF before its first received byte)."""
    prog = PROG_SPI[:7] + [addi(5, 0, 0), lw(4, SPI_DATA, 1)] + PROG_SPI[7:]  # read at #8 (900)
    hexf = flash_hex(tmp_path / "spird.hex", {0: prog})
    pins = Pins().set_uo(301, 0x10).set_uo(501, 0x00)
    pins.spi_xfer(702, 0xA5, 2, 0x00)
    pins.set_uo(1001, 0x00).set_uo(1201, 0x5A)
    exp = {"spi_div_d": 1, "result_gpio": 0x5A}
    for v, ok in ((0xFF, True), (0x00, False)):
        rets = iss_log(hexf, "--vread", f"13={v}")
        r = stub("rvfi", hexf, tmp_path, pins, rets, every(14), exp)
        if ok:
            assert r["pass"], r["error"]
        else:
            assert not r["pass"] and "SPI_DATA read 0x00" in r["error"], r["error"]
            assert "the device sent 0xff on MISO" in r["error"], r["error"]


def test_spi_transfer_in_flight_at_end_fails(tools, tmp_path):
    """rvfi mode: a run that ends with an SPI transfer still in progress fails (the program keeps
    the SPI pins, final_altfn 0x70)."""
    prog = PROG_SPI[:7] + [addi(5, 0, 0), addi(3, 0, 0x5A), sw(3, GPIO_OUT, 1), SELF]
    hexf = flash_hex(tmp_path / "spiend.hex", {0: prog})
    rets = iss_log(hexf)
    pins = Pins().set_uo(301, 0x10).set_uo(501, 0x00)
    for k in range(4):  # four bits of 0xA5, then SCK stays low
        m = ((0xA5 >> (7 - k)) & 1) << 6
        pins.set_uo(702 + 4 * k, m).set_uo(704 + 4 * k, m | 0x20)
    pins.set_uo(718, 0x00).set_uo(1001, 0x0A)
    exp = {"spi_div_d": 1, "result_gpio": 0x5A, "final_altfn": 0x70}
    rets += [rets[-1]] * 30  # the self-loop keeps retiring while the harness waits for the transfer
    r = stub("rvfi", hexf, tmp_path, pins, rets, every(42), exp)
    assert not r["pass"] and "(first rising SCK edge at cycle 704) was still in progress" in (
        r["error"]), r["error"]


PROG_UART = [lui(1, 0x80000), addi(2, 0, 1), sw(2, ALTFN, 1),      # ALTFN = 1: uo[0] is UART TX
             addi(2, 0, 16), sw(2, UART_CTRL, 1),                  # DIV = 16
             addi(2, 0, 0x55), sw(2, UART_DATA, 1),                # send 'U' (retires at 700)
             addi(3, 0, 0x5A), sw(3, GPIO_OUT, 1), sw(0, ALTFN, 1),  # result, ALTFN = 0
             SELF]


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_uart_stop_bit_zero(tools, tmp_path, mode):
    """A frame whose stop bit is 0 and whose line then stays low: rvfi mode knows ALTFN, so it is a
    framing error at once; pins mode can't tell it from the pin going back to GPIO 0 (no byte),
    and then the reference's UART_DATA write has no frame (R33)."""
    hexf = flash_hex(tmp_path / "uart.hex", {0: PROG_UART})
    rets = iss_log(hexf)
    bits = [0] + [(0x55 >> k) & 1 for k in range(8)] + [0]

    def uo(c):
        line = bits[(c - 705) // 17] if 705 <= c < 705 + 170 else (0 if c >= 705 else 1)
        base = 0x5A if c >= 901 else 0x00
        return base | (line if c < 1001 else 0) if c >= 301 else 0

    pins = wave(Pins(), 1, 1200, uo)
    r = stub(mode, hexf, tmp_path, pins, rets, every(12), {"uart_div": 16, "result_gpio": 0x5A})
    if mode == "rvfi":
        assert not r["pass"] and "stop bit of 0" in r["error"], r["error"]
    else:
        assert not r["pass"] and "UART TX sent 0 bytes, the reference run wrote 1" in r["error"], (
            r["error"])


def test_uart_orphan_frame_fails(tools, tmp_path):
    """rvfi mode: a UART frame on the pins that no UART_DATA write started fails at the next
    retirement."""
    prog = PROG_UART[:5] + [addi(5, 0, 0), addi(6, 0, 0)] + PROG_UART[7:]  # no UART_DATA write
    hexf = flash_hex(tmp_path / "orphan.hex", {0: prog})
    rets = iss_log(hexf)
    pins = Pins().set_uo(301, 0x01)
    pins.uart_frame(605, 0x55, 17, 0x00)
    pins.set_uo(901, 0x5B).set_uo(1001, 0x5A)
    r = stub("rvfi", hexf, tmp_path, pins, rets, every(12), {"uart_div": 16, "result_gpio": 0x5A})
    assert not r["pass"] and "a UART TX frame started at cycle 605" in r["error"], r["error"]
    assert "is not a UART_DATA write" in r["error"], r["error"]


def test_uart_txidle_read_follows_pins(tools, tmp_path):
    """R30: IRQ_PEND.TXIDLE = !TX_BUSY, checked against the pins like TX_BUSY: 0 while the frame
    is in progress over the whole reading instruction, 1 when no frame is."""
    prog = PROG_UART[:7] + [addi(5, 0, 0), lw(4, 64, 1)] + PROG_UART[7:]  # IRQ_PEND read, #8
    hexf = flash_hex(tmp_path / "txidle.hex", {0: prog})

    def run(idle, t7, t8):
        rets = iss_log(hexf, "--vread", f"16={0x08 if idle else 0x00}")
        cycles = every(7) + [t7, t8] + [1100, 1200, 1300, 1400, 1500]
        pins = Pins().set_uo(301, 0x01)
        pins.uart_frame(705, 0x55, 17, 0x00)
        pins.set_uo(1201, 0x5B).set_uo(1301, 0x5A)
        return stub("rvfi", hexf, tmp_path, pins, rets, cycles,
                    {"uart_div": 16, "uart_tx_expect": "U", "result_gpio": 0x5A})

    assert run(False, 720, 760)["pass"]
    r = run(True, 720, 760)
    assert not r["pass"] and "TXIDLE (IRQ_PEND[3], = !TX_BUSY) read 1" in r["error"], r["error"]
    assert run(True, 900, 1000)["pass"]
    r = run(False, 900, 1000)
    assert not r["pass"] and "TXIDLE (IRQ_PEND[3], = !TX_BUSY) read 0" in r["error"], r["error"]


def test_gpio_in_read_window(tools, tmp_path):
    """R30 (contract 6): a GPIO_IN load returns ui_in as the design saw it at an edge of the load's
    execution or the 3 before it (the previous retirement p = 300, this one t = 400: edges
    298-400). ui[1] idles high where no UART RX model drives it (R24)."""
    prog = [lui(1, 0x80000), addi(5, 0, 0), addi(6, 0, 0), lw(2, 4, 1),  # GPIO_IN at #3 (400)
            addi(3, 0, 0x5A), sw(3, GPIO_OUT, 1), SELF]
    hexf = flash_hex(tmp_path / "gin.hex", {0: prog})
    pins = Pins().set_uo(601, 0x5A)

    def run(v, script):
        exp = {"result_gpio": 0x5A}
        if script is not None:
            exp["gpio_script"] = script
        return stub("rvfi", hexf, tmp_path, pins, iss_log(hexf, "--vread", f"1={v}"), every(8),
                    exp)

    # anchor = the first retirement (100); script cycle c applies from edge 100 + c on
    a = [[0, 0x12], [198, 0x22]]  # 0x12 up to edge 297, 0x22 from 298
    b = [[0, 0x12], [199, 0x22]]  # 0x12 up to edge 298
    assert run(0x22, a)["pass"]
    r = run(0x12, a)
    assert not r["pass"] and "GPIO_IN read 0x12" in r["error"], r["error"]
    assert "at most 3 cycles stale" in r["error"], r["error"]
    assert run(0x12, b)["pass"]
    r = run(0x55, a)
    assert not r["pass"] and "GPIO_IN read 0x55" in r["error"], r["error"]
    assert run(0x02, None)["pass"]  # no script: ui_in = 0x02 (UART RX idle high)
    r = run(0x00, None)
    assert not r["pass"] and "GPIO_IN read 0x00" in r["error"], r["error"]


def test_mip_meip_taken_from_rd_wdata(tools, tmp_path):
    """R14: a csrr of mip takes MEIP (bit 11) from the design's rvfi_rd_wdata; its other bits read
    0 and are compared."""
    hexf = flash_hex(tmp_path / "mip.hex", {0: [csrrs(2, 0x344, 0), SELF]})
    for v in (0x800, 0):
        rets = iss_log(hexf, "--vread", f"32={v}")
        assert rets[0]["rdv"] == v
        r = stub("rvfi", hexf, tmp_path, Pins(), rets, every(3))
        assert r["pass"], (v, r["error"])
    rets = iss_log(hexf, "--vread", "32=0x800")
    rets[0]["rdv"] = 0x801
    r = stub("rvfi", hexf, tmp_path, Pins(), rets, every(3))
    assert not r["pass"] and "rvfi_rd_wdata = 0x801, expected 0x800" in r["error"], r["error"]


def test_wfi_latency_exemption(tools, tmp_path):
    """R31 (contract 4): WFI retires, then may stall. Only the gap that starts at a WFI's
    retirement is exempt from the 2,048-cycle cap (at the retirement and in the watchdog); the gap
    before a WFI is not, and neither is the same gap after another instruction."""
    hexf = flash_hex(tmp_path / "wfi.hex", {0: [WFI, SELF]})
    r = stub("rvfi", hexf, tmp_path, Pins(), iss_log(hexf), [100, 3100, 3200])
    assert r["pass"] and r["max_cycles_per_insn"] == 100, r
    for prog in ([addi(1, 0, 1), WFI, SELF], [addi(1, 0, 1), SELF]):
        hexb = flash_hex(tmp_path / "wfib.hex", {0: prog})
        rets = iss_log(hexb)
        r = stub("rvfi", hexb, tmp_path, Pins(), rets, [100] + [3100 + 100 * k for k in
                                                               range(len(rets) - 1)])
        assert r["error"].startswith("cycle 2149: latency cap: no retirement within 2048 cycles "
                                     "of the previous retirement"), r["error"]


PROG_VOL_ALT = [lui(1, 0x80000), lw(6, 4, 1), addi(2, 0, 1), sw(2, ALTFN, 1),  # ends ALTFN = 1
                addi(3, 0, 0x5A), sw(3, GPIO_OUT, 1), SELF]


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_result_gpio_on_gpio_pins_only(tools, tmp_path, mode):
    """R24: result_gpio is compared on the pins in GPIO mode at the end, ~final_altfn. A program
    that ends with uo[0] as the idle UART line shows 0x5B for result_gpio 0x5A."""
    hexf = flash_hex(tmp_path / "alt.hex", {0: PROG_VOL_ALT})
    rets = iss_log(hexf, "--vread", "1=2")
    pins = Pins().set_uo(401, 0x01).set_uo(601, 0x5B)
    ok = {"result_gpio": 0x5A, "final_altfn": 1}
    r = stub(mode, hexf, tmp_path, pins, rets, every(8), ok)
    assert r["pass"], r["error"]
    if mode == "pins":
        assert r["end"] == "result_gpio"
    r = stub(mode, hexf, tmp_path, pins, rets, every(8), {"result_gpio": 0x5A},
             extra=("--max-cycles", "20000"))
    if mode == "rvfi":
        assert not r["pass"] and "ended with ALTFN = 0x01" in r["error"], r["error"]
    else:
        assert not r["pass"] and "timeout after 20001 cycles" in r["error"], r["error"]
    if mode == "pins":  # 0xEE outside final_altfn fails at once
        bad = Pins().set_uo(401, 0x01).set_uo(601, 0xEF)
        r = stub(mode, hexf, tmp_path, bad, rets, every(8), ok)
        assert not r["pass"] and "self-check failed" in r["error"], r["error"]


def test_degenerate_result_compare_is_a_setup_error(tools, tmp_path):
    """R24: result_gpio & ~final_altfn = 0 or final_altfn = 0xFF compares nothing an idle design
    wouldn't pass: exit 2."""
    hexf = flash_hex(tmp_path / "alt.hex", {0: PROG_VOL_ALT})
    err = stub_setup_error("pins", hexf, tmp_path, {"result_gpio": 1, "final_altfn": 1})
    assert "degenerate" in err, err
    err = stub_setup_error("rvfi", hexf, tmp_path, {"result_gpio": 0x5A, "final_altfn": 255})
    assert "final_altfn must be an integer 0..254" in err, err


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_fail_value_fails_fast(tools, tmp_path, mode):
    """GPIO_OUT = 0xEE (the default trap or a failed check) fails at once: rvfi mode at the store's
    retirement, pins mode when the pins show it."""
    prog = [lui(1, 0x80000), lw(6, 4, 1), addi(2, 0, 0xEE), sw(2, GPIO_OUT, 1),  # #3 at 400
            addi(5, 0, 0), SELF]
    hexf = flash_hex(tmp_path / "ee.hex", {0: prog})
    rets = iss_log(hexf, "--vread", "1=2")
    r = stub(mode, hexf, tmp_path, Pins().set_uo(401, 0xEE), rets, every(7), {"result_gpio": 0x5A})
    assert not r["pass"] and "self-check failed" in r["error"], r["error"]
    assert ("cycle 400:" if mode == "rvfi" else "cycle 401:") in r["error"], r["error"]


PROG_PWM = [lui(1, 0x80000), addi(2, 0, 5), sw(2, 32, 1),            # PWM_DUTY0 = 5
            lui(2, 2), addi(2, 2, 0x409), sw(2, 28, 1),              # PWM_CTRL: PER 9, Q 1, on
            addi(2, 0, 2), sw(2, ALTFN, 1),                          # uo[1] = PWM0 (#7, 800)
            addi(3, 0, 0xA5), sw(3, GPIO_OUT, 1),                    # the 0xA5 mark (#9, 1000)
            addi(5, 0, 0), sw(0, ALTFN, 1),                          # ALTFN = 0 (#11, 1200)
            addi(3, 0, 0x5A), sw(3, GPIO_OUT, 1), SELF]


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_pwm_through_the_harness(tools, tmp_path, mode):
    """expect.json pwm: after the 0xA5 mark (on the GPIO-mode pins), PWM0 on uo[1] must run with
    period (PER+1) 2^Q = 20 and high min(DUTY, PER+1) 2^Q = 10, measured after one settling
    period, within 4 periods; rvfi mode also fails ALTFN[1] cleared before the measurement ends."""
    hexf = flash_hex(tmp_path / "pwm.hex", {0: PROG_PWM})
    rets = iss_log(hexf)
    exp = {"pwm": [{"ch": 0, "per": 9, "q": 1, "duty": 5}], "result_gpio": 0x5A}

    def uo(c, stop=10**9):
        base = 0x5A if c >= 1401 else 0xA5 if c >= 1001 else 0x00
        pwm = 801 <= c < min(1201, stop) and (c - 801) % 20 < 10
        return base | (0x02 if pwm else 0)

    r = stub(mode, hexf, tmp_path, wave(Pins(), 1, 1700, uo), rets, every(16), exp)
    assert r["pass"], r["error"]
    bad = dict(exp, pwm=[{"ch": 0, "per": 10, "q": 1, "duty": 5}])  # expects period 22
    r = stub(mode, hexf, tmp_path, wave(Pins(), 1, 1700, uo), rets, every(16), bad)
    assert not r["pass"] and "PWM0 (uo[1])" in r["error"] and "expected period 22" in r["error"], (
        r["error"])
    r = stub(mode, hexf, tmp_path, wave(Pins(), 1, 1700, lambda c: uo(c, stop=1010)), rets,
             every(16), exp)
    assert not r["pass"] and "no rising edge in the two periods after settling" in r["error"], (
        r["error"])
    if mode == "rvfi":  # ALTFN cleared 20 cycles after the mark, before the first measured edge
        cycles = every(10) + [1010, 1020, 1300, 1400, 1500, 1600]
        r = stub(mode, hexf, tmp_path, wave(Pins(), 1, 1700, uo), rets, cycles, exp)
        assert not r["pass"] and "ALTFN bit 1 was cleared before the measurement" in r["error"], (
            r["error"])


def test_uart_pins_lenient_cut_frame(tools, tmp_path):
    """pins mode, a program the ISS can't run alone (its ALTFN writes are unknown): a frame whose
    stop bit is 0 and whose line then rises may be the pin going to GPIO 0 and back, so it is
    abandoned, not a framing error. uart_tx_expect then catches the missing byte; without
    uart_tx_expect the abandoned frame fails at once."""
    prog = [lui(1, 0x80000), lw(6, 4, 1)] + PROG_UART[1:]  # GPIO_IN read: volatile
    hexf = flash_hex(tmp_path / "uvol.hex", {0: prog})
    bits = [0] + [(0x55 >> k) & 1 for k in range(8)]

    def uo(c, stop):
        if c < 401:
            return 0
        if c >= 1101:
            return 0x5A
        base = 0x5A if c >= 1001 else 0x00
        if 805 <= c < 805 + 153:
            return base | bits[(c - 805) // 17]
        if 805 + 153 <= c < 990 and not stop:
            return base
        return base | 1

    good = {"uart_div": 16, "uart_tx_expect": "U", "result_gpio": 0x5A}
    r = stub("pins", hexf, tmp_path, wave(Pins(), 1, 1200, lambda c: uo(c, True)), expect=good)
    assert r["pass"] and r["uart_tx"] == "U", r
    cut = wave(Pins(), 1, 1200, lambda c: uo(c, False))
    r = stub("pins", hexf, tmp_path, cut, expect=good)
    assert not r["pass"] and 'UART TX sent "", expect.json uart_tx_expect is "U"' in r["error"], (
        r["error"])
    r = stub("pins", hexf, tmp_path, cut, expect={"uart_div": 16, "result_gpio": 0x5A})
    assert not r["pass"] and "no uart_tx_expect to judge the bytes" in r["error"], r["error"]
    assert "cycle 990:" in r["error"] and "started at cycle 805" in r["error"], r["error"]


def test_uart_pins_byte_against_reference(tools, tmp_path):
    """pins mode, deterministic program (R33): the UART frame must carry the reference run's
    UART_DATA byte."""
    hexf = flash_hex(tmp_path / "uart.hex", {0: PROG_UART})
    pins = Pins().set_uo(301, 0x01)
    pins.uart_frame(705, 0x54, 17, 0x00)
    pins.set_uo(901, 0x5B).set_uo(1001, 0x5A)
    r = stub("pins", hexf, tmp_path, pins, expect={"uart_div": 16, "result_gpio": 0x5A})
    assert not r["pass"] and "UART TX sent 0x54 (#0, started at cycle 705)" in r["error"], (
        r["error"])
    assert "reference run's UART_DATA write #0 is 0x55" in r["error"], r["error"]


def test_uart_gate_soft_while_altfn_clear_executes(tools, tmp_path):
    """rvfi mode: while a store clearing ALTFN[0] executes, uo[0] may take its GPIO value early
    (contract 6), so a falling line there is no framing error (the frame is abandoned); the same
    drop while another instruction executes still is one."""
    hexf = flash_hex(tmp_path / "uart.hex", {0: PROG_UART})
    rets = iss_log(hexf)
    exp = {"uart_div": 16, "uart_tx_expect": "U", "result_gpio": 0x5A}
    # #8 GPIO_OUT = 0x5A retires at 900, #9 ALTFN = 0 at 1300; the pin drops at 950
    pins = Pins().set_uo(301, 0x01)
    pins.uart_frame(705, 0x55, 17, 0x00)
    pins.set_uo(901, 0x5B).set_uo(950, 0x5A)
    r = stub("rvfi", hexf, tmp_path, pins, rets, every(9) + [1300, 1400, 1500], exp)
    assert r["pass"] and r["uart_tx"] == "U", r["error"]
    # the drop at 900, while #8 (GPIO_OUT, ALTFN unchanged) executes: a stop bit of 0
    early = Pins().set_uo(301, 0x01)
    early.uart_frame(705, 0x55, 17, 0x00)
    early.set_uo(900, 0x00).set_uo(1201, 0x5A)
    r = stub("rvfi", hexf, tmp_path, early, rets, every(8) + [1200, 1300, 1400, 1500], exp)
    assert not r["pass"] and "stop bit of 0" in r["error"], r["error"]


def test_expect_max_cycles_budget(tools, tmp_path):
    """R35: expect.json max_cycles is the run budget; the CLI --max-cycles wins over it, and
    run_cosim passes --max-cycles only when given, so it never replaces the expect value."""
    prog = [lui(1, 0x80000), lw(2, 4, 1), addi(3, 0, 0x5A), sw(3, GPIO_OUT, 1), SELF]
    hexf = flash_hex(tmp_path / "vbud.hex", {0: prog})
    exp = {"result_gpio": 0x5A, "max_cycles": 15000}
    r = stub("pins", hexf, tmp_path, Pins(), expect=exp)
    assert not r["pass"] and "timeout after 15001 cycles (expect.json max_cycles)" in r["error"], (
        r["error"])
    r = stub("pins", hexf, tmp_path, Pins(), expect=exp, extra=("--max-cycles", "12000"))
    assert not r["pass"] and "timeout after 12001 cycles (--max-cycles)" in r["error"], r["error"]
    assert "max_cycles" in stub_setup_error("pins", hexf, tmp_path, {**exp, "max_cycles": 0})
    # through run_cosim: <name>.expect.json beside the hex
    (tmp_path / "vbud.expect.json").write_text(json.dumps(exp))
    s, _ = run_cosim.run_one("pins", hexf, 1, src=PLAYBACK)
    assert not s["pass"] and "timeout after 15001 cycles (expect.json max_cycles)" in s["error"], (
        s["error"])
    s, _ = run_cosim.run_one("pins", hexf, 1, max_cycles=12000, src=PLAYBACK)
    assert not s["pass"] and "timeout after 12001 cycles (--max-cycles)" in s["error"], s["error"]


# ------------------------------------------------------------------------------------------------
# 3. the tCEM mutant (R12), run_cosim.py (R18), the fuzz
@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_tcem_mutant_fails(tools, tmp_path, mode):
    """mutants/tcem_stall holds PSRAM CS_n low with SCK paused for 400 cycles during a read:
    past tCEM = 8 us = 320 cycles at 25 ns (contract 2.1.7)."""
    hexf = flash_hex(tmp_path / "loop.hex", {0: PROG_LOOP})
    s, res = run_cosim.run_one(mode, hexf, 1, src=TCEM)
    assert not s["pass"] and "tCEM" in s["error"], s["error"]
    assert list(s) == list(run_cosim.KEYS) and s["coremark_cycles_per_iter"] is None


# Cell mutants (contract section 1: src/ may instantiate the whitelisted sky130 cells; build.sh
# reads their behavioral models from verif/cells). Each is the playback stub with its uo_out
# register built from sky130_fd_sc_hd__sdfxtp_1 scan flops, written into a temporary src dir.
UO_REG = """            uo_r <= 8'h00;
"""
UO_LOAD = """                {oe_r, uio_r, uo_r} <= pn[{pi, 1'b1}][23:0];
"""
UO_DECL = "    reg [7:0] uo_r, uio_r, oe_r;\n"
CELL_UO = {
    # the reset folded into the load: SCE = load, SCD = the new value, D = Q (an enable flop)
    "good": ("~rst_n | hit", "rst_n ? pn[{pi, 1'b1}][7:0] : 8'h00", ".D(uo_r[k]), .SCD(uo_d[k])"),
    # select polarity swapped: SCD and D exchanged, so SCE = 1 holds and SCE = 0 loads
    "swapped": ("~rst_n | hit", "rst_n ? pn[{pi, 1'b1}][7:0] : 8'h00",
                ".D(uo_d[k]), .SCD(uo_r[k])"),
    # no reset: the register relies on its power-up value until the first pin record
    "no_reset": ("hit", "pn[{pi, 1'b1}][7:0]", ".D(uo_r[k]), .SCD(uo_d[k])"),
}


def cell_stub(tmp: Path, variant: str) -> Path:
    load, data, conns = CELL_UO[variant]
    text = (PLAYBACK / "tt_um_fesens_r32v.v").read_text()
    for old in (UO_REG, UO_LOAD, UO_DECL):
        assert text.count(old) == 1, old
    gen = (f"    reg [7:0] uio_r, oe_r;\n    wire [7:0] uo_r;\n"
           f"    wire hit = rst_n & (pn[{{pi, 1'b0}}] == cnt + 32'd1);\n"
           f"    wire uo_ld = {load};\n    wire [7:0] uo_d = {data};\n"
           f"    genvar k;\n    generate\n        for (k = 0; k < 8; k = k + 1) begin : g_uo\n"
           f"            sky130_fd_sc_hd__sdfxtp_1 u_ff (.CLK(clk), {conns}, .SCE(uo_ld), "
           f".Q(uo_r[k]));\n        end\n    endgenerate\n")
    text = (text.replace(UO_REG, "").replace(UO_DECL, gen)
            .replace(UO_LOAD, "                {oe_r, uio_r} <= pn[{pi, 1'b1}][23:8];\n"))
    d = tmp / f"cell_{variant}"
    d.mkdir()
    (d / "tt_um_fesens_r32v.v").write_text(text)
    return d


def gpio_scenario(tmp_path):
    """test_gpio_one_cycle_rule's program: GPIO_OUT = 0x5A, the store retiring at cycle 300; the
    stub shows it on uo_out from cycle 301."""
    prog = [lui(1, 0x80000), addi(2, 0, 0x5A), sw(2, GPIO_OUT, 1), SELF]
    hexf = flash_hex(tmp_path / "g.hex", {0: prog})
    pins = Pins()
    pins.set_uo(301, 0x5A)
    return hexf, iss_log(hexf), pins


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_cell_stub_good_passes(tools, tmp_path, mode):
    """The scan-flop register with SCE = load passes in both modes and all four seeds: build.sh
    compiles the cell models, and the cosim runs a design that instantiates cells."""
    hexf, rets, pins = gpio_scenario(tmp_path)
    src = cell_stub(tmp_path, "good")
    for seed in run_cosim.SEEDS:
        r = stub(mode, hexf, tmp_path, pins, rets, every(5), {"result_gpio": 0x5A}, seed=seed,
                 src=src)
        assert r["pass"], (seed, r["error"])
    args = (run_cosim.build_dir_for(mode, src, "", False) / "verilator.args").read_text()
    assert "/verif/cells/sky130_fd_sc_hd__sdfxtp_1.v\n" in args


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_cell_stub_swapped_select_fails(tools, tmp_path, mode):
    """sdfxtp with SCD and D exchanged (the select polarity wrong) fails the cosim."""
    hexf, rets, pins = gpio_scenario(tmp_path)
    r = stub(mode, hexf, tmp_path, pins, rets, every(5), {"result_gpio": 0x5A},
             src=cell_stub(tmp_path, "swapped"))
    assert not r["pass"] and "uo_out" in r["error"], r["error"]


def test_cell_stub_power_up_reliance_fails(tools, tmp_path):
    """Invariant 6 reaches cell flops: --x-initial unique and the harness's seeded randReset
    randomize the model's Q, so a scan-flop register without a reset fails (uo_out is not 0
    after reset). The same mutant built with --x-initial 0 (every flop powers up 0) passes, so
    the failure comes from the randomized cell state alone."""
    hexf, rets, pins = gpio_scenario(tmp_path)
    src = cell_stub(tmp_path, "no_reset")
    res = [stub("rvfi", hexf, tmp_path, pins, rets, every(5), {"result_gpio": 0x5A}, seed=s,
                src=src) for s in run_cosim.SEEDS]
    assert all(not r["pass"] and "uo_out" in r["error"] for r in res), [r["error"] for r in res]
    exe = run_cosim.ensure_built("rvfi", src, vflags="--x-initial 0")
    _n[0] += 1
    pp, rp, ep = tmp_path / "zp.hex", tmp_path / "zr.hex", tmp_path / "ze.json"
    pins.write(pp)
    rvfi_rom(rp, rets, every(5))
    ep.write_text(json.dumps({"result_gpio": 0x5A}))
    p = subprocess.run([str(exe), str(hexf), "--seed", "1", "--expect", str(ep), f"+PINS={pp}",
                        f"+RVFI={rp}"], capture_output=True, text=True, timeout=900)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert r["pass"], r["error"]


def submodule_stub(tmp: Path) -> Path:
    """The playback stub with its uo_out register built from 8 instances of a design submodule
    (r32v_ureg, in its own file) whose state is an unreset `output reg`, each connected to a
    bit-select uo_r[k]: the design relies on that flop's power-up value until the first pin
    record."""
    text = (PLAYBACK / "tt_um_fesens_r32v.v").read_text()
    for old in (UO_REG, UO_LOAD, UO_DECL):
        assert text.count(old) == 1, old
    gen = ("    reg [7:0] uio_r, oe_r;\n    wire [7:0] uo_r;\n"
           "    wire hit = rst_n & (pn[{pi, 1'b0}] == cnt + 32'd1);\n"
           "    genvar k;\n    generate\n        for (k = 0; k < 8; k = k + 1) begin : g_uo\n"
           "            r32v_ureg u_q (.clk(clk), .ld(hit), .d(pn[{pi, 1'b1}][k]), "
           ".q(uo_r[k]));\n        end\n    endgenerate\n")
    text = (text.replace(UO_REG, "").replace(UO_DECL, gen)
            .replace(UO_LOAD, "                {oe_r, uio_r} <= pn[{pi, 1'b1}][23:8];\n"))
    d = tmp / "submodule_outreg"
    d.mkdir()
    (d / "tt_um_fesens_r32v.v").write_text(text)
    (d / "r32v_ureg.v").write_text(
        "`default_nettype none\n"
        "module r32v_ureg (input wire clk, input wire ld, input wire d, output reg q);\n"
        "    always @(posedge clk) if (ld) q <= d;\n"
        "endmodule\n`default_nettype wire\n")
    return d


def test_submodule_output_reg_power_up_reliance_fails(tools, tmp_path):
    """Invariant 6 reaches a design submodule's `output reg` tied to a bit-select. Inlined,
    Verilator makes that port a __Vcellout temporary initialized to 0, not at random, and this
    stub passed under all four seeds. verif/cosim/noinline.vlt keeps r32v_* modules out of
    inlining, so the flop is randomized and the reliance fails (uo_out is not 0 during reset).
    Built with --x-initial 0 it passes, so the failure comes from the randomized submodule state
    alone; with the inlining forced back (a test-only configuration file) it passes again."""
    hexf, rets, pins = gpio_scenario(tmp_path)
    src = submodule_stub(tmp_path)
    res = [stub("rvfi", hexf, tmp_path, pins, rets, every(5), {"result_gpio": 0x5A}, seed=s,
                src=src) for s in run_cosim.SEEDS]
    assert any(not r["pass"] for r in res), "the power-up reliance passed under seeds 1-4"
    assert all(r["pass"] or "uo_out" in r["error"] for r in res), [r["error"] for r in res]
    args = (run_cosim.build_dir_for("rvfi", src, "", False) / "verilator.args").read_text()
    assert "/verif/cosim/noinline.vlt\n" in args
    inline = tmp_path / "inline.vlt"
    inline.write_text('`verilator_config\ninline -module "r32v_*"\n')
    pp, rp, ep = tmp_path / "zp.hex", tmp_path / "zr.hex", tmp_path / "ze.json"
    pins.write(pp)
    rvfi_rom(rp, rets, every(5))
    ep.write_text(json.dumps({"result_gpio": 0x5A}))
    for vflags, seeds in (("--x-initial 0", (1,)), (str(inline), run_cosim.SEEDS)):
        exe = run_cosim.ensure_built("rvfi", src, vflags=vflags)
        for s in seeds:
            p = subprocess.run([str(exe), str(hexf), "--seed", str(s), "--expect", str(ep),
                                f"+PINS={pp}", f"+RVFI={rp}"], capture_output=True, text=True,
                               timeout=900)
            r = json.loads(p.stdout.strip().splitlines()[-1])
            assert r["pass"], (vflags, s, r["error"])


@pytest.mark.parametrize("v3_flag", [[], ["--v3"]], ids=["default", "v3-no-op"])
def test_run_cosim_cli(tools, tmp_path, v3_flag):
    """R18: the CLI runs the v3 harness and prints KEYS (coremark_cycles_per_iter included), with
    or without the old --v3 flag (a no-op since the swap); the gate is the plan's program list x
    both modes x seeds 1-4, plus the fuzz at seeds 1-5."""
    hexf = flash_hex(tmp_path / "loop.hex", {0: PROG_LOOP})
    p = subprocess.run([sys.executable, str(COSIM / "run_cosim.py"), *v3_flag, "--mode", "pins",
                        "--prog", str(hexf), "--src", str(TCEM)], capture_output=True, text=True)
    s = json.loads(p.stdout.strip().splitlines()[-1])
    assert p.returncode == 1 and list(s) == list(run_cosim.KEYS), (p.returncode, s)
    assert "tCEM" in s["error"] and s["coremark_cycles_per_iter"] is None
    assert run_cosim.SEEDS == (1, 2, 3, 4) and run_cosim.FUZZ_SEEDS == (1, 2, 3, 4, 5)
    assert run_cosim.LAT_CAP == 2048 and run_cosim.CLK_NS == 25.0
    plan = {"selftest", "csr", "irq_timer", "irq_stress", "wfi", "coherence", "uart_echo",
            "spi_loop", "pwm", "ir_loop", "gpio_edge", "mem_init_check", "coremark"}
    plan |= {f"trap_{t}" for t in ("ecall", "ebreak", "jal", "jalr", "branch", "zero", "ones")}
    plan |= {f"random_{n}" for n in range(1, 7)}
    assert set(run_cosim.PLAN_PROGRAMS) == plan and len(run_cosim.PLAN_PROGRAMS) == len(plan)
    # --all runs every program `make list` reports; the plan's 26 must be among them
    listed = sorted(plan) + ["gpio_demo", "uart_echo_fast"]
    assert run_cosim.missing_plan_programs(listed) == []
    assert run_cosim.missing_plan_programs([p for p in listed if p != "wfi"]) == ["wfi"]
    assert run_cosim.build_dir_for("rvfi", run_cosim.SRC, "", False).name == "rvfi"


def test_fuzz_checks_liveness(tools):
    """The liveness fuzz on random flash images: a stub that never retires fails the rvfi-mode
    fuzz with the latency cap; pins mode checks only the pin rules (and the idle stub passes)."""
    s = run_cosim.run_fuzz("rvfi", 1, runs=4, cap=10, src=PLAYBACK)
    assert list(s) == run_cosim.FUZZ_KEYS
    assert not s["pass"] and s["liveness_checked"] is True, json.dumps(s)
    want = "latency cap: no retirement within 2048 cycles of reset release"
    assert want in s["error"], s["error"]
    s = run_cosim.run_fuzz("pins", 1, runs=2, cap=10, src=PLAYBACK)
    assert s["pass"] and s["liveness_checked"] is False and s["runs"] == 3, json.dumps(s)
    s = run_cosim.run_fuzz("rvfi", 3, runs=2, cap=10, src=TCEM)
    assert not s["pass"] and "tCEM" in s["error"], json.dumps(s)
