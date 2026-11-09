"""Prior-day high/low break, continuation, filtered by session VWAP side.

Hypothesis (lab_journal 2026-11-09): stops and breakout orders cluster just
beyond the prior session's RTH high/low; a break that happens on the same
side of today's VWAP carries, one against it is a liquidity grab.

Rule, one trade per day:
- Long only if the session opened below PDH; short only if it opened above PDL.
- Between first_entry and last_entry, the first 1-min close above PDH (with
  close > session VWAP when vwap_filter) -> long call; below PDL (close <
  VWAP) -> short put.
- Underlying stop: a close back through the level by buffer_bps. Underlying
  target: target_r x (entry close - stop) from the entry close.
- ATM 0DTE, max_hold_min, hard exit at exit_by.
"""
import numpy as np

from . import Signal

NAME = "pdhl_break"
DESCRIPTION = "First break of prior-day high/low on the VWAP side; stop back through the level, 2R target."
DEFAULTS = {
    "buffer_bps": 10,
    "vwap_filter": True,
    "first_entry": "09:45",
    "last_entry": "14:30",
    "target_r": 2.0,
    "max_hold_min": 60,
    "otm": 0,
    "exit_by": "15:30",
}


def signals(day, ctx, params):
    p = {**DEFAULTS, **(params or {})}
    prev = ctx.get("prev")
    if prev is None or not len(day):
        return []
    pdh, pdl = float(prev["high"]), float(prev["low"])
    o = float(day["open"].iloc[0])
    can_long, can_short = o < pdh, o > pdl
    if not (can_long or can_short):
        return []
    typ = (day["high"].values + day["low"].values + day["close"].values) / 3.0
    vol = day["volume"].values.astype(float)
    cv = np.cumsum(vol)
    vwap = np.where(cv > 0, np.cumsum(typ * vol) / np.where(cv > 0, cv, 1), typ)
    cl = day["close"].values
    hhmm = day.index.strftime("%H:%M")
    b = p["buffer_bps"] / 1e4
    for i in range(len(day)):
        t = hhmm[i]
        if t < p["first_entry"]:
            continue
        if t > p["last_entry"]:
            break
        c = float(cl[i])
        if can_long and c > pdh and (not p["vwap_filter"] or c > vwap[i]):
            stop = pdh * (1 - b)
            return [Signal(day.index[i], "long", otm=p["otm"], u_stop=stop,
                           u_target=c + p["target_r"] * (c - stop), exit_by=p["exit_by"],
                           max_hold_min=p["max_hold_min"], tag="pdh_up")]
        if can_short and c < pdl and (not p["vwap_filter"] or c < vwap[i]):
            stop = pdl * (1 + b)
            return [Signal(day.index[i], "short", otm=p["otm"], u_stop=stop,
                           u_target=c - p["target_r"] * (stop - c), exit_by=p["exit_by"],
                           max_hold_min=p["max_hold_min"], tag="pdl_down")]
    return []
