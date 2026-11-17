"""Short 0DTE premium, defined risk: one credit spread or iron condor a day
at a fixed time. Hypothesis and frozen test grid:
docs/lab-credit-spreads-proposal.md.

Mechanism: the variance risk premium. On ordinary days the ATM 0DTE straddle
is priced above the move that follows (the 2026-11-16 straddle study), and
0DTE time decay is fastest in the afternoon. Wings cap the loss; the open
question is whether the premium left after buying them beats costs.

Strikes are chosen in lab/spreads.py at the signal bar from the ATM
straddle's last trades, so the signal itself needs only the clock plus an
optional day filter (frozen 2026-11-17, see the proposal's grid section):

  range    - a quiet, range-bound day so far: spot at entry is inside the
             first hour's range (09:30-10:29) and the day's high-low so far
             is at most RANGE_FRAC x the mean high-low of the prior 20
             sessions.
  iv_rich  - options priced rich against recent moves: prior-day VIX close
             is at least IV_RICH x SPY's 20-session realized volatility
             (close-to-close, annualized). Both inputs are known before
             the open.
"""
import numpy as np

from . import Signal

RANGE_FRAC = 0.75
IV_RICH = 1.25

NAME = "credit_spread"
DESCRIPTION = "One defined-risk 0DTE credit spread or iron condor a day at a fixed time."
DEFAULTS = {
    "structure": "iron_condor",   # put_credit | call_credit | iron_condor
    "entry": "13:45",             # fills at the first trades after the bar closing at this time
    "em_mult": 1.0,               # short strikes this many expected moves from spot
    "width": 2,                   # $ per side
    "credit_target": 0.5,         # take profit with half the credit kept
    "credit_stop": 2.0,           # stop at a loss of 2x the credit
    "exit_by": "15:45",
    "min_credit": 0.05,
    "filter": "none",             # none | range | iv_rich
}
_BIAS = {"put_credit": "long", "call_credit": "short", "iron_condor": "neutral"}


def signals(day, ctx, params):
    p = {**DEFAULTS, **(params or {})}
    hh, mm = map(int, p["entry"].split(":"))
    want = f"{hh:02d}:{mm - 1:02d}" if mm else f"{hh - 1:02d}:59"   # bar that closes at `entry`
    hit = day[day.index.strftime("%H:%M") == want]
    if hit.empty:
        return []
    if not _passes(p["filter"], day.loc[:hit.index[0]], ctx):
        return []
    return [Signal(hit.index[0], _BIAS[p["structure"]], exit_by=p["exit_by"],
                   structure=p["structure"], width=p["width"], em_mult=p["em_mult"],
                   credit_target=p["credit_target"], credit_stop=p["credit_stop"],
                   min_credit=p["min_credit"], tag=f"{p['structure']}_{p['entry']}_{p['filter']}")]


def _passes(name, sofar, ctx):
    if name in (None, "none"):
        return True
    prior = ctx.get("daily")
    if prior is None or len(prior) < 20:
        return False
    if name == "range":
        fh = sofar[sofar.index.strftime("%H:%M") < "10:30"]
        if len(fh) < 55:
            return False
        spot = float(sofar["close"].iloc[-1])
        inside = float(fh["low"].min()) <= spot <= float(fh["high"].max())
        avg_rng = float((prior["high"] - prior["low"]).tail(20).mean())
        rng = float(sofar["high"].max() - sofar["low"].min())
        return inside and rng <= RANGE_FRAC * avg_rng
    if name == "iv_rich":
        vix = ctx.get("vix_prev")
        if not vix:
            return False
        r = np.log(prior["close"]).diff().tail(20)
        rv = float(r.std() * np.sqrt(252) * 100)
        return rv > 0 and vix >= IV_RICH * rv
    raise ValueError(f"unknown filter {name}")
