"""Event backtester for single-leg long option trades on minute bars.

For each session it asks the strategy for signals, then walks each trade
minute by minute on the option's own price:

  * entry at the open of the first option bar after the signal bar (the
    signal is only known at the bar's close), within ENTRY_GRACE minutes or
    the trade is recorded as unfilled — illiquid contracts don't get a free
    fill
  * premium stop = resting stop-market: fills at min(open, stop) less spread
    and stop slippage
  * premium target = resting limit: fills only when the bar trades *through*
    it by a tick, at the limit
  * underlying stop/target, max hold and exit_by: decided on a bar's close,
    filled at the next option bar's open less spread
  * stop and target in the same bar -> stop (the bar's path is unknown)

Pricing: real Alpaca option bars where they exist (2024+), otherwise
Black-Scholes off the underlying (options.model_path) with sigma = prior
VIX close x IV_MULT. Every trade records which it used — the two are never
pooled silently in a report.
"""
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import data, options
from .alpaca import NY, occ_symbol

OPTIONS_START = pd.Timestamp("2024-02-01").date()
ENTRY_GRACE = 3
IV_MULT = 1.0
TICK = 0.01


@dataclass
class Trade:
    date: object
    tag: str
    direction: str
    occ: str
    priced: str
    signal_ts: object
    entry_ts: object = None
    entry: float = np.nan
    exit_ts: object = None
    exit: float = np.nan
    exit_reason: str = ""
    contracts: int = 1
    pnl: float = np.nan
    ret: float = np.nan
    filled: bool = False


def daily_bars(minutes):
    g = minutes.groupby(minutes.index.date)
    d = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                      "close": g["close"].last(), "volume": g["volume"].sum()})
    d.index = pd.to_datetime(d.index)
    return d


def session_context(daily, i, vix=None):
    """Everything a strategy may know at the start of session i."""
    prior = daily.iloc[:i]
    return {
        "date": daily.index[i].date(),
        "daily": prior,                              # prior sessions only
        "prev": prior.iloc[-1] if len(prior) else None,
        "vix_prev": None if vix is None else _asof(vix, daily.index[i] - pd.Timedelta(days=1)),
        "sessions": list(daily.index.date),          # calendar only, for expiry lookup
        "session_idx": i,
    }


def _asof(series, ts):
    s = series.loc[:ts]
    return float(s.iloc[-1]) if len(s) else None


def _expiry(ctx, dte):
    i = ctx["session_idx"] + dte
    sess = ctx["sessions"]
    if i < len(sess):
        return sess[i]
    # beyond loaded data: approximate with business days
    return (pd.Timestamp(sess[-1]) + pd.offsets.BDay(i - len(sess) + 1)).date()


def simulate(sig, day, ctx, pricing="auto", contracts=1):
    right = "C" if sig.direction == "long" else "P"
    spot = float(day.loc[sig.ts, "close"])
    strike = options.strike_for(spot, right, sig.otm)
    expiry = _expiry(ctx, sig.dte)
    occ = occ_symbol("SPY", expiry, right, strike)
    d = ctx["date"]

    opt, priced = None, "model"
    if pricing != "model" and d >= OPTIONS_START:
        opt = data.option_day(occ, d)
        priced = "real"
        if opt.empty:
            if pricing == "real":
                return Trade(d, sig.tag, sig.direction, occ, "real", sig.ts, exit_reason="no_data")
            opt, priced = None, "model"
    if opt is None:
        vix = ctx.get("vix_prev") or 18.0
        sigma = vix / 100.0 * IV_MULT
        close = pd.Timestamp(f"{expiry} 16:00", tz=NY)
        opt = options.model_path(day.loc[sig.ts:], strike, right, close, sigma)

    t = Trade(d, sig.tag, sig.direction, occ, priced, sig.ts, contracts=contracts)
    after = opt.loc[opt.index > sig.ts]
    if after.empty or (after.index[0] - sig.ts) > pd.Timedelta(minutes=ENTRY_GRACE):
        t.exit_reason = "unfilled"
        return t
    t.entry_ts = after.index[0]
    t.entry = options.buy_fill(float(after.iloc[0]["open"]))
    t.filled = True

    stop = t.entry * (1 - sig.stop_pct) if sig.stop_pct else None
    target = t.entry * (1 + sig.target_pct) if sig.target_pct else None
    exit_by = pd.Timestamp(f"{d} {sig.exit_by}", tz=NY)
    max_hold = t.entry_ts + pd.Timedelta(minutes=sig.max_hold_min) if sig.max_hold_min else None

    pending = None   # reason decided on a close, filled at next bar's open
    for ts, b in after.iterrows():
        if pending:
            t.exit_ts, t.exit, t.exit_reason = ts, options.sell_fill(float(b["open"])), pending
            break
        if stop is not None and b["low"] <= stop:
            px = min(float(b["open"]), stop)
            t.exit_ts, t.exit, t.exit_reason = ts, options.sell_fill(px, stop=True), "stop"
            break
        if target is not None and b["high"] >= target + TICK:
            t.exit_ts, t.exit, t.exit_reason = ts, target, "target"
            break
        if ts in day.index:
            u = day.loc[ts]
            if sig.u_stop is not None and ((sig.direction == "long" and u["close"] <= sig.u_stop) or
                                           (sig.direction == "short" and u["close"] >= sig.u_stop)):
                pending = "u_stop"
            elif sig.u_target is not None and ((sig.direction == "long" and u["close"] >= sig.u_target) or
                                               (sig.direction == "short" and u["close"] <= sig.u_target)):
                pending = "u_target"
        if not pending and ts >= exit_by:
            pending = "time"
        if not pending and max_hold is not None and ts >= max_hold:
            pending = "max_hold"
    if t.exit_ts is None:
        last = after.iloc[-1]
        t.exit_ts, t.exit, t.exit_reason = after.index[-1], options.sell_fill(float(last["close"])), pending or "eod"

    t.pnl = (t.exit - t.entry) * 100 * contracts - 2 * options.FEE_PER_CONTRACT * contracts
    t.ret = t.pnl / (t.entry * 100 * contracts)
    return t


