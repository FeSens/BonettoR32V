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


def eval_json(tmp_path, sha, area, cells, ok=True, dirty=False, name="eval.json"):
    gates = {g: {"pass": ok if g == "gl" else True, "detail": ""}
             for g in ("lint", "cosim", "equiv", "formal", "arch", "area", "gl")}
    doc = {"sha": sha, "dirty": dirty, "pass": ok, "gates": gates, "area_um2": area,
           "cells": cells, "cpi": 90.0, "timestamp": "2026-10-16T00:00:00Z"}
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
        idea="idea", notes=""):
    args = ["add", "--id", id_, "--branch", f"hyp/{id_}-x", "--sha", sha, "--idea", idea,
            "--verdict", verdict, "--notes", notes]
    if area is not None:
        args += ["--eval", eval_json(tmp_path, sha, area, cells, ok, dirty, f"eval{id_}.json")]
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
    assert add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500, idea="seed core") == 0
    assert add(paths, tmp_path, "001", HYP1, "rejected", 20100.0, 1510, idea="bigger") == 0
    assert add(paths, tmp_path, "002", HYP2, "merged", 15000.5, 1200, idea="bit-serial") == 0
    assert add(paths, tmp_path, "003", HYP3, "failed", idea="latch rf", notes="cosim hang") == 0
    e = entries(paths)
    assert [x["id"] for x in e] == ["000", "001", "002", "003"]
    assert list(e[0]) == list(ledger.FIELDS)
    assert e[0]["parent_sha"] is None and e[0]["delta_um2"] is None
    assert e[1]["parent_sha"] == SEED and e[1]["delta_um2"] == 100.0
    assert e[2]["parent_sha"] == SEED and e[2]["delta_um2"] == -4999.5
    assert e[3]["parent_sha"] == HYP2 and e[3]["area_um2"] is None and e[3]["gates"] == {}
    assert e[2]["gates"]["gl"] is True
    capsys.readouterr()
    assert cli(paths, "show") == 0
    out = capsys.readouterr().out
    assert "champion 002" in out and HYP2 in out and "15,000.5" in out
    assert cli(paths, "show", "--json") == 0
    assert json.loads(capsys.readouterr().out)["id"] == "002"
    assert cli(paths, "next-id") == 0
    assert capsys.readouterr().out.strip() == "004"


def test_board_contents(paths, tmp_path):
    add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500, idea="seed core")
    add(paths, tmp_path, "001", HYP1, "rejected", 20100.0, 1510, idea="bigger | wider")
    add(paths, tmp_path, "002", HYP2, "merged", 15000.0, 1200, idea="bit-serial")
    assert cli(paths, "board") == 0
    text = open(paths[1]).read()
    history = text.split("## Champion history")[1].split("## Last")[0]
    assert "| 0 | 000 |" in history and "| 1 | 002 |" in history and "001" not in history
    assert "-25.00%" in history and "-5,000.0" in history
    recent = text.split("## Last 20 hypotheses")[1]
    rows = [l for l in recent.splitlines() if l.startswith("| 0")]
    assert [r.split("|")[1].strip() for r in rows] == ["002", "001", "000"]
    assert "bigger \\| wider" in recent
    assert "**002**" in text


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
    add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500, idea="seed")
    add(paths, tmp_path, "001", HYP1, "merged", 15000.0, 1200, idea="bit-serial")
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
    capsys.readouterr()
    assert cli(paths, "show") == 0
    assert "champion 000" in capsys.readouterr().out
    # The next hypothesis is measured against the restored champion.
    add(paths, tmp_path, "003", HYP2, "rejected", 19000.0, 1450)
    assert entries(paths)[3]["parent_sha"] == SEED and entries(paths)[3]["delta_um2"] == -1000.0
    cli(paths, "board")
    text = open(paths[1]).read()
    assert "rolled back by 002" in text and "| champion |" in text and "**000**" in text


def test_rolled_back_must_name_current_champion(paths, tmp_path, capsys):
    add(paths, tmp_path, "000", SEED, "merged", 20000.0, 1500)
    add(paths, tmp_path, "001", HYP1, "merged", 15000.0, 1200)
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
        assert list(e) == list(ledger.FIELDS)
        assert e["verdict"] in ledger.VERDICTS


def test_repo_board_is_regenerated():
    led = os.path.join(os.path.dirname(HERE), "loop", "ledger.jsonl")
    board = os.path.join(os.path.dirname(HERE), "loop", "LEADERBOARD.md")
    with open(board) as f:
        assert f.read() == ledger.render_board(ledger.load(led)), \
            "loop/LEADERBOARD.md is stale: run python3 tools/ledger.py board"
