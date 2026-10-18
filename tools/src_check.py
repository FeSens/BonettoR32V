#!/usr/bin/env python3
"""Src hygiene: enforce contract section 1's restricted preprocessor, so every tool sees one design.

Runs first in the lint gate, and tools/equiv.py runs it before building anything. Verilator,
yosys, SBY, Icarus and LibreLane each predefine different macros and handle preprocessor corners
differently, so src/ may only use a tiny allowlisted subset, checked on a real tokenizer, with a
differential backstop that compares what three real preprocessors produce. Constructs that
simulate differently from the synthesized circuit (delays, waits, system tasks, resolved nets,
hierarchical references, stale function locals, ...) fail too, so RTL simulation, the formal and
equivalence models and the netlist all describe one circuit.

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
  6. Every module (and macromodule, primitive, interface, program, checker, package, class,
     config) is named tt_um_fesens_r32v or r32v_[A-Za-z0-9_]*, as a plain identifier, so no
     design module can shadow a checker's (rvfi_wrapper, tb, iss, ...).

Constructs that simulate differently from what synthesis builds (check_constructs; tokens on
directive lines are not code and are skipped):
  7. Timing: `#` only as `<name> #(` where <name> is a design module name (the parameter port
     list `module r32v_x #(` or a parameterized instantiation `r32v_x #(`), so no delay control
     anywhere. `always` must be followed by `@`, `@` must follow `always`, and the event control
     is @(*), @* or a list of posedge/negedge signals (no level-sensitive lists).
  8. System tasks and functions: only $signed and $unsigned.
  9. Keywords: specify, specparam, defparam, force, release, deassign, wait, fork/join, disable,
     event (and the `->` trigger), task, final, SystemVerilog assertions, real, realtime, time,
     primitive, table, tri*, wand, wor, trireg, uwire, supply0/1, pullup, pulldown, drive and
     charge strengths, always_comb/always_ff/always_latch, and the gate primitives (`or` only as
     the separator inside an event control).
 10. No hierarchical references: `.` only in named connections, right after `(` or `,` and
     followed by `name(`.
 11. No escaped identifiers, string literals or real literals.
 12. Functions: a local variable (or the function's own result) must not be read before it is
     assigned on every path (no stale or uninitialized values; Icarus keeps them between calls,
     synthesis makes them undefined).

Differential backstop (check_differential): for the plain and the RISCV_FORMAL builds, preprocess
src/*.v (sorted) with `verilator -E -P`, yosys `read_verilog -ppdump` and `iverilog -E`, tokenize
all three without comments or whitespace, drop `timescale (yosys consumes it) and yosys's file
markers, and require identical token streams.

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
# Keywords that name a design element at file scope (Verilog-2005 plus the SystemVerilog units
# Verilator accepts); the name that follows each must pass MODULE_NAME_RE.
DEFINITION_KEYWORDS = {"module", "macromodule", "primitive", "interface", "program", "checker",
                       "package", "class", "config"}
MODULE_NAME_RE = re.compile(r"^(tt_um_fesens_r32v|r32v_[A-Za-z0-9_]*)$")
SYSTEM_FUNCTIONS_OK = {"$signed", "$unsigned"}
# Keywords whose meaning is simulation-only, tool-dependent or not plain Verilog.
FORBIDDEN_KEYWORDS = {
    "specify", "endspecify", "specparam", "defparam", "force", "release", "deassign", "wait",
    "fork", "join", "join_any", "join_none", "disable", "event", "task", "endtask", "final",
    "assert", "assume", "cover", "restrict", "property", "endproperty", "sequence",
    "endsequence", "expect",
    "real", "realtime", "time",
    "primitive", "endprimitive", "table", "endtable",
    "tri", "tri0", "tri1", "triand", "trior", "trireg", "wand", "wor", "uwire", "supply0",
    "supply1", "pullup", "pulldown", "strong0", "strong1", "pull0", "pull1", "weak0", "weak1",
    "highz0", "highz1", "small", "medium", "large",
    "always_comb", "always_ff", "always_latch",
}
GATE_PRIMITIVES = {"and", "or", "nand", "nor", "xor", "xnor", "buf", "not", "bufif0", "bufif1",
                   "notif0", "notif1", "nmos", "pmos", "rnmos", "rpmos", "cmos", "rcmos", "tran",
                   "tranif0", "tranif1", "rtran", "rtranif0", "rtranif1"}
REAL_RE = re.compile(r"[0-9][0-9_]*(\.[0-9_]+)?([eE][+-]?[0-9_]+)?")
TIME_UNITS = {"s": 10 ** 15, "ms": 10 ** 12, "us": 10 ** 9, "ns": 10 ** 6, "ps": 10 ** 3, "fs": 1}
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
    in_module = False
    for idx, (kind, text, line) in enumerate(toks):
        if kind == "id" and text in ("module", "macromodule", "endmodule"):
            in_module = text != "endmodule"
        if kind != "dir":
            continue
        if text in ("timescale", "default_nettype") and in_module:
            errors.append(f"src/{name}:{line}: `{text} must be outside module ... endmodule")
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
            m = TIMESCALE_RE.match(code)
            if not m:
                errors.append(f"{where}: malformed `timescale (alone on its line, e.g. "
                              f"`timescale 1ns/1ps)")
            elif int(m.group(3)) * TIME_UNITS[m.group(4)] > \
                    int(m.group(1)) * TIME_UNITS[m.group(2)]:
                errors.append(f"{where}: `timescale precision is coarser than its unit")
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


def check_module_names(name, toks):
    """Every definition is tt_um_fesens_r32v or r32v_*, so none can shadow a checker's module."""
    errors = []
    for i, (kind, text, line) in enumerate(toks):
        if kind != "id" or text not in DEFINITION_KEYWORDS:
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if nxt is None or nxt[0] != "id" or not MODULE_NAME_RE.match(nxt[1]):
            got = repr(nxt[1]) if nxt else "end of file"
            errors.append(f"src/{name}:{line}: {text} name {got} must be tt_um_fesens_r32v or "
                          f"start with r32v_ (contract section 1)")
    return errors


