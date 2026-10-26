"""Research CLI — what the Lab routine runs from its sandbox.

  python -m lab.research probe
  python -m lab.research backtest orb --params '{"or_minutes": 30}' --start 2024-02-01
  python -m lab.research sweep orb --grid '{"or_minutes": [5, 15, 30]}'
  python -m lab.research causal orb
  python -m lab.research calibrate --days 40

Every command prints one JSON document to stdout so the routine can paste it
into lab_journal verbatim.
"""
import argparse
import itertools
import json
import random
import sys

import numpy as np
import pandas as pd

from . import alpaca, backtest, data, options, strategies
from .alpaca import NY


def _out(obj):
    print(json.dumps(obj, indent=2, default=str))


def cmd_probe(a):
    """Which data the keys can actually reach — run first on a new account."""
    res = {}
    try:
        res["clock"] = alpaca.clock()
        acct = alpaca.account()
        res["account"] = {k: acct.get(k) for k in ("status", "equity", "buying_power", "options_trading_level",
                                                     "options_approved_level", "pattern_day_trader")}
        res["paper"] = alpaca.is_paper()
    except Exception as e:
        res["trading_error"] = str(e)
    for feed in ("sip", "iex"):
        try:
            df = alpaca.stock_bars("SPY", "2016-01-04", "2016-01-05", feed=feed)
            res[f"stock_{feed}_2016"] = len(df)
        except Exception as e:
            res[f"stock_{feed}_2016"] = str(e)[:200]
    # walk back month by month to find the earliest options history
    found = None
    for m in pd.period_range("2023-01", "2024-12", freq="M"):
        d = (m.start_time + pd.offsets.BDay(10)).date()
        try:
            day = alpaca.stock_bars("SPY", d, d + pd.Timedelta(days=1))
            if day.empty:
                continue
            spot = float(day.between_time("10:00", "10:01")["close"].iloc[0])
            occ = alpaca.occ_symbol("SPY", d, "C", round(spot))
            n = len(alpaca.option_bars([occ], d, d + pd.Timedelta(days=1))[occ])
        except Exception as e:
            res.setdefault("option_errors", []).append(f"{m}: {str(e)[:120]}")
            continue
        if n and not found:
            found = str(d)
        res.setdefault("option_probe", {})[str(m)] = n
    res["earliest_options_month"] = found
    _out(res)


def cmd_backtest(a):
    strat = strategies.load(a.strategy)
    params = {**strat.DEFAULTS, **json.loads(a.params or "{}")}
    tr = backtest.run(strat, params, start=a.start, end=a.end, pricing=a.pricing, progress=a.verbose)
    if a.save:
        tr.to_csv(a.save, index=False)
    _out({"strategy": a.strategy, "params": params, "start": a.start, "end": a.end,
          "pricing": a.pricing, "stats": backtest.stats(tr)})


def cmd_sweep(a):
    """Grid search. Ranks by in-sample avg pnl and reports holdout alongside —
    the holdout column is the one that matters, and a sweep that only looks
    good in-sample is a finding about overfitting, not a strategy."""
    strat = strategies.load(a.strategy)
    grid = json.loads(a.grid)
    keys = list(grid)
    minutes = data.stock_minutes("SPY", pd.Timestamp(a.start) - pd.Timedelta(days=400), a.end or pd.Timestamp.now().date())
    rows = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = {**strat.DEFAULTS, **json.loads(a.params or "{}"), **dict(zip(keys, combo))}
        tr = backtest.run(strat, params, start=a.start, end=a.end, pricing=a.pricing, minutes=minutes)
        s = backtest.stats(tr)
        rows.append({"params": dict(zip(keys, combo)),
                     "n": s["all"].get("n"),
                     "is_avg": s.get("in_sample", {}).get("avg_pnl"),
                     "ho_avg": s.get("holdout", {}).get("avg_pnl"),
                     "ho_n": s.get("holdout", {}).get("n"),
                     "ho_pf": s.get("holdout", {}).get("profit_factor"),
                     "all_ci90": s["all"].get("avg_pnl_ci90")})
    rows.sort(key=lambda r: -(r["is_avg"] or -1e9))
    _out({"strategy": a.strategy, "grid": grid, "n_combos": len(rows),
          "warning": f"{len(rows)} combos tried — expect the best in-sample row to be optimistic",
          "results": rows})


def cmd_causal(a):
    """Lookahead check: a signal emitted on the full day must also be emitted,
    at the same ts, when the strategy only sees bars up to that ts — and no
    prefix may emit a signal the full day doesn't."""
    strat = strategies.load(a.strategy)
    params = {**strat.DEFAULTS, **json.loads(a.params or "{}")}
    minutes = _minutes(a)
    daily = backtest.daily_bars(minutes)
    days = data.sessions(minutes)
    idx = [i for i in range(20, len(daily)) if len(days[daily.index[i].date()]) >= 300]
    random.Random(1).shuffle(idx)
    problems = []
    for i in idx[:a.days]:
        day = days[daily.index[i].date()]
        ctx = backtest.session_context(daily, i)
        full = {(str(s.ts), s.direction) for s in strat.signals(day, ctx, params)}
        seen = set()
        for k in range(1, len(day) + 1, a.step):
            pre = day.iloc[:k]
            last = pre.index[-1]
            for s in strat.signals(pre, ctx, params):
                if s.ts == last or a.step > 1:
                    seen.add((str(s.ts), s.direction))
        live_only = {x for x in seen if x not in full}
        missed = {x for x in full if x not in seen}
        if a.step == 1 and (live_only or missed):
            problems.append({"date": str(daily.index[i].date()), "live_only": sorted(live_only), "missed": sorted(missed)})
    _out({"strategy": a.strategy, "days_checked": min(a.days, len(idx)), "ok": not problems, "problems": problems[:10]})


