"""Tests for the contract v3 cosim harness (verif/cosim/harness.cpp, periph_models.h,
run_cosim.py --v3). The contract v2 tests are in test_cosim_harness_v2.py until the swap.

1. The peripheral pin models (periph_models.h) through their unit-test binary test_periph:
   UART RX bit edges at round(k (DIV+1) (1 +- 1%)), UART TX exact bit times, the SPI echo device
   (mode 0, MISO held through 3 cycles after each rising edge), the PWM meter.
2. The harness on hand-written stub designs. test_fixtures/playback replays a pin script and an
   RVFI script given as plusargs (one Verilator build per mode); the scripts come from tiny
   hand-assembled programs run on the reference ISS (iss_main --v3 --log), so every retirement
   matches the ISS unless a test changes it on purpose.
3. The tcem_stall mutant (ruling R12) and run_cosim.py --v3 (ruling R18).

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
    """The ISS's retirements for a program (iss_main --v3 --log), interrupt entries left out."""
    p = subprocess.run([str(TOOLS / "iss_main"), "--v3", str(hexf), "--log", "--json", "--max",
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
    exe = run_cosim.ensure_built(mode, src, v3=True)
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


def every(n, step=100, first=100):
    return [first + step * k for k in range(n)]


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
    # the frame starting later than DIV+2 cycles after the write retired (rvfi mode)
    if mode == "rvfi":
        late = Pins().set_uo(301, 0x01)
        late.uart_frame(721, 0x55, 17, 0x00)
        late.set_uo(901, 0x5B).set_uo(1001, 0x5A)
        r = stub(mode, hexf, tmp_path, late, rets, every(12), exp)
        assert not r["pass"] and "no UART start bit within DIV+2" in r["error"], r["error"]


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


@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_spi_master_pins(tools, tmp_path, mode):
    """Contract 5.6 at the pins: MOSI changes only while SCK is low, phases are 2^D cycles, and
    (rvfi mode) the byte on MOSI is the byte the program wrote to SPI_DATA."""
    prog = [lui(1, 0x80000), addi(2, 0, 0x70), sw(2, ALTFN, 1),    # uo[4..6]: CS_n, SCK, MOSI
            addi(2, 0, 2), sw(2, SPI_CTRL, 1),                     # CS_n = 0, D = 1
            addi(2, 0, 0xA5), sw(2, SPI_DATA, 1),                  # transfer 0xA5 (#6 at 700)
            sw(0, ALTFN, 1), addi(3, 0, 0x5A), sw(3, GPIO_OUT, 1), SELF]
    hexf = flash_hex(tmp_path / "spi.hex", {0: prog})
    rets = iss_log(hexf)
    exp = {"spi_div_d": 1, "result_gpio": 0x5A}

    def run(byte, glitch=None):
        pins = Pins().set_uo(301, 0x10).set_uo(501, 0x00)  # CS_n high (reset 1), then low
        assert pins.spi_xfer(702, byte, 2, 0x00, glitch) == 734
        pins.set_uo(801, 0x00).set_uo(1001, 0x5A)
        return stub(mode, hexf, tmp_path, pins, rets, every(12), exp)

    r = run(0xA5)
    assert r["pass"], r["error"]
    r = run(0xA5, glitch=705)
    assert not r["pass"] and "MOSI (uo[6]) changed" in r["error"], r["error"]
    r = run(0xA4)
    if mode == "rvfi":
        assert not r["pass"] and "SPI MOSI sent 0xa4" in r["error"], r["error"]
    else:
        assert r["pass"], r["error"]  # pins mode can't relate the byte to the store


# ------------------------------------------------------------------------------------------------
# 3. the tCEM mutant (R12), run_cosim.py --v3 (R18), the v3 fuzz
@pytest.mark.parametrize("mode", ["rvfi", "pins"])
def test_tcem_mutant_fails(tools, tmp_path, mode):
    """mutants/tcem_stall holds PSRAM CS_n low with SCK paused for 400 cycles during a read:
    past tCEM = 8 us = 320 cycles at 25 ns (contract 2.1.7)."""
    hexf = flash_hex(tmp_path / "loop.hex", {0: PROG_LOOP})
    s, res = run_cosim.run_one(mode, hexf, 1, src=TCEM, v3=True)
    assert not s["pass"] and "tCEM" in s["error"], s["error"]
    assert list(s) == list(run_cosim.KEYS) and s["coremark_cycles_per_iter"] is None


def test_run_cosim_v3_cli(tools, tmp_path):
    """R18: --v3 runs the v3 harness and prints KEYS (coremark_cycles_per_iter included); the v3
    gate is the plan's program list x both modes x seeds 1-4, plus the fuzz at seeds 1-5."""
    hexf = flash_hex(tmp_path / "loop.hex", {0: PROG_LOOP})
    p = subprocess.run([sys.executable, str(COSIM / "run_cosim.py"), "--v3", "--mode", "pins",
                        "--prog", str(hexf), "--src", str(TCEM)], capture_output=True, text=True)
    s = json.loads(p.stdout.strip().splitlines()[-1])
    assert p.returncode == 1 and list(s) == list(run_cosim.KEYS), (p.returncode, s)
    assert "tCEM" in s["error"] and s["coremark_cycles_per_iter"] is None
    assert run_cosim.SEEDS_V3 == (1, 2, 3, 4) and run_cosim.FUZZ_SEEDS_V3 == (1, 2, 3, 4, 5)
    assert run_cosim.LAT_CAP_V3 == 2048 and run_cosim.CLK_NS == 25.0
    plan = {"selftest", "csr", "irq_timer", "irq_stress", "wfi", "coherence", "uart_echo",
            "spi_loop", "pwm", "ir_loop", "gpio_edge", "mem_init_check", "coremark"}
    plan |= {f"trap_{t}" for t in ("ecall", "ebreak", "jal", "jalr", "branch", "zero", "ones")}
    plan |= {f"random_{n}" for n in range(1, 7)}
    assert set(run_cosim.V3_PROGRAMS) == plan and len(run_cosim.V3_PROGRAMS) == len(plan)
    assert run_cosim.build_dir_for("rvfi", run_cosim.SRC, "", False, v3=True).name == "rvfi-v3"


def test_fuzz_v3_checks_liveness(tools):
    """The v3 liveness fuzz on random flash images: a stub that never retires fails the rvfi-mode
    fuzz with the latency cap; pins mode checks only the pin rules (and the idle stub passes)."""
    s = run_cosim.run_fuzz("rvfi", 1, runs=4, cap=10, src=PLAYBACK, v3=True)
    assert list(s) == run_cosim.FUZZ_KEYS_V3
    assert not s["pass"] and s["liveness_checked"] is True, json.dumps(s)
    want = "latency cap: no retirement within 2048 cycles of reset release"
    assert want in s["error"], s["error"]
    s = run_cosim.run_fuzz("pins", 1, runs=2, cap=10, src=PLAYBACK, v3=True)
    assert s["pass"] and s["liveness_checked"] is False and s["runs"] == 3, json.dumps(s)
    s = run_cosim.run_fuzz("rvfi", 3, runs=2, cap=10, src=TCEM, v3=True)
    assert not s["pass"] and "tCEM" in s["error"], json.dumps(s)
