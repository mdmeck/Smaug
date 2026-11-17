"""Short 0DTE premium, defined risk: one credit spread or iron condor a day
at a fixed time. Hypothesis and frozen test grid:
docs/lab-credit-spreads-proposal.md.

Mechanism: the variance risk premium. On ordinary days the ATM 0DTE straddle
is priced above the move that follows (the 2026-11-16 straddle study), and
0DTE time decay is fastest in the afternoon. Wings cap the loss; the open
question is whether the premium left after buying them beats costs.

Strikes are chosen in lab/spreads.py at the signal bar from the ATM
straddle's last trades, so the signal itself needs only the clock.
"""
from . import Signal

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
}
_BIAS = {"put_credit": "long", "call_credit": "short", "iron_condor": "neutral"}


def signals(day, ctx, params):
    p = {**DEFAULTS, **(params or {})}
    hh, mm = map(int, p["entry"].split(":"))
    want = f"{hh:02d}:{mm - 1:02d}" if mm else f"{hh - 1:02d}:59"   # bar that closes at `entry`
    hit = day[day.index.strftime("%H:%M") == want]
    if hit.empty:
        return []
    return [Signal(hit.index[0], _BIAS[p["structure"]], exit_by=p["exit_by"],
                   structure=p["structure"], width=p["width"], em_mult=p["em_mult"],
                   credit_target=p["credit_target"], credit_stop=p["credit_stop"],
                   min_credit=p["min_credit"], tag=f"{p['structure']}_{p['entry']}")]