def run(strategy, params=None, start="2024-02-01", end=None, pricing="auto", symbol="SPY",
        max_trades_per_day=None, minutes=None, progress=False):
    end = end or (pd.Timestamp.now(tz=NY) - pd.Timedelta(days=1)).date()
    if minutes is None:
        # one extra year of daily context for strategies that look back
        minutes = data.stock_minutes(symbol, pd.Timestamp(start) - pd.Timedelta(days=400), end)
    daily = daily_bars(minutes)
    vix = data.vix_daily(pd.Timestamp(start) - pd.Timedelta(days=10), end)
    days = data.sessions(minutes)
    first = np.searchsorted(daily.index, pd.Timestamp(start))
    trades = []
    for i in range(first, len(daily)):
        d = daily.index[i].date()
        day = days[d]
        if len(day) < 300:   # half sessions / bad data: skip rather than distort
            continue
        ctx = session_context(daily, i, vix)
        sigs = sorted(strategy.signals(day, ctx, params), key=lambda s: s.ts)
        busy_until = None
        n = 0
        for s in sigs:
            if busy_until is not None and s.ts <= busy_until:
                continue
            if max_trades_per_day and n >= max_trades_per_day:
                break
            tr = simulate(s, day, ctx, pricing=pricing)
            trades.append(tr)
            if tr.filled:
                busy_until = tr.exit_ts
                n += 1
        if progress and i % 50 == 0:
            print(f"  {d} trades={len(trades)}", flush=True)
    return pd.DataFrame([t.__dict__ for t in trades])


def stats(trades, holdout=0.25):
    """Summary on filled trades, plus the same on the time-ordered last
    `holdout` fraction of sessions (never shuffled)."""
    f = trades[trades["filled"]] if len(trades) else trades

    def block(x):
        if not len(x):
            return {"n": 0}
        wins = x[x.pnl > 0].pnl.sum()
        losses = -x[x.pnl <= 0].pnl.sum()
        daily = x.groupby("date").pnl.sum()
        eq = x.pnl.cumsum()
        return {
            "n": int(len(x)),
            "win_rate": round(float((x.pnl > 0).mean()), 3),
            "avg_pnl": round(float(x.pnl.mean()), 2),
            "avg_ret": round(float(x.ret.mean()), 4),
            "total_pnl": round(float(x.pnl.sum()), 2),
            "profit_factor": round(float(wins / losses), 2) if losses > 0 else None,
            "max_drawdown": round(float((eq - eq.cummax()).min()), 2),
            "sharpe_daily": round(float(daily.mean() / daily.std() * math.sqrt(252)), 2) if len(daily) > 2 and daily.std() > 0 else None,
            # bootstrap 90% CI on mean pnl, resampling whole days
            "avg_pnl_ci90": _boot_ci(x),
            "exit_reasons": x.exit_reason.value_counts().to_dict(),
        }

    out = {"all": block(f)}
    if len(f):
        dates = sorted(f.date.unique())
        cut = dates[int(len(dates) * (1 - holdout))]
        out["in_sample"] = block(f[f.date < cut])
        out["holdout"] = block(f[f.date >= cut])
        out["holdout_from"] = str(cut)
        out["by_year"] = {str(y): block(g) for y, g in f.groupby(pd.to_datetime(f.date).dt.year)}
        out["priced"] = f.priced.value_counts().to_dict()
    out["unfilled"] = int((~trades["filled"]).sum()) if len(trades) else 0
    return out


def _boot_ci(x, n=1000, seed=7):
    by_day = [g.pnl.values for _, g in x.groupby("date")]
    if len(by_day) < 5:
        return None
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(n):
        pick = rng.integers(0, len(by_day), len(by_day))
        v = np.concatenate([by_day[i] for i in pick])
        means.append(v.mean())
    return [round(float(np.percentile(means, 5)), 2), round(float(np.percentile(means, 95)), 2)]
