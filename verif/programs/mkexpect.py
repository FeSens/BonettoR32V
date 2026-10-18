#!/usr/bin/env python3
"""Write <name>.expect.json for a contract-v3 test program (docs/contract.md), the file the
cosim harness reads (Task 3's field names), and check the program is valid.

Usage: mkexpect.py --iss ISS_MAIN --hex PROG.hex --elf PROG.elf --out PROG.expect.json
                   [--declared] [--max-cycles C] [--coremark-iters N]

--declared marks a peripheral program (the Makefile's EVENT_PROGS, the one list of them).
--max-cycles C writes max_cycles = C: the program's cycle budget, for a declared program that
needs more than the harness's default (ruling R35: irq_stress).

Every expect.json has:
  prog          the program name
  kind          "iss": the expectations come from a standalone ISS run (deterministic programs);
                "declared": from the declared stimulus below (programs whose outcome depends on
                peripheral timing or interrupts, which the standalone ISS doesn't model: ruling R13)
  result_gpio   0x5A, the final GPIO_OUT value that means pass (contract 4 program end: the
                program writes it, then retires `jal x0, 0` twice at the same pc)
  psram_window  [[lo, hi], ...]: half-open PSRAM byte-address ranges (addr[22:0]) holding the
                program's results, every byte of them written by the program, to compare with the
                reference after the run. Deterministic crt0 programs: .data + .bss, plus the
                ranges the program lists in its `_psram_window` table (pairs of CPU addresses,
                ended by 0, 0). Declared programs: [] (their memory depends on timing).
  final_altfn   ALTFN at the end: 0, except uart_echo / uart_echo_fast (0x01: uo0 stays the idle
                UART line, so the end makes no false start bit). Pins in GPIO mode at the end:
                result_gpio & ~final_altfn.

kind "iss" adds the `iss_main --check-init` summary: retired, end ("selfloop"), end_pc,
uo_trace (GPIO_OUT & ~ALTFN, one entry per change), gpio_out, altfn, traps, interrupts.
The run must end in the self-loop with GPIO_OUT = 0x5A, never read a register or an arbitrary-reset
CSR before writing it, never execute an unspecified instruction or touch the core region, and
leave ALTFN = 0. trap_* programs take exactly one trap, the others none.
  coremark adds coremark_iters (the timed iteration count, between GPIO_OUT = 0xC0 and 0xC1),
  log_contains and log_ring (its NUL-terminated output text in PSRAM, CPU 0x017F_0000), and
  min_sp (the lowest stack pointer).

kind "declared" adds the stimulus fields the harness drives and checks (values from stim.h):
  uart_echo, uart_echo_fast: uart_div, uart_rx ([[cycle_after_first_retire, byte], ...]),
                uart_tx_expect, uart_rate_ppm (+10000 / -10000: bit time +1 % / -1 %)
  spi_loop:     spi_div_d, spi_tx_expect (the MOSI bytes, in order)
  pwm:          pwm ([{ch, pin, per, q, duty}]: PWM channels 0-2 on uo1-uo3, and ch 7 = the timer
                on uo7 in PWM mode as per = 65535, q = P, duty = CMP), measured after the program
                writes GPIO_OUT = 0xA5
  ir_loop:      loop_uo7_to_ui0 (true)
  gpio_edge, wfi: gpio_script ([[cycle, ui_value], ...], ui_in from that cycle on)
  irq_stress:   max_cycles (its run-length budget; the harness's --max-cycles overrides it)
and the program still runs on the standalone ISS up to a bound, which must stop at the bound (or
the self-loop) with no unspecified step and no read before write. It also writes <name>.stim, the
same stimulus for progsim.
"""
import argparse
import json
import os
import re
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PASS = 0x5A
PSRAM_MASK = (1 << 23) - 1
REGION_LO = 0x7FFF80
LOG_RING = 0x017F0000
LOG_RING_END = 0x017F0800
MARKS = {0xA5, 0xC0, 0xC1}
FAIL_MARK = 0xEE  # GPIO_OUT after a failed self-check: never on the way to a pass
ISS_MAX = {"coremark": 40_000_000}
FINAL_ALTFN = {"uart_echo": 0x01, "uart_echo_fast": 0x01}
ISS_MAX_DEFAULT = 3_000_000
EVENT_ISS_MAX = 200_000


