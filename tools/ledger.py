#!/usr/bin/env python3
"""The loop's record: loop/ledger.jsonl (one JSON object per line) and loop/LEADERBOARD.md.

Commands:
  ledger.py add --id NNN --branch B --sha SHA --idea "..."
                --verdict {merged,rejected,failed,rolled_back}
                [--eval build/eval.json] [--notes "..."] [--parent-sha SHA]
                [--tile-fraction F] [--formal-deep pass|fail] [--force-merge]
      Appends one entry. Fields: id, ts, branch, sha, parent_sha, idea, verdict, coremark_ips,
      delta_ips, clock_period_ns, area_um2, cells, delta_um2, gates, formal, forced, notes,
      tile_fraction, formal_deep. coremark_ips, clock_period_ns, area_um2, cells, gates and
      formal ("pass", "fail" or "skipped") come from --eval. parent_sha defaults to the current
      champion's sha; delta_ips is coremark_ips minus the champion's (positive is faster, which
      is better) and delta_um2 is area_um2 minus the champion's (negative is smaller).
      tile_fraction (from `make tilefit`, 0 < F <= 1) and formal_deep (pass, fail) are optional,
      null by default.
      A `merged` entry needs --eval with pass: true, a clean tree (dirty: false), the same sha,
      a coremark_ips, a clock_period_ns and an area_um2; formal may be "skipped" (contract v3
      section 7) but not "fail". It must also become the champion: faster than the current
      (scored) champion, or as fast and smaller in area_um2 (the contract's tiebreak).
      --force-merge (the lead's override) skips only that ranking check and is recorded as
      "forced": true; a forced merge becomes the champion. An --eval whose sha differs from
      --sha is refused for every verdict.
      A `rolled_back` entry names the current champion's sha (for example after formal-deep
      fails); the champion becomes the best remaining standing merged entry. Without --eval it
      copies the champion's coremark_ips, formal, area_um2 and cells.
  ledger.py annotate --id NNN [--tile-fraction F] [--formal-deep pass|fail] [--notes-append T]
      Records champion-round results on an existing entry (rewrites that one line).
  ledger.py board     regenerates loop/LEADERBOARD.md (champion history, last 20 hypotheses)
  ledger.py show      prints the current champion; --json for raw
  ledger.py next-id   prints the next sequential id

Champion: among the merged entries that no later rolled_back entry names, the one with the
highest coremark_ips (contract v3 section 7); ties go to the smaller area_um2, then the later
entry. A standing forced merge resets the race: only it and later entries compete (it is what
main holds). Entries from before v3 have no coremark_ips: while no competing entry has one, the
latest is the champion (the v2 rule), and any scored entry outranks them.
Lines that are blank or start with '#' are skipped. Entries written before v3 (LEGACY_FIELDS)
read as they are; `annotate` rewrites one in the v3 field order with the new fields null.
"""
import argparse
import datetime
import json
import math
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(REPO, "loop", "ledger.jsonl")
BOARD = os.path.join(REPO, "loop", "LEADERBOARD.md")
VERDICTS = ("merged", "rejected", "failed", "rolled_back")
FORMAL_DEEP = ("pass", "fail")
FORMAL = ("pass", "fail", "skipped")    # eval.json "formal" (formal runs only with --formal)
FIELDS = ("id", "ts", "branch", "sha", "parent_sha", "idea", "verdict", "coremark_ips",
          "delta_ips", "clock_period_ns", "area_um2", "cells", "delta_um2", "gates", "formal",
          "forced", "notes", "tile_fraction", "formal_deep")
LEGACY_FIELDS = ("id", "ts", "branch", "sha", "parent_sha", "idea", "verdict", "area_um2",
                 "cells", "delta_um2", "gates", "notes", "tile_fraction", "formal_deep")   # v2
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


def replay(entries):
    """Apply merged and rolled_back entries in order.

    Returns (standing, rolled): the merged entries still standing (oldest first) and
    {id of a rolled-back merged entry: id of the rolled_back entry that names it}.
    """
    standing, rolled = [], {}
    for e in entries:
        if e.get("verdict") == "merged":
            standing.append(e)
        elif e.get("verdict") == "rolled_back":
            for i in range(len(standing) - 1, -1, -1):
                if same_sha(standing[i].get("sha"), e.get("sha")):
                    rolled[standing[i]["id"]] = e["id"]
                    del standing[i]
                    break
    return standing, rolled


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def champion(entries):
    standing, _ = replay(entries)
    forced = [i for i, e in enumerate(standing) if e.get("forced") is True]
    if forced:                      # a forced merge is what main holds: the race restarts there
        standing = standing[forced[-1]:]
    scored = [(i, e) for i, e in enumerate(standing) if is_num(e.get("coremark_ips"))]
    if not scored:
        return standing[-1] if standing else None

    def rank(item):
        i, e = item
        area = e.get("area_um2")
        return e["coremark_ips"], -area if is_num(area) else -math.inf, i

    return max(scored, key=rank)[1]


