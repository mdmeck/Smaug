"""How a Lab result is judged: locked periods, a matched random control, try
counting that raises the bar, and one scoreboard record shape.

From the 2026-11-11 Lab plan and the guardrails at the end of
`docs/strategy-break-retest.md`. Opt-in for now: the nightly Lab routine
(Track B) still uses backtest.run/stats and its own prompt's rules, and
nothing here changes what it does. The strategy track (Track A) goes through
`evaluate()` and `lab/playbook.py`.

Periods (the only place they live):
  discovery  2016-01-01 -> 2023-12-31  stock bars only, unlimited looks
  develop    2024-02-15 -> 2026-02-24  real option bars, every run counted
  holdout    2026-02-25 -> 2026-11-10  sealed: one look per family, logged
  forward    2026-11-11 ->             shadowed nightly

The one metric is edge per trade = strategy avg P/L minus a matched random
control. Two controls:
  direction  same entry minute, random side, same exits and costs. With two
             sides this has an exact expectation (the mean of the two), so no
             draws are needed: edge = (P/L - P/L of the opposite side) / 2.
  timing     the same number of entries on the same days at minutes drawn
             from the strategy's own entry-time distribution, random side,
             the same risk distance and exits. `draws` draws, fixed seed.

The pass bar rises with tries: the edge's lower confidence bound must clear
zero at one-sided 1 - 0.10/k, k = tries in the family. Tries are counted from
the scoreboard (`evals`), so they can't be typed in by hand.

Scoreboard: the `evals` table (webapp/supabase/schema.sql) when SUPABASE_URL
and SUPABASE_SERVICE_ROLE_KEY are set, else a JSONL file at LAB_EVALS
(default lab/.cache/evals.jsonl).
"""
import hashlib
import json
import os
import random
import subprocess
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest, data
from .alpaca import NY

PERIODS = {
    "discovery": ("2016-01-01", "2023-12-31"),
    "develop": ("2024-02-15", "2026-02-24"),
    "holdout": ("2026-02-25", "2026-11-10"),
    "forward": ("2026-11-11", None),
}
BASE_ALPHA = 0.10
EVALS_FILE = Path(os.environ.get("LAB_EVALS", Path(__file__).parent / ".cache" / "evals.jsonl"))


class HoldoutError(RuntimeError):
    pass


def period_range(name):
    s, e = PERIODS[name]
    e = e or str((pd.Timestamp.now(tz=NY) - pd.Timedelta(days=1)).date())
    return s, e


def touches_holdout(start, end):
    hs, he = (pd.Timestamp(x).date() for x in PERIODS["holdout"])
    s, e = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    return s <= he and e >= hs


def check_window(start, end, period):
    """A run may only touch the holdout when it is the holdout look."""
    if period != "holdout" and touches_holdout(start, end):
        raise HoldoutError(f"{start}..{end} reaches into the sealed holdout {PERIODS['holdout']}; "
                           "only evaluate(..., period='holdout') may look, once per family")


def required_confidence(k):
    """One-sided confidence the edge's lower bound must clear zero at."""
    return 1 - BASE_ALPHA / max(1, int(k))


def params_hash(params):
    return hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:12]


def git_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=Path(__file__).parent, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- scoreboard

def _use_supabase():
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))


def load_evals(family=None):
    if _use_supabase():
        from . import supa
        q = {"select": "*", "experiment": "eq.lab"}
        if family:
            q["family"] = f"eq.{family}"
        return supa.select("evals", **q) or []
    if not EVALS_FILE.exists():
        return []
    rows = [json.loads(x) for x in EVALS_FILE.read_text().splitlines() if x.strip()]
    return [r for r in rows if family is None or r.get("family") == family]


def record(row):
    """Append one scoreboard row (append-only, like analysis_runs)."""
    row = {"experiment": "lab", "git_sha": git_sha(), **row}
    if _use_supabase():
        from . import supa
        return supa.insert("evals", row)
    EVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with EVALS_FILE.open("a") as f:
        f.write(json.dumps({**row, "created_at": pd.Timestamp.now(tz="UTC").isoformat()}, default=str) + "\n")
    return row


