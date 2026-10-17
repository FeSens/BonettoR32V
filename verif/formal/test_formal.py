"""Tests for the BonettoR32V formal gate (verif/formal).

  pytest verif/formal/test_formal.py -v

- The rv32e ISA files are up to date with gen_isa_rv32e.py.
- The SPI monitor decodes a hand-written SPI waveform (a stub driver with MISO noise
  while SCK is low) into exactly the expected events.
- The depth policy and timing summary.
- The verdict aggregation (a group counterexample, SKIPPED tasks).
- Every mutant in mutants/ FAILS the checks its expect.json names, with a
  counterexample, at the seed's depth (or the depth of the design its "timing" names);
  the legal variants ("expect": "pass") PASS ("checks": "all": the whole fast suite).
  Mutants marked "slow" (the hyp/002 cores, hours each) run only with
  BONETTO_FORMAL_SLOW=1.
- The seed (src/) PASSES the fast suite (checks.cfg). Set BONETTO_SKIP_SEED=1 to skip.

With BONETTO_FORMAL_HOST set, every formal run goes to that host (its own lock).

The formal runs (mutants, seed) take the heavy-job lock /tmp/bonetto-heavy.lock once
for the whole session (the same lock as `lockf -k /tmp/bonetto-heavy.lock`). If you
already run pytest under that lock, set BONETTO_HEAVY_LOCK_HELD=1.
"""
import fcntl
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
LOCK_PATH = "/tmp/bonetto-heavy.lock"

sys.path.insert(0, HERE)
import run_formal  # noqa: E402


def mutants():
    if not os.path.isdir(MUT_DIR):
        return []
    return sorted(d for d in os.listdir(MUT_DIR)
                  if os.path.isfile(os.path.join(MUT_DIR, d, "expect.json")))


