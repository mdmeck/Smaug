"""Offline checks for the contract cap and the scale-out / runner exits.

    python -m lab.scale_test

No keys and no network: a synthetic session drives the backtester and the
real runner + broker (through replay_test's fake Alpaca and Supabase).
Exits non-zero on the first failed check.
"""
import sys

import numpy as np
import pandas as pd

from . import alpaca, backtest, broker, exits, replay_test, runner
from .alpaca import NY
from .strategies import Signal

D = pd.Timestamp("2026-06-17").date()   # a Wednesday; model-priced in both paths


def check(cond, what):
    if not cond:
        print("FAIL", what)
        sys.exit(1)
    print("ok  ", what)


def synthetic_day(d=D):
    """Flat 15-minute range, a steady 2-hour rally, then a slide to the close:
    an ORB long that reaches its target, then a 5-minute EMA cross."""
    idx = pd.date_range(f"{d} 09:30", f"{d} 15:59", freq="1min", tz=NY)
    px = np.empty(len(idx))
    px[:15] = 600 + 0.05 * np.sin(np.arange(15))
    up = np.arange(1, 121) * 0.04
    px[15:135] = 600.1 + up
    px[135:] = px[134] - np.arange(1, len(idx) - 134) * 0.03
    close = pd.Series(px, index=idx)
    open_ = close.shift(1).fillna(close.iloc[0])
    df = pd.DataFrame({"open": open_, "close": close})
    df["high"] = df[["open", "close"]].max(axis=1) + 0.02
    df["low"] = df[["open", "close"]].min(axis=1) - 0.02
    df["volume"] = 1000
    return df[["open", "high", "low", "close", "volume"]]


def minutes_with_history():
    days = [synthetic_day(x.date()) for x in pd.bdate_range(pd.Timestamp(D) - pd.Timedelta(days=9), D)]
    return pd.concat(days)


def test_scale_qty():
    cases = {(5, 0.6): 3, (5, 0.8): 4, (5, 0.5): 2, (4, 0.5): 2, (2, 0.9): 1,
             (5, 0.05): 1, (1, 0.6): 0, (5, None): 0, (5, 0): 0}
    for (n, f), want in cases.items():
        check(exits.scale_qty(n, f) == want, f"scale_qty({n}, {f}) == {want}")


def test_cap():
    real = alpaca.is_paper
    try:
        for paper, mode, saved, want in [(True, "paper", 5, 5), (True, "paper", 10, 5), (True, "paper", 1, 1),
                                         (False, "live", 5, 1), (True, "live", 5, 1), (False, "paper", 5, 1)]:
            alpaca.is_paper = lambda p=paper: p
            got = broker.max_contracts({"mode": mode, "max_contracts": saved})
            check(got == want, f"cap paper_endpoint={paper} mode={mode} saved={saved} -> {want}")
    finally:
        alpaca.is_paper = real


def test_ema_series_matches_prefix():
    day = synthetic_day()
    for direction in ("long", "short"):
        s = exits.ema_against_series(day, direction, [9, 21, 5])
        bad = [ts for ts in day.index if bool(s[ts]) != exits.ema_cross_against(day.loc[:ts], direction, [9, 21, 5])]
        check(not bad, f"ema_against_series == ema_cross_against on every prefix ({direction})")
    s = exits.ema_against_series(day, "long", [9, 21, 5])
    first = s[s].index.min()
    check(first is not None and first.strftime("%H:%M") > "11:45", f"long EMA cross only after the top (first {first})")