def build_tokens(toks, rvfi):
    """The code tokens one build compiles: `ifdef/`ifndef RISCV_FORMAL resolved, directive lines
    (which hold only the directive, see check_directives) dropped."""
    dir_lines = {line for kind, _, line in toks if kind == "dir"}
    active, out = [], []
    for kind, text, line in toks:
        if kind == "dir":
            if text in ("ifdef", "ifndef"):
                active.append(rvfi if text == "ifdef" else not rvfi)
            elif text == "else" and active:
                active[-1] = not active[-1]
            elif text == "endif" and active:
                active.pop()
        elif line not in dir_lines and all(active):
            out.append((kind, text, line))
    return out


def close_of(toks, p):
    """Index of the bracket closing the one at toks[p], or None."""
    depth = 0
    for q in range(p, len(toks)):
        if toks[q][0] != "op":
            continue
        if toks[q][1] in ("(", "[", "{"):
            depth += 1
        elif toks[q][1] in (")", "]", "}"):
            depth -= 1
            if depth == 0:
                return q
    return None


def event_list_ok(inner):
    """@( ... ) contents: a lone *, or posedge/negedge <id>[<index>] joined by `or` or `,`."""
    if [t for _, t, _ in inner] == ["*"]:
        return True
    p = 0
    while True:
        if p + 1 >= len(inner) or inner[p][1] not in ("posedge", "negedge") or \
                inner[p + 1][0] != "id":
            return False
        p += 2
        if p < len(inner) and inner[p][1] == "[":
            q = close_of(inner, p)
            if q is None:
                return False
            p = q + 1
        if p == len(inner):
            return True
        if inner[p][1] not in ("or", ","):
            return False
        p += 1