@pytest.fixture(scope="session")
def heavy_lock():
    """Hold the heavy-job lock for every formal run of the session (one acquisition).
    Not needed when the runs go to a remote host (BONETTO_FORMAL_HOST): that host has
    its own lock."""
    if os.environ.get("BONETTO_HEAVY_LOCK_HELD") == "1" or os.environ.get("BONETTO_FORMAL_HOST"):
        yield
        return
    with open(LOCK_PATH, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def formal(args, timeout=4 * 3600):
    """run_formal.py; callers depend on the heavy_lock fixture."""
    cmd = [sys.executable, RUN] + args
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
    # depth=short: reset + first retirement + the group's slowest class + margin.
    t = {"first_retire_cycle": 319, "max_cycles_per_insn": 736,
         "per_class": {"alu_reg": {"max_cycles": 416}, "load": {"max_cycles": 736},
                       "branch": {"max_cycles": 320}}}
    assert run_formal.short_depth(cfg, t) == 1 + 319 + 736 + 2
    assert run_formal.short_depth(cfg, t, ["add", "sub"]) == 1 + 319 + 416 + 2
    assert run_formal.short_depth(cfg, t, ["beq", "lw"]) == 1 + 319 + 736 + 2
    assert run_formal.short_depth(cfg, t, ["jal"]) == 1 + 319 + 736 + 2   # class not measured
    assert sorted(sum(run_formal.CLASS_GROUPS, [])) == sorted(run_formal.isa_insns())
    # The cosim's per_class format ({class: cycles}, its own class names).
    t = {"first_retire_cycle": 116, "max_cycles_per_insn": 230,
         "per_class": {"alu": 116, "shift": 116, "load": 230, "io_load": 118, "trap": 116,
                       "branch_taken": 116, "branch_not_taken": 116}}
    assert run_formal.short_depth(cfg, t, ["add", "sll"]) == 1 + 116 + 116 + 2
    assert run_formal.short_depth(cfg, t, ["lw"]) == 1 + 116 + 230 + 2
    assert run_formal.short_depth(cfg, t, ["beq"]) == 1 + 116 + 116 + 2
    assert run_formal.short_depth(cfg, t, ["sw"]) == 1 + 116 + 230 + 2   # store not measured


def test_check_verdicts():
    """A check PASSes only if its proof task passed and its reach task reached every
    required trigger; a counterexample is a FAIL; a missing trigger is UNREACHED; a task
    that did not finish (timeout, error, unknown) is a FAIL."""
    c = run_formal.classify
    req = ["reach_insn", "reach_insn_trap"]
    det = lambda *l: {"failed": list(l)}
    both = det("reach_insn", "reach_insn_trap")
    assert c("PASS", det(), "FAIL", both, req)[0] == "PASS"
    assert c("PASS", det(), "FAIL", det("reach_insn"), req)[0] == "UNREACHED"
    assert c("PASS", det(), "PASS", det(), req)[0] == "UNREACHED"
    v, info = c("FAIL", det("insn_rd_wdata"), "FAIL", both, req)
    assert v == "FAIL" and info["assertions"] == ["insn_rd_wdata"]
    assert c("FAIL", det(), "FAIL", both, req)[0] == "FAIL"
    for st in ("TIMEOUT", "UNKNOWN", "ERROR"):
        assert c(st, det(), "FAIL", both, req)[0] == "FAIL"
        assert c("PASS", det(), st, both, req)[0] == "FAIL"
    assert run_formal.insn_reach("jal") == ["reach_insn_jal", "reach_insn_jal_trap"]
    assert run_formal.insn_reach("add") == ["reach_insn_add"]


def test_task_line():
    """Progress lines: a reach task whose SBY status is FAIL with every required label is
    REACHED (the reach labels are assert(!trigger)); a missing label, or a reach task that
    "passed" (nothing reached), is UNREACHED and names what is missing."""
    t = run_formal.task_line
    req = ["reach_insn_add", "reach_insn_sub"]
    assert "REACHED   2/2 triggers" in t("insn_group0_reach", "FAIL", 9.0, req[::-1], req)
    line = t("insn_group0_reach", "FAIL", 9.0, ["reach_insn_add"], req)
    assert "UNREACHED 1/2" in line and "missing: reach_insn_sub" in line
    assert "UNREACHED 0/2" in t("insn_group0_reach", "PASS", 9.0, [], req)
    assert "TIMEOUT" in t("insn_group0_reach", "TIMEOUT", 9.0, [], req)
    assert "failed assertions: insn_rd_wdata" in t("insn_group0", "FAIL", 9.0, ["insn_rd_wdata"])


def test_timing_merge():
    """The timing is the max over the cosim programs, per class too."""
    a = {"first_retire_cycle": 115, "max_cycles_per_insn": 230,
         "per_class": {"alu": 116, "load": 230}}
    b = {"first_retire_cycle": 993, "max_cycles_per_insn": 893,
         "per_class": {"alu": 700, "store": 893}}
    m = run_formal.merge_timings([a, b])
    assert m["first_retire_cycle"] == 993 and m["max_cycles_per_insn"] == 893
    assert m["per_class"] == {"alu": 700, "load": 230, "store": 893}


def _cex_vcd(path, insn):
    """A minimal counterexample trace: one retirement of `insn` in step 1."""
    path.write_text("$scope module rvfi_testbench $end\n"
                    "$var wire 1 ! rvfi_valid $end\n$var wire 32 \" rvfi_insn $end\n"
                    "$upscope $end\n$enddefinitions $end\n"
                    f"#0\n0!\nb0 \"\n#10\n1!\nb{insn:032b} \"\n#20\n0!\n")
    return str(path)


def test_aggregate_group_failure(tmp_path):
    """A counterexample in a two-member group: the culprit FAILs with its assertion
    and trace, the other member FAILs as not proven, and nothing crashes (review I2)."""
    vcd = _cex_vcd(tmp_path / "trace.vcd", 0x40000033)   # sub x0, x0, x0
    proof = ("insn_group0", "FAIL", 12.3,
             {"failed": ["insn_rd_wdata"], "traces": {"insn_rd_wdata": vcd}, "errors": []})
    reach = ("insn_group0_reach", "FAIL", 4.5,
             {"failed": ["reach_insn_add", "reach_insn_sub"], "traces": {}, "errors": []})
    units = [("insn_group0", [("insn_add", ["reach_insn_add"]),
                              ("insn_sub", ["reach_insn_sub"])])]
    checks, runtimes, failures, reached, skipped = run_formal.aggregate(
        units, {"insn_group0": proof, "insn_group0_reach": reach})
    assert checks == {"insn_add": "FAIL", "insn_sub": "FAIL"}
    assert failures["insn_sub"]["assertions"] == ["insn_rd_wdata"]
    assert failures["insn_sub"]["trace"] == vcd
    assert "not proven" in failures["insn_add"]["why"] and "assertions" not in failures["insn_add"]
    assert failures["insn_add"]["trace"] == vcd
    assert runtimes["insn_add"] == {"proof": 12.3, "reach": 4.5, "task": "insn_group0"}
    assert skipped == []


def test_aggregate_skipped():
    """Fail-fast: a proof that FAILed stays FAIL even if its reach task was skipped; a
    check whose proof did not run is SKIPPED, or UNREACHED if its finished reach task
    misses a trigger."""
    det = lambda *l: {"failed": list(l), "traces": {}, "errors": []}
    res = {
        "spi": ("spi", "FAIL", 9.0, det("spi_rule2")),
        "spi_reach": ("spi_reach", "SKIPPED", 0.0, det()),
        "gpio": ("gpio", "SKIPPED", 0.0, det()),
        "gpio_reach": ("gpio_reach", "FAIL", 3.0, det("reach_gpio_load", "reach_gpio_store")),
        "reg": ("reg", "PASS", 20.0, det()),
        "reg_reach": ("reg_reach", "SKIPPED", 0.0, det()),
        "ill": ("ill", "SKIPPED", 0.0, det()),
        "ill_reach": ("ill_reach", "PASS", 5.0, det("reach_ill_zero")),
    }
    units = [("spi", [("spi", ["reach_spi_end_read"])]),
             ("gpio", [("gpio", ["reach_gpio_load", "reach_gpio_store"])]),
             ("reg", [("reg", ["reach_reg"])]),
             ("ill", [("ill", ["reach_ill_zero", "reach_ill_ones"])])]
    checks, _, failures, _, skipped = run_formal.aggregate(units, res)
    assert checks == {"spi": "FAIL", "gpio": "SKIPPED", "reg": "SKIPPED", "ill": "UNREACHED"}
    assert failures["spi"]["assertions"] == ["spi_rule2"]
    assert failures["ill"]["unreached"] == ["reach_ill_ones"]
    assert sorted(skipped) == ["gpio", "reg"]


def test_reap_remote_orphans(tmp_path, monkeypatch):
    """Orphaned remote runs (this machine's run dirs whose local runner is dead): a
    finished one's result is fetched and its dir deleted, a dead one's dir is deleted, a
    running one stays. A live runner's dirs and other machines' dirs are never touched.
    The 'ssh' here runs the commands locally."""
    dead = subprocess.Popen(["true"])
    dead.wait()
    base = tmp_path / "remote"
    done, gone, running = (f"run-mac-{dead.pid}-{i}" for i in (1, 4, 5))
    keep = [f"run-mac-{os.getpid()}-2", f"run-other-{dead.pid}-3", "formal.lock", running]
    for n in [done, gone] + keep:
        (base / n).mkdir(parents=True)
    (base / done / "done").write_text("0\n")
    (base / running / "remote.pid").write_text(f"{os.getpid()}\n")
    monkeypatch.setattr(run_formal, "REMOTE_BASE", str(base))
    ssh = ["sh", "-c"]
    assert sorted(run_formal.remote_orphans(ssh, "mac")) == sorted(
        [(done, "done"), (gone, "dead"), (running, "running")])
    calls = []

    def fake_fetch(host, rdir, out, logs):
        calls.append(rdir)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w").write("{}")
        return True
    monkeypatch.setattr(run_formal, "fetch_remote", fake_fetch)
    got = run_formal.reap_remote(ssh, "host", "mac", str(tmp_path / "local"))
    assert calls == [f"{base}/{done}"]
    assert got == [str(tmp_path / "local" / f"orphan-{done}" / "formal.json")]
    assert sorted(os.listdir(base)) == sorted(keep)


def test_stage_takes_every_top_level_v(tmp_path):
    """Contract section 1: every top-level src/*.v is design, test_*.v included;
    subdirectories are not."""
    src = tmp_path / "src"
    (src / "test_unit").mkdir(parents=True)
    for n in ("tt_um_fesens_r32v.v", "test_helper.v", "r32v_alu.v"):
        (src / n).write_text("// x\n")
    (src / "test_unit" / "tb.v").write_text("// x\n")
    (src / "notes.md").write_text("x\n")
    _, rtl = run_formal.stage(str(src), str(tmp_path / "work"))
    assert [os.path.basename(f) for f in rtl] == ["r32v_alu.v", "test_helper.v",
                                                  "tt_um_fesens_r32v.v"]


def test_reach_and_proof_tasks_are_separate(tmp_path):
    """The proof task has no reach statements; the reach task keeps only them."""
    run_formal.setup_path()
    p = run_formal.write_sby(str(tmp_path), "x", "bonetto_spi_check", 100,
                             "smtbmc yices step=all", 60)
    r = run_formal.write_sby(str(tmp_path), "x_reach", "bonetto_spi_check", 100,
                             "smtbmc yices step=29", 60, role="reach",
                             reach=["reach_spi_end_read", "reach_spi_end_write"])
    pt, rt = open(p).read(), open(r).read()
    assert "chformal -cover -remove" in pt and "BONETTO_REACH_AS_ASSERT" not in pt
    assert "--keep-going" not in pt and "-t 0:100:100" in pt
    assert "BONETTO_REACH_AS_ASSERT" in rt
    assert "select -set keep c:*.reach_spi_end_read c:*.reach_spi_end_write" in rt
    assert "chformal -assert -remove c:* @keep %d" in rt
    assert "smtbmc --keep-going --nopresat yices -- -t 0:29:100" in rt


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
        if nbits >= 24:
            # Section 6 classes: the bytes with at least one data bit (a partial last byte
            # counts); none: a region transaction iff A is in the region.
            byts = {(addr + k) & 0xffff for k in range((data + 7) // 8)}
            reg = {b for b in byts if b >= 0xff80}
            region = int(bool(reg) and reg == byts) if byts else int(addr >= 0xff80)
            mixed = int(bool(reg) and reg != byts)
            events[end]["cls"] = (addr, min(data, 1023), region, mixed)

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
    idle(1)
    txn(0x03, 0xff80, None, False, half=1, mem=[0x11, 0x22])                 # region READ
    idle(1)
    txn(0x03, 0xfff0, None, False, half=1, mem=[0], stop_bits=24)            # null READ, region
    idle(1)
    txn(0x03, 0x4000, None, False, half=1, mem=[0], stop_bits=24)            # null READ, program
    idle(1)
    txn(0x03, 0xffff, None, False, half=1, mem=[0x33, 0x44], stop_bits=24 + 9)  # one bit past 0xFFFF
    idle(1)
    txn(0x02, 0xfffe, [0, 1] * 8, True, half=1)                              # region WRITE
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
              "  wire [15:0] ev_end_addr; wire [9:0] ev_end_nbits; wire ev_end_region, ev_end_mixed;",
              "  bonetto_spi_monitor m (.clock(clock), .reset(step == 16'd0), .cs(cs), .mosi(mosi), .sck(sck), .miso(miso),",
              "    .mon_en(mon_en), .mon_pair(mon_pair), .mon_cs(mon_cs), .mon_sck(mon_sck), .mon_mosi(mon_mosi),",
              "    .mon_miso(mon_miso), .mon_pcs(mon_pcs), .mon_psck(mon_psck), .mon_pmosi(mon_pmosi), .mon_rise(mon_rise),",
              "    .ev_cmd_done(ev_cmd_done), .ev_cmd(ev_cmd), .ev_rd_bit(ev_rd_bit), .ev_rd_addr(ev_rd_addr),",
              "    .ev_rd_idx(ev_rd_idx), .ev_rd_val(ev_rd_val), .ev_byte(ev_byte), .ev_byte_wr(ev_byte_wr),",
              "    .ev_byte_addr(ev_byte_addr), .ev_byte_data(ev_byte_data), .ev_end(ev_end), .ev_end_hdr(ev_end_hdr),",
              "    .ev_end_write(ev_end_write), .ev_end_bytes(ev_end_bytes), .ev_end_bit(ev_end_bit),",
              "    .ev_end_addr(ev_end_addr), .ev_end_nbits(ev_end_nbits), .ev_end_region(ev_end_region),",
              "    .ev_end_mixed(ev_end_mixed));",
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
        if "cls" in ev:
            addr, nb, region, mixed = ev["cls"]
            a += [f"ev_end_addr == 16'h{addr:04x}", f"ev_end_nbits == {nb}",
                  f"ev_end_region == {region}", f"ev_end_mixed == {mixed}"]
        elif "end" in ev:
            a += ["!ev_end_region"]
        lines.append(f"      {st}: assert ({' && '.join(a)});")
    lines += ["      default: assert (!ev_cmd_done && !ev_rd_bit && !ev_byte && !ev_end);",
              "    endcase",
              "  end",
              "endmodule"]
    return "\n".join(lines) + "\n"


def test_monitor_decodes_stub(tmp_path):
    run_formal.setup_path()
    steps, events = _stub_waveform()
    assert sum(1 for e in events.values() if "byte" in e) == 3 + 2 + 0 + 1 + 2 + 1 + 2
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

def _src_with(tmp_path, rtl_dir):
    """A copy of src/ with the top-level design files of rtl_dir on top."""
    src = tmp_path / "src"
    shutil.copytree(os.path.join(ROOT, "src"), src)
    for f in os.listdir(rtl_dir):
        if f.endswith((".v", ".sv", ".vh")):
            shutil.copy(os.path.join(rtl_dir, f), src / f)
    return src


TIMINGS = {}


def design_timing(name, tmp_path_factory):
    """The cosim timing of the seed (name None) or of verif/cosim/mutants/<name>."""
    if name not in TIMINGS:
        d = tmp_path_factory.mktemp("timing")
        args = ["--timing-only", str(d / "timing.json")]
        if name:
            args += ["--src", str(_src_with(d, os.path.join(ROOT, "verif", "cosim", "mutants", name)))]
        rc, text = formal(args, timeout=7200)
        assert rc == 0, text[-3000:]
        TIMINGS[name] = str(d / "timing.json")
    return TIMINGS[name]


@pytest.fixture(scope="session")
def seed_timing(tmp_path_factory, heavy_lock):
    return design_timing(None, tmp_path_factory)


def fast_checks():
    cfg = run_formal.load_config(os.path.join(HERE, "checks.cfg"))
    names = set()
    for name, kind, _ in cfg["checks"]:
        names |= {f"insn_{i}" for i in run_formal.isa_insns()} if kind == "insn" else {name}
    return names | {"structure", "equiv"}


def _expect(mutant):
    return json.load(open(os.path.join(MUT_DIR, mutant, "expect.json")))


@pytest.mark.parametrize("mutant", [
    pytest.param(m, marks=pytest.mark.skipif(
        _expect(m).get("slow") and os.environ.get("BONETTO_FORMAL_SLOW") != "1",
        reason="slow (hours): BONETTO_FORMAL_SLOW=1")) for m in mutants()])
def test_mutant_fails(mutant, tmp_path, tmp_path_factory, seed_timing, heavy_lock):
    """Each mutant FAILS (a counterexample) in the checks its expect.json names, at the
    seed's depth (or its "timing" design's); the legal variants and the liveness boundary
    mutants marked "pass" PASS ("checks": "all": the whole fast suite). A check that is
    only in the deep config runs with --deep. "rtl" points at a shared cosim mutant."""
    mdir = os.path.join(MUT_DIR, mutant)
    expect = _expect(mutant)
    src = _src_with(tmp_path, os.path.join(ROOT, expect["rtl"]) if expect.get("rtl") else mdir)
    timing = design_timing(expect["timing"], tmp_path_factory) if expect.get("timing") else seed_timing
    out = tmp_path / "formal.json"
    args = ["--src", str(src), "--timing", timing, "--out", str(out), "--keep-going"]
    if expect["checks"] != "all":
        args += ["--only", "^(" + "|".join(expect["checks"]) + ")$"]
        if expect.get("deep") or not set(expect["checks"]) <= fast_checks():
            args.append("--deep")
    rc, text = formal(args, timeout=12 * 3600)
    res = json.load(open(out))
    if expect.get("expect") == "pass":
        ok = rc == 0 and (res["pass"] if expect["checks"] == "all" else
                          all(res["checks"].get(c) == "PASS" for c in expect["checks"]))
        assert ok, f"{mutant}: expected PASS in {expect['checks']}, got " \
                   f"{ {c: v for c, v in res['checks'].items() if v != 'PASS'} }\n" \
                   f"{res.get('failures')}\n{text[-2000:]}"
        print(f"{mutant}: PASS in {expect['checks']} ({len(res['checks'])} checks, "
              f"depths {sorted(set(res.get('per_check_depth', {}).values()))}, "
              f"{res.get('runtime_s')} s)")
        return
    # A FAIL must be a counterexample (a failed assertion), not a timeout or an error;
    # equiv fails in the pre-pass and has no SBY task.
    fails = res.get("failures", {})
    failed = [c for c, v in res["checks"].items() if v == "FAIL" and
              (c == "equiv" or fails.get(c, {}).get("assertions"))]
    assert rc != 0, f"{mutant} passed formal:\n{text[-3000:]}"
    assert set(expect["checks"]) & set(failed), \
        f"{mutant}: expected a counterexample in {expect['checks']}, got {res['checks']}\n" \
        f"{fails}\n{text[-2000:]}"
    print(f"{mutant}: FAIL in {sorted(set(expect['checks']) & set(failed))}: "
          f"{ {c: fails.get(c, {}).get('assertions') for c in failed} }")


@pytest.mark.skipif(os.environ.get("BONETTO_SKIP_SEED") == "1", reason="BONETTO_SKIP_SEED=1")
def test_seed_passes(tmp_path, heavy_lock):
    out = tmp_path / "formal.json"
    rc, text = formal(["--out", str(out)])
    res = json.load(open(out))
    assert rc == 0 and res["pass"], f"{res.get('checks')}\n{res.get('failures')}\n{text[-3000:]}"
    # The fast config runs every check at the short depth, the deepest class group's.
    assert res["depth"] == res["depth_policy"]["d_short"] == max(res["per_check_depth"].values())
    assert all(v == "PASS" for v in res["checks"].values())
