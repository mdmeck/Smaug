"""Afternoon break of the lunch range, with the day's trend, into the close.

Hypothesis (lab_journal 2026-11-10): 0DTE gamma is largest in the last two
hours; on a day that has already trended, short-gamma dealer hedging should
extend a break out of the quiet 12:30-13:59 range in the trend direction.

Rule, one trade per day:
- Trend: close of the 13:59 bar vs the session open >= trend_bps; its sign
  is the direction.
- Lunch range: high/low of the 12:30-13:59 bars.
- From 14:00 to last_entry, the first 1-min close beyond the lunch range in
  the trend direction that is also on the trend side of session VWAP.
- ATM 0DTE. Underlying stop at the lunch-range midpoint, target target_r x R
  from the entry close, hard exit at exit_by.
"""
import numpy as np

from . import Signal

NAME = "afternoon_break"
DESCRIPTION = "Trend day (>=20bp by 13:59): first 14:00-15:00 close beyond the lunch range on the VWAP side; stop at range mid, 1.5R."
DEFAULTS = {
    "trend_bps": 20,
    "range_start": "12:30",
    "range_end": "13:59",
    "first_entry": "14:00",
    "last_entry": "15:00",
    "target_r": 1.5,
    "otm": 0,
    "exit_by": "15:45",
}


def signals(day, ctx, params):
    p = {**DEFAULTS, **(params or {})}
    if not len(day):
        return []
    hhmm = day.index.strftime("%H:%M")
    if hhmm[-1] < p["first_entry"]:
        return []
    rng = (hhmm >= p["range_start"]) & (hhmm <= p["range_end"])
    end_idx = np.where(hhmm == p["range_end"])[0]
    if not rng.any() or not len(end_idx):
        return []
    o = float(day["open"].iloc[0])
    c_end = float(day["close"].iloc[end_idx[0]])
    trend = (c_end / o - 1) * 1e4
    if abs(trend) < p["trend_bps"]:
        return []
    direction = "long" if trend > 0 else "short"
    hi = float(day["high"].values[rng].max())
    lo = float(day["low"].values[rng].min())
    mid = (hi + lo) / 2.0
    typ = (day["high"].values + day["low"].values + day["close"].values) / 3.0
    vol = day["volume"].values.astype(float)
    cv = np.cumsum(vol)
    vwap = np.where(cv > 0, np.cumsum(typ * vol) / np.where(cv > 0, cv, 1), typ)
    cl = day["close"].values
    for i in range(len(day)):
        t = hhmm[i]
        if t < p["first_entry"]:
            continue
        if t > p["last_entry"]:
            break
        c = float(cl[i])
        if direction == "long" and c > hi and c > vwap[i]:
            return [Signal(day.index[i], "long", otm=p["otm"], u_stop=mid,
                           u_target=c + p["target_r"] * (c - mid), exit_by=p["exit_by"],
                           tag="aft_up", meta={"trend_bps": round(trend, 1)})]
        if direction == "short" and c < lo and c < vwap[i]:
            return [Signal(day.index[i], "short", otm=p["otm"], u_stop=mid,
                           u_target=c - p["target_r"] * (mid - c), exit_by=p["exit_by"],
                           tag="aft_down", meta={"trend_bps": round(trend, 1)})]
    return []