def check_tile_fraction(value):
    if value is not None and not (0 < value <= 1):
        raise LedgerError(f"tile_fraction {value} must be in (0, 1]")
    return value


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


def becomes_champion(champ, ips, area):
    """Would a merged entry with this score and area outrank `champ` (contract v3 section 7)?"""
    if champ is None or not is_num(champ.get("coremark_ips")):
        return True                 # no champion yet, or only unscored (v2) entries
    if ips != champ["coremark_ips"]:
        return ips > champ["coremark_ips"]
    return not is_num(champ.get("area_um2")) or area < champ["area_um2"]


def make_entry(entries, args, ts=None):
    if not re.fullmatch(r"\d{3,}", args.id):
        raise LedgerError(f"id {args.id!r} must be a zero-padded number like 007")
    if any(e.get("id") == args.id for e in entries):
        raise LedgerError(f"id {args.id} already in the ledger")
    if args.verdict not in VERDICTS:
        raise LedgerError(f"verdict must be one of {', '.join(VERDICTS)}")
    force = bool(getattr(args, "force_merge", False))
    if force and args.verdict != "merged":
        raise LedgerError("--force-merge applies only to --verdict merged")
    sha = resolve_sha(args.sha)
    area = cells = ips = formal = clock = None
    gates = {}
    if args.eval:
        with open(args.eval) as f:
            ev = json.load(f)
        if not same_sha(ev.get("sha"), sha):
            raise LedgerError(f"{args.eval} is for sha {ev.get('sha')}, not {sha}: re-run "
                              f"make eval on the exact sha")
        area, cells = ev.get("area_um2"), ev.get("cells")
        ips, formal, clock = ev.get("coremark_ips"), ev.get("formal"), ev.get("clock_period_ns")
        gates = {name: g.get("pass") for name, g in (ev.get("gates") or {}).items()}
        if formal is not None and formal not in FORMAL:
            raise LedgerError(f"{args.eval} has formal: {formal!r}; expected one of "
                              f"{', '.join(FORMAL)}")
        if args.verdict == "merged":
            if ev.get("pass") is not True:
                raise LedgerError(f"{args.eval} has pass: {ev.get('pass')}; a merged entry "
                                  f"needs every gate passing")
            if ev.get("dirty") is not False:
                raise LedgerError(f"{args.eval} has dirty: {ev.get('dirty')}; evaluate a clean "
                                  f"checkout of the sha")
            if formal == "fail":
                raise LedgerError(f"{args.eval} has formal: fail; a merged entry may skip "
                                  f"formal, never fail it")
            if not (is_num(ips) and ips > 0):
                raise LedgerError(f"{args.eval} has no coremark_ips (got {ips!r}); a merged "
                                  f"entry needs the CoreMark score (contract v3 section 7)")
            if not (is_num(clock) and clock > 0):
                raise LedgerError(f"{args.eval} has no clock_period_ns (got {clock!r}); the "
                                  f"score needs the clock it was measured at")
            if not is_num(area):
                raise LedgerError(f"{args.eval} has no area_um2")
    elif args.verdict == "merged":
        raise LedgerError("a merged entry needs --eval")
    champ = champion(entries)
    if args.verdict == "merged" and not force and not becomes_champion(champ, ips, area):
        raise LedgerError(
            f"{args.eval}: coremark_ips {ips!r}, area_um2 {area} would not become the champion "
            f"({champ['id']}: coremark_ips {champ['coremark_ips']!r}, area_um2 "
            f"{champ.get('area_um2')}); a merged entry must be faster, or as fast and smaller "
            f"(contract v3 section 7). The lead may pass --force-merge (recorded as forced).")
    delta = delta_ips = None
    if args.verdict == "rolled_back":
        if not champ or not same_sha(champ["sha"], sha):
            raise LedgerError(f"rolled_back must name the current champion's sha "
                              f"({champ['sha'] if champ else 'none'}), not {sha}")
        parent = args.parent_sha or champ.get("parent_sha")
        if not args.eval:
            area, cells = champ.get("area_um2"), champ.get("cells")
            ips, formal = champ.get("coremark_ips"), champ.get("formal")
            clock = champ.get("clock_period_ns")
    else:
        parent = args.parent_sha if args.parent_sha else (champ["sha"] if champ else None)
        if champ and is_num(area) and is_num(champ.get("area_um2")):
            delta = round(area - champ["area_um2"], 3)
        if champ and is_num(ips) and is_num(champ.get("coremark_ips")):
            delta_ips = round(ips - champ["coremark_ips"], 6)
    entry = {
        "id": args.id,
        "ts": ts or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "branch": args.branch,
        "sha": sha,
        "parent_sha": parent,
        "idea": args.idea,
        "verdict": args.verdict,
        "coremark_ips": ips,
        "delta_ips": delta_ips,
        "clock_period_ns": clock,
        "area_um2": area,
        "cells": cells,
        "delta_um2": delta,
        "gates": gates,
        "formal": formal,
        "forced": force,
        "notes": args.notes or "",
        "tile_fraction": check_tile_fraction(args.tile_fraction),
        "formal_deep": args.formal_deep,
    }
    return {k: entry[k] for k in FIELDS}


