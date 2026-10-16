#!/usr/bin/env python3
"""Src hygiene: src/ may only hold what every tool reads the same way.

Runs first in the lint gate, and tools/equiv.py runs it before building anything. Every gate
(Verilator cosim, yosys synthesis and equivalence, SBY formal, Icarus gate-level) reads src/ with a
different predefined macro set, so a design could show each tool a different circuit. These rules
close that and keep file names from reaching tool command lines as anything but names.

Names and layout:
  - every entry under src/ (recursively) matches ^[A-Za-z0-9_][A-Za-z0-9_.-]*$;
  - no symlinks anywhere under src/;
  - no subdirectories, except test_* (a hypothesis's own unit tests) and __pycache__ (Python
    creates it when src/test_*.py runs); nothing in them is read by a gate;
  - every .v file matches ^[A-Za-z0-9_]+\\.v$; no .sv or .svh (contract section 1).

Inside the HDL (src/*.v and every file they `include), outside comments:
  - `ifdef/`ifndef/`elsif may only test RISCV_FORMAL or a macro `define'd in src/ itself;
  - `define/`undef may not name a tool macro (SYNTHESIS, YOSYS, VERILATOR, FORMAL, SIM*,
    RISCV_FORMAL*, RVFI_*, COCOTB*, GL_TEST, ...); `undefineall and `` token pasting are rejected;
  - `include only "name" where name is a bare file in src/;
  - no `initial` and no declaration initializers (`reg r = 0;`): the chip has no power-up values
    (contract section 1), and formal must not assume one;
  - no attributes or pragma comments that make synthesis differ from simulation: (* full_case *),
    (* parallel_case *), (* init *), (* nosync *), (* nolatches *), (* blackbox *),
    (* whitebox *), (* nomem2reg *), and // translate_off|translate_on|full_case|parallel_case;
  - no non-ASCII characters in code.

Usage: src_check.py [--src DIR]. Exit 0 when clean, 1 with one line per problem.
"""
import argparse
import os
import re
import sys

NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
VFILE_RE = re.compile(r"^[A-Za-z0-9_]+\.v$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
CONDITION_MACROS = {"RISCV_FORMAL"}
TOOL_MACROS = {"SYNTHESIS", "YOSYS", "VERILATOR", "FORMAL", "SIM", "SIMULATION", "FUNCTIONAL",
               "USE_POWER_PINS", "UNIT_DELAY", "GL_TEST", "IVERILOG", "ICARUS", "COCOTB_SIM",
               "VCS", "MODEL_TECH", "QUESTA", "XCELIUM", "SYSTEMVERILOG"}
TOOL_PREFIXES = ("RISCV_FORMAL", "RVFI_", "rvformal_", "COCOTB", "VERILATOR", "YOSYS",
                 "SYNTHESIS", "FORMAL", "SIM", "__", "SV_")
BAD_ATTRS = {"full_case", "parallel_case", "init", "nosync", "nolatches", "blackbox", "whitebox",
             "lib_whitebox", "nomem2reg"}
BAD_PRAGMA = re.compile(r"translate_off|translate_on|full_case|parallel_case", re.I)
EXEMPT_DIRS = ("__pycache__",)


def is_tool_macro(name):
    return name in TOOL_MACROS or name.startswith(TOOL_PREFIXES)


def lex(text):
    """Split Verilog text into (code, code_without_strings, comments).

    Comments become spaces (newlines kept, so offsets keep their line numbers). Escaped
    identifiers (\\foo//bar ) are copied whole, so a `//` inside one doesn't hide the rest of the
    line, exactly as the tools read it.
    """
    code, nostr, comments = [], [], []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":                                   # escaped identifier: up to whitespace
            j = i
            while j < n and not text[j].isspace():
                j += 1
            code.append(text[i:j])
            nostr.append(text[i:j])
            i = j
        elif c == '"':
            j = i + 1
            while j < n and text[j] not in '"\n':
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            code.append(text[i:j])
            nostr.append('"' + " " * max(0, j - i - 2) + '"')
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            comments.append((i, text[i:j]))
            code.append(" " * (j - i))
            nostr.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            comments.append((i, text[i:j]))
            blank = "".join("\n" if ch == "\n" else " " for ch in text[i:j])
            code.append(blank)
            nostr.append(blank)
            i = j
        else:
            code.append(c)
            nostr.append(c)
            i += 1
    return "".join(code), "".join(nostr), comments


def line_of(text, offset):
    return text.count("\n", 0, offset) + 1


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


def check_hdl(src):
    """Return errors for the preprocessor, initial-value and attribute rules."""
    errors = []
    queue = sorted(f for f in os.listdir(src) if VFILE_RE.match(f)
                   and os.path.isfile(os.path.join(src, f)) and not os.path.islink(
                       os.path.join(src, f)))
    texts = {}
    while queue:
        name = queue.pop(0)
        if name in texts:
            continue
        with open(os.path.join(src, name), encoding="utf-8", errors="replace") as f:
            texts[name] = lex(f.read())
        for m in re.finditer(r"`[ \t]*include\b[ \t]*(\S*)", texts[name][0]):
            arg = m.group(1)
            mm = re.fullmatch(r'"([^"/\\]+)"', arg)
            target = mm.group(1) if mm else None
            if (target and NAME_RE.match(target) and os.path.isfile(os.path.join(src, target))
                    and not os.path.islink(os.path.join(src, target))):
                queue.append(target)

    defined = set()
    for name, (code, _, _) in texts.items():
        for m in re.finditer(r"`[ \t]*define[ \t]+([A-Za-z_][A-Za-z0-9_$]*)", code):
            defined.add(m.group(1))

    for name, (code, nostr, comments) in sorted(texts.items()):
        where = lambda off: f"src/{name}:{line_of(code, off)}"  # noqa: E731
        for m in re.finditer(r"`[ \t]*([A-Za-z_][A-Za-z0-9_$]*)", code):
            kw = m.group(1)
            rest = code[m.end():]
            arg_m = re.match(r"[ \t]*(\S*)", rest)
            arg = arg_m.group(1) if arg_m else ""
            if kw in ("ifdef", "ifndef", "elsif"):
                if not IDENT_RE.match(arg):
                    errors.append(f"{where(m.start())}: `{kw} needs a plain macro name, "
                                  f"got {arg!r}")
                elif arg not in CONDITION_MACROS and (arg not in defined or is_tool_macro(arg)):
                    errors.append(f"{where(m.start())}: `{kw} {arg}: conditions may only test "
                                  f"RISCV_FORMAL or a macro defined in src/ (tools predefine "
                                  f"different macros)")
            elif kw in ("define", "undef"):
                name_m = re.match(r"[ \t]+([A-Za-z_][A-Za-z0-9_$]*)", rest)
                if not name_m:
                    errors.append(f"{where(m.start())}: `{kw} needs a plain macro name")
                elif is_tool_macro(name_m.group(1)):
                    errors.append(f"{where(m.start())}: `{kw} {name_m.group(1)}: tool macros "
                                  f"may not be defined or undefined in src/")
            elif kw == "undefineall":
                errors.append(f"{where(m.start())}: `undefineall is not allowed")
            elif kw == "include":
                mm = re.fullmatch(r'"([^"/\\]+)"', arg)
                target = mm.group(1) if mm else None
                if not (target and NAME_RE.match(target)
                        and os.path.isfile(os.path.join(src, target))
                        and not os.path.islink(os.path.join(src, target))):
                    errors.append(f"{where(m.start())}: `include {arg}: only \"name\" of a "
                                  f"file in src/ may be included")
        for m in re.finditer(r"``", code):
            errors.append(f"{where(m.start())}: `` token pasting is not allowed")
        for m in re.finditer(r"\binitial\b", nostr):
            errors.append(f"{where(m.start())}: `initial` is not allowed (no power-up values, "
                          f"contract section 1)")
        for m in re.finditer(r"\b(reg|integer)\b[^;]*?(?<![=!<>])=(?![=>])", nostr):
            errors.append(f"{where(m.start())}: declaration initializer (`{m.group(0)[:40]}...`)"
                          f" is a power-up value; not allowed")
        for m in re.finditer(r"\(\*(?!\))(.*?)\*\)", nostr, re.S):
            for attr in re.findall(r"(?:^|,)\s*([A-Za-z_][A-Za-z0-9_$]*)", m.group(1)):
                if attr in BAD_ATTRS:
                    errors.append(f"{where(m.start())}: attribute (* {attr} *) makes synthesis "
                                  f"differ from simulation; not allowed")
        for off, text in comments:
            if BAD_PRAGMA.search(text):
                errors.append(f"{where(off)}: pragma comment {text.strip()[:60]!r} makes "
                              f"synthesis differ from simulation; not allowed")
        for m in re.finditer(r"[^\x00-\x7f]", code):
            errors.append(f"{where(m.start())}: non-ASCII character in code")
            break
    return errors


def check(src):
    """All src hygiene problems for directory `src`, as a list of one-line strings."""
    errors = check_layout(src)
    if os.path.isdir(src) and not os.path.islink(src):
        errors += check_hdl(src)
    return errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="src/ hygiene check (see docs/eval.md)")
    ap.add_argument("--src", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src"))
    args = ap.parse_args(argv)
    errors = check(args.src)
    for e in errors:
        print("src_check: " + e)
    print(f"src_check: {'FAIL' if errors else 'PASS'} ({len(errors)} problem(s))")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