def fail(msg: str) -> None:
    print(f"mkexpect: {msg}", file=sys.stderr)
    sys.exit(1)


# ---- inputs ------------------------------------------------------------------------------------
def parse_stim(path: str = os.path.join(HERE, "stim.h")) -> dict:
    vals = {}
    with open(path) as f:
        for m in re.finditer(r"^#define\s+(\w+)\s+(\S.*?)\s*(?:/\*.*)?$", f.read(), re.M):
            name, v = m.group(1), m.group(2)
            if v.startswith('"'):
                vals[name] = json.loads(v)
            elif re.fullmatch(r"0x[0-9a-fA-F]+|\d+", v):
                vals[name] = int(v, 0)
    return vals


def elf_parts(path: str):
    """(symbols {name: value}, load segments [(vaddr, bytes)]) of an ELF32 file."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        fail(f"{path}: not a little-endian ELF32 file")
    phoff, shoff = struct.unpack_from("<II", data, 28)
    phentsize, phnum, shentsize, shnum = struct.unpack_from("<HHHH", data, 42)
    segs = []
    for i in range(phnum):
        p_type, p_offset, p_vaddr, _paddr, p_filesz, _m, _f, _a = struct.unpack_from(
            "<8I", data, phoff + i * phentsize)
        if p_type == 1 and p_filesz:
            segs.append((p_vaddr, data[p_offset:p_offset + p_filesz]))
    secs = [struct.unpack_from("<10I", data, shoff + i * shentsize) for i in range(shnum)]
    syms = {}
    for s in secs:
        if s[1] != 2:  # SHT_SYMTAB
            continue
        strtab = secs[s[6]]
        for off in range(s[4], s[4] + s[5], 16):
            name_off, value, _size, _info, _other, _shndx = struct.unpack_from("<IIIBBH", data, off)
            start = strtab[4] + name_off
            name = data[start:data.index(b"\0", start)].decode()
            if name:
                syms[name] = value
    return syms, segs


def read_words(segs, addr: int, n: int) -> list:
    for vaddr, seg in segs:
        if vaddr <= addr and addr + 4 * n <= vaddr + len(seg):
            off = addr - vaddr
            return list(struct.unpack_from(f"<{n}I", seg, off))
    fail(f"no loaded bytes at {addr:#x}")


def psram_windows(name: str, syms: dict, segs, crt0_data: bool) -> list:
    """Half-open PSRAM byte ranges of the program's results, merged and sorted."""
    cpu = []
    if crt0_data and syms.get("_bss_end", 0) > syms.get("_data_start", 0):
        cpu.append((syms["_data_start"], syms["_bss_end"]))
    if "_psram_window" in syms:
        addr = syms["_psram_window"]
        while True:
            lo, hi = read_words(segs, addr, 2)
            if lo == 0 and hi == 0:
                break
            cpu.append((lo, hi))
            addr += 8
    out = []
    for lo, hi in cpu:
        if not (lo >> 24) & 1 or lo >> 31 or hi <= lo or (hi - 1) >> 24 != lo >> 24:
            fail(f"{name}: PSRAM window {lo:#x}-{hi:#x} is not one PSRAM range")
        plo, phi = lo & PSRAM_MASK, ((hi - 1) & PSRAM_MASK) + 1
        if phi <= plo or phi > REGION_LO:
            fail(f"{name}: PSRAM window {lo:#x}-{hi:#x} wraps or reaches the core region")
        out.append([plo, phi])
    out.sort()
    merged = []
    for lo, hi in out:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged


def run_iss(iss: str, hexfile: str, max_retire: int):
    p = subprocess.run([iss, hexfile, "--json", "--check-init", "--max", str(max_retire)],
                       capture_output=True, text=True, timeout=600)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    if not lines:
        fail(f"{hexfile}: iss_main gave no summary (exit {p.returncode}): {p.stderr.strip()}")
    return p.returncode, json.loads(lines[-1]), p.stderr.strip()


# ---- declared stimuli (ruling R13) ---------------------------------------------------------------
def stim_uart(s: dict, ppm: int):
    rx = [[s["UART_T0"] + i * s["UART_GAP"], ord(c)] for i, c in enumerate(s["UART_MSG"])]
    if len(rx) != s["UART_N"]:
        fail("stim.h: UART_MSG must have UART_N characters")
    rx += [[s["UART_OVR_T"], s["UART_KEEP"]], [s["UART_OVR_T"] + s["UART_GAP"], s["UART_DROP"]]]
    fields = {"uart_div": s["UART_DIV"], "uart_rx": rx,
              "uart_tx_expect": s["UART_HELLO"] + s["UART_MSG"] + chr(s["UART_KEEP"]),
              "uart_rate_ppm": ppm}
    lines = [f"uart_div {s['UART_DIV']}", f"uart_ppm {ppm}"] + [f"uart_rx {c} {b}" for c, b in rx]
    return fields, lines


def stim_gpio_edge(s: dict):
    script = [[0, s["GE_V0"]]] + [[s["GE_T0"] + (k - 1) * s["GE_GAP"], s[f"GE_V{k}"]]
                                  for k in range(1, 8)]
    return {"gpio_script": script}, [f"gpio {c} {v}" for c, v in script]


