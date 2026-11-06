"""First pullback after an opening drive — continuation.

Hypothesis (lab_journal 2026-10-29): a strong first 15 minutes reflects
overnight order imbalance worked through the morning; the first shallow
pullback that holds resumes in the drive's direction.

Rule, one trade per day:
- Drive: open of the first bar to close of bar `drive_minutes` >= drive_bps.
  Its sign is the direction.
- Pullback: after the drive window, retrace from the running drive extreme
  of at least retrace_min of (extreme - open); more than retrace_max kills
  the day. A new extreme before the trigger restarts the measurement.
- Trigger: first close beyond the previous bar's high (long) / low (short)
  once the pullback is deep enough, no later than last_entry.
- ATM 0DTE; underlying stop at the pullback extreme; underlying target at
  target_r x that risk from the trigger close; time exit at exit_by.
"""
from . import Signal

NAME = "drive_pullback"
DESCRIPTION = "Opening drive (15m) then first 35-70% pullback; enter on resumption, 1.5R underlying target."
DEFAULTS = {
    "drive_minutes": 15,
    "drive_bps": 15,
    "retrace_min": 0.35,
    "retrace_max": 0.70,
    "last_entry": "10:30",
    "target_r": 1.5,
    "otm": 0,
    "exit_by": "12:00",
}


def signals(day, ctx, params):
    p = {**DEFAULTS, **(params or {})}
    n = p["drive_minutes"]
    if len(day) <= n:
        return []
    o = float(day["open"].iloc[0])
    c = float(day["close"].iloc[n - 1])
    move_bps = (c - o) / o * 1e4
    if abs(move_bps) < p["drive_bps"]:
        return []
    s = 1 if move_bps > 0 else -1          # +1 long, -1 short; mirror prices for shorts
    hi = day["high"].values
    lo = day["low"].values
    cl = day["close"].values
    hhmm = day.index.strftime("%H:%M")

    def fav(i):    # favourable extreme of bar i in drive direction
        return hi[i] if s > 0 else lo[i]

    def adv(i):    # adverse extreme of bar i
        return lo[i] if s > 0 else hi[i]

    ext = max(fav(i) * s for i in range(n)) * s
    pb = None
    armed = False
    for i in range(n, len(day)):
        if hhmm[i] > p["last_entry"]:
            return []
        if fav(i) * s > ext * s:
            ext, pb, armed = fav(i), None, False
            continue
        pb = adv(i) if pb is None else (min(pb, adv(i)) if s > 0 else max(pb, adv(i)))
        span = (ext - o) * s
        if span <= 0:
            return []
        depth = (ext - pb) * s / span
        if depth > p["retrace_max"]:
            return []
        if depth >= p["retrace_min"]:
            armed = True
        prev_fav = fav(i - 1)
        if armed and cl[i] * s > prev_fav * s:
            entry = float(cl[i])
            risk = abs(entry - pb)
            if risk <= 0:
                continue
            target = entry + s * p["target_r"] * risk
            return [Signal(day.index[i], "long" if s > 0 else "short", otm=p["otm"],
                           u_stop=float(pb), u_target=float(target), exit_by=p["exit_by"],
                           tag="drive_pb_up" if s > 0 else "drive_pb_down",
                           meta={"drive_bps": round(move_bps, 1), "depth": round(depth, 3)})]
    return []