def check_constructs(name, toks):
    """Rules 7-11: constructs that simulate differently from the synthesized circuit."""
    errors = []

    def err(line, msg):
        errors.append(f"src/{name}:{line}: {msg} (contract section 1)")

    n = len(toks)
    in_event = set()
    for i, (kind, text, line) in enumerate(toks):
        if kind != "op" or text != "@":
            continue
        if not (i and toks[i - 1][:2] == ("id", "always")):
            err(line, "`@` event control is only allowed directly after `always` (no waits or "
                      "intra-assignment events)")
        nxt = toks[i + 1][1] if i + 1 < n else None
        if nxt == "*":
            continue
        close = close_of(toks, i + 1) if nxt == "(" else None
        if close is None or not event_list_ok(toks[i + 2:close]):
            err(line, "event control must be @(*), @* or a list of posedge/negedge signals (a "
                      "level-sensitive list simulates differently from the synthesized logic)")
        if close is not None:
            in_event.update(range(i + 2, close))
    for i, (kind, text, line) in enumerate(toks):
        prev = toks[i - 1] if i else ("", "", 0)
        nxt = toks[i + 1] if i + 1 < n else ("", "", 0)
        if kind == "id":
            if text in FORBIDDEN_KEYWORDS:
                err(line, f"`{text}` is not allowed")
            elif text in GATE_PRIMITIVES and not (text == "or" and i in in_event):
                err(line, f"`{text}` is not allowed (gate primitive; `or` only inside an event "
                          f"control)")
            elif text == "always" and nxt[1] != "@":
                err(line, "`always` must be followed by an event control: @(posedge clk), @(*) "
                          "or @*")
        elif kind == "sys" and text not in SYSTEM_FUNCTIONS_OK:
            err(line, f"system task/function `{text}` is not allowed (only $signed, $unsigned)")
        elif kind == "escid":
            err(line, f"escaped identifier {text!r} is not allowed (plain identifiers only)")
        elif kind == "str":
            err(line, f"string literal {text[:30]!r} is not allowed")
        elif kind == "num" and "'" not in text and REAL_RE.fullmatch(text) and \
                ("." in text or "e" in text.lower()):
            err(line, f"real literal {text!r} is not allowed")
        elif kind == "op" and text == "#":
            if not (nxt[1] == "(" and prev[0] == "id" and MODULE_NAME_RE.match(prev[1])):
                err(line, "`#` delay control is not allowed (`#(` only after a design module "
                          "name: `module r32v_x #(` or an `r32v_x #(` instantiation)")
        elif kind == "op" and text == "->":
            err(line, "`->` event trigger is not allowed")
        elif kind == "op" and text == ".":
            if prev[0] in ("id", "escid") or prev[1] in ("]", ")"):
                err(line, f"hierarchical reference `{prev[1]}.{nxt[1]}` is not allowed")
            elif not (prev[1] in ("(", ",") and nxt[0] == "id" and i + 2 < n
                      and toks[i + 2][1] == "("):
                err(line, "`.` is only allowed in a named connection: `.name(`")
    return errors


class FunctionShapeError(Exception):
    pass