def cmd_calibrate(a):
    """Fit IV_MULT: implied vol of real ATM 0DTE option bars vs prior VIX, by
    time of day. Run once real options data is reachable; the result goes into
    backtest.IV_MULT so pre-2024 modeled trades are priced like the real ones."""
    minutes = data.stock_minutes("SPY", a.start, a.end or (pd.Timestamp.now(tz=NY) - pd.Timedelta(days=1)).date())
    vix = data.vix_daily(pd.Timestamp(a.start) - pd.Timedelta(days=10), pd.Timestamp.now().date())
    days = sorted(data.sessions(minutes).items())
    random.Random(3).shuffle(days)
    rows = []
    for d, day in days[:a.days]:
        if len(day) < 300:
            continue
        v = backtest._asof(vix, pd.Timestamp(d) - pd.Timedelta(days=1))
        for hhmm in ("10:00", "11:30", "13:00", "14:30", "15:30"):
            ts = pd.Timestamp(f"{d} {hhmm}", tz=NY)
            if ts not in day.index:
                continue
            spot = float(day.loc[ts, "close"])
            for right in ("C", "P"):
                k = options.strike_for(spot, right, 0)
                occ = alpaca.occ_symbol("SPY", d, right, k)
                ob = data.option_day(occ, d)
                ob = ob.loc[(ob.index >= ts) & (ob.index <= ts + pd.Timedelta(minutes=2))]
                if ob.empty:
                    continue
                px = float(ob.iloc[0]["close"])
                t = options.years_to_expiry(ts + pd.Timedelta(minutes=1), pd.Timestamp(f"{d} 16:00", tz=NY))
                iv = options.implied_vol(px, spot, k, t, right)
                if v and iv == iv:
                    rows.append({"date": str(d), "tod": hhmm, "right": right, "iv": iv, "vix": v, "ratio": iv / (v / 100)})
    df = pd.DataFrame(rows)
    if df.empty:
        _out({"ok": False, "reason": "no real option bars found"})
        return
    _out({"n": len(df), "median_ratio": round(float(df.ratio.median()), 3),
          "by_tod": df.groupby("tod").ratio.median().round(3).to_dict(),
          "by_right": df.groupby("right").ratio.median().round(3).to_dict()})


def _minutes(a):
    if getattr(a, "source", "alpaca") == "supabase":
        return supabase_bars()
    end = a.end or (pd.Timestamp.now(tz=NY) - pd.Timedelta(days=1)).date()
    return data.stock_minutes("SPY", a.start, end)


def supabase_bars():
    """The pipeline's public `bars` table — offline fallback when no Alpaca
    keys are present (the last year of SPY only)."""
    import requests
    url = "https://uslbwanjvritktievkxc.supabase.co/rest/v1/bars"
    key = "sb_publishable_e3RuFiItE8K2lwt5IXkhIw_vowfx3AS"
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    rows, off = [], 0
    while True:
        r = requests.get(url, headers={**h, "Range": f"{off}-{off + 999}"},
                         params={"select": "ts,open,high,low,close,volume", "order": "ts"}, timeout=30)
        r.raise_for_status()
        b = r.json()
        rows += b
        if len(b) < 1000:
            break
        off += 1000
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(NY)
    return df.set_index("ts").sort_index().between_time("09:30", "15:59")


def main(argv=None):
    p = argparse.ArgumentParser(prog="lab.research")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    for name in ("backtest", "sweep", "causal"):
        s = sub.add_parser(name)
        s.add_argument("strategy", choices=strategies.available())
        s.add_argument("--params")
        s.add_argument("--start", default="2024-02-01")
        s.add_argument("--end")
        s.add_argument("--pricing", default="auto", choices=["auto", "real", "model"])
        if name == "backtest":
            s.add_argument("--save")
            s.add_argument("-v", "--verbose", action="store_true")
        if name == "sweep":
            s.add_argument("--grid", required=True)
        if name == "causal":
            s.add_argument("--days", type=int, default=15)
            s.add_argument("--step", type=int, default=1)
            s.add_argument("--source", default="alpaca", choices=["alpaca", "supabase"])
    c = sub.add_parser("calibrate")
    c.add_argument("--start", default="2024-03-01")
    c.add_argument("--end")
    c.add_argument("--days", type=int, default=40)
    a = p.parse_args(argv)
    {"probe": cmd_probe, "backtest": cmd_backtest, "sweep": cmd_sweep,
     "causal": cmd_causal, "calibrate": cmd_calibrate}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
