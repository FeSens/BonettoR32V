#!/usr/bin/env python3
"""Patch a copy of verif/programs so no program touches the reserved region 0xFF80..0xFFFF
(variant 2: registers at 0xFFC0, the pc in the x0 slot, scratch at 0xFF80).

Usage: patch_progs.py <copied_programs_dir>
Each patch must apply exactly once, so a change upstream fails loudly here.
"""
import sys
from pathlib import Path

d = Path(sys.argv[1])
BASE = 0xFF80


def patch(name, old, new):
    p = d / name
    s = p.read_text()
    if s.count(old) != 1:
        sys.exit(f"patch_progs: {name}: expected exactly one {old!r}")
    p.write_text(s.replace(old, new))


# Stack below the reserved region.
patch("link.ld", "_stack_top = RAM_BASE + 0x10000;", "_stack_top = RAM_BASE + %#x;" % BASE)
# Section 6 of selftest wraps through 0xFFFC..0xFFFF: leave it out (the first SPIKE_XCHECK guard).
patch("selftest.S", "#ifndef SPIKE_XCHECK\n# ----------------------------------------"
      "-----------------------------------------------------\n# 6. Misaligned",
      "#if 0\n# ----------------------------------------"
      "-----------------------------------------------------\n# 6. Misaligned")
# Random streams: RAM loads and stores never reach the region (and so never wrap at 64 KB).
patch("gen_random.py",
      "        lo = ea & 0xFFFF\n        return not store or WIN_LO <= lo <= WIN_HI - (n - 1) or lo >= TOP_LO",
      ("        lo = ea & 0xFFFF\n        if lo + n - 1 >= %#x:\n            return False\n"
       "        return not store or WIN_LO <= lo <= WIN_HI - (n - 1)") % BASE)
patch("gen_random.py",
      "        elif n > 1 and r.random() < 0.7:\n            lo = 0x10000 - r.randint(1, n - 1)  # crosses the 64 KB wrap\n"
      "        else:\n            lo = r.randint(TOP_LO, 0xFFFF)",
      ("        elif store:\n            lo = r.randint(WIN_LO, WIN_HI - (n - 1))\n"
       "        else:\n            lo = r.randint(0, %#x - n)") % BASE)
print("patched", d)