class FunctionScan:
    """Rule 12: definite assignment of function locals before any read.

    Walks one function's statements (begin/end, if/else, case with default, for, while, repeat,
    blocking or nonblocking assignments). D is the set of locals assigned in full on every path
    so far; a read of a local outside D fails. Partial assignments (x[i] = ...) never make a
    local definite. Anything the walker does not understand fails, so the check stays strict.
    """

    def __init__(self, fname, toks):
        self.file, self.toks, self.errors, self.reported = fname, toks, [], set()
        self.locals, self.func = set(), None

    def tok(self, p):
        if p >= len(self.toks):
            raise FunctionShapeError("unexpected end of function")
        return self.toks[p][1]

    def find(self, p, sym):
        depth = 0
        for q in range(p, len(self.toks)):
            k, t, _ = self.toks[q]
            if k != "op":
                continue
            if t in ("(", "[", "{"):
                depth += 1
            elif t in (")", "]", "}"):
                depth -= 1
            elif t == sym and depth == 0:
                return q
        raise FunctionShapeError(f"missing {sym!r}")

    def close(self, p):
        if self.tok(p) != "(":
            raise FunctionShapeError("expected (")
        q = close_of(self.toks, p)
        if q is None:
            raise FunctionShapeError("unbalanced (")
        return q

    def reads(self, a, b, D):
        for q in range(a, b):
            k, t, line = self.toks[q]
            called = q + 1 < len(self.toks) and self.toks[q + 1][1] == "("
            if k == "id" and t in self.locals and t not in D and not called and \
                    t not in self.reported:
                self.reported.add(t)
                self.errors.append(f"src/{self.file}:{line}: function {self.func} reads {t} "
                                   f"before assigning it on every path (an uninitialized or "
                                   f"stale value; contract section 1)")

    def assign(self, a, b, D):
        eq, depth = None, 0
        for q in range(a, b):
            k, t, _ = self.toks[q]
            depth += k == "op" and t in ("(", "[", "{")
            depth -= k == "op" and t in (")", "]", "}")
            if depth == 0 and k == "op" and t in ("=", "<="):
                eq = q
                break
        if eq is None:
            raise FunctionShapeError("expected an assignment")
        self.reads(eq + 1, b, D)
        elems = [(a, eq)]
        if self.tok(a) == "{":
            if close_of(self.toks, a) != eq - 1:
                raise FunctionShapeError("malformed concatenation target")
            elems, s, depth = [], a + 1, 0
            for q in range(a + 1, eq):
                t = self.toks[q][1]
                depth += t in ("(", "[", "{")
                depth -= t in (")", "]", "}")
                if (t == "," and depth == 0) or q == eq - 1:
                    elems.append((s, q))
                    s = q + 1
        full = set()
        for s, e in elems:
            if s >= e or self.toks[s][0] != "id":
                raise FunctionShapeError("malformed assignment target")
            if e - s == 1:
                full.add(self.toks[s][1])
            else:
                self.reads(s + 1, e, D)
        return D | full

    def decls(self, p):
        while self.tok(p) in VARIABLE_TYPES or self.tok(p) in ("parameter", "localparam"):
            q = self.find(p, ";")
            if self.tok(p) in VARIABLE_TYPES:
                depth, skip = 0, False
                for k, t, _ in self.toks[p + 1:q]:
                    depth += t in ("(", "[", "{")
                    depth -= t in (")", "]", "}")
                    if depth == 0 and t == "=":
                        skip = True
                    elif depth == 0 and t == ",":
                        skip = False
                    elif depth == 0 and k == "id" and not skip and t not in ("signed",
                                                                            "unsigned"):
                        self.locals.add(t)
            p = q + 1
        return p

    def stmt(self, p, D):
        t = self.tok(p)
        if t == "begin":
            p += 1
            if self.tok(p) == ":":
                p += 2
            p = self.decls(p)
            while self.tok(p) != "end":
                p, D = self.stmt(p, D)
            return p + 1, D
        if t == "if":
            c = self.close(p + 1)
            self.reads(p + 2, c, D)
            p, d1 = self.stmt(c + 1, D)
            if p < len(self.toks) and self.tok(p) == "else":
                p, d2 = self.stmt(p + 1, D)
                return p, d1 & d2
            return p, D
        if t in ("case", "casez", "casex"):
            c = self.close(p + 1)
            self.reads(p + 2, c, D)
            p, outs, dflt = c + 1, [], False
            while self.tok(p) != "endcase":
                if self.tok(p) == "default":
                    dflt, p = True, p + 1
                    if self.tok(p) == ":":
                        p += 1
                else:
                    q = self.find(p, ":")
                    self.reads(p, q, D)
                    p = q + 1
                p, d = self.stmt(p, D)
                outs.append(d)
            return p + 1, (frozenset.intersection(*outs) if dflt and outs else D)
        if t == "for":
            c = self.close(p + 1)
            s1 = self.find(p + 2, ";")
            s2 = self.find(s1 + 1, ";")
            d1 = self.assign(p + 2, s1, D)
            self.reads(s1 + 1, s2, d1)
            p, d2 = self.stmt(c + 1, d1)
            self.assign(s2 + 1, c, d2)
            return p, d1
        if t in ("while", "repeat"):
            c = self.close(p + 1)
            self.reads(p + 2, c, D)
            p, _ = self.stmt(c + 1, D)
            return p, D
        if t == ";":
            return p + 1, D
        q = self.find(p, ";")
        return q + 1, self.assign(p, q, D)

    def run(self):
        """toks run from `function` up to (not including) `endfunction`."""
        line = self.toks[0][2]
        try:
            p, depth = 1, 0
            while self.tok(p) != ";" or depth:
                k, t, _ = self.toks[p]
                if t == "(" and depth == 0 and self.func:     # ANSI port list
                    p = self.close(p) + 1
                    continue
                depth += t in ("(", "[", "{")
                depth -= t in (")", "]", "}")
                if k == "id" and depth == 0 and t not in ("automatic", "signed", "unsigned") \
                        and t not in VARIABLE_TYPES:
                    self.func = t
                p += 1
            if not self.func:
                raise FunctionShapeError("no function name")
            self.locals.add(self.func)
            p += 1
            while self.tok(p) in ("input", "output", "inout"):
                p = self.find(p, ";") + 1
            p = self.decls(p)
            D = frozenset()
            while p < len(self.toks):
                p, D = self.stmt(p, D)
        except FunctionShapeError as e:
            self.errors.append(f"src/{self.file}:{line}: function {self.func or '?'}: src_check "
                               f"cannot follow its body ({e}); write it with begin/end, if/else, "
                               f"case, for and assignments (contract section 1)")
        return self.errors


