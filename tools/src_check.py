#!/usr/bin/env python3
"""Src hygiene: enforce contract section 1's restricted preprocessor, so every tool sees one design.

Runs first in the lint gate, and tools/equiv.py runs it before building anything. Verilator,
yosys, SBY, Icarus and LibreLane each predefine different macros and handle preprocessor corners
differently, so src/ may only use a tiny allowlisted subset, checked on a real tokenizer, with a
differential backstop that compares what two real preprocessors produce.

Layout (check_layout):
  - every entry under src/ (recursively) matches ^[A-Za-z0-9_][A-Za-z0-9_.-]*$; no symlinks;
  - no subdirectories except test_* and __pycache__ (Python makes it when src/test_*.py runs);
  - every .v file matches ^[A-Za-z0-9_]+\\.v$; no .sv or .svh.

Tokens of every src/*.v (check_hdl), comments and strings handled:
  1. Only these directives: `ifdef RISCV_FORMAL, `ifndef RISCV_FORMAL, `else, `endif,
     `default_nettype none|wire, `timescale <n><unit>/<n><unit>, each alone on its line, with
     balanced ifdef/endif nesting. Any other backtick token (`define, macro uses, `include,
     `undef, `elsif, `celldefine, `resetall, ...) fails.
  2. The token `initial` fails. Initializers on variable declarations (reg, integer, time, real,
     realtime, and the SystemVerilog logic/bit/int/byte/shortint/longint) fail; parameter and
     localparam (typed or not) are fine. `wire w = e;` is a continuous assignment, not a power-up
     value, and stays allowed (contract section 1 names reg and integer only).
  3. `(*` fails unless it is the @(*) sensitivity list (whitespace allowed inside).
  4. Comments whose first word is verilator, synopsys, synthesis, pragma or yosys (any case), or
     that contain translate_, lint_off, full_case or parallel_case, fail.
  5. Carriage returns, other control characters (tab and newline aside) and non-ASCII fail
     anywhere in the file.

Differential backstop (check_differential): for the plain and the RISCV_FORMAL builds, preprocess
src/*.v (sorted) with `verilator -E -P` and with yosys `read_verilog -ppdump`, tokenize both
without comments or whitespace, drop `timescale (yosys consumes it) and yosys's file markers, and
require identical token streams.

Usage: src_check.py [--src DIR] [--no-differential]. Exit 0 when clean, 1 with one line per
problem.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
VFILE_RE = re.compile(r"^[A-Za-z0-9_]+\.v$")
SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./+-]+$")
EXEMPT_DIRS = ("__pycache__",)
TOOLCHAIN_BIN = os.path.expanduser("~/bonetto/riscv-autoarch/.toolchain/oss-cad-suite/bin")

VARIABLE_TYPES = {"reg", "integer", "time", "real", "realtime", "logic", "bit", "int", "byte",
                  "shortint", "longint"}
METACOMMENT_FIRST = {"verilator", "synopsys", "synthesis", "pragma", "yosys"}
METACOMMENT_ANY = re.compile(r"translate_|lint_off|full_case|parallel_case", re.I)
TIMESCALE_RE = re.compile(r"^\s*`timescale\s+(1|10|100)\s*(s|ms|us|ns|ps|fs)\s*/\s*(1|10|100)\s*"
                          r"(s|ms|us|ns|ps|fs)\s*$")
OPS = ("<<<", ">>>", "===", "!==", "==", "!=", "<=", ">=", "&&", "||", "<<", ">>", "**", "~&",
       "~|", "~^", "^~", "->", "+:", "-:")
NUMBER_RE = re.compile(r"(?:[0-9][0-9_]*)?[ \t]*'[sS]?[bBoOdDhH][ \t]*[0-9a-fA-FxXzZ?_]+"
                       r"|[0-9][0-9_]*(?:\.[0-9_]+)?(?:[eE][+-]?[0-9_]+)?")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
SYSNAME_RE = re.compile(r"\$[A-Za-z0-9_$]+")


class LexError(Exception):
    def __init__(self, line, msg):
        super().__init__(msg)
        self.line = line


def tokenize(text):
    """Return (tokens, comments): tokens are (kind, text, line), comments are (text, line).

    kinds: dir (a backtick directive or macro name, text without the backtick), id, sys, escid,
    num, str, op. Raises LexError on anything the tools could read differently.
    """
    toks, comments = [], []
    i, n, line = 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t":
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            comments.append((text[i:j], line))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise LexError(line, "unterminated /* comment")
            comments.append((text[i:j + 2], line))
            line += text.count("\n", i, j + 2)
            i = j + 2
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\n":
                    raise LexError(line, "unterminated string")
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise LexError(line, "unterminated string")
            toks.append(("str", text[i:j + 1], line))
            i = j + 1
        elif c == "`":
            m = IDENT_RE.match(text, i + 1)
            if not m:
                raise LexError(line, f"stray backtick sequence {text[i:i + 3]!r}")
            toks.append(("dir", m.group(0), line))
            i = m.end()
        elif c == "\\":
            j = i + 1
            while j < n and not text[j].isspace():
                j += 1
            toks.append(("escid", text[i:j], line))
            i = j
        elif c == "(" and text.startswith("(*", i):
            # Only the @(*) sensitivity list may put * right after (.
            k = len(toks)
            m = re.match(r"\(\*[ \t]*\)", text[i:])
            if not (m and k and toks[-1] == ("op", "@", toks[-1][2])):
                raise LexError(line, "attribute instance (* ... *) is not allowed")
            toks += [("op", "(", line), ("op", "*", line), ("op", ")", line)]
            i += m.end()
        elif c.isdigit() or c == "'":
            m = NUMBER_RE.match(text, i)
            if m and m.group(0):
                toks.append(("num", m.group(0), line))
                i = m.end()
            else:
                toks.append(("op", c, line))
                i += 1
        elif c == "$":
            m = SYSNAME_RE.match(text, i)
            if not m:
                raise LexError(line, "stray $")
            toks.append(("sys", m.group(0), line))
            i = m.end()
        elif c.isalpha() or c == "_":
            m = IDENT_RE.match(text, i)
            toks.append(("id", m.group(0), line))
            i = m.end()
        else:
            for op in OPS:
                if text.startswith(op, i):
                    toks.append(("op", op, line))
                    i += len(op)
                    break
            else:
                toks.append(("op", c, line))
                i += 1
    return toks, comments


def check_layout(src):
    errors = []
    if os.path.islink(src) or not os.path.isdir(src):
        return [f"{src}: must be a real directory"]
    for entry in sorted(os.listdir(src)):
        path = os.path.join(src, entry)
        rel = f"src/{entry}"
        if not NAME_RE.match(entry):
            errors.append(f"{rel!r}: name must match {NAME_RE.pattern}")
            continue
        if os.path.islink(path):
            errors.append(f"{rel}: symlinks are not allowed in src/")
        elif os.path.isdir(path):
            if not (entry.startswith("test_") or entry in EXEMPT_DIRS):
                errors.append(f"{rel}/: subdirectories are not allowed in src/ (only test_* and "
                              f"__pycache__)")
                continue
            for root, dirs, files in os.walk(path):
                for name in sorted(dirs + files):
                    p = os.path.join(root, name)
                    r = "src/" + os.path.relpath(p, src)
                    if not NAME_RE.match(name):
                        errors.append(f"{r!r}: name must match {NAME_RE.pattern}")
                    elif os.path.islink(p):
                        errors.append(f"{r}: symlinks are not allowed in src/")
        elif entry.endswith(".v") and not VFILE_RE.match(entry):
            errors.append(f"{rel}: Verilog file names must match {VFILE_RE.pattern}")
        elif entry.endswith((".sv", ".svh")):
            errors.append(f"{rel}: SystemVerilog is not allowed (contract section 1: plain "
                          f"Verilog)")
    return errors


def verilog_files(src):
    return sorted(f for f in os.listdir(src) if VFILE_RE.match(f)
                  and os.path.isfile(os.path.join(src, f))
                  and not os.path.islink(os.path.join(src, f)))


def check_directives(name, toks, lines):
    errors = []
    stack = []                                   # one entry per open ifdef: has_else
    seen_lines = {}
    for idx, (kind, text, line) in enumerate(toks):
        if kind != "dir":
            continue
        where = f"src/{name}:{line}"
        raw = lines[line - 1] if line - 1 < len(lines) else ""
        code = re.sub(r"//.*$", "", raw).strip()
        if seen_lines.get(line):
            errors.append(f"{where}: one directive per line")
        seen_lines[line] = True
        if text in ("ifdef", "ifndef"):
            if not re.fullmatch(rf"`{text}\s+RISCV_FORMAL", code):
                errors.append(f"{where}: only `{text} RISCV_FORMAL is allowed, alone on its line "
                              f"(got {code!r})")
            stack.append(False)
        elif text == "else":
            if code != "`else":
                errors.append(f"{where}: `else must be alone on its line")
            if not stack:
                errors.append(f"{where}: `else without `ifdef")
            elif stack[-1]:
                errors.append(f"{where}: second `else for one `ifdef")
            else:
                stack[-1] = True
        elif text == "endif":
            if code != "`endif":
                errors.append(f"{where}: `endif must be alone on its line")
            if not stack:
                errors.append(f"{where}: `endif without `ifdef")
            else:
                stack.pop()
        elif text == "default_nettype":
            if not re.fullmatch(r"`default_nettype\s+(none|wire)", code):
                errors.append(f"{where}: only `default_nettype none or wire, alone on its line")
        elif text == "timescale":
            if not TIMESCALE_RE.match(code):
                errors.append(f"{where}: malformed `timescale (alone on its line, e.g. "
                              f"`timescale 1ns/1ps)")
        else:
            errors.append(f"{where}: `{text} is not allowed (contract section 1: only `ifdef/"
                          f"`ifndef RISCV_FORMAL, `else, `endif, `default_nettype, `timescale)")
    if stack:
        errors.append(f"src/{name}: {len(stack)} `ifdef/`ifndef without `endif")
    return errors


def check_declarations(name, toks):
    """Initializers on variable declarations; parameter/localparam are allowed."""
    errors = []
    for i, (kind, text, line) in enumerate(toks):
        if kind != "id" or text not in VARIABLE_TYPES:
            continue
        # Walk back to the start of this declaration: parameter/localparam make it a constant.
        j, is_param = i - 1, False
        while j >= 0 and toks[j][1] not in (";", ",", "(", ")", "begin", "end"):
            if toks[j][1] in ("parameter", "localparam"):
                is_param = True
                break
            j -= 1
        if is_param:
            continue
        depth = 0
        for kind2, text2, line2 in toks[i + 1:]:
            if text2 in ("(", "[", "{"):
                depth += 1
            elif text2 in (")", "]", "}"):
                depth -= 1
                if depth < 0:
                    break
            elif text2 == ";" and depth == 0:
                break
            elif text2 == "=" and kind2 == "op" and depth == 0:
                errors.append(f"src/{name}:{line}: initializer on a `{text}` declaration is a "
                              f"power-up value (contract section 1)")
                break
    return errors


def check_hdl(src):
    errors = []
    for name in verilog_files(src):
        with open(os.path.join(src, name), "rb") as f:
            data = f.read()
        bad = [b for b in set(data) if b > 127 or (b < 32 and b not in (9, 10))]
        if bad:
            line = data[:data.index(bytes([bad[0]]))].count(b"\n") + 1
            what = ("carriage return (CRLF)" if 13 in bad else
                    "non-ASCII byte" if any(b > 127 for b in bad) else "control character")
            errors.append(f"src/{name}:{line}: {what}; files must be plain ASCII with LF endings")
            continue
        text = data.decode("ascii")
        try:
            toks, comments = tokenize(text)
        except LexError as e:
            errors.append(f"src/{name}:{e.line}: {e}")
            continue
        errors += check_directives(name, toks, text.split("\n"))
        for kind, t, line in toks:
            if kind == "id" and t == "initial":
                errors.append(f"src/{name}:{line}: `initial` is not allowed (no power-up values, "
                              f"contract section 1)")
        errors += check_declarations(name, toks)
        for ctext, line in comments:
            body = ctext[2:-2] if ctext.startswith("/*") else ctext[2:]
            first = re.match(r"[\s*/]*([A-Za-z_]+)", body)
            if (first and first.group(1).lower() in METACOMMENT_FIRST) or \
                    METACOMMENT_ANY.search(body):
                errors.append(f"src/{name}:{line}: tool metacomment {ctext.strip()[:60]!r} is "
                              f"not allowed (contract section 1)")
    return errors


def check(src):
    """Static problems (layout and tokens) for directory `src`, as one-line strings."""
    errors = check_layout(src)
    if os.path.isdir(src) and not os.path.islink(src):
        errors += check_hdl(src)
    return errors


# --- differential backstop ---------------------------------------------------------------------

def tool_env():
    env = dict(os.environ)
    path = env.get("PATH", "")
    if os.path.isdir(TOOLCHAIN_BIN) and TOOLCHAIN_BIN not in path.split(os.pathsep):
        env["PATH"] = TOOLCHAIN_BIN + os.pathsep + path
    return env


def normalized(text):
    """Comment- and whitespace-free token texts, without `timescale and yosys file markers."""
    keep = []
    for raw in text.split("\n"):
        s = raw.strip()
        if s.startswith(("`timescale", "`file_push", "`file_pop", "`line")):
            continue
        keep.append(raw)
    toks, _ = tokenize("\n".join(keep))
    return [t for _, t, _ in toks]


def yosys_ppdump(files, rvfi, env, work):
    log = os.path.join(work, "pp.log")
    defs = "-DRISCV_FORMAL " if rvfi else ""
    cmd = f"read_verilog -ppdump {defs}" + " ".join(f'"{f}"' for f in files)
    p = subprocess.run(["yosys", "-q", "-l", log, "-p", cmd], capture_output=True, text=True,
                       env=env)
    with open(log, errors="replace") as f:
        text = f.read()
    if p.returncode != 0:
        return None, (p.stdout + p.stderr + text).strip().splitlines()[-1:]
    dumps = re.findall(r"-- Verilog code after preprocessor --\n(.*?)-- END OF DUMP --", text,
                       re.S)
    return "\n".join(dumps), None


def verilator_pp(files, rvfi, env):
    cmd = ["verilator", "-E", "-P"] + (["-DRISCV_FORMAL"] if rvfi else []) + files
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if p.returncode != 0:
        return None, (p.stderr or p.stdout).strip().splitlines()[:1]
    return p.stdout, None


def check_differential(src, env=None):
    """Compare verilator -E -P and yosys -ppdump token streams, plain and RISCV_FORMAL."""
    env = env or tool_env()
    for tool in ("verilator", "yosys"):
        if shutil.which(tool, path=env["PATH"]) is None:
            return [f"differential check: {tool} not on PATH"]
    files = [os.path.join(os.path.abspath(src), f) for f in verilog_files(src)]
    if not files:
        return []
    for f in files:
        if not SAFE_PATH.match(f):
            return [f"differential check: unsafe path {f!r}"]
    errors = []
    with tempfile.TemporaryDirectory() as work:
        for rvfi in (False, True):
            label = "RISCV_FORMAL" if rvfi else "plain"
            vtext, verr = verilator_pp(files, rvfi, env)
            ytext, yerr = yosys_ppdump(files, rvfi, env, work)
            if verr or yerr:
                errors.append(f"differential check ({label}): preprocessor failed: "
                              f"{(verr or yerr)}")
                continue
            try:
                a, b = normalized(vtext), normalized(ytext)
            except LexError as e:
                errors.append(f"differential check ({label}): cannot tokenize output ({e})")
                continue
            if a != b:
                k = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
                errors.append(f"differential check ({label}): verilator and yosys preprocess "
                              f"src/ differently at token {k}: verilator "
                              f"{' '.join(a[max(0, k - 3):k + 4])!r} vs yosys "
                              f"{' '.join(b[max(0, k - 3):k + 4])!r}")
    return errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="src/ hygiene check (see docs/eval.md)")
    ap.add_argument("--src", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src"))
    ap.add_argument("--no-differential", action="store_true",
                    help="static rules only (skip the verilator/yosys preprocessor comparison)")
    args = ap.parse_args(argv)
    errors = check(args.src)
    if not errors and not args.no_differential:
        errors = check_differential(args.src)
    for e in errors:
        print("src_check: " + e)
    print(f"src_check: {'FAIL' if errors else 'PASS'} ({len(errors)} problem(s))")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