def tries(family):
    """Distinct (params, period) combinations the family has been run on.
    Re-running the same configuration on the same period isn't a new try."""
    return len({(r.get("params_hash"), r.get("period")) for r in load_evals(family)})


def holdout_spent(family):
    return any(r.get("period") == "holdout" for r in load_evals(family))


# ---------------------------------------------------------------- statistics

def boot_mean_ci(values, groups, conf=0.90, n=2000, seed=7):
    """Two-sided `conf` bootstrap CI of the mean, resampling whole groups
    (days), so same-day trades aren't counted as independent."""
    v = np.asarray(values, float)
    g = np.asarray(groups)
    keys = list(dict.fromkeys(g.tolist()))
    if len(keys) < 5:
        return None
    by = [v[g == k] for k in keys]
    rng = np.random.default_rng(seed)
    means = np.empty(n)
    for b in range(n):
        pick = rng.integers(0, len(by), len(by))
        means[b] = np.concatenate([by[i] for i in pick]).mean()
    a = (1 - conf) / 2
    return [float(np.quantile(means, a)), float(np.quantile(means, 1 - a))]


def lower_bound(values, groups, k):
    """The edge's one-sided lower bound at required_confidence(k)."""
    conf = required_confidence(k)
    ci = boot_mean_ci(values, groups, conf=2 * conf - 1)
    return None if ci is None else ci[0]


def z_vs_half(hits):
    """P(+1R before -1R) against a coin. hits: +1 / -1 (0 = neither, dropped)."""
    h = np.asarray([x for x in hits if x != 0])
    if not len(h):
        return {"n": 0}
    p = float((h > 0).mean())
    z = (p - 0.5) / np.sqrt(0.25 / len(h))
    return {"n": int(len(h)), "p": round(p, 4), "z": round(float(z), 2)}


# ---------------------------------------------------------------- option-priced evaluation

def _mirror(sig, day):
    """The same trade on the other side: same minute, the stop and target the
    same distance from the entry close, on the other side of it."""
    c = float(day.loc[sig.ts, "close"])
    d = "short" if sig.direction == "long" else "long"
    ms = None if sig.u_stop is None else 2 * c - sig.u_stop
    mt = None if sig.u_target is None else 2 * c - sig.u_target
    return replace(sig, direction=d, u_stop=ms, u_target=mt, tag=sig.tag + "~mirror")


def _moved(sig, day, ts, direction):
    """sig's exits, as distances from the entry close, re-applied at ts."""
    c0 = float(day.loc[sig.ts, "close"])
    c1 = float(day.loc[ts, "close"])
    flip = 1 if direction == sig.direction else -1

    def lvl(x):
        return None if x is None else c1 + flip * (x - c0)
    return replace(sig, ts=ts, direction=direction, u_stop=lvl(sig.u_stop), u_target=lvl(sig.u_target),
                   tag="random")


