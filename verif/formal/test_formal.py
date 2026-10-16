"""Tests for the BonettoR32V formal gate (verif/formal).

  pytest verif/formal/test_formal.py -v

- The rv32e ISA files are up to date with gen_isa_rv32e.py.
- The SPI monitor decodes a hand-written SPI waveform (a stub driver with MISO noise
  while SCK is low) into exactly the expected events.
- The depth policy and timing summary.
- Every mutant in mutants/ (the seed with one bug) FAILS the checks its expect.json
  names, at the seed's depth.
- The seed (src/) PASSES the full suite (checks.cfg). Set BONETTO_SKIP_SEED=1 to skip.

Formal runs take the heavy-job lock (lockf -k /tmp/bonetto-heavy.lock).
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RUN = os.path.join(HERE, "run_formal.py")
MUT_DIR = os.path.join(HERE, "mutants")
LOCK = ["lockf", "-k", "/tmp/bonetto-heavy.lock"] if shutil.which("lockf") else []

sys.path.insert(0, HERE)
import run_formal  # noqa: E402


def mutants():
    if not os.path.isdir(MUT_DIR):
        return []
    return sorted(d for d in os.listdir(MUT_DIR)
                  if os.path.isfile(os.path.join(MUT_DIR, d, "expect.json")))


def formal(args, timeout=4 * 3600):
    cmd = LOCK + [sys.executable, RUN] + args
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


# ---------------------------------------------------------------------------

def test_isa_generated_up_to_date():
    p = subprocess.run([sys.executable, os.path.join(HERE, "gen_isa_rv32e.py"), "--check"],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    insns = open(os.path.join(HERE, "isa_rv32e.txt")).read().split()
    assert len(insns) == 40 and {"fence", "ecall", "ebreak", "sub", "lw"} <= set(insns)


def test_depth_policy():
    cfg = {"reset_steps": 1, "depth_margin": 2, "depth_floor": 0, "depth_cap": 2048}
    d, dmin = run_formal.pick_depth(cfg, {"first_retire_cycle": 114, "max_cycles_per_insn": 230})
    assert dmin == 1 + 230 + 230 + 2 and d == dmin
    cfg["depth_floor"] = 600
    assert run_formal.pick_depth(cfg, {"first_retire_cycle": 114, "max_cycles_per_insn": 230})[0] == 600
    t = run_formal.timing_from_retires([(114, 0x00000013, 0), (344, 0x00002083, 0), (460, 0x00100073, 1)])
    assert t["first_retire_cycle"] == 114 and t["max_cycles_per_insn"] == 230
    assert t["per_class"]["load"]["max_cycles"] == 230


# ---------------------------------------------------------------------------
# SPI monitor against a hand-written stub driver.

def _stub_waveform():
    """Steps of (cs, sck, mosi, miso) and the events the contract implies."""
    steps = []
    events = {}   # step -> dict of expected event fields

    def put(cs, sck, mosi, miso):
        steps.append((cs, sck, mosi, miso))
        return len(steps) - 1

    def idle(n):
        for _ in range(n):
            put(1, 0, 0, 1)

    def txn(cmd, addr, data_bits, write, half=1, mem=None, stop_bits=None):
        """data_bits: list of MOSI bits (write) or None (read, MISO from mem)."""
        hdr = [(cmd >> (7 - i)) & 1 for i in range(8)] + [(addr >> (15 - i)) & 1 for i in range(16)]
        nbits = len(hdr) + (len(data_bits) if write else len(mem) * 8)
        if stop_bits is not None:
            nbits = stop_bits
        put(0, 0, hdr[0], 0)   # CS falls with SCK low
        for i in range(nbits):
            if i < 24:
                mo, mi = hdr[i], None
            else:
                k, b = divmod(i - 24, 8)
                if write:
                    mo, mi = data_bits[i - 24], None
                else:
                    mo, mi = 0, (mem[k] >> (7 - b)) & 1
            for _ in range(half):       # low phase: MOSI set up, MISO noise
                put(0, 0, mo, 1 - (mi if mi is not None else 0))
            rise = None
            for j in range(half):       # high phase: memory drives the bit
                s = put(0, 1, mo, mi if mi is not None else (j & 1))
                if rise is None:
                    rise = s
            ev = events.setdefault(rise, {})
            if i == 7:
                ev["cmd"] = cmd
            if i >= 24:
                k, b = divmod(i - 24, 8)
                a = (addr + k) & 0xffff
                if not write:
                    ev["rd"] = (a, b, mi)
                if b == 7:
                    byte = mem[k] if not write else int("".join(map(str, data_bits[8*k:8*k+8])), 2)
                    ev["byte"] = (int(write), a, byte)
        put(0, 0, 0, 1)                 # SCK low before CS rises
        end = put(1, 0, 0, 1)
        data = max(0, nbits - 24)
        events.setdefault(end, {})["end"] = (int(nbits >= 24), int(write), int(data >= 8), data % 8)

    idle(3)
    txn(0x03, 0xfffe, None, False, half=1, mem=[0x5a, 0xc3, 0x81])          # READ wraps to 0x0000
    idle(2)
    bits = [int(c) for c in "1010010100111100"]                              # 0xA5, 0x3C
    txn(0x02, 0x1234, bits, True, half=2)                                    # stretched phases
    idle(1)
    txn(0x03, 0x00ff, None, False, half=1, mem=[0x96], stop_bits=24 + 5)     # READ ends mid-byte
    idle(1)
    txn(0x02, 0x0010, [1] * 16, True, half=1, stop_bits=24 + 11)             # WRITE ends mid-byte
    idle(1)
    txn(0x03, 0x0000, None, False, half=1, mem=[0], stop_bits=10)            # header incomplete
    idle(2)
    return steps, events


def _stub_verilog(steps, events):
    lines = ["module stub_tb (input clock);",
             "  reg [15:0] step = 16'd0;",
             "  always @(posedge clock) step <= step + 16'd1;",
             "  reg cs, sck, mosi, miso;",
             "  always @* begin",
             "    {cs, sck, mosi, miso} = 4'b1001;",
             "    case (step)"]
    for i, (c, s, mo, mi) in enumerate(steps):
        lines.append(f"      {i}: {{cs, sck, mosi, miso}} = 4'b{c}{s}{mo}{mi};")
    lines += ["    endcase", "  end",
              "  wire mon_en, mon_pair, mon_cs, mon_sck, mon_mosi, mon_miso, mon_pcs, mon_psck, mon_pmosi, mon_rise;",
              "  wire ev_cmd_done, ev_rd_bit, ev_rd_val, ev_byte, ev_byte_wr, ev_end, ev_end_hdr, ev_end_write, ev_end_bytes;",
              "  wire [7:0] ev_cmd, ev_byte_data; wire [15:0] ev_rd_addr, ev_byte_addr; wire [2:0] ev_rd_idx, ev_end_bit;",
              "  bonetto_spi_monitor m (.clock(clock), .reset(step == 16'd0), .cs(cs), .mosi(mosi), .sck(sck), .miso(miso),",
              "    .mon_en(mon_en), .mon_pair(mon_pair), .mon_cs(mon_cs), .mon_sck(mon_sck), .mon_mosi(mon_mosi),",
              "    .mon_miso(mon_miso), .mon_pcs(mon_pcs), .mon_psck(mon_psck), .mon_pmosi(mon_pmosi), .mon_rise(mon_rise),",
              "    .ev_cmd_done(ev_cmd_done), .ev_cmd(ev_cmd), .ev_rd_bit(ev_rd_bit), .ev_rd_addr(ev_rd_addr),",
              "    .ev_rd_idx(ev_rd_idx), .ev_rd_val(ev_rd_val), .ev_byte(ev_byte), .ev_byte_wr(ev_byte_wr),",
              "    .ev_byte_addr(ev_byte_addr), .ev_byte_data(ev_byte_data), .ev_end(ev_end), .ev_end_hdr(ev_end_hdr),",
              "    .ev_end_write(ev_end_write), .ev_end_bytes(ev_end_bytes), .ev_end_bit(ev_end_bit));",
              "  always @* begin",
              "    if (step > 16'd0) case (step)"]
    for st in sorted(events):
        ev = events[st]
        a = [f"ev_cmd_done == {int('cmd' in ev)}", f"ev_rd_bit == {int('rd' in ev)}",
             f"ev_byte == {int('byte' in ev)}", f"ev_end == {int('end' in ev)}"]
        if "cmd" in ev:
            a.append(f"ev_cmd == 8'h{ev['cmd']:02x}")
        if "rd" in ev:
            addr, idx, val = ev["rd"]
            a += [f"ev_rd_addr == 16'h{addr:04x}", f"ev_rd_idx == {idx}", f"ev_rd_val == {val}"]
        if "byte" in ev:
            w, addr, data = ev["byte"]
            a += [f"ev_byte_wr == {w}", f"ev_byte_addr == 16'h{addr:04x}", f"ev_byte_data == 8'h{data:02x}"]
        if "end" in ev:
            h, w, b, bit = ev["end"]
            a += [f"ev_end_hdr == {h}", f"ev_end_write == {w}", f"ev_end_bytes == {b}", f"ev_end_bit == {bit}"]
        lines.append(f"      {st}: assert ({' && '.join(a)});")
    lines += ["      default: assert (!ev_cmd_done && !ev_rd_bit && !ev_byte && !ev_end);",
              "    endcase",
              "  end",
              "endmodule"]
    return "\n".join(lines) + "\n"


def test_monitor_decodes_stub(tmp_path):
    run_formal.setup_path()
    steps, events = _stub_waveform()
    assert sum(1 for e in events.values() if "byte" in e) == 3 + 2 + 0 + 1
    (tmp_path / "stub_tb.v").write_text(_stub_verilog(steps, events))
    depth = len(steps) + 2
    (tmp_path / "stub.sby").write_text(f"""[options]
