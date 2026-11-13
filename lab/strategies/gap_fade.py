"""Fade a large opening gap that the first five minutes don't extend.

Hypothesis (lab_journal 2026-11-13): overnight moves happen on thin
liquidity and are partly inventory; when the opening minutes fail to push
the gap further, RTH participants retrace it toward the prior close.

Rule, one trade per day:
- gap = session open / prior RTH close - 1; trade only if |gap| >= gap_bps.
- Signal at the close of the `signal_bar` bar (09:34, closes 09:35), only if
  that close has not extended the gap (gap up: close <= open; gap down:
  close >= open).
- Underlying target = prior close + (1 - fill) * (open - prior close), i.e.
  `fill` of the gap retraced. The close must still be >= min_r_bps of price
  on the gap side of the target.
- Fade it: gap up -> put, gap down -> call. ATM 0DTE. Stop the same
  distance beyond the entry (1R:1R), hard exit at exit_by.
"""
from . import Signal

NAME = "gap_fade"
DESCRIPTION = "|gap| >= 30bp and 09:34 close not beyond the open: fade toward 50% fill, 1:1 stop, out by 11:00."
DEFAULTS = {
    "gap_bps": 30,
    "signal_bar": "09:34",
    "fill": 0.5,
    "min_r_bps": 5,
    "otm": 0,
    "exit_by": "11:00",
}


def signals(day, ctx, params):
    p = {**DEFAULTS, **(params or {})}
    prev = ctx.get("prev") if ctx else None
    if prev is None or not len(day):
        return []
    hhmm = day.index.strftime("%H:%M")
    if hhmm[0] != "09:30":
        return []
    hit = [i for i, t in enumerate(hhmm) if t == p["signal_bar"]]
    if not hit:
        return []
    i = hit[0]
    pc = float(prev["close"])
    o = float(day["open"].iloc[0])
    gap = o / pc - 1
    if abs(gap) * 1e4 < p["gap_bps"]:
        return []
    c = float(day["close"].iloc[i])
    target = pc + (1 - p["fill"]) * (o - pc)
    meta = {"gap_bps": round(gap * 1e4, 1)}
    if gap > 0:
        if c > o or (c - target) / c * 1e4 < p["min_r_bps"]:
            return []
        return [Signal(day.index[i], "short", otm=p["otm"], u_target=target, u_stop=c + (c - target),
                       exit_by=p["exit_by"], tag="gf_down", meta=meta)]
    if c < o or (target - c) / c * 1e4 < p["min_r_bps"]:
        return []
    return [Signal(day.index[i], "long", otm=p["otm"], u_target=target, u_stop=c - (target - c),
                   exit_by=p["exit_by"], tag="gf_up", meta=meta)]