def test_backtest_scale():
    day = synthetic_day()
    ctx = {"date": D, "vix_prev": 16.0, "sessions": [D], "session_idx": 0}
    ts = day.index[15]
    plain = Signal(ts, "long", stop_pct=0.35, target_pct=0.5, exit_by="15:30")
    t1 = backtest.simulate(plain, day, ctx, pricing="model", contracts=5)
    check(t1.exit_reason == "target" and t1.scale_qty == 0, "no rule: 5 contracts all out at the target")

    sig = Signal(ts, "long", stop_pct=0.35, target_pct=0.5, exit_by="15:30", scale_frac=0.6, trail_ema=[9, 21, 5])
    t = backtest.simulate(sig, day, ctx, pricing="model", contracts=5)
    check(t.scale_qty == 3, f"scale 3 of 5 at the target (got {t.scale_qty})")
    check(abs(t.scale_price - t.entry * 1.5) < 1e-9, "scale leg fills at the target")
    check(t.exit_reason in ("ema_trail", "breakeven"), f"runner exits on the trail or breakeven (got {t.exit_reason})")
    check(t.exit_ts > t.scale_ts, "runner exits after the scale")
    want = ((t.scale_price - t.entry) * 300 + (t.exit - t.entry) * 200
            - 2 * backtest.options.FEE_PER_CONTRACT * 5)
    check(abs(t.pnl - want) < 1e-6, "pnl = scale leg + runner leg - fees")
    check(t.pnl > t1.pnl, f"runner adds to the trend day here ({t.pnl:.0f} vs {t1.pnl:.0f})")

    one = backtest.simulate(sig, day, ctx, pricing="model", contracts=1)
    check(one.scale_qty == 0 and one.exit_reason == "target", "one contract: the rule is a no-op")

    tr = Signal(ts, "long", stop_pct=0.35, target_pct=0.5, exit_by="15:30", scale_frac=0.6, trail_pct=0.2)
    t3 = backtest.simulate(tr, day, ctx, pricing="model", contracts=5)
    check(t3.scale_qty == 3 and t3.exit_reason in ("trail", "breakeven"), f"trail_pct runner exits on the trail (got {t3.exit_reason})")

    # params override an unmodified strategy's signal
    s2 = exits.apply_overrides(Signal(ts, "long"), {"scale_frac": 0.8, "trail_ema": [9, 21, 5], "runner_be": False})
    check(s2.scale_frac == 0.8 and s2.trail_ema == [9, 21, 5] and s2.runner_be is False, "params fill the runner fields")


def test_runner_scale():
    runner.print = lambda *x, **k: None
    minutes = minutes_with_history()
    controls = {"trading_enabled": True, "mode": "paper", "max_contracts": 5, "max_open_positions": 1,
                "max_trades_per_day": 1, "max_daily_loss": 5000, "max_premium_per_trade": 10000}
    params = {"stop_pct": 0.35, "target_pct": 0.5, "scale_frac": 0.6, "trail_ema": [9, 21, 5]}
    market, fs = replay_test.replay_day(D, minutes, dict(controls), module="orb", params=params, contracts=5)
    r = replay_test.check(D, market, fs, None)
    check(not r["problems"], f"runner day clean ({r['problems']})")
    t = [x for x in fs.t["lab_trades"] if x["status"] == "closed"]
    check(len(t) == 1, "one closed trade")
    t = t[0]
    check(t["contracts"] == 5 and t["scale_qty"] == 3, f"runner bought 5 and scaled 3 (got {t['contracts']}/{t.get('scale_qty')})")
    check(t["exit_reason"] in ("ema_trail", "breakeven"), f"runner leg exits on the trail or breakeven (got {t['exit_reason']})")
    sells = [o for o in market.orders.values() if o["side"] == "sell" and o["status"] == "filled"]
    check(sorted(int(o["qty"]) for o in sells) == [2, 3], f"filled sells were 3 then 2 ({[o['qty'] for o in sells]})")
    want = (t["scale_price"] - t["entry_price"]) * 300 + (t["exit_price"] - t["entry_price"]) * 200
    check(abs(t["pnl"] - round(want, 2)) < 0.02, "recorded pnl covers both legs")
    check(not market.rejects, "no sell rejected for reserved qty")

    # 6 contracts is over the paper ceiling: refused, nothing sent
    market, fs = replay_test.replay_day(D, minutes, dict(controls, max_contracts=10), module="orb",
                                        params=params, contracts=6)
    rows = fs.t["lab_trades"]
    check(rows and all(x["status"] == "rejected" for x in rows) and "max 5" in rows[0]["notes"],
          "6 contracts refused on paper")
    check(not [o for o in market.orders.values() if o["side"] == "buy"], "no buy order sent for 6")


def main():
    test_scale_qty()
    test_cap()
    test_ema_series_matches_prefix()
    test_backtest_scale()
    test_runner_scale()
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
