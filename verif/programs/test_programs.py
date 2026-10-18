"""Checks of the contract-v3 test programs (docs/contract.md) and their expect.json files.

    python3 -m pytest verif/programs/test_programs.py -q      (or: make -C verif/programs check)

It builds every program (`make -C verif/programs`), then checks:
  - every program the harness runs has <name>.{elf,hex,expect.json};
  - the hex files are sparse $readmemh flash images (`@addr` records, bytes only, inside 16 MiB);
  - the deterministic programs ran to the self-loop on the standalone ISS (`iss_main`) with
    GPIO_OUT = result_gpio = 0x5A (ruling R13), and their expect.json carries the ISS run;
  - the random streams cover what gen_random.py promises (alias bits, the bytes below the core
    region, flash loads and stores, IO slots and lanes, backward branches);
  - the peripheral programs carry the declared metadata Task 3's harness reads, with the exact
    field names and shapes, consistent with stim.h;
  - progsim (a timed peripheral model around the ISS, progsim.cpp) runs every peripheral program
    to the self-loop with GPIO_OUT = 0x5A, with no read before write, at speeds from 3 to 2,048
    cycles per instruction (the contract-4 cap: every stimulus schedule leaves room for the
    slowest legal core) and within the harness's 50 M-cycle budget for declared programs; the
    interrupt programs also with --irq-defer (the core retires one more instruction after MIE
    turns on, which contract 4 allows).
Light: the ISS and progsim runs are bounded (CoreMark, the longest, is about 0.8 M retirements).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "verif" / "programs"
OUT = ROOT / "build" / "programs"
ISS = ROOT / "build" / "cosim" / "iss_main"
PROGSIM = OUT / "progsim"

TRAPS = ("ecall", "ebreak", "jal", "jalr", "branch", "zero", "ones")
ISS_PROGS = (["selftest", "csr", "coherence", "mem_init_check", "gpio_demo", "coremark"]
             + [f"trap_{k}" for k in TRAPS] + [f"random_{i}" for i in range(1, 7)])


def make_list(target: str) -> list:
    return subprocess.run(["make", "-s", "-C", str(HERE), target], capture_output=True, text=True,
                          check=True).stdout.split()


EVENT_PROGS = make_list("list-declared")  # the Makefile's EVENT_PROGS: the one list of them
ALL = ISS_PROGS + EVENT_PROGS
PASS = 0x5A
REGION_LO = 0x7FFF80
BUDGET = 50_000_000  # the harness's default cycle budget for declared programs (Task 3)


@pytest.fixture(scope="module")
def built():
    subprocess.run(["make", "-C", str(HERE), "-j4", "all", str(PROGSIM)], check=True,
                   capture_output=True)
    return {p: json.loads((OUT / f"{p}.expect.json").read_text()) for p in ALL}


def stim():
    sys.path.insert(0, str(HERE))
    import mkexpect  # noqa: E402
    return mkexpect.parse_stim()


def test_make_list_matches():
    assert make_list("list") == ALL


def test_stim_parse():
    s = stim()
    assert s["UART_MSG"] == "Hello, BonettoR!" and s["UART_KEEP"] == 0x4B and s["GE_V7"] == 0xFF


@pytest.mark.parametrize("prog", ALL)
def test_outputs_exist_and_hex_is_sparse_flash(built, prog):
    for ext in ("elf", "hex", "expect.json"):
        assert (OUT / f"{prog}.{ext}").is_file(), f"{prog}.{ext}"
    addr, records = None, 0
    for line in (OUT / f"{prog}.hex").read_text().splitlines():
        for tok in line.split():
            if tok.startswith("@"):
                addr = int(tok[1:], 16)
                records += 1
                continue
            assert addr is not None, "bytes before the first @addr record"
            assert re.fullmatch(r"[0-9a-f]{2}", tok), tok
            assert addr < 1 << 24, "outside the 16 MiB flash"
            addr += 1
    assert records >= 1


@pytest.mark.parametrize("prog", ALL)
def test_common_fields(built, prog):
    e = built[prog]
    assert e["prog"] == prog
    assert e["result_gpio"] == PASS
    assert e["kind"] == ("iss" if prog in ISS_PROGS else "declared")
    assert e["final_altfn"] == (1 if prog.startswith("uart_echo") else 0)
    for lo, hi in e["psram_window"]:
        assert 0 <= lo < hi <= REGION_LO, (lo, hi)  # PSRAM byte addresses, never the core region


@pytest.mark.parametrize("prog", ISS_PROGS)
def test_iss_programs_end_in_selfloop_with_pass(built, prog):
    e = built[prog]
    assert e["end"] == "selfloop"
    assert e["gpio_out"] == PASS
    assert e["uo_trace"] and e["uo_trace"][-1] == PASS
    assert 0xEE not in e["uo_trace"], "the fail mark on the way (random streams never write it)"
    assert e["altfn"] == 0
    assert e["interrupts"] == 0
    assert e["traps"] == (1 if prog.startswith("trap_") else 0)
    assert e["retired"] > 0
    if not prog.startswith("trap_") and prog != "csr":  # those two keep results in registers
        assert e["psram_window"], "deterministic programs name their result bytes"


def test_no_marks_outside_their_programs(built):
    """0xA5 (PWM measurement start) and 0xC0/0xC1 (CoreMark timing marks) are reserved GPIO_OUT
    values: the deterministic programs other than coremark never show them (random streams are
    exempt: their GPIO values are random)."""
    for p in ISS_PROGS:
        if p == "coremark" or p.startswith("random_"):
            continue
        assert not {0xA5, 0xC0, 0xC1} & set(built[p]["uo_trace"]), p


def test_coremark_marks(built):
    e = built["coremark"]
    assert e["coremark_iters"] >= 1
    t = e["uo_trace"]
    assert t == [0xC0, 0xC1, PASS]
    assert e["log_contains"] == ["Correct operation validated"]
    assert e["log_ring"] == [0x7F0000, 0x7F0800] and e["log_ring"] in e["psram_window"]
    assert e["min_sp"] > 0x017F0800


def test_uart_fields(built):
    s = stim()
    msg, keep = s["UART_MSG"], chr(s["UART_KEEP"])
    for prog, ppm in (("uart_echo", 10000), ("uart_echo_fast", -10000)):
        e = built[prog]
        assert e["uart_div"] == 39
        assert e["uart_rate_ppm"] == ppm
        rx = e["uart_rx"]
        assert all(len(x) == 2 and isinstance(x[0], int) and 0 <= x[1] <= 255 for x in rx)
        cycles = [c for c, _ in rx]
        assert cycles == sorted(cycles)
        assert len(rx) == 18 and bytes(b for _, b in rx[:16]).decode() == msg
        assert [b for _, b in rx[16:]] == [s["UART_KEEP"], s["UART_DROP"]]
        assert e["uart_tx_expect"] == "hello" + msg + keep
        # frames never overlap: the gap is far longer than 10 bits of 40 cycles at +1 %
        assert min(b - a for a, b in zip(cycles, cycles[1:])) > 10 * 40 * 1.01


def test_spi_pwm_loop_fields(built):
    assert built["spi_loop"]["spi_div_d"] == 1
    assert built["spi_loop"]["spi_tx_expect"] == list(range(17))
    pwm = sorted(built["pwm"]["pwm"], key=lambda d: d["ch"])
    assert pwm == [
        {"ch": 0, "pin": 1, "per": 99, "q": 1, "duty": 0},
        {"ch": 1, "pin": 2, "per": 99, "q": 1, "duty": 50},
        {"ch": 2, "pin": 3, "per": 99, "q": 1, "duty": 120},
        {"ch": 7, "pin": 7, "per": 65535, "q": 0, "duty": 300},
    ]
    assert built["ir_loop"]["loop_uo7_to_ui0"] is True


@pytest.mark.parametrize("prog", ["gpio_edge", "wfi"])
def test_gpio_scripts(built, prog):
    sc = built[prog]["gpio_script"]
    assert sc[0][0] == 0 and all(len(x) == 2 and 0 <= x[1] <= 255 for x in sc)
    cycles = [c for c, _ in sc]
    assert cycles == sorted(cycles) and len(set(cycles)) == len(cycles)
    if prog != "gpio_edge":
        assert all(v & 2 for _, v in sc), "ui1 (UART RX) stays idle high"


def test_irq_programs_have_no_stimulus(built):
    common = {"prog", "kind", "result_gpio", "psram_window", "final_altfn"}
    assert set(built["irq_timer"]) == common  # the timer interrupts them
    assert set(built["irq_stress"]) == common | {"max_cycles"}
    assert built["irq_stress"]["max_cycles"] == 800_000_000  # ruling R35
    assert all("max_cycles" not in built[p] for p in ALL if p != "irq_stress")


# ---- random streams: what gen_random.py promises ------------------------------------------------
def iss_log(prog: str, max_retire: int = 20000):
    p = subprocess.run([str(ISS), str(OUT / f"{prog}.hex"), "--log", "--json", "--max",
                        str(max_retire)], capture_output=True, text=True, timeout=120)
    lines = [json.loads(ln) for ln in p.stdout.splitlines() if ln.startswith("{")]
    return [x for x in lines if "i" in x], lines[-1]


@pytest.mark.parametrize("seed", range(1, 7))
def test_random_streams_cover_the_v3_map(built, seed):
    log, summ = iss_log(f"random_{seed}")
    assert summ["end"] == "selfloop" and summ["gpio_out"] == PASS
    mem = [r for r in log if r["rmask"] or r["wmask"]]
    size = lambda r: bin(r["rmask"] | r["wmask"]).count("1")  # noqa: E731
    psram = [r for r in mem if not r["maddr"] >> 31 and (r["maddr"] >> 24) & 1]
    flash = [r for r in mem if not r["maddr"] >> 31 and not (r["maddr"] >> 24) & 1]
    io = [r for r in mem if r["maddr"] >> 31]
    for kind in ("rmask", "wmask"):
        assert any(r[kind] and r["maddr"] & 0x7E800000 for r in psram), f"PSRAM alias ({kind})"
        ends = [r for r in psram if r[kind] and (r["maddr"] & 0x7FFFFF) + size(r) == REGION_LO]
        assert ends, f"an access ending at the last byte below the core region ({kind})"
        assert any(r[kind] and r["maddr"] & 3 for r in psram), f"misaligned PSRAM ({kind})"
        assert any(r[kind] and r["maddr"] & 0x7FFFFF0 for r in io), f"IO address bits ({kind})"
        assert {r[kind] for r in io if r[kind]} >= {1, 3, 15}, f"IO widths ({kind})"
    assert all((r["maddr"] & 0x7FFFFF) + size(r) <= REGION_LO for r in psram)
    assert any(r["rmask"] and r["maddr"] >> 25 for r in flash), "flash alias loads"
    assert any(r["wmask"] for r in flash), "flash stores (no effect)"
    assert any(r["rmask"] and (r["maddr"] & 0xFFFFFF) < 0x2000 for r in flash), "loads of code"
    slots = {(r["maddr"] >> 2) & 31 for r in io}
    assert {0, 4} <= slots and len(slots) >= 8
    assert any((r["maddr"] & 3) == 2 and r["wmask"] == 3 for r in io), "halfword at slot byte 2"
    for x in (14, 15):
        assert any(r["rd"] == x for r in log) and any(x in (r["rs1"], r["rs2"]) for r in log)
    back = [r for r in log if (r["insn"] & 0x7F) == 0x63 and r["npc"] < r["pc"]]
    assert len(back) >= 5
    again = subprocess.run([sys.executable, str(HERE / "gen_random.py"), str(seed), "3000"],
                           capture_output=True, text=True, check=True).stdout
    assert again == (OUT / f"random_{seed}.S").read_text()


# ---- progsim: the peripheral programs at several core speeds ------------------------------------
def progsim(prog: str, cpi: str, *extra: str) -> dict:
    budget = json.loads((OUT / f"{prog}.expect.json").read_text()).get("max_cycles", BUDGET)
    cmd = [str(PROGSIM), str(OUT / f"{prog}.hex"), "--cpi", cpi, "--max-cycles", str(budget),
           *extra]
    if (OUT / f"{prog}.stim").stat().st_size:
        cmd += ["--stim", str(OUT / f"{prog}.stim")]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    res = json.loads(p.stdout.strip().splitlines()[-1])
    assert res["end"] == "selfloop" and res["gpio_out"] == PASS, (res, p.stderr[-2000:])
    assert not res["uninit_read"], f"read before write at pc {res['uninit_pc']:#x}"
    return res  # within the budget: progsim stopped at max_cycles otherwise (end = max)


def symbols(prog: str) -> dict:
    nm = subprocess.run(["riscv32-unknown-elf-nm", str(OUT / f"{prog}.elf")], capture_output=True,
                        text=True, check=True).stdout
    return {f[2]: int(f[0], 16) for f in (ln.split() for ln in nm.splitlines()) if len(f) == 3}


@pytest.mark.parametrize("cpi", ["3", "12", "50", "300", "800", "2048", "rand:20:2048"])
@pytest.mark.parametrize("prog", EVENT_PROGS)
def test_progsim_event_programs_pass(built, prog, cpi):
    res = progsim(prog, cpi)
    e = built[prog]
    assert res["altfn"] == e["final_altfn"]
    if "uart_tx_expect" in e:
        assert res["uart_tx"] == e["uart_tx_expect"]
    if "spi_tx_expect" in e:
        assert res["spi_tx"] == e["spi_tx_expect"]
    if prog in ("irq_timer", "wfi", "irq_stress"):
        assert res["interrupts"] >= 1
    if prog == "pwm":
        assert 0xA5 in res["uo_trace"] or (0xA5 & ~0x8E) in res["uo_trace"]
    if prog == "irq_stress":
        # interrupts land all over the steps, and one right after the WFI (progsim's WFI stalls)
        sym = symbols(prog)
        pcs = [pc for pc, _ in res["intr_pcs"]]
        assert sum(sym["steps"] <= pc < sym["steps_end"] for pc in pcs) >= 40
        assert sym["wfi_poll"] in pcs, "no interrupt landed right after the WFI"
        # the forced pending interrupt lands right after the CSRRSI and the MRET (progsim takes
        # it at once)
        assert sym["force_en"] in pcs and sym["force_mret"] in pcs


@pytest.mark.parametrize("cpi", ["12", "800", "rand:20:2048"])
@pytest.mark.parametrize("prog", ["irq_timer", "irq_stress", "wfi"])
def test_progsim_interrupts_deferred_one_instruction(built, prog, cpi):
    progsim(prog, cpi, "--irq-defer")
