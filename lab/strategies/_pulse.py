"""Test-only (underscore = hidden from research and never promoted): fires
every `every` minutes, alternating direction, so lab.replay_test exercises
repeated entries, max-hold exits and the daily trade limit."""
from . import Signal

NAME = "_pulse"
DESCRIPTION = "replay-test fixture"
DEFAULTS = {"every": 20, "max_hold_min": 7, "target_pct": 0.15, "stop_pct": 0.15}


def signals(day, ctx, params):
    p = {**DEFAULTS, **(params or {})}
    out = []
    for i, ts in enumerate(day.index):
        if i and i % p["every"] == 0:
            out.append(Signal(ts, "long" if (i // p["every"]) % 2 else "short", stop_pct=p["stop_pct"],
                              target_pct=p["target_pct"], max_hold_min=p["max_hold_min"], tag="pulse"))
    return out
