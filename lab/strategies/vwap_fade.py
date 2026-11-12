"""Fade a stretch away from session VWAP in a flat session.

Hypothesis (lab_journal 2026-11-12): on a day with no net direction, a move
far from VWAP is a temporary imbalance that inventory-driven liquidity
leans against, so price returns to VWAP before extending the same distance.

Rule, one trade per day:
- From first_entry to last_entry, the first close where
  |close / open - 1| < flat_bps (running, causal flat-day filter), and
  |close - VWAP| >= k * running session range and >= min_ext_bps of price.
- Fade it: above VWAP -> put, below -> call. ATM 0DTE.
- Underlying target = VWAP at the signal bar; stop the same distance beyond
  the entry (1R:1R). Max hold max_hold_min, hard exit at exit_by.
"""
import numpy as np

from . import Signal

NAME = "vwap_fade"
DESCRIPTION = "Flat day (|ret| < 20bp): first 10:30-14:30 close >= 0.4x range and 10bp from VWAP; fade to VWAP, 1:1 stop."
DEFAULTS = {
    "flat_bps": 20,
    "k": 0.4,
    "min_ext_bps": 10,
    "first_entry": "10:30",
    "last_entry": "14:30",
    "otm": 0,
    "max_hold_min": 60,
    "exit_by": "15:30",
}


def signals(day, ctx, params):
    p = {**DEFAULTS, **(params or {})}
    if not len(day):
        return []
    hhmm = day.index.strftime("%H:%M")
    if hhmm[-1] < p["first_entry"]:
        return []
    o = float(day["open"].iloc[0])
    hi = np.maximum.accumulate(day["high"].values.astype(float))
    lo = np.minimum.accumulate(day["low"].values.astype(float))
    typ = (day["high"].values + day["low"].values + day["close"].values) / 3.0
    vol = day["volume"].values.astype(float)
    cv = np.cumsum(vol)
    vwap = np.where(cv > 0, np.cumsum(typ * vol) / np.where(cv > 0, cv, 1), typ)
    cl = day["close"].values.astype(float)
    for i in range(len(day)):
        t = hhmm[i]
        if t < p["first_entry"]:
            continue
        if t > p["last_entry"]:
            break
        c = cl[i]
        if abs(c / o - 1) * 1e4 >= p["flat_bps"]:
            continue
        ext = c - vwap[i]
        if abs(ext) < p["k"] * (hi[i] - lo[i]) or abs(ext) / c * 1e4 < p["min_ext_bps"]:
            continue
        v = float(vwap[i])
        meta = {"ext_bps": round(ext / c * 1e4, 1), "range_bps": round((hi[i] - lo[i]) / c * 1e4, 1)}
        if ext > 0:
            return [Signal(day.index[i], "short", otm=p["otm"], u_target=v, u_stop=c + ext,
                           max_hold_min=p["max_hold_min"], exit_by=p["exit_by"], tag="vf_down", meta=meta)]
        return [Signal(day.index[i], "long", otm=p["otm"], u_target=v, u_stop=c + ext,
                       max_hold_min=p["max_hold_min"], exit_by=p["exit_by"], tag="vf_up", meta=meta)]
    return []