def annotate(entries, args):
    """Return the entries with champion-round results recorded on entry args.id."""
    hits = [e for e in entries if e.get("id") == args.id]
    if not hits:
        raise LedgerError(f"no entry with id {args.id}")
    if args.tile_fraction is None and args.formal_deep is None and not args.notes_append:
        raise LedgerError("annotate needs --tile-fraction, --formal-deep or --notes-append")
    # The v3 field order, new fields null on a v2 entry; any unknown keys are kept after them.
    old = hits[0]
    e = {**{k: old.get(k) for k in FIELDS}, **{k: v for k, v in old.items() if k not in FIELDS}}
    if args.tile_fraction is not None:
        e["tile_fraction"] = check_tile_fraction(args.tile_fraction)
    if args.formal_deep is not None:
        e["formal_deep"] = args.formal_deep
    if args.notes_append:
        e["notes"] = (e.get("notes") + "; " if e.get("notes") else "") + args.notes_append
    return [e if x is old else x for x in entries]


def fmt_tile(v):
    return "-" if v is None else f"{v:.3f}"


def fmt_num(v, nd=1):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def fmt_delta(v):
    return "-" if v is None else f"{v:+,.1f}"


def fmt_ips(v):
    return f"{v:,.4f}" if is_num(v) else "-"


def fmt_delta_ips(v):
    return f"{v:+,.4f}" if is_num(v) else "-"


def fmt_ips_full(v):
    """The exact stored score (shortest round-trip repr), e.g. for MIN_IPS."""
    return repr(v) if is_num(v) else "-"