def evaluate(strategy, params=None, period="develop", symbol="SPY", pricing="real", family=None,
             draws=20, seed=7, start=None, end=None, write=True, market=None):
    """One standard scoreboard record for a strategy on one locked period,
    priced on option bars, against both random controls."""
    family = family or strategy.NAME
    params = {**strategy.DEFAULTS, **(params or {})}
    ps, pe = period_range(period)
    start, end = start or ps, end or pe
    if period == "holdout":
        if holdout_spent(family):
            raise HoldoutError(f"family {family!r} has already used its one holdout look")
    else:
        check_window(start, end, period)

    if market is None:
        market = backtest.load_market(symbol, start, end, getattr(strategy, "NEEDS", None))
    minutes = market["rth"]
    daily = backtest.daily_bars(minutes)
    vix = data.vix_daily(pd.Timestamp(start) - pd.Timedelta(days=10), end)
    days = data.sessions(minutes)
    first = np.searchsorted(daily.index, pd.Timestamp(start))
    last = np.searchsorted(daily.index, pd.Timestamp(end), side="right")

    rows, taken = [], []
    for i in range(first, last):
        d = daily.index[i].date()
        day = days[d]
        if len(day) < 300:
            continue
        ctx = backtest.session_context(daily, i, vix, market)
        busy = None
        for s in sorted(strategy.signals(day, ctx, params), key=lambda x: x.ts):
            if busy is not None and s.ts <= busy:
                continue
            t = backtest.simulate(s, day, ctx, pricing=pricing)
            if not t.filled:
                continue
            busy = t.exit_ts
            m = backtest.simulate(_mirror(s, day), day, ctx, pricing=pricing)
            rows.append({"date": d, "pnl": t.pnl, "mirror_pnl": m.pnl if m.filled else np.nan,
                         "entry_min": s.ts.hour * 60 + s.ts.minute, "priced": t.priced})
            taken.append((i, s))

    # timing control: same days, entry minutes from the strategy's own distribution
    rng = random.Random(seed)
    mins = [r["entry_min"] for r in rows]
    timing = []
    for i, s in taken:
        d = daily.index[i].date()
        day = days[d]
        ctx = backtest.session_context(daily, i, vix, market)
        for _ in range(draws):
            m = rng.choice(mins)
            ts = pd.Timestamp(f"{d} {m // 60:02d}:{m % 60:02d}", tz=NY)
            if ts not in day.index:
                continue
            t = backtest.simulate(_moved(s, day, ts, rng.choice(["long", "short"])), day, ctx, pricing=pricing)
            if t.filled:
                timing.append({"date": d, "pnl": t.pnl})

    df = pd.DataFrame(rows)
    k = tries(family) + 1
    out = {"family": family, "strategy": strategy.NAME, "module": strategy.__name__.split(".")[-1],
           "symbol": symbol, "params": params, "params_hash": params_hash(params), "period": period,
           "pricing": pricing, "start": str(start), "end": str(end), "variants_in_family": k,
           "required_confidence": round(required_confidence(k), 4)}
    if df.empty:
        out.update({"n": 0, "verdict": "no trades"})
    else:
        both = df.dropna(subset=["mirror_pnl"])
        edge = 0.5 * (both.pnl - both.mirror_pnl)
        ci = boot_mean_ci(df.pnl, df.date)
        eci = boot_mean_ci(edge, both.date)
        lb = lower_bound(edge, both.date, k)
        tdf = pd.DataFrame(timing)
        wins, losses = df[df.pnl > 0].pnl.sum(), -df[df.pnl <= 0].pnl.sum()
        out.update({
            "n": int(len(df)), "avg_pnl": round(float(df.pnl.mean()), 2),
            "ci90_lo": None if ci is None else round(ci[0], 2), "ci90_hi": None if ci is None else round(ci[1], 2),
            "pf": round(float(wins / losses), 2) if losses > 0 else None,
            "win_rate": round(float((df.pnl > 0).mean()), 3),
            "baseline_avg_pnl": round(float(0.5 * (both.pnl + both.mirror_pnl).mean()), 2) if len(both) else None,
            "edge": round(float(edge.mean()), 2) if len(both) else None,
            "edge_ci90_lo": None if eci is None else round(eci[0], 2),
            "edge_ci90_hi": None if eci is None else round(eci[1], 2),
            "detail": {"edge_lower_bound_at_required": None if lb is None else round(lb, 2),
                       "timing_control_avg_pnl": round(float(tdf.pnl.mean()), 2) if len(tdf) else None,
                       "timing_draws": len(tdf),
                       "edge_vs_timing": round(float(df.pnl.mean() - tdf.pnl.mean()), 2) if len(tdf) else None,
                       "priced": df.priced.value_counts().to_dict(),
                       "by_year": {str(y): {"n": int(len(g)), "edge": round(float(0.5 * (g.pnl - g.mirror_pnl).mean()), 2)}
                                   for y, g in both.groupby(pd.to_datetime(both.date).dt.year)}},
        })
        out["verdict"] = "pass" if (lb is not None and lb > 0) else "fail"
    if write:
        record(out)
    return out
