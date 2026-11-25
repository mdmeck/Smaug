"""The trader's review marks as the Lab's answer key.

`review_marks` holds candles he marked on the Review tab: a good `entry`
(Long/Short), a good `exit`, or a trade to `skip`. This module scores a
detector against them: how many of his entries it fires near, and how many
of his skips it fires on anyway. P/L still decides whether a strategy is
any good; agreement with the marks says whether it is finding *his* trades.

    python -m lab.marks score --source midday
        the midday review's stored setups (midday_setups), on marked days
    python -m lab.marks score --strategy pdhl_break [--params '{...}']
        any lab/strategies module, replayed on every marked (ticker, day)
        with Alpaca minute bars
    python -m lab.marks list
    python -m lab.marks selftest

A signal "catches" a mark when it is on the same side and its entry bar is
within --tolerance minutes (default 10) of the marked candle's start. Marks
on higher timeframes cover their whole candle: a 5m mark at 10:30 is caught
by a 10:33 signal with a tolerance of 0. Exits are reported but not scored:
the strategies' exits are rules, not bars, so they are compared by the
backtester, not here.

Read-only: nothing here writes to Supabase.
"""
import argparse
import json
import sys

import pandas as pd

NY = "America/New_York"
TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1D": 390}


def load_marks(tickers=None):
    from . import supa
    params = {"select": "*", "order": "ts"}
    if tickers:
        params["ticker"] = f"in.({','.join(tickers)})"
    return supa.select("review_marks", **params) or []


def _ts(x):
    t = pd.Timestamp(x)
    return t.tz_localize("UTC").tz_convert(NY) if t.tzinfo is None else t.tz_convert(NY)


def _side(x):
    return {"long": "Long", "short": "Short"}.get(str(x).lower(), x)


def score(marks, signals, tolerance_min=10):
    """marks: review_marks rows. signals: [{ticker, side, ts}] with side
    Long/Short (or long/short) and ts the entry bar. Only days that the
    signal source covered should be passed in, or every mark on an uncovered
    day reads as a miss.

    Returns counts plus each entry/skip mark with the nearest same-side
    signal, and the signals that matched no mark (unrated: he hasn't said
    whether those are good)."""
    tol = pd.Timedelta(minutes=tolerance_min)
    sigs = [{**s, "side": _side(s["side"]), "ts": _ts(s["ts"])} for s in signals]
    used = set()
    rows = []
    for m in marks:
        if m["kind"] not in ("entry", "skip"):
            continue
        start = _ts(m["ts"])
        end = start + pd.Timedelta(minutes=TF_MIN.get(m.get("timeframe") or "1m", 1))
        best = None
        for i, s in enumerate(sigs):
            if s["ticker"] != m["ticker"] or s["side"] != m["side"]:
                continue
            gap = pd.Timedelta(0) if start <= s["ts"] < end else min(abs(s["ts"] - start), abs(s["ts"] - end))
            if gap <= tol and (best is None or gap < best[1]):
                best = (i, gap)
        if best:
            used.add(best[0])
        rows.append({
            "ticker": m["ticker"], "ts": str(start), "kind": m["kind"], "side": m["side"],
            "setup": m.get("setup") or "", "reason": m.get("reason") or "",
            "matched": best is not None,
            "signal_ts": None if best is None else str(sigs[best[0]]["ts"]),
            "minutes_off": None if best is None else round(best[1].total_seconds() / 60, 1),
        })
    entries = [r for r in rows if r["kind"] == "entry"]
    skips = [r for r in rows if r["kind"] == "skip"]
    caught = sum(r["matched"] for r in entries)
    fired = sum(r["matched"] for r in skips)
    unrated = [{"ticker": s["ticker"], "side": s["side"], "ts": str(s["ts"])}
               for i, s in enumerate(sigs) if i not in used]
    return {
        "tolerance_min": tolerance_min,
        "entries": len(entries), "caught": caught,
        "recall": round(caught / len(entries), 3) if entries else None,
        "skips": len(skips), "fired_on_skips": fired,
        "signals": len(sigs), "unrated_signals": len(unrated),
        "exits_marked": sum(m["kind"] == "exit" for m in marks),
        "marks": rows, "unrated": unrated,
    }


def _day(m):
    return _ts(m["ts"]).date()


def midday_signals(days, passed_only=False):
    """The midday review's setups on those days, and which days it ran."""
    from . import supa
    if not days:
        return [], set()
    ds = ",".join(sorted({d.isoformat() for d in days}))
    rows = supa.select("midday_setups", user_id=None, select="ticker,side,entry_at,passed,run_date",
                       run_date=f"in.({ds})") or []
    runs = supa.select("midday_runs", user_id=None, select="run_date,market_open",
                       run_date=f"in.({ds})") or []
    covered = {pd.Timestamp(r["run_date"]).date() for r in runs if r.get("market_open")}
    sigs = [{"ticker": r["ticker"], "side": r["side"], "ts": r["entry_at"]}
            for r in rows if r["passed"] or not passed_only]
    return sigs, covered