mode bmc
depth {depth}

[engines]
smtbmc yices

[script]
read -formal bonetto_macros.vh wrapper.sv stub_tb.v
hierarchy -top stub_tb
prep -flatten -nordff -top stub_tb

[files]
{os.path.join(HERE, "checks", "bonetto_macros.vh")}
{os.path.join(HERE, "wrapper.sv")}
{os.path.join(ROOT, "third_party", "riscv-formal", "checks", "rvfi_macros.vh")}
{tmp_path / "stub_tb.v"}
""")
    # wrapper.sv's rvfi_wrapper needs the RVFI macros even though only the monitor is used.
    (tmp_path / "defs.vh").write_text("")
    sby = (tmp_path / "stub.sby").read_text().replace(
        "read -formal bonetto_macros.vh wrapper.sv stub_tb.v",
        "read -formal -D RISCV_FORMAL -D RISCV_FORMAL_NRET=1 -D RISCV_FORMAL_XLEN=32 "
        "-D RISCV_FORMAL_ILEN=32 rvfi_macros.vh bonetto_macros.vh wrapper.sv stub_tb.v")
    (tmp_path / "stub.sby").write_text(sby)
    p = subprocess.run(["sby", "-f", "stub.sby"], cwd=tmp_path, capture_output=True, text=True)
    assert "DONE (PASS" in p.stdout, p.stdout[-3000:]


# ---------------------------------------------------------------------------
# Mutants and the seed.

@pytest.fixture(scope="session")
def seed_timing(tmp_path_factory):
    out = tmp_path_factory.mktemp("timing") / "seed_timing.json"
    rc, text = formal(["--timing-only", str(out)], timeout=3600)
    assert rc == 0, text[-3000:]
    return str(out)


@pytest.mark.parametrize("mutant", mutants())
def test_mutant_fails(mutant, tmp_path, seed_timing):
    mdir = os.path.join(MUT_DIR, mutant)
    expect = json.load(open(os.path.join(mdir, "expect.json")))
    src = tmp_path / "src"
    shutil.copytree(os.path.join(ROOT, "src"), src)
    for f in os.listdir(mdir):
        if f.endswith((".v", ".sv", ".vh")):
            shutil.copy(os.path.join(mdir, f), src / f)
    out = tmp_path / "formal.json"
    regex = "^(" + "|".join(expect["checks"]) + ")$"
    rc, text = formal(["--src", str(src), "--timing", seed_timing, "--only", regex,
                       "--out", str(out)])
    res = json.load(open(out))
    # A FAIL must be a counterexample (a failed assertion), not a timeout or an error;
    # equiv fails in the pre-pass and has no SBY task.
    failed = [c for c, v in res["checks"].items() if v == "FAIL" and
              (c == "equiv" or res["failures"].get(c, {}).get("assertions"))]
    assert rc != 0, f"{mutant} passed formal:\n{text[-3000:]}"
    assert set(expect["checks"]) & set(failed), \
        f"{mutant}: expected a counterexample in {expect['checks']}, got {res['checks']}\n" \
        f"{res.get('failures')}\n{text[-2000:]}"
    print(f"{mutant}: FAIL in {sorted(set(expect['checks']) & set(failed))}: "
          f"{ {c: res['failures'].get(c, {}).get('assertions') for c in failed} }")


@pytest.mark.skipif(os.environ.get("BONETTO_SKIP_SEED") == "1", reason="BONETTO_SKIP_SEED=1")
def test_seed_passes(tmp_path):
    out = tmp_path / "formal.json"
    rc, text = formal(["--out", str(out)])
    res = json.load(open(out))
    assert rc == 0 and res["pass"], f"{res.get('checks')}\n{res.get('failures')}\n{text[-3000:]}"
    assert res["depth"] >= res["depth_policy"]["d_min"]
    assert all(v == "PASS" for v in res["checks"].values())