def fmt_clock(v):
    return "-" if v is None else f"{v:g}" if is_num(v) else str(v)


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
           "Score: `coremark_ips`, CoreMark iterations per second from `make eval` "
           "(f_clk / cycles per iteration, contract v3 section 7; higher wins, ties go to the "
           "smaller `area_um2`). Deltas are against the champion at the time of the entry; "
           "history `delta %` is the score change against the previous standing merged entry. "
           "Entries from before v3 have no score (`-`); they were ranked by area. "
           "Scores here are rounded: take MIN_IPS from `python3 tools/ledger.py show --json`. "
           "A forced merge (`--force-merge`, the lead's override) becomes the champion even when "
           "slower.", ""]
    champ = champion(entries)
    _, rolled = replay(entries)
    out.append("## Current champion")
    out.append("")
    if champ:
        out.append(f"**{champ['id']}** `{champ['sha'][:12]}` ({champ['branch']}): "
                   f"{fmt_ips(champ.get('coremark_ips'))} CoreMark iterations/s "
                   f"(clock {fmt_clock(champ.get('clock_period_ns'))} ns), "
                   f"{fmt_num(champ.get('area_um2'))} µm², {fmt_num(champ.get('cells'))} cells, "
                   f"formal {champ.get('formal') or '-'}, "
                   f"tile fraction {fmt_tile(champ.get('tile_fraction'))}, deep formal "
                   f"{champ.get('formal_deep') or '-'}. {cell(champ['idea'], 200)}")
    else:
        out.append("No champion yet.")
    out.append("")
    out.append("## Champion history")
    out.append("")
    merged = [e for e in entries if e.get("verdict") == "merged"]
    if merged:
        out.append("| # | id | date | sha | CoreMark/s | delta % | clock ns | area µm² | cells "
                   "| delta µm² | formal | tile fraction | deep formal | status | idea |")
        out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        prev = None
        for i, e in enumerate(merged):
            pct = "-"
            if prev and is_num(e.get("coremark_ips")):
                pct = f"{100.0 * (e['coremark_ips'] - prev) / prev:+.2f}%"
            status = f"rolled back by {rolled[e['id']]}" if e["id"] in rolled else (
                "champion" if champ and e["id"] == champ["id"] else "-")
            if e.get("forced") is True:
                status += " (forced)"
            out.append(f"| {i} | {e['id']} | {e['ts'][:10]} | `{e['sha'][:10]}` | "
                       f"{fmt_ips(e.get('coremark_ips'))} | {pct} | "
                       f"{fmt_clock(e.get('clock_period_ns'))} | "
                       f"{fmt_num(e.get('area_um2'))} | {fmt_num(e.get('cells'))} | "
                       f"{fmt_delta(e.get('delta_um2'))} | {e.get('formal') or '-'} | "
                       f"{fmt_tile(e.get('tile_fraction'))} | {e.get('formal_deep') or '-'} | "
                       f"{status} | {cell(e.get('idea'))} |")
            if is_num(e.get("coremark_ips")) and e["id"] not in rolled:
                prev = e["coremark_ips"]
    else:
        out.append("No merged entries yet.")
    out.append("")
    out.append(f"## Last {RECENT} hypotheses")
    out.append("")
    recent = entries[-RECENT:][::-1]
    if recent:
        out.append("| id | date | branch | verdict | CoreMark/s | delta CoreMark/s | clock ns "
                   "| area µm² | delta µm² | formal | failed gates | idea | notes |")
        out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for e in recent:
            verdict = e["verdict"] + (" (forced)" if e.get("forced") is True else "")
            out.append(f"| {e['id']} | {e['ts'][:10]} | {cell(e.get('branch'), 40)} | "
                       f"{verdict} | {fmt_ips(e.get('coremark_ips'))} | "
                       f"{fmt_delta_ips(e.get('delta_ips'))} | "
                       f"{fmt_clock(e.get('clock_period_ns'))} | {fmt_num(e.get('area_um2'))} | "
                       f"{fmt_delta(e.get('delta_um2'))} | {e.get('formal') or '-'} | "
                       f"{failed_gates(e)} | "
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
    a.add_argument("--force-merge", action="store_true",
                   help="lead override: merge even if the entry would not become the champion "
                        "(recorded as forced: true; it becomes the champion)")
    for p in (a, sub.add_parser("annotate", help="record champion-round results on an entry")):
        p.add_argument("--tile-fraction", type=float, help="from make tilefit, in (0, 1]")
        p.add_argument("--formal-deep", choices=FORMAL_DEEP)
    ann = sub.choices["annotate"]
    ann.add_argument("--id", required=True)
    ann.add_argument("--notes-append", default="")
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
            print(f"ledger: added {entry['id']} ({entry['verdict']}"
                  f"{', forced' if entry['forced'] else ''}), CoreMark "
                  f"{fmt_ips(entry['coremark_ips'])} iterations/s, delta "
                  f"{fmt_delta_ips(entry['delta_ips'])}, area {fmt_num(entry['area_um2'])} µm², "
                  f"delta {fmt_delta(entry['delta_um2'])}")
            print("ledger: run `python3 tools/ledger.py board` and commit loop/")
        elif args.cmd == "annotate":
            entries = annotate(entries, args)
            target = next(e for e in entries if e.get("id") == args.id)
            # Rewrite only the target's line; comment and blank lines stay byte for byte.
            with open(args.ledger) as f:
                raw = f.readlines()
            out, done = [], False
            for line in raw:
                s = line.strip()
                if not done and s and not s.startswith("#") and json.loads(s).get("id") == args.id:
                    out.append(json.dumps(target, ensure_ascii=False) + "\n")
                    done = True
                else:
                    out.append(line)
            tmp = args.ledger + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(out)
            os.replace(tmp, args.ledger)
            print(f"ledger: annotated {args.id}; run `python3 tools/ledger.py board`")
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
                print(f"  CoreMark {fmt_ips_full(champ.get('coremark_ips'))} iterations/s at "
                      f"{fmt_clock(champ.get('clock_period_ns'))} ns, formal "
                      f"{champ.get('formal') or '-'}"
                      f"{', forced merge' if champ.get('forced') is True else ''}")
                print(f"  area {fmt_num(champ.get('area_um2'))} um2, "
                      f"{fmt_num(champ.get('cells'))} cells, merged {champ['ts']}")
                print(f"  tile fraction {fmt_tile(champ.get('tile_fraction'))}, deep formal "
                      f"{champ.get('formal_deep') or '-'}")
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