def strategy_signals(name, params, pairs):
    """Replay a lab/strategies module on each (ticker, day). Same call as the
    backtester: RTH minutes up to the close, prior-session context."""
    from . import backtest, data
    from .strategies import load
    mod = load(name)
    out, covered = [], set()
    for ticker in sorted({t for t, _ in pairs}):
        dset = sorted(d for t, d in pairs if t == ticker)
        minutes = data.stock_minutes(ticker, pd.Timestamp(dset[0]) - pd.Timedelta(days=60), dset[-1])
        daily = backtest.daily_bars(minutes)
        dates = list(daily.index.date)
        for d in dset:
            if d not in dates:
                continue
            i = dates.index(d)
            ctx = backtest.session_context(daily, i)
            day = minutes[minutes.index.date == d]
            for s in mod.signals(day, ctx, params):
                out.append({"ticker": ticker, "side": _side(s.direction), "ts": s.ts})
            covered.add((ticker, d))
    return out, covered


def cmd_score(a):
    marks = load_marks(a.tickers.split(",") if a.tickers else None)
    if a.source == "midday":
        sigs, days = midday_signals({_day(m) for m in marks}, a.passed_only)
        marks = [m for m in marks if _day(m) in days]
        label = "midday_setups" + (" (passed only)" if a.passed_only else "")
    else:
        params = json.loads(a.params) if a.params else {}
        pairs = {(m["ticker"], _day(m)) for m in marks if m["timeframe"] != "1D"}
        sigs, covered = strategy_signals(a.strategy, params, pairs)
        marks = [m for m in marks if (m["ticker"], _day(m)) in covered]
        label = a.strategy
    res = score(marks, sigs, a.tolerance)
    res["source"] = label
    if not a.detail:
        res.pop("marks")
        res.pop("unrated")
    print(json.dumps(res, indent=1, default=str))
    return 0


def cmd_list(a):
    marks = load_marks(a.tickers.split(",") if a.tickers else None)
    for m in marks:
        print(f"{_ts(m['ts']):%Y-%m-%d %H:%M} {m['ticker']:<5} {m['timeframe']:<3} {m['kind']:<5} "
              f"{(m.get('side') or ''):<5} {m['price']:>9.2f}  {m.get('setup') or ''} {m.get('reason') or ''}")
    print(f"{len(marks)} mark(s)", file=sys.stderr)
    return 0


def selftest():
    marks = [
        {"ticker": "SPY", "ts": "2026-11-23T15:30:00+00:00", "timeframe": "5m", "kind": "entry", "side": "Long"},
        {"ticker": "SPY", "ts": "2026-11-23T16:00:00+00:00", "timeframe": "1m", "kind": "entry", "side": "Short"},
        {"ticker": "SPY", "ts": "2026-11-23T17:00:00+00:00", "timeframe": "1m", "kind": "skip", "side": "Long"},
        {"ticker": "SPY", "ts": "2026-11-23T17:30:00+00:00", "timeframe": "1m", "kind": "exit", "side": "Long"},
    ]
    sigs = [
        {"ticker": "SPY", "side": "long", "ts": pd.Timestamp("2026-11-23 10:33", tz=NY)},   # inside the 5m candle
        {"ticker": "SPY", "side": "Long", "ts": "2026-11-23T16:02:00+00:00"},               # wrong side for 11:00
        {"ticker": "SPY", "side": "Long", "ts": "2026-11-23T17:08:00+00:00"},               # on his skip
        {"ticker": "QQQ", "side": "Short", "ts": "2026-11-23T16:00:00+00:00"},              # other ticker
    ]
    r = score(marks, sigs, tolerance_min=0)
    assert (r["entries"], r["caught"], r["skips"], r["fired_on_skips"]) == (2, 1, 1, 0), r
    r = score(marks, sigs, tolerance_min=10)
    assert (r["caught"], r["fired_on_skips"], r["unrated_signals"], r["exits_marked"]) == (1, 1, 2, 1), r
    assert r["marks"][0]["minutes_off"] == 0.0, r["marks"][0]
    print("selftest ok")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m lab.marks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--source", choices=["midday"])
    g.add_argument("--strategy")
    s.add_argument("--params")
    s.add_argument("--tickers", help="comma-separated; default every marked ticker")
    s.add_argument("--tolerance", type=float, default=10, help="minutes")
    s.add_argument("--passed-only", action="store_true", help="midday: only setups that passed every filter")
    s.add_argument("--detail", action="store_true", help="include every mark and unrated signal")
    lp = sub.add_parser("list")
    lp.add_argument("--tickers")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return selftest()
    return {"score": cmd_score, "list": cmd_list}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
