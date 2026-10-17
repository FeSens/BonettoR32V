#!/usr/bin/env python3
"""Run the reference ISS on a program image, check the program is valid, write <name>.expect.json.

Usage: mkexpect.py --iss ISS_MAIN --hex PROG.hex --out PROG.expect.json

Checks (any failure exits 1, so `make -C verif/programs` fails):
  - the program never reads a register before writing it (the core's registers start unknown);
  - it never executes a reserved or unspecified instruction, and never touches the core region
    0xFF80-0xFFFF (contract v2 section 3; the ISS reports such an access as unspecified);
  - it halts: on EBREAK, or for trap_<kind> programs on exactly that fatal trap;
  - selftest leaves uo_out = 0xA5 under both GPIO input schedules;
  - gpio_demo retires fewer than 2,000 instructions;
  - coremark prints "Correct operation validated" in the RAM log ring and its stack stays above
    0xF800 (the log ring sits below the stack).
The expect file holds the ISS's retired count and uo_out trace for the pins schedule with seed 1
(what the gate-level test checks), plus the RVFI schedule, plus "log_contains" for programs whose
RAM log ring the cosim must check.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

TRAPS = {"trap_ecall": 0x00000073, "trap_jal": 0x006000EF, "trap_jalr": 0x003280E7,
         "trap_branch": 0x00630363, "trap_zero": 0x00000000, "trap_ones": 0xFFFFFFFF}
LOG_RING = 0xF000
LOG_RING_END = 0xF800


def log_ring_text(ram: bytes) -> str:
    raw = ram[LOG_RING:LOG_RING_END]
    end = raw.find(b"\0")
    return raw[:end if end >= 0 else len(raw)].decode("latin-1")


def fail(msg: str) -> None:
    print(f"mkexpect: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iss", required=True)
    ap.add_argument("--hex", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    name = os.path.basename(a.hex)[:-len(".hex")]
    with tempfile.TemporaryDirectory() as td:
        ram_path = os.path.join(td, "ram.bin")
        p = subprocess.run([a.iss, a.hex, "--check-init", "--ui", "pins", "--seed", "1",
                            "--expect", a.out, "--dump-ram", ram_path],
                           capture_output=True, text=True)
        summary = json.loads(p.stdout.strip().splitlines()[-1])
        if p.returncode == 5:
            fail(f"{name}: register read before any write at pc {summary['uninit_pc']:#x}")
        if p.returncode == 2:
            fail(f"{name}: reserved/unspecified instruction: {p.stderr.strip()}")
        if p.returncode == 4:
            fail(f"{name}: did not halt")
        with open(ram_path, "rb") as f:
            ram = f.read()
    exp = json.load(open(a.out))
    if name in TRAPS:
        if p.returncode != 3 or exp["halt_insn"] != TRAPS[name]:
            fail(f"{name}: expected a halt on {TRAPS[name]:#010x}, got {summary}")
    elif p.returncode != 0:
        fail(f"{name}: expected an EBREAK halt, got exit {p.returncode}: {summary}")
    if exp["halt"] != exp["rvfi"]["halt"]:
        fail(f"{name}: the two GPIO schedules halt differently")
    if name == "selftest":
        if exp["uo_final"] != 0xA5 or exp["rvfi"]["uo_final"] != 0xA5:
            fail(f"selftest failed on the ISS: uo_out = {exp['uo_final']:#x} / "
                 f"{exp['rvfi']['uo_final']:#x} (failing test id at verdict + 4)")
    if name == "gpio_demo" and exp["retired"] >= 2000:
        fail(f"gpio_demo retires {exp['retired']} instructions (limit 2,000)")
    exp["prog"] = name
    if name == "coremark":
        text = log_ring_text(ram)
        if "Correct operation validated" not in text:
            fail(f"coremark did not validate on the ISS. Log:\n{text}")
        if summary["min_sp"] < LOG_RING_END:
            fail(f"coremark stack reached {summary['min_sp']:#x}, into the log ring")
        exp["log_contains"] = ["Correct operation validated"]
        exp["log"] = text
        exp["min_sp"] = summary["min_sp"]
    with open(a.out, "w") as f:
        json.dump(exp, f, indent=1)
        f.write("\n")
    print(f"{name}: retired {exp['retired']}, halt {exp['halt']}, uo_out {exp['uo_final']:#04x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
