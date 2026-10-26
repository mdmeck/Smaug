"""Opening-range breakout — the plumbing baseline, not a claimed edge.

Long a call on the first close above the opening-range high (short a put on
the first close below the low), with the opposite side of the range as the
underlying stop and a premium target. One trade per day.
"""
from . import Signal

NAME = "orb"
DESCRIPTION = "Opening-range breakout baseline (first close outside the N-minute range)."
DEFAULTS = {
    "or_minutes": 15,
    "otm": 0,
    "target_pct": 0.5,
    "stop_pct": 0.35,
    "last_entry": "11:30",
    "exit_by": "15:30",
}


def signals(day, ctx, params):
    p = {**DEFAULTS, **(params or {})}
    if len(day) <= p["or_minutes"]:
        return []
    rng = day.iloc[:p["or_minutes"]]
    hi, lo = rng["high"].max(), rng["low"].min()
    after = day.iloc[p["or_minutes"]:]
    after = after[after.index.strftime("%H:%M") <= p["last_entry"]]
    for ts, b in after.iterrows():
        if b["close"] > hi:
            return [Signal(ts, "long", otm=p["otm"], stop_pct=p["stop_pct"], target_pct=p["target_pct"],
                           u_stop=lo, exit_by=p["exit_by"], tag="orb_up")]
        if b["close"] < lo:
            return [Signal(ts, "short", otm=p["otm"], stop_pct=p["stop_pct"], target_pct=p["target_pct"],
                           u_stop=hi, exit_by=p["exit_by"], tag="orb_down")]
    return []
