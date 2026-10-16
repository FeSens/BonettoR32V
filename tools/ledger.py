#!/usr/bin/env python3
"""The loop's record: loop/ledger.jsonl (one JSON object per line) and loop/LEADERBOARD.md.

Commands:
  ledger.py add --id NNN --branch B --sha SHA --idea "..." --verdict {merged,rejected,failed}
                [--eval build/eval.json] [--notes "..."] [--parent-sha SHA]
      Appends one entry. Fields: id, ts, branch, sha, parent_sha, idea, verdict, area_um2,
      cells, delta_um2, gates, notes. area_um2, cells and gates come from --eval.
      parent_sha defaults to the current champion's sha; delta_um2 is area_um2 minus the
      champion's area_um2 (negative is smaller, which is better).
      A `merged` entry needs --eval with pass: true, a clean tree (dirty: false) and the same sha.
      An --eval whose sha differs from --sha is refused for every verdict.
  ledger.py board     regenerates loop/LEADERBOARD.md (champion history, last 20 hypotheses)
  ledger.py show      prints the current champion (the latest merged entry); --json for raw
  ledger.py next-id   prints the next sequential id

Lines that are blank or start with '#' are skipped.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(REPO, "loop", "ledger.jsonl")
BOARD = os.path.join(REPO, "loop", "LEADERBOARD.md")
VERDICTS = ("merged", "rejected", "failed")
FIELDS = ("id", "ts", "branch", "sha", "parent_sha", "idea", "verdict", "area_um2", "cells",
          "delta_um2", "gates", "notes")
RECENT = 20


class LedgerError(Exception):
    pass


def load(path):
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path) as f:
        for n, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            try:
                entries.append(json.loads(s))
            except ValueError as e:
                raise LedgerError(f"{path}:{n}: not valid JSON ({e})")
    return entries


def champion(entries):
    merged = [e for e in entries if e.get("verdict") == "merged"]
    return merged[-1] if merged else None


def next_id(entries):
    nums = [int(e["id"]) for e in entries if re.fullmatch(r"\d+", str(e.get("id", "")))]
    return f"{(max(nums) + 1) if nums else 0:03d}"


def resolve_sha(sha):
    """Full sha via git when the commit is known here, else the sha as given."""
    try:
        p = subprocess.run(["git", "-C", REPO, "rev-parse", "--verify", "--quiet",
                            sha + "^{commit}"], capture_output=True, text=True)
    except OSError:
        return sha
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else sha


def same_sha(a, b):
    a, b = (a or "").lower(), (b or "").lower()
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a)) and min(len(a), len(b)) >= 7


def make_entry(entries, args, ts=None):
    if not re.fullmatch(r"\d{3,}", args.id):
        raise LedgerError(f"id {args.id!r} must be a zero-padded number like 007")
    if any(e.get("id") == args.id for e in entries):
        raise LedgerError(f"id {args.id} already in the ledger")
    if args.verdict not in VERDICTS:
        raise LedgerError(f"verdict must be one of {', '.join(VERDICTS)}")
    sha = resolve_sha(args.sha)
    area = cells = None
    gates = {}
    if args.eval:
        with open(args.eval) as f:
            ev = json.load(f)
        if not same_sha(ev.get("sha"), sha):
            raise LedgerError(f"{args.eval} is for sha {ev.get('sha')}, not {sha}: re-run "
                              f"make eval on the exact sha")
        area, cells = ev.get("area_um2"), ev.get("cells")
        gates = {name: g.get("pass") for name, g in (ev.get("gates") or {}).items()}
        if args.verdict == "merged":
            if ev.get("pass") is not True:
                raise LedgerError(f"{args.eval} has pass: {ev.get('pass')}; a merged entry "
                                  f"needs every gate passing")
            if ev.get("dirty") is not False:
                raise LedgerError(f"{args.eval} has dirty: {ev.get('dirty')}; evaluate a clean "
                                  f"checkout of the sha")
            if not isinstance(area, (int, float)):
                raise LedgerError(f"{args.eval} has no area_um2")
    elif args.verdict == "merged":
        raise LedgerError("a merged entry needs --eval")
    champ = champion(entries)
    parent = args.parent_sha if args.parent_sha else (champ["sha"] if champ else None)
    delta = None
    if champ and isinstance(area, (int, float)) and isinstance(champ.get("area_um2"), (int, float)):
        delta = round(area - champ["area_um2"], 3)
    entry = {
        "id": args.id,
        "ts": ts or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "branch": args.branch,
        "sha": sha,
        "parent_sha": parent,
        "idea": args.idea,
        "verdict": args.verdict,
        "area_um2": area,
        "cells": cells,
        "delta_um2": delta,
        "gates": gates,
        "notes": args.notes or "",
    }
    return {k: entry[k] for k in FIELDS}


def fmt_num(v, nd=1):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def fmt_delta(v):
    return "-" if v is None else f"{v:+,.1f}"


def cell(text, limit=90):
    s = " ".join(str(text or "").split()).replace("|", "\\|")
    return s if len(s) <= limit else s[:limit - 3] + "..."


def failed_gates(e):
    bad = [n for n, ok in (e.get("gates") or {}).items() if ok is False]
    return ", ".join(bad) if bad else ("-" if e.get("gates") else "n/a")


def render_board(entries):
    out = ["# BonettoR32V leaderboard", "",
           "Generated by `python3 tools/ledger.py board` from `loop/ledger.jsonl`. Don't edit by "
           "hand.", "",
           "Score: `area_um2`, the sky130_fd_sc_hd cell area from `make area` (lower wins). "
           "Delta is against the champion at the time of the entry.", ""]
    champ = champion(entries)
    out.append("## Current champion")
    out.append("")
    if champ:
        out.append(f"**{champ['id']}** `{champ['sha'][:12]}` ({champ['branch']}): "
                   f"{fmt_num(champ['area_um2'])} µm², {fmt_num(champ['cells'])} cells. "
                   f"{cell(champ['idea'], 200)}")
    else:
        out.append("No champion yet.")
    out.append("")
    out.append("## Champion history")
    out.append("")
    merged = [e for e in entries if e.get("verdict") == "merged"]
    if merged:
        out.append("| # | id | date | sha | area µm² | cells | delta µm² | delta % | idea |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        prev = None
        for i, e in enumerate(merged):
            pct = "-"
            if prev is not None and isinstance(e.get("area_um2"), (int, float)) and prev:
                pct = f"{100.0 * (e['area_um2'] - prev) / prev:+.2f}%"
            out.append(f"| {i} | {e['id']} | {e['ts'][:10]} | `{e['sha'][:10]}` | "
                       f"{fmt_num(e.get('area_um2'))} | {fmt_num(e.get('cells'))} | "
                       f"{fmt_delta(e.get('delta_um2'))} | {pct} | {cell(e.get('idea'))} |")
            if isinstance(e.get("area_um2"), (int, float)):
                prev = e["area_um2"]
    else:
        out.append("No merged entries yet.")
    out.append("")
    out.append(f"## Last {RECENT} hypotheses")
    out.append("")
    recent = entries[-RECENT:][::-1]
    if recent:
        out.append("| id | date | branch | verdict | area µm² | delta µm² | failed gates | idea | "
                   "notes |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for e in recent:
            out.append(f"| {e['id']} | {e['ts'][:10]} | {cell(e.get('branch'), 40)} | "
                       f"{e['verdict']} | {fmt_num(e.get('area_um2'))} | "
                       f"{fmt_delta(e.get('delta_um2'))} | {failed_gates(e)} | "
                       f"{cell(e.get('idea'), 70)} | {cell(e.get('notes'), 70)} |")
    else:
        out.append("No entries yet.")
    out.append("")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="BonettoR32V loop ledger (see docs/eval.md)")
    ap.add_argument("--ledger", default=LEDGER, help=argparse.SUPPRESS)
    ap.add_argument("--board-path", default=BOARD, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="append one entry")
    a.add_argument("--id", required=True)
    a.add_argument("--branch", required=True)
    a.add_argument("--sha", required=True)
    a.add_argument("--idea", required=True)
    a.add_argument("--verdict", required=True, choices=VERDICTS)
    a.add_argument("--eval", help="build/eval.json of a `make eval` on exactly --sha")
    a.add_argument("--notes", default="")
    a.add_argument("--parent-sha", help="default: the current champion's sha")
    sub.add_parser("board", help="regenerate loop/LEADERBOARD.md")
    s = sub.add_parser("show", help="print the current champion")
    s.add_argument("--json", action="store_true")
    sub.add_parser("next-id", help="print the next sequential id")
    args = ap.parse_args(argv)

    try:
        entries = load(args.ledger)
        if args.cmd == "add":
            entry = make_entry(entries, args)
            os.makedirs(os.path.dirname(os.path.abspath(args.ledger)), exist_ok=True)
            with open(args.ledger, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            print(f"ledger: added {entry['id']} ({entry['verdict']}), area "
                  f"{fmt_num(entry['area_um2'])} µm², delta {fmt_delta(entry['delta_um2'])}")
            print("ledger: run `python3 tools/ledger.py board` and commit loop/")
        elif args.cmd == "board":
            with open(args.board_path, "w") as f:
                f.write(render_board(entries))
            print(f"ledger: wrote {os.path.relpath(args.board_path)} ({len(entries)} entries)")
        elif args.cmd == "show":
            champ = champion(entries)
            if args.json:
                print(json.dumps(champ, indent=2, ensure_ascii=False))
            elif champ is None:
                print("No champion yet (no merged entry in the ledger).")
            else:
                print(f"champion {champ['id']}: {champ['sha']} ({champ['branch']})")
                print(f"  area {fmt_num(champ['area_um2'])} um2, {fmt_num(champ['cells'])} cells, "
                      f"merged {champ['ts']}")
                print(f"  idea:  {champ['idea']}")
                if champ.get("notes"):
                    print(f"  notes: {champ['notes']}")
        elif args.cmd == "next-id":
            print(next_id(entries))
    except (LedgerError, OSError, ValueError) as e:
        print(f"ledger: ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
