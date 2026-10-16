#!/usr/bin/env python3
"""Contract check: a hypothesis diff touches only the paths a hypothesis may change.

Usage: contract_check.py [--physical] [--repo DIR] BASE HEAD

BASE is the champion sha the hypothesis started from, HEAD the hypothesis sha. The check compares
the two trees directly (`git diff BASE HEAD`, the same as BASE..HEAD), so HEAD must carry the exact
contract of BASE: a branch based on an older champion fails if the contract moved since.

Allowed (CLAUDE.md, docs/contract.md section 7):
  - src/**, except src/config.json (this covers the optional unit tests src/test_*);
  - physical/**, only with --physical (physical hypotheses the lead assigns).
Everything else fails, including deletions and renames out of src/ (renames are listed as a
delete plus an add, so both paths are judged). Symlinks and submodules (gitlinks) fail anywhere.

Independently of the path list, it fingerprints the don't-touch list: every path in it must have
the same git object id in BASE and HEAD (the riscv-formal submodule pointer included).

Exit 0 on PASS, 1 on FAIL (the offending paths are listed), 2 on usage errors.
"""
import argparse
import subprocess
import sys

DONT_TOUCH = ["verif", "tools", "test", "third_party", "info.yaml", "src/config.json",
              "docs/contract.md", "CLAUDE.md", "Makefile", ".github", "loop", ".claude"]
SUBMODULE = "third_party/riscv-formal"


class GitError(Exception):
    pass


def git(repo, *args):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def resolve(repo, rev):
    try:
        return git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}").strip()
    except GitError:
        raise GitError(f"cannot resolve {rev!r} to a commit in {repo}")


def object_id(repo, commit, path):
    """Git object id of `path` in `commit`, or None when the path does not exist there."""
    p = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet",
                        f"{commit}:{path}"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def changed(repo, base, head):
    """[(status, src_mode, dst_mode, path)] for every path that differs, renames split."""
    out = git(repo, "diff", "--raw", "--no-renames", "--no-ext-diff", "-z", base, head)
    fields = out.split("\0")
    rows = []
    i = 0
    while i < len(fields) - 1:
        meta = fields[i]
        if not meta.startswith(":"):
            i += 1
            continue
        src_mode, dst_mode, _, _, status = meta[1:].split()
        rows.append((status[0], src_mode, dst_mode, fields[i + 1]))
        i += 2
    return rows


def path_allowed(path, physical):
    if path == "src/config.json":
        return False, "src/config.json is contract (TT flow config, lead only)"
    if path.startswith("src/"):
        return True, ""
    if path.startswith("physical/"):
        if physical:
            return True, ""
        return False, "physical/** needs --physical (lead-assigned physical hypothesis)"
    return False, "outside the allowed paths (src/**, physical/** with --physical)"


def check(repo, base_rev, head_rev, physical=False):
    """Return (ok, report_lines)."""
    base = resolve(repo, base_rev)
    head = resolve(repo, head_rev)
    lines = [f"contract_check: BASE {base[:12]} ({base_rev}) HEAD {head[:12]} ({head_rev})"
             + (" --physical" if physical else "")]
    bad = []
    rows = changed(repo, base, head)
    kind = {"A": "added", "D": "deleted", "M": "modified", "T": "type changed"}
    for status, src_mode, dst_mode, path in rows:
        ok, why = path_allowed(path, physical)
        if ok and ("120000" in (src_mode, dst_mode) or "160000" in (src_mode, dst_mode)):
            ok, why = False, "symlinks and submodules are not allowed in a hypothesis"
        if not ok:
            bad.append(f"  {path} ({kind.get(status, status)}): {why}")

    fp_bad = []
    for path in DONT_TOUCH:
        a, b = object_id(repo, base, path), object_id(repo, head, path)
        if a != b:
            fp_bad.append(f"  {path}: {a or 'absent'} -> {b or 'absent'}")
    sub_a, sub_b = object_id(repo, base, SUBMODULE), object_id(repo, head, SUBMODULE)
    if sub_a != sub_b:
        fp_bad.append(f"  {SUBMODULE} submodule pointer: {sub_a or 'absent'} -> "
                      f"{sub_b or 'absent'}")

    if bad:
        lines.append(f"FAIL: {len(bad)} changed path(s) outside the allowed paths:")
        lines += bad
    if fp_bad:
        lines.append(f"FAIL: don't-touch fingerprint differs for {len(fp_bad)} path(s):")
        lines += fp_bad
    ok = not bad and not fp_bad
    if ok:
        lines.append(f"PASS: {len(rows)} changed path(s), all allowed; don't-touch fingerprint "
                     f"unchanged ({len(DONT_TOUCH)} paths, submodule {(sub_a or 'absent')[:12]})")
    return ok, lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hypothesis contract check (see docs/eval.md)")
    ap.add_argument("base")
    ap.add_argument("head")
    ap.add_argument("--physical", action="store_true", help="also allow physical/**")
    ap.add_argument("--repo", default=".", help="repository (default: current directory)")
    args = ap.parse_args(argv)
    try:
        ok, lines = check(args.repo, args.base, args.head, args.physical)
    except GitError as e:
        print(f"contract_check: ERROR: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
