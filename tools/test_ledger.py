"""Tests for tools/ledger.py (temporary ledger and leaderboard files)."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ledger  # noqa: E402

SEED = "a" * 40
HYP1 = "b" * 40
HYP2 = "c" * 40
HYP3 = "d" * 40


def eval_json(tmp_path, sha, area, cells, ok=True, dirty=False, name="eval.json", ips=1.0,
              formal="skipped", clock=25):
    """A v3 build/eval.json: formal is in `gates` only when it ran (eval.py --formal)."""
    names = ["lint", "area", "cosim", "arch", "equiv", "gl"] + ([] if formal == "skipped"
                                                               else ["formal"])
    gates = {g: {"pass": ok if g == "gl" else (formal == "pass") if g == "formal" else True,
                 "detail": ""} for g in names}
    doc = {"sha": sha, "dirty": dirty, "pass": ok, "gates": gates, "area_um2": area,
           "cells": cells, "coremark_ips": ips, "cpi": 90.0, "formal": formal,
           "clock_period_ns": clock, "timestamp": "2026-10-16T00:00:00Z"}
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return str(p)


@pytest.fixture
def paths(tmp_path):
    return str(tmp_path / "ledger.jsonl"), str(tmp_path / "LEADERBOARD.md")


def cli(paths, *args):
    led, board = paths
    return ledger.main(["--ledger", led, "--board-path", board, *args])


def entries(paths):
    return ledger.load(paths[0])


def add(paths, tmp_path, id_, sha, verdict, area=None, cells=None, ok=True, dirty=False,
        idea="idea", notes="", ips=1.0, formal="skipped", clock=25, force=False):
    args = ["add", "--id", id_, "--branch", f"hyp/{id_}-x", "--sha", sha, "--idea", idea,
            "--verdict", verdict, "--notes", notes] + (["--force-merge"] if force else [])
    if area is not None:
        args += ["--eval", eval_json(tmp_path, sha, area, cells, ok, dirty, f"eval{id_}.json",
                                     ips, formal, clock)]
    return cli(paths, *args)


def test_empty_ledger(paths, capsys):
    assert cli(paths, "show") == 0
    assert "No champion yet" in capsys.readouterr().out
    assert cli(paths, "next-id") == 0
    assert capsys.readouterr().out.strip() == "000"
    assert cli(paths, "board") == 0
    text = open(paths[1]).read()
    assert "No champion yet." in text and "No entries yet." in text


def test_comment_and_blank_lines_are_skipped(paths):
    with open(paths[0], "w") as f:
        f.write("# header\n\n")
    assert entries(paths) == []


def test_seed_then_hypotheses(paths, tmp_path, capsys):
    assert add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500, idea="seed core",
               ips=1.0) == 0
    assert add(paths, tmp_path, "001", HYP1, "rejected", 20100.0, 1510, idea="bigger",
               ips=0.75) == 0
    assert add(paths, tmp_path, "002", HYP2, "merged", 15000.5, 1200, idea="bit-serial",
               ips=1.5) == 0
    assert add(paths, tmp_path, "003", HYP3, "failed", idea="latch rf", notes="cosim hang") == 0
    e = entries(paths)
    assert [x["id"] for x in e] == ["000", "001", "002", "003"]
    assert list(e[0]) == list(ledger.FIELDS)
    assert e[0]["parent_sha"] is None and e[0]["delta_um2"] is None
    assert e[0]["delta_ips"] is None and e[0]["coremark_ips"] == 1.0
    assert e[1]["parent_sha"] == SEED and e[1]["delta_um2"] == 100.0
    assert e[1]["delta_ips"] == -0.25
    assert e[2]["parent_sha"] == SEED and e[2]["delta_um2"] == -4999.5
    assert e[2]["delta_ips"] == 0.5
    assert e[3]["parent_sha"] == HYP2 and e[3]["area_um2"] is None and e[3]["gates"] == {}
    assert e[3]["coremark_ips"] is None and e[3]["delta_ips"] is None and e[3]["formal"] is None
    assert e[2]["gates"]["gl"] is True
    capsys.readouterr()
    assert cli(paths, "show") == 0
    out = capsys.readouterr().out
    assert "champion 002" in out and HYP2 in out and "15,000.5" in out
    assert "CoreMark 1.5 iterations/s at 25 ns" in out
    assert e[2]["clock_period_ns"] == 25 and e[2]["forced"] is False
    assert e[3]["clock_period_ns"] is None
    assert cli(paths, "show", "--json") == 0
    assert json.loads(capsys.readouterr().out)["id"] == "002"
    assert cli(paths, "next-id") == 0
    assert capsys.readouterr().out.strip() == "004"


def test_board_contents(paths, tmp_path):
    add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500, idea="seed core", ips=1.0)
    add(paths, tmp_path, "001", HYP1, "rejected", 20100.0, 1510, idea="bigger | wider", ips=0.9)
    add(paths, tmp_path, "002", HYP2, "merged", 15000.0, 1200, idea="bit-serial", ips=1.25)
    assert cli(paths, "board") == 0
    text = open(paths[1]).read()
    assert "Score: `coremark_ips`" in text
    history = text.split("## Champion history")[1].split("## Last")[0]
    assert "| 0 | 000 |" in history and "| 1 | 002 |" in history and "001" not in history
    assert "| 1.2500 | +25.00% |" in history and "-5,000.0" in history
    assert "| skipped |" in history                       # formal column
    recent = text.split("## Last 20 hypotheses")[1]
    rows = [l for l in recent.splitlines() if l.startswith("| 0")]
    assert [r.split("|")[1].strip() for r in rows] == ["002", "001", "000"]
    assert "bigger \\| wider" in recent
    assert "| 0.9000 | -0.1000 |" in recent               # CoreMark/s and its delta
    assert "**002**" in text and "1.2500 CoreMark iterations/s" in text


def test_board_keeps_only_last_20(paths, tmp_path):
    add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500)
    for i in range(1, 25):
        assert add(paths, tmp_path, f"{i:03d}", f"{i:040x}", "failed") == 0
    cli(paths, "board")
    recent = open(paths[1]).read().split("## Last 20 hypotheses")[1]
    ids = [l.split("|")[1].strip() for l in recent.splitlines() if l.startswith("| 0")]
    assert len(ids) == 20 and ids[0] == "024" and ids[-1] == "005"


@pytest.mark.parametrize("kw,msg", [
    ({"ok": False}, "pass: False"),
    ({"dirty": True}, "dirty: True"),
])
def test_merged_needs_clean_passing_eval(paths, tmp_path, capsys, kw, msg):
    assert add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500, **kw) == 1
    assert msg in capsys.readouterr().err
    assert entries(paths) == []


def test_merged_needs_eval(paths, tmp_path, capsys):
    assert add(paths, tmp_path, "000", SEED, "merged") == 1
    assert "needs --eval" in capsys.readouterr().err


def test_failing_eval_is_fine_for_rejected(paths, tmp_path):
    assert add(paths, tmp_path, "000", SEED, "rejected", 20000.0, 1500, ok=False) == 0
    assert entries(paths)[0]["gates"]["gl"] is False


def test_eval_sha_mismatch_refused(paths, tmp_path, capsys):
    ev = eval_json(tmp_path, HYP1, 100.0, 10)
    rc = cli(paths, "add", "--id", "000", "--branch", "b", "--sha", SEED, "--idea", "i",
             "--verdict", "rejected", "--eval", ev)
    assert rc == 1
    assert "re-run make eval on the exact sha" in capsys.readouterr().err


def test_short_sha_matches(paths, tmp_path):
    ev = eval_json(tmp_path, SEED, 100.0, 10)
    rc = cli(paths, "add", "--id", "000", "--branch", "b", "--sha", SEED[:10], "--idea", "i",
             "--verdict", "merged", "--eval", ev)
    assert rc == 0


def test_duplicate_and_bad_ids_refused(paths, tmp_path, capsys):
    assert add(paths, tmp_path, "000", SEED, "failed") == 0
    assert add(paths, tmp_path, "000", HYP1, "failed") == 1
    assert "already in the ledger" in capsys.readouterr().err
    assert add(paths, tmp_path, "7", HYP1, "failed") == 1
    assert "zero-padded" in capsys.readouterr().err


def test_explicit_parent_sha(paths, tmp_path):
    add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500)
    rc = cli(paths, "add", "--id", "001", "--branch", "b", "--sha", HYP1, "--idea", "i",
             "--verdict", "failed", "--parent-sha", HYP3)
    assert rc == 0
    assert entries(paths)[1]["parent_sha"] == HYP3


def test_optional_fields_default_null_and_can_be_set(paths, tmp_path):
    add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500)
    rc = cli(paths, "add", "--id", "001", "--branch", "b", "--sha", HYP1, "--idea", "i",
             "--verdict", "merged", "--eval", eval_json(tmp_path, HYP1, 19000.0, 1400),
             "--tile-fraction", "0.42", "--formal-deep", "pass")
    assert rc == 0
    e = entries(paths)
    assert e[0]["tile_fraction"] is None and e[0]["formal_deep"] is None
    assert e[1]["tile_fraction"] == 0.42 and e[1]["formal_deep"] == "pass"


@pytest.mark.parametrize("frac", ["0", "1.5", "-0.1"])
def test_bad_tile_fraction_refused(paths, tmp_path, capsys, frac):
    rc = cli(paths, "add", "--id", "000", "--branch", "b", "--sha", SEED, "--idea", "i",
             "--verdict", "failed", "--tile-fraction", frac)
    assert rc == 1 and "tile_fraction" in capsys.readouterr().err


def test_rolled_back_restores_previous_champion(paths, tmp_path, capsys):
    add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500, idea="seed", ips=1.0)
    add(paths, tmp_path, "001", HYP1, "merged", 15000.0, 1200, idea="bit-serial", ips=2.0)
    assert ledger.champion(entries(paths))["id"] == "001"
    rc = cli(paths, "annotate", "--id", "001", "--formal-deep", "fail",
             "--notes-append", "deep formal: reg check cex")
    assert rc == 0
    rc = cli(paths, "add", "--id", "002", "--branch", "hyp/001-bit-serial", "--sha", HYP1,
             "--idea", "roll back 001", "--verdict", "rolled_back", "--formal-deep", "fail")
    assert rc == 0
    e = entries(paths)
    assert e[1]["formal_deep"] == "fail" and "reg check cex" in e[1]["notes"]
    assert e[2]["verdict"] == "rolled_back" and e[2]["parent_sha"] == SEED
    assert e[2]["area_um2"] == 15000.0 and e[2]["delta_um2"] is None
    assert e[2]["coremark_ips"] == 2.0 and e[2]["delta_ips"] is None
    capsys.readouterr()
    assert cli(paths, "show") == 0
    assert "champion 000" in capsys.readouterr().out
    # The next hypothesis is measured against the restored champion.
    add(paths, tmp_path, "003", HYP2, "rejected", 19000.0, 1450, ips=1.5)
    assert entries(paths)[3]["parent_sha"] == SEED and entries(paths)[3]["delta_um2"] == -1000.0
    assert entries(paths)[3]["delta_ips"] == 0.5
    cli(paths, "board")
    text = open(paths[1]).read()
    assert "rolled back by 002" in text and "| champion |" in text and "**000**" in text


def test_rolled_back_must_name_current_champion(paths, tmp_path, capsys):
    add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500, ips=1.0)
    add(paths, tmp_path, "001", HYP1, "merged", 15000.0, 1200, ips=2.0)
    rc = cli(paths, "add", "--id", "002", "--branch", "b", "--sha", SEED, "--idea", "i",
             "--verdict", "rolled_back")
    assert rc == 1 and "current champion" in capsys.readouterr().err


def test_annotate_tile_fraction_shows_on_board(paths, tmp_path, capsys):
    add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500)
    assert cli(paths, "annotate", "--id", "000", "--tile-fraction", "0.375") == 0
    assert entries(paths)[0]["tile_fraction"] == 0.375
    cli(paths, "board")
    text = open(paths[1]).read()
    assert "tile fraction 0.375" in text
    history = text.split("## Champion history")[1].split("## Last")[0]
    assert "| tile fraction |" in history and "| 0.375 |" in history
    capsys.readouterr()
    cli(paths, "show")
    assert "tile fraction 0.375" in capsys.readouterr().out


def test_annotate_preserves_comments_and_blank_lines(paths, tmp_path):
    add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500)
    add(paths, tmp_path, "001", HYP1, "rejected", 21000.0, 1600)
    with open(paths[0]) as f:
        lines = f.readlines()
    text = "# BonettoR32V ledger: one JSON object per line\n\n" + lines[0] + \
           "# round 1\n" + "\n" + lines[1]
    with open(paths[0], "w") as f:
        f.write(text)
    assert cli(paths, "annotate", "--id", "001", "--notes-append", "area grew") == 0
    after = open(paths[0]).read().split("\n")
    before = text.split("\n")
    assert len(after) == len(before)
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert changed == [5]                                  # only entry 001's line
    assert after[0] == "# BonettoR32V ledger: one JSON object per line"
    assert after[1] == "" and after[3] == "# round 1" and after[4] == ""
    assert after[2] == before[2]                           # entry 000 byte for byte
    assert '"notes": "area grew"' in after[5]


def test_annotate_errors(paths, tmp_path, capsys):
    add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500)
    assert cli(paths, "annotate", "--id", "009", "--tile-fraction", "0.5") == 1
    assert "no entry with id 009" in capsys.readouterr().err
    assert cli(paths, "annotate", "--id", "000") == 1
    assert "annotate needs" in capsys.readouterr().err


def test_repo_ledger_is_valid():
    path = os.path.join(os.path.dirname(HERE), "loop", "ledger.jsonl")
    assert os.path.exists(path)
    for e in ledger.load(path):
        assert list(e) in (list(ledger.FIELDS), list(ledger.LEGACY_FIELDS))
        assert e["verdict"] in ledger.VERDICTS


def test_repo_board_is_regenerated():
    led = os.path.join(os.path.dirname(HERE), "loop", "ledger.jsonl")
    board = os.path.join(os.path.dirname(HERE), "loop", "LEADERBOARD.md")
    with open(board) as f:
        assert f.read() == ledger.render_board(ledger.load(led)), \
            "loop/LEADERBOARD.md is stale: run python3 tools/ledger.py board"


# --- v3: the score is coremark_ips -------------------------------------------------------------

@pytest.mark.parametrize("formal", ["skipped", "pass"])
def test_merged_with_formal_skipped(paths, tmp_path, capsys, formal):
    assert add(paths, tmp_path, "000", SEED, "merged", 11000.0, 900, ips=0.8,
               formal=formal) == 0
    e = entries(paths)[0]
    assert e["verdict"] == "merged" and e["formal"] == formal and e["coremark_ips"] == 0.8
    assert ("formal" in e["gates"]) is (formal == "pass")
    capsys.readouterr()
    assert cli(paths, "show") == 0
    assert f"formal {formal}" in capsys.readouterr().out
    cli(paths, "board")
    assert f"formal {formal}" in open(paths[1]).read()


@pytest.mark.parametrize("kw,msg", [
    ({"formal": "fail"}, "formal: fail"),
    ({"ips": None}, "no coremark_ips"),
    ({"ips": 0}, "no coremark_ips"),
    ({"ips": "fast"}, "no coremark_ips"),
])
def test_merged_needs_score_and_no_failed_formal(paths, tmp_path, capsys, kw, msg):
    assert add(paths, tmp_path, "000", SEED, "merged", 11000.0, 900, **kw) == 1
    assert msg in capsys.readouterr().err
    assert entries(paths) == []


def test_bad_formal_value_refused(paths, tmp_path, capsys):
    assert add(paths, tmp_path, "000", SEED, "rejected", 11000.0, 900, formal="maybe") == 1
    assert "formal" in capsys.readouterr().err


def v3_line(id_, sha, ips, area, forced=False, verdict="merged", idea="idea"):
    """A v3 ledger line written by hand (add refuses merges that would not become champion)."""
    e = {k: None for k in ledger.FIELDS}
    e.update({"id": id_, "ts": "2026-10-17T12:00:00Z", "branch": f"hyp/{id_}-x", "sha": sha,
              "idea": idea, "verdict": verdict, "coremark_ips": ips, "clock_period_ns": 25,
              "area_um2": area, "cells": 900, "gates": {}, "formal": "skipped",
              "forced": forced, "notes": ""})
    return json.dumps(e) + "\n"


def test_champion_is_max_ips(paths, tmp_path, capsys):
    with open(paths[0], "w") as f:
        f.write(v3_line("100", SEED, 1.0, 11000.0, idea="mcu seed")
                + v3_line("101", HYP1, 3.0, 11400.0, idea="quad fetch")
                + v3_line("102", HYP2, 2.0, 10000.0, idea="smaller, slower"))
    assert ledger.champion(entries(paths))["id"] == "101"  # highest score, not the latest
    # Tiebreak (contract v3 section 7): the smaller synthesized area.
    with open(paths[0], "a") as f:
        f.write(v3_line("103", HYP3, 3.0, 11500.0, idea="same, bigger"))
    assert ledger.champion(entries(paths))["id"] == "101"
    assert add(paths, tmp_path, "104", "e" * 40, "merged", 11300.0, 940, ips=3.0,
               idea="same, smaller") == 0
    assert ledger.champion(entries(paths))["id"] == "104"
    capsys.readouterr()
    assert cli(paths, "show") == 0
    assert "champion 104" in capsys.readouterr().out
    cli(paths, "board")
    text = open(paths[1]).read()
    assert "**104**" in text
    history = text.split("## Champion history")[1].split("## Last")[0]
    rows = {r.split("|")[2].strip(): r for r in history.splitlines() if r.startswith("| ")
            and r.split("|")[2].strip().isdigit()}
    assert rows["104"].rstrip().endswith("| champion | same, smaller |")
    assert "| champion |" not in rows["101"]
    # Rolling back the champion restores the best remaining entry, not the latest one.
    assert cli(paths, "add", "--id", "105", "--branch", "b", "--sha", "e" * 40, "--idea", "r",
               "--verdict", "rolled_back") == 0
    assert ledger.champion(entries(paths))["id"] == "101"


def legacy_line(id_, sha, verdict, area):
    """A v2 (area-scored) ledger line: no coremark_ips, delta_ips or formal."""
    e = {"id": id_, "ts": "2026-10-16T12:00:00Z", "branch": f"hyp/{id_}-v2", "sha": sha,
         "parent_sha": None, "idea": "v2 idea", "verdict": verdict, "area_um2": area,
         "cells": 500, "delta_um2": None, "gates": {"formal": True, "gl": True}, "notes": "",
         "tile_fraction": None, "formal_deep": None}
    assert list(e) == list(ledger.LEGACY_FIELDS)
    return json.dumps(e) + "\n"


def test_legacy_area_only_entries_still_read(paths, tmp_path, capsys):
    with open(paths[0], "w") as f:
        f.write("# v2 history\n" + legacy_line("004", SEED, "merged", 5000.0)
                + legacy_line("005", HYP1, "merged", 4780.0)
                + legacy_line("006", HYP2, "rejected", 4900.0))
    e = entries(paths)
    assert ledger.champion(e)["id"] == "005"             # no scores: the latest merged entry
    for cmd in (["show"], ["show", "--json"], ["board"], ["next-id"]):
        assert cli(paths, *cmd) == 0
    assert "champion 005" in capsys.readouterr().out
    text = open(paths[1]).read()
    assert "**005**" in text and "4,780.0" in text
    # The first scored merge takes over from every unscored (v2) entry.
    assert add(paths, tmp_path, "100", HYP3, "merged", 11000.0, 900, ips=0.7) == 0
    e = entries(paths)
    assert ledger.champion(e)["id"] == "100"
    assert e[-1]["parent_sha"] == HYP1 and e[-1]["delta_ips"] is None
    assert e[-1]["delta_um2"] == 6220.0
    # Annotating a v2 entry rewrites it in the v3 field order, new fields null.
    assert cli(paths, "annotate", "--id", "005", "--tile-fraction", "0.3") == 0
    e5 = next(x for x in entries(paths) if x["id"] == "005")
    assert list(e5) == list(ledger.FIELDS) and e5["coremark_ips"] is None
    assert e5["tile_fraction"] == 0.3 and e5["area_um2"] == 4780.0


# --- review fix round 1: a merge must become the champion; full-precision show; clock ------------

@pytest.mark.parametrize("ips,area,ok", [
    (1.9, 10000.0, False),               # slower, even though smaller
    (2.0, 11001.0, False),               # tie, bigger area
    (2.0, 11000.0, False),               # tie, same area: not smaller
    (2.0, 10999.0, True),                # tie, smaller area: the contract's tiebreak
    (2.1, 11400.0, True),                # faster, even though bigger
], ids=["slower", "tie-bigger", "tie-same-area", "tie-smaller", "faster"])
def test_merged_must_become_champion(paths, tmp_path, capsys, ips, area, ok):
    assert add(paths, tmp_path, "100", SEED, "merged", 11000.0, 900, ips=2.0) == 0
    capsys.readouterr()
    assert add(paths, tmp_path, "101", HYP1, "merged", area, 900, ips=ips) == (0 if ok else 1)
    if ok:
        assert ledger.champion(entries(paths))["id"] == "101"
        assert entries(paths)[1]["forced"] is False
    else:
        err = capsys.readouterr().err
        assert "would not become the champion" in err and "--force-merge" in err
        assert [e["id"] for e in entries(paths)] == ["100"]
    # Non-merged verdicts are never ranked.
    assert add(paths, tmp_path, "102", HYP2, "rejected", 12000.0, 999, ips=0.5) == 0


def test_force_merge_is_recorded_and_takes_over(paths, tmp_path, capsys):
    assert add(paths, tmp_path, "100", SEED, "merged", 11000.0, 900, ips=2.0) == 0
    assert add(paths, tmp_path, "101", HYP1, "merged", 11000.0, 900, ips=1.5, force=True,
               idea="timing repair costs speed") == 0
    e = entries(paths)
    assert e[1]["forced"] is True and e[1]["delta_ips"] == -0.5
    # The forced merge is what main now holds, so it is the champion and the next bar.
    assert ledger.champion(e)["id"] == "101"
    assert add(paths, tmp_path, "102", HYP2, "merged", 11000.0, 900, ips=1.75) == 0
    assert ledger.champion(entries(paths))["id"] == "102"
    assert entries(paths)[2]["parent_sha"] == HYP1
    capsys.readouterr()
    cli(paths, "board")
    text = open(paths[1]).read()
    history = text.split("## Champion history")[1].split("## Last")[0]
    assert "| - (forced) |" in history
    recent = text.split("## Last 20 hypotheses")[1]
    assert "| merged (forced) |" in recent


def test_force_merge_rollback_restores_the_earlier_best(paths, tmp_path):
    add(paths, tmp_path, "100", SEED, "merged", 11000.0, 900, ips=2.0)
    add(paths, tmp_path, "101", HYP1, "merged", 11000.0, 900, ips=1.5, force=True)
    assert cli(paths, "add", "--id", "102", "--branch", "b", "--sha", HYP1, "--idea", "r",
               "--verdict", "rolled_back") == 0
    assert ledger.champion(entries(paths))["id"] == "100"


def test_force_merge_only_with_merged(paths, tmp_path, capsys):
    assert add(paths, tmp_path, "100", SEED, "rejected", 11000.0, 900, force=True) == 1
    assert "--force-merge" in capsys.readouterr().err
    assert entries(paths) == []


def test_force_merge_does_not_skip_the_other_checks(paths, tmp_path, capsys):
    assert add(paths, tmp_path, "100", SEED, "merged", 11000.0, 900, ips=2.0) == 0
    assert add(paths, tmp_path, "101", HYP1, "merged", 11000.0, 900, ips=1.0, ok=False,
               force=True) == 1
    assert "pass: False" in capsys.readouterr().err


def test_show_prints_the_full_precision_score(paths, tmp_path, capsys):
    ips = 2 / 3
    assert add(paths, tmp_path, "100", SEED, "merged", 11000.0, 900, ips=ips) == 0
    capsys.readouterr()
    assert cli(paths, "show") == 0
    assert f"CoreMark {ips!r} iterations/s" in capsys.readouterr().out
    assert cli(paths, "show", "--json") == 0
    assert json.loads(capsys.readouterr().out)["coremark_ips"] == ips     # exact, for MIN_IPS


def test_clock_period_is_stored_and_shown(paths, tmp_path, capsys):
    assert add(paths, tmp_path, "100", SEED, "merged", 11000.0, 900, ips=1.0, clock=25) == 0
    assert add(paths, tmp_path, "101", HYP1, "merged", 11000.0, 900, ips=1.2, clock=20) == 0
    e = entries(paths)
    assert (e[0]["clock_period_ns"], e[1]["clock_period_ns"]) == (25, 20)
    cli(paths, "board")
    text = open(paths[1]).read()
    assert "clock 20 ns" in text                                  # the champion line
    history = text.split("## Champion history")[1].split("## Last")[0]
    assert "| clock ns |" in history and "| 25 |" in history and "| 20 |" in history
    assert "| clock ns |" in text.split("## Last 20 hypotheses")[1]
    # A rollback without --eval copies the champion's clock.
    assert cli(paths, "add", "--id", "102", "--branch", "b", "--sha", HYP1, "--idea", "r",
               "--verdict", "rolled_back") == 0
    assert entries(paths)[2]["clock_period_ns"] == 20


@pytest.mark.parametrize("clock", [None, 0, "25"])
def test_merged_needs_a_clock_period(paths, tmp_path, capsys, clock):
    assert add(paths, tmp_path, "100", SEED, "merged", 11000.0, 900, ips=1.0, clock=clock) == 1
    assert "clock_period_ns" in capsys.readouterr().err
