#!/usr/bin/env python3
"""Decode NEC IR frames from a BonettoR32V pin dump (the cosim harness's --dump-pins file:
"cycle hex_uo_out" lines, then "cycle end").

    nec_decode.py PINS.txt [--pin 1] [--fcpu 50000000] [--tol 0.10]

The IR LED pin (uo[1]) carries carrier bursts: a mark is a run of carrier pulses, a space the
gap between runs. A pulse gap longer than 4 carrier periods (about 100 us) ends a mark. Each mark
lasts from its first rising edge to its last falling edge; each space from there to the next
mark. NEC (times within +-tol of nominal):
  frame:  9,000 us mark, 4,500 us space, 32 bits LSB first (562.5 us mark, then a 562.5 us space
          for 0 or 1,687.5 us for 1), a 562.5 us stop mark
  repeat: 9,000 us mark, 2,250 us space, 562.5 us mark
Prints one JSON line per frame and a summary line with the carrier (period and high time in
cycles, frequency, duty).

Each decoded frame also carries "bounds": for each of its marks and spaces in order, the range
[lo, hi] in cycles that the gate's own mark or space (the IR gate's timer output, before the
carrier) must lie in. The carrier keeps its phase, so a whole first pulse means the gate opened
while the carrier was low, at most (period - high) cycles before the first rise, and a cut one
means it opened at that rise; likewise at the end of the mark. A test can then check the gate
timing to the cycle, which the mark lengths alone (quantized by the carrier) can't show.
"""
import argparse
import json
import statistics
import sys

UNIT = 562.5


def read_dump(path):
    """[(cycle, uo)] for every change, and the last cycle."""
    rows, end = [], None
    with open(path) as f:
        for ln in f:
            a, b = ln.split()
            if b == "end":
                end = int(a)
            else:
                rows.append((int(a), int(b, 16)))
    return rows, end


def edges(rows, pin):
    """[(cycle, level)] for each change of one pin, starting with its first value."""
    out, prev = [], None
    for c, v in rows:
        lv = (v >> pin) & 1
        if lv != prev:
            out.append((c, lv))
            prev = lv
    return out


def bursts(ed, gap_cycles):
    """Marks as [first_rise, last_fall, [rise cycles], [high cycles]]."""
    marks, cur, rise = [], None, None
    for c, lv in ed:
        if lv:
            if cur is not None and c - cur[1] > gap_cycles:
                marks.append(cur)
                cur = None
            rise = c
        elif rise is not None:
            if cur is None:
                cur = [rise, c, [rise], [c - rise]]
            else:
                cur[1] = c
                cur[2].append(rise)
                cur[3].append(c - rise)
            rise = None
    if cur is not None:
        marks.append(cur)
    return marks


def gate_bounds(m, period, high):
    """(open_lo, open_hi, close_lo, close_hi): when the gate opened and closed around mark m."""
    low = period - high
    first, last = m[3][0], m[3][-1]
    o = (m[0] - low, m[0]) if first >= high else (m[0], m[0])
    c = (m[1], m[1] + low) if last >= high else (m[1], m[1])
    return o + c


def near(x, want, tol):
    return abs(x - want) <= tol * want


