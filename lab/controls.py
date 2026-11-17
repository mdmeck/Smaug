"""Matched random control and edge, from the 2026-11-11 Lab plan.

Edge = strategy avg P/L per trade - control avg P/L per trade, where the
control trades the same days, the same number of times, with the strategy's
own signal settings (exits, structure, strikes rule) but at a random minute
inside `window` — and, for single-leg trades, a random side. It pays the
same costs through the same simulate(), so what's left is what the rule's
choices add over trading at random.

The pass bar rises with tries: the edge CI's lower bound must clear zero at
one-sided confidence 1 - 0.10 / k, k = variants tried in the family.
"""
import dataclasses

import numpy as np
import pandas as pd

from . import backtest, data


def random_control(strategy, params, trades, minutes, draws=20, seed=7, window=("09:45", "14:30"),
                   vix=None, pricing="auto"):
    f = trades[trades["filled"]] if len(trades) else trades
    if not len(f):
        return pd.DataFrame()
    per_day = f.groupby("date").size().to_dict()
    daily = backtest.daily_bars(minutes)
    days = data.sessions(minutes)
    rng = np.random.default_rng(seed)
    out = []
    for i in range(len(daily)):
        d = daily.index[i].date()
        if d not in per_day:
            continue
        day = days[d]
        ctx = backtest.session_context(daily, i, vix)
        sigs = sorted(strategy.signals(day, ctx, params), key=lambda s: s.ts)
        if not sigs:
            continue
        hhmm = day.index.strftime("%H:%M")
        pool = day.index[(hhmm >= window[0]) & (hhmm <= window[1])]
        if not len(pool):
            continue
        for k in range(draws):
            for s in sigs[:per_day[d]]:
                ts = pool[rng.integers(0, len(pool))]
                kw = {"ts": ts}
                if s.structure == "single":
                    kw["direction"] = "long" if rng.random() < 0.5 else "short"
                tr = backtest.simulate(dataclasses.replace(s, **kw), day, ctx, pricing=pricing)
                row = dict(tr.__dict__)
                row["draw"] = k
                out.append(row)
    return pd.DataFrame(out)


def edge(trades, control, tries=1, n=2000, seed=7):
    """Paired by day: resample whole days, compare the strategy's mean trade
    P/L with the control's on the same days."""
    s = trades[trades["filled"]]
    c = control[control["filled"]] if len(control) else control
    if not len(s) or not len(c):
        return {"n": int(len(s)), "control_n": int(len(c)), "edge": None}
    sd = s.groupby("date").pnl.agg(["sum", "count"])
    cd = c.groupby("date").pnl.agg(["sum", "count"])
    dates = sorted(set(sd.index) & set(cd.index))
    S, SN = sd.loc[dates, "sum"].values, sd.loc[dates, "count"].values
    C, CN = cd.loc[dates, "sum"].values, cd.loc[dates, "count"].values
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        pick = rng.integers(0, len(dates), len(dates))
        vals.append(S[pick].sum() / SN[pick].sum() - C[pick].sum() / CN[pick].sum())
    alpha = 0.10 / max(1, int(tries))
    e = float(S.sum() / SN.sum() - C.sum() / CN.sum())
    lb = float(np.percentile(vals, 100 * alpha))
    return {"n": int(SN.sum()), "control_n": int(CN.sum()), "days": len(dates),
            "strategy_avg": round(float(S.sum() / SN.sum()), 2),
            "control_avg": round(float(C.sum() / CN.sum()), 2),
            "edge": round(e, 2),
            "edge_ci90": [round(float(np.percentile(vals, 5)), 2), round(float(np.percentile(vals, 95)), 2)],
            "tries": int(tries), "one_sided_conf": round(1 - alpha, 4),
            "edge_lower_bound": round(lb, 2), "passes_rising_bar": lb > 0}
