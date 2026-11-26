"""Scale-out and runner exits, shared by backtest.py and runner.py.

A trade with `scale_frac` set and more than one contract takes partial
profit at its first target (the premium target or the underlying target,
whichever comes first) and keeps the rest as a runner:

    5 contracts, scale_frac 0.6  ->  sell 3 at the target, run 2

The runner then exits on whichever comes first:
  * its stop, moved to the entry price after the scale when `runner_be`
  * `trail_pct`: premium falls that fraction below its high since the scale
  * `trail_ema` [fast, slow, minutes]: the fast EMA crosses the slow one
    against the position on a closed bar of that many minutes
    (e.g. [9, 21, 5], the trader's "leave trailers until the 5-minute EMAs
    cross")
  * the trade's own u_stop, exit_by, max_hold, and the runner's halts

Strategies can set these on the Signal, or they can be given to any
strategy through its params (`apply_overrides`), so a scale-out rule can be
tried on an existing module without editing it.
"""
import pandas as pd

RUNNER_KEYS = ("scale_frac", "trail_pct", "trail_ema", "runner_be")


def apply_overrides(sig, params):
    """Fill any runner field the strategy left unset from its params."""
    for k in RUNNER_KEYS:
        if params and k in params and params[k] is not None:
            if k == "runner_be" or getattr(sig, k) is None:
                setattr(sig, k, params[k])
    return sig


def scale_qty(contracts, frac):
    """Contracts to sell at the first target. Always leaves at least one
    runner and sells at least one, so it's 0 (no scale, exit all at the
    target as before) for a single contract or no rule."""
    if not frac or contracts < 2:
        return 0
    return max(1, min(contracts - 1, int(round(contracts * float(frac)))))


def ema_cross_against(day, direction, trail_ema):
    """True when, on the last *closed* bar of `minutes` length, the fast EMA
    is on the wrong side of the slow one for the position: below it for a
    long, above it for a short.

    `day` is the session's 1-minute bars so far (index = bar open time). A
    `minutes` bucket counts as closed only once its last minute is in `day`,
    so a half-formed bar never decides an exit — the same rule in the
    backtest (called on each prefix) and the runner.
    """
    if not trail_ema or not len(day):
        return False
    fast, slow, minutes = (list(trail_ema) + [5])[:3]
    minutes = int(minutes)
    closes = day["close"].resample(f"{minutes}min", label="left", closed="left").last().dropna()
    if not len(closes):
        return False
    # drop the last bucket if its final minute hasn't printed yet
    if day.index[-1] < closes.index[-1] + pd.Timedelta(minutes=minutes - 1):
        closes = closes.iloc[:-1]
    if len(closes) < 2:
        return False
    f = closes.ewm(span=int(fast), adjust=False).mean().iloc[-1]
    s = closes.ewm(span=int(slow), adjust=False).mean().iloc[-1]
    return bool(f < s) if direction == "long" else bool(f > s)


def ema_against_series(day, direction, trail_ema):
    """`ema_cross_against(day.loc[:ts], ...)` for every minute ts at once, for
    the backtest's per-bar loop. scale_test checks the two agree."""
    if not trail_ema or not len(day):
        return pd.Series(False, index=day.index)
    fast, slow, minutes = (list(trail_ema) + [5])[:3]
    minutes = int(minutes)
    closes = day["close"].resample(f"{minutes}min", label="left", closed="left").last().dropna()
    f = closes.ewm(span=int(fast), adjust=False).mean()
    s = closes.ewm(span=int(slow), adjust=False).mean()
    against = (f < s) if direction == "long" else (f > s)
    against.iloc[:1] = False            # needs two closed bars
    # a bucket is known from its last minute on
    against.index = against.index + pd.Timedelta(minutes=minutes - 1)
    out = against.reindex(against.index.union(day.index)).ffill().reindex(day.index)
    return out.fillna(False).astype(bool)