def decode(rows, pin=1, fcpu=50e6, tol=0.10, carrier_hz=38000):
    """(frames, carrier) from the dump rows."""
    cyc_us = fcpu / 1e6
    ed = edges(rows, pin)
    marks = bursts(ed, 4 * fcpu / carrier_hz)
    # carrier rises: a mark's first rise may be the gate opening in the middle of a pulse, the
    # others are the carrier's own
    rises = [r for m in marks for r in m[2][1:]]
    periods = [b - a for m in marks for a, b in zip(m[2][1:], m[2][2:])]
    highs = [h for m in marks for h in m[3][1:-1]]  # whole pulses (not cut by a mark edge)
    carrier = {}
    if periods and highs:
        p, h = statistics.median(periods), statistics.median(highs)
        carrier = {"period_cycles": p, "high_cycles": h, "hz": round(fcpu / p, 1),
                   "duty": round(h / p, 4), "period_min": min(periods),
                   "period_max": max(periods), "marks": len(marks),
                   # one phase through every mark: the carrier runs freely under the gate
                   "phase_ok": p == int(p) and len({(r - rises[0]) % int(p) for r in rises}) == 1}
        gb = [gate_bounds(m, p, h) for m in marks]

    def bounds(k0, n):
        """[lo, hi] for marks k0..k0+n-1 and the spaces between them."""
        out = []
        for k in range(k0, k0 + n):
            out.append([gb[k][2] - gb[k][1], gb[k][3] - gb[k][0]])
            if k + 1 < k0 + n:
                out.append([gb[k + 1][0] - gb[k][3], gb[k + 1][1] - gb[k][2]])
        return out
    # durations in us: mark k, then the space after it (None after the last)
    seq = []
    for k, m in enumerate(marks):
        mark = (m[1] - m[0]) / cyc_us
        space = (marks[k + 1][0] - m[1]) / cyc_us if k + 1 < len(marks) else None
        seq.append((m[0], mark, space))
    frames, k = [], 0
    while k < len(seq):
        start, mark, space = seq[k]
        if not near(mark, 9000, tol) or space is None:
            frames.append({"kind": "junk", "start": start, "mark_us": round(mark, 1)})
            k += 1
            continue
        if near(space, 2250, tol) and k + 1 < len(seq) and near(seq[k + 1][1], UNIT, tol):
            frames.append({"kind": "repeat", "start": start,
                           "bounds": bounds(k, 2) if carrier else None})
            k += 2
            continue
        if not near(space, 4500, tol):
            frames.append({"kind": "junk", "start": start, "space_us": round(space, 1)})
            k += 1
            continue
        data, ok, worst = 0, True, 0.0
        for i in range(32):
            if k + 1 + i >= len(seq):
                ok = False
                break
            _, bm, bs = seq[k + 1 + i]
            if bs is None or not near(bm, UNIT, tol):
                ok = False
                break
            worst = max(worst, abs(bm - UNIT) / UNIT)
            if near(bs, UNIT, tol):
                worst = max(worst, abs(bs - UNIT) / UNIT)
            elif near(bs, 3 * UNIT, tol):
                worst = max(worst, abs(bs - 3 * UNIT) / (3 * UNIT))
                data |= 1 << i
            else:
                ok = False
                break
        stop_i = k + 33
        if ok and stop_i < len(seq) and near(seq[stop_i][1], UNIT, tol):
            a, ai, c, ci = data & 0xFF, (data >> 8) & 0xFF, (data >> 16) & 0xFF, data >> 24
            frames.append({"kind": "nec", "start": start, "data": data, "address": a,
                           "address_inv_ok": ai == a ^ 0xFF, "address16": data & 0xFFFF,
                           "command": c, "command_inv_ok": ci == c ^ 0xFF,
                           "worst_timing_error": round(worst, 4),
                           "lead_mark_us": round(mark, 1), "lead_space_us": round(space, 1),
                           "bounds": bounds(k, 34) if carrier else None})
            k = stop_i + 1
        else:
            frames.append({"kind": "junk", "start": start, "at_bit": i})
            k += 1
    return frames, carrier


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dump")
    ap.add_argument("--pin", type=int, default=1)
    ap.add_argument("--fcpu", type=float, default=50e6)
    ap.add_argument("--tol", type=float, default=0.10)
    a = ap.parse_args()
    rows, _ = read_dump(a.dump)
    frames, carrier = decode(rows, a.pin, a.fcpu, a.tol)
    for fr in frames:
        print(json.dumps(fr))
    print(json.dumps({"frames": len(frames), "carrier": carrier}))
    return 0 if frames and all(f["kind"] != "junk" for f in frames) else 1


if __name__ == "__main__":
    sys.exit(main())