def check_functions(name, toks):
    errors = []
    i = 0
    while i < len(toks):
        if toks[i][:2] != ("id", "function"):
            i += 1
            continue
        end = next((j for j in range(i, len(toks)) if toks[j][:2] == ("id", "endfunction")),
                   None)
        if end is None:
            errors.append(f"src/{name}:{toks[i][2]}: function without endfunction")
            break
        errors += FunctionScan(name, toks[i:end]).run()
        i = end + 1
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
        errors += check_module_names(name, toks)
        # Rules 7-12 run on what each build actually compiles: the plain and the RISCV_FORMAL
        # token streams, ifdefs resolved and directive lines dropped.
        seen = set(errors)
        for rvfi in (False, True):
            code = build_tokens(toks, rvfi)
            for e in check_constructs(name, code) + check_functions(name, code):
                if e not in seen:
                    seen.add(e)
                    errors.append(e)
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


def iverilog_pp(files, rvfi, env, work):
    out = os.path.join(work, "ivl_pp.v")
    cmd = ["iverilog", "-E", "-o", out] + (["-DRISCV_FORMAL"] if rvfi else []) + files
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    msg = (p.stderr + p.stdout).strip()
    if p.returncode != 0 or msg:            # Icarus only warns on, e.g., an undefined macro
        return None, msg.splitlines()[:1] or [f"exit {p.returncode}"]
    with open(out) as f:
        return f.read(), None


PREPROCESSORS = ("verilator", "yosys", "iverilog")


def check_differential(src, env=None):
    """Compare verilator -E -P, yosys -ppdump and iverilog -E token streams, plain and
    RISCV_FORMAL."""
    env = env or tool_env()
    for tool in PREPROCESSORS:
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
            outs = {"verilator": verilator_pp(files, rvfi, env),
                    "yosys": yosys_ppdump(files, rvfi, env, work),
                    "iverilog": iverilog_pp(files, rvfi, env, work)}
            failed = [(tool, err) for tool, (_, err) in outs.items() if err]
            if failed:
                errors.append(f"differential check ({label}): preprocessor failed: "
                              f"{failed[0][0]}: {failed[0][1]}")
                continue
            try:
                streams = {tool: normalized(text) for tool, (text, _) in outs.items()}
            except LexError as e:
                errors.append(f"differential check ({label}): cannot tokenize output ({e})")
                continue
            a = streams["verilator"]
            for tool in PREPROCESSORS[1:]:
                b = streams[tool]
                if a == b:
                    continue
                k = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
                errors.append(f"differential check ({label}): verilator and {tool} preprocess "
                              f"src/ differently at token {k}: verilator "
                              f"{' '.join(a[max(0, k - 3):k + 4])!r} vs {tool} "
                              f"{' '.join(b[max(0, k - 3):k + 4])!r}")
    return errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="src/ hygiene check (see docs/eval.md)")
    ap.add_argument("--src", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src"))
    ap.add_argument("--no-differential", action="store_true",
                    help="static rules only (skip the three-way preprocessor comparison)")
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