def stim_wfi(s: dict):
    idle, pin = 0x02, 1 << s["WFI_PIN"]
    script = [[0, idle]]
    for k in range(s["WFI_PULSES"]):
        t = s["WFI_T0"] + k * s["WFI_PERIOD"]
        script += [[t, idle | pin], [t + s["WFI_PERIOD"] // 2, idle]]
    return {"gpio_script": script}, [f"gpio {c} {v}" for c, v in script]


def declared(name: str, s: dict):
    if name == "uart_echo":
        return stim_uart(s, s["UART_PPM"])
    if name == "uart_echo_fast":
        return stim_uart(s, -s["UART_PPM"])
    if name == "spi_loop":
        return ({"spi_div_d": s["SPI_DIV_D"], "spi_tx_expect": list(range(s["SPI_N"] + 1))},
                ["spi_echo 1"])
    if name == "pwm":
        q, per = s["PWM_PRESCALE"], s["PWM_PERIOD"]
        chans = [{"ch": i, "pin": i + 1, "per": per, "q": q, "duty": s[f"PWM_DUTY_{i}"]}
                 for i in range(3)]
        chans.append({"ch": 7, "pin": 7, "per": 65535, "q": s["PWM_TMR_P"],
                      "duty": s["PWM_TMR_CMP"]})
        return {"pwm": chans}, []
    if name == "ir_loop":
        return {"loop_uo7_to_ui0": True}, ["loop 1"]
    if name in ("irq_timer", "irq_stress"):  # no stimulus: the timer interrupts them
        return {}, []
    if name == "gpio_edge":
        return stim_gpio_edge(s)
    if name == "wfi":
        return stim_wfi(s)
    fail(f"{name}: no declared stimulus")


# ---- main --------------------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iss", required=True)
    ap.add_argument("--hex", required=True)
    ap.add_argument("--elf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--declared", action="store_true",
                    help="a peripheral program: expectations from the declared stimulus")
    ap.add_argument("--max-cycles", type=int,
                    help="a declared program's cycle budget (expect.json max_cycles)")
    ap.add_argument("--coremark-iters", type=int)
    a = ap.parse_args()
    name = os.path.basename(a.hex)[:-len(".hex")]
    syms, segs = elf_parts(a.elf)
    uses_crt0 = "_default_trap" in syms
    exp = {"prog": name, "kind": "declared" if a.declared else "iss",
           "result_gpio": PASS}

    if a.declared:
        rc, summ, err = run_iss(a.iss, a.hex, EVENT_ISS_MAX)
        if rc == 5:
            fail(f"{name}: register or CSR read before any write at pc {summ['uninit_pc']:#x}")
        if summ["end"] not in ("max", "selfloop"):
            fail(f"{name}: standalone ISS run ended with {summ['end']}: {err}")
        exp["psram_window"] = []
        exp["final_altfn"] = FINAL_ALTFN.get(name, 0)
        fields, stim_lines = declared(name, parse_stim())
        exp.update(fields)
        if a.max_cycles:
            exp["max_cycles"] = a.max_cycles
        with open(a.out[:-len(".expect.json")] + ".stim", "w") as f:
            f.write("".join(ln + "\n" for ln in stim_lines))
        summary_line = f"declared ({', '.join(fields) or 'result_gpio only'})"
    else:
        rc, summ, err = run_iss(a.iss, a.hex, ISS_MAX.get(name, ISS_MAX_DEFAULT))
        if rc == 5:
            fail(f"{name}: register or CSR read before any write at pc {summ['uninit_pc']:#x}")
        if summ["end"] != "selfloop":
            fail(f"{name}: the ISS run must end in the self-loop, got {summ['end']} at pc "
                 f"{summ['end_pc']:#x} after {summ['retired']} retirements: {err}")
        if summ["gpio_out"] != PASS:
            fail(f"{name}: failed on the ISS: GPIO_OUT = {summ['gpio_out']:#04x}, uo trace "
                 f"{[hex(v) for v in summ['uo_trace'][-4:]]} (the value before 0xee is the "
                 f"failure id)")
        if summ["altfn"] != 0 or summ["touched_region"] or summ["interrupts"]:
            fail(f"{name}: altfn {summ['altfn']:#x}, touched_region {summ['touched_region']}, "
                 f"interrupts {summ['interrupts']}")
        want_traps = 1 if name.startswith("trap_") else 0
        if summ["traps"] != want_traps:
            fail(f"{name}: {summ['traps']} traps, expected {want_traps}")
        trace = summ["uo_trace"]
        if name != "coremark" and not name.startswith("random_") and MARKS & set(trace):
            fail(f"{name}: shows a reserved mark value (0xA5, 0xC0, 0xC1) on GPIO_OUT")
        if FAIL_MARK in trace:
            fail(f"{name}: GPIO_OUT showed the fail mark 0xEE on the way: {[hex(v) for v in trace]}")
        for k in ("retired", "end", "end_pc", "uo_trace", "gpio_out", "altfn", "traps",
                  "interrupts"):
            exp[k] = summ[k]
        exp["psram_window"] = psram_windows(name, syms, segs, uses_crt0)
        exp["final_altfn"] = 0
        if name == "coremark":
            if a.coremark_iters is None or a.coremark_iters < 1:
                fail("coremark: --coremark-iters is required")
            if trace.count(0xC0) != 1 or trace.count(0xC1) != 1 or \
                    trace.index(0xC0) > trace.index(0xC1):
                fail(f"coremark: expected one 0xC0 then one 0xC1 mark, got {trace}")
            if summ["min_sp"] < LOG_RING_END:
                fail(f"coremark stack reached {summ['min_sp']:#x}, into the log ring")
            exp["coremark_iters"] = a.coremark_iters
            exp["log_contains"] = ["Correct operation validated"]
            exp["log_ring"] = [LOG_RING & PSRAM_MASK, LOG_RING_END & PSRAM_MASK]
            exp["min_sp"] = summ["min_sp"]
        summary_line = f"retired {summ['retired']}, end {summ['end']}, GPIO_OUT {summ['gpio_out']:#04x}"

    with open(a.out, "w") as f:
        json.dump(exp, f, indent=1)
        f.write("\n")
    print(f"{name}: {summary_line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
