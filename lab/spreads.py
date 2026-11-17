"""Defined-risk 0DTE credit spreads: strike choice, the backtest simulator,
the no-naked-shorts invariant, and the order body for one atomic combo.

Paper only, by rule (docs/lab-credit-spreads-proposal.md). Three shapes, SPY,
same expiry, one contract per leg:

  put_credit   sell OTM put,  buy a put `width` lower
  call_credit  sell OTM call, buy a call `width` higher
  iron_condor  both of the above, short strikes on opposite sides of spot

Short strikes sit `em_mult` expected moves from spot, where the expected
move is the ATM straddle's price at the signal bar (its last trades at or
before the bar, so nothing from the future). Pricing is real option bars
only: a flat-vol model misses the put skew a seller is paid for, so there
is no modeled fallback, not even for exploration.

Costs follow lab/options.py on every leg, both ways: opening sells the short
at bid (trade - half spread) and buys the long at ask; closing does the
reverse. Fees are per contract per side on every leg. Stop exits add
STOP_SLIP_FRAC on every leg, since a combo has no resting stop to fill.

    python -m lab.spreads --selftest      # synthetic checks, no keys needed
"""
import argparse
import contextlib
import math
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import data, options
from .alpaca import NY, occ_symbol

STRUCTURES = ("put_credit", "call_credit", "iron_condor")
ENTRY_GRACE = 3          # minutes: every leg must trade within this of the signal
STALE_MIN = 3            # a short leg older than this can't trigger an exit
CHAIN_HALF = 20          # strikes either side of the open fetched in one request
FLAT_BY = "15:45"        # never held into the close: no pin or assignment risk
OPTIONS_START = pd.Timestamp("2024-02-15").date()   # Alpaca option bars begin here


@dataclass
class Leg:
    right: str           # "P" | "C"
    strike: float
    side: str            # "sell" (short) | "buy" (long)
    occ: str = ""


@dataclass
class SpreadTrade:
    date: object
    tag: str
    direction: str
    occ: str             # legs joined with "|", for the trades table
    priced: str
    signal_ts: object
    structure: str = ""
    entry_ts: object = None
    entry: float = np.nan      # net credit per share, after crossing
    exit_ts: object = None
    exit: float = np.nan       # net debit per share to close, after crossing
    exit_reason: str = ""
    contracts: int = 1
    pnl: float = np.nan
    ret: float = np.nan        # pnl / max loss
    filled: bool = False
    width: float = np.nan
    max_loss: float = np.nan   # (width - credit) x 100 x contracts
    em: float = np.nan
    capped: bool = False
    legs: list = field(default_factory=list)


# ------------------------------------------------------------ structure


def build_legs(structure, spot, em, em_mult, width, expiry, symbol="SPY"):
    """Strikes on $1 increments. Each short strike is at least one strike OTM
    and each long strike is further OTM than its short."""
    if structure not in STRUCTURES:
        raise ValueError(f"unknown structure {structure}")
    if not width or width < 1:
        raise ValueError("width must be >= 1")
    atm = round(spot)
    dist = em_mult * em
    legs = []
    if structure in ("put_credit", "iron_condor"):
        sp = min(math.floor(spot - dist), atm - 1)
        legs += [Leg("P", float(sp), "sell"), Leg("P", float(sp - width), "buy")]
    if structure in ("call_credit", "iron_condor"):
        sc = max(math.ceil(spot + dist), atm + 1)
        legs += [Leg("C", float(sc), "sell"), Leg("C", float(sc + width), "buy")]
    for lg in legs:
        lg.occ = occ_symbol(symbol, expiry, lg.right, lg.strike)
    assert_defined_risk([(lg.occ, -1 if lg.side == "sell" else 1) for lg in legs])
    return legs


def _parse_occ(occ):
    # SPY260116C00580000 -> (underlying, expiry yymmdd, right, strike)
    root = occ[:-15]
    return root, occ[-15:-9], occ[-9], int(occ[-8:]) / 1000


def naked_shorts(positions):
    """`positions` is [(occ, signed qty)] — negative = short. Returns the
    uncovered short contracts as [(occ, qty)]. A short is covered only by a
    long of the same underlying, expiry and right, further out of the money
    (lower strike for puts, higher for calls), one long per short. Empty
    list = safe."""
    book = {}
    for occ, q in positions:
        q = int(q)
        if q == 0:
            continue
        root, exp, right, k = _parse_occ(occ)
        book.setdefault((root, exp, right), []).append([k, q, occ])
    bad = []
    for (root, exp, right), rows in book.items():
        longs = [[k, q] for k, q, _ in rows if q > 0]
        shorts = [[k, -q, o] for k, q, o in rows if q < 0]
        # cover the shorts nearest the money first, each with the nearest
        # further-OTM long that still has contracts left
        shorts.sort(key=lambda r: -r[0] if right == "P" else r[0])
        for s in shorts:
            k_s, need, o = s
            cands = [l for l in longs if l[1] > 0 and (l[0] < k_s if right == "P" else l[0] > k_s)]
            cands.sort(key=lambda l: -l[0] if right == "P" else l[0])
            for l in cands:
                take = min(need, l[1])
                l[1] -= take
                need -= take
                if not need:
                    break
            if need:
                bad.append((o, need))
    return bad


def assert_defined_risk(positions):
    bad = naked_shorts(positions)
    if bad:
        raise ValueError(f"uncovered short legs: {bad}")


def mleg_order(legs, contracts, net, intent):
    """Alpaca multi-leg order body. `net` is per share: a credit is negative
    (Alpaca's convention), a debit positive. `intent` is "open" or "close".
    Every leg is covered inside the same order, which is what Alpaca requires
    and what keeps a partial fill from ever leaving a naked short."""
    body_legs = []
    for lg in legs:
        if intent == "open":
            side, pi = ("sell", "sell_to_open") if lg.side == "sell" else ("buy", "buy_to_open")
        else:
            side, pi = ("buy", "buy_to_close") if lg.side == "sell" else ("sell", "sell_to_close")
        body_legs.append({"symbol": lg.occ, "ratio_qty": "1", "side": side, "position_intent": pi})
    if intent == "open":
        assert_defined_risk([(lg.occ, -1 if lg.side == "sell" else 1) for lg in legs])
    return {"order_class": "mleg", "qty": str(int(contracts)), "type": "limit",
            "limit_price": f"{net:.2f}", "time_in_force": "day", "legs": body_legs}


# ------------------------------------------------------------- backtest


def _bars(occ, day_date):
    """option_day, with an empty result always carrying a time index (the
    Alpaca client returns a plain empty frame for a contract that didn't
    trade, which can't be compared with timestamps)."""
    df = data.option_day(occ, day_date)
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz=NY))
    return df


def _last_at_or_before(df, ts, max_age_min=None):
    x = df.loc[df.index <= ts]
    if x.empty:
        return None, None
    t = x.index[-1]
    if max_age_min is not None and (ts - t) > pd.Timedelta(minutes=max_age_min):
        return None, None
    return float(x.iloc[-1]["close"]), t


def expected_move(day_date, spot, ts, expiry):
    """ATM straddle price from the last trades at or before `ts` (within 3
    minutes). None when either leg has no fresh trade."""
    k = float(round(spot))
    px = 0.0
    for right in ("C", "P"):
        occ = occ_symbol("SPY", expiry, right, k)
        p, _ = _last_at_or_before(_bars(occ, day_date), ts, max_age_min=3)
        if p is None:
            return None
        px += p
    return px


def simulate_spread(sig, day, ctx, contracts=1, expiry=None):
    d = ctx["date"]
    expiry = expiry or d          # 0DTE only
    spot = float(day.loc[sig.ts, "close"])
    t = SpreadTrade(d, sig.tag, sig.direction, "", "real", sig.ts, structure=sig.structure,
                    contracts=contracts, width=float(sig.width or 0))
    if d < OPTIONS_START:
        t.exit_reason = "no_data"
        return t
    open_px = float(day.iloc[0]["open"])
    data.option_chain_day(d, [occ_symbol("SPY", expiry, r, float(k)) for r in ("P", "C")
                              for k in range(round(open_px) - CHAIN_HALF, round(open_px) + CHAIN_HALF + 1)])
    em = expected_move(d, spot, sig.ts, expiry)
    if em is None or em <= 0:
        t.exit_reason = "no_em"
        return t
    t.em = round(em, 3)
    legs = build_legs(sig.structure, spot, em, sig.em_mult or 1.0, sig.width, expiry)
    t.legs = [f"{lg.side} {lg.right}{lg.strike:g}" for lg in legs]
    t.occ = "|".join(lg.occ for lg in legs)
    bars = {lg.occ: _bars(lg.occ, d) for lg in legs}

    # entry: each leg's first trade after the signal bar, within the grace
    first = {}
    for lg in legs:
        b = bars[lg.occ]
        after = b.loc[b.index > sig.ts]
        if after.empty or (after.index[0] - sig.ts) > pd.Timedelta(minutes=ENTRY_GRACE):
            t.exit_reason = "unfilled"
            return t
        first[lg.occ] = (after.index[0], float(after.iloc[0]["open"]))
    credit = sum(options.sell_fill(first[lg.occ][1]) for lg in legs if lg.side == "sell") - \
        sum(options.buy_fill(first[lg.occ][1]) for lg in legs if lg.side == "buy")
    if credit < sig.min_credit:
        t.exit_reason = "thin_credit"
        t.entry = round(credit, 4)
        return t
    t.entry_ts = max(v[0] for v in first.values())
    t.entry = credit
    t.filled = True
    w = float(sig.width)
    t.max_loss = (w - credit) * 100 * contracts

    exit_by = pd.Timestamp(f"{d} {min(sig.exit_by or FLAT_BY, FLAT_BY)}", tz=NY)
    target = credit * (1 - sig.credit_target) if sig.credit_target else None
    stop = credit * (1 + sig.credit_stop) if sig.credit_stop else None
    shorts = [lg for lg in legs if lg.side == "sell"]
    longs = [lg for lg in legs if lg.side == "buy"]
    last = {lg.occ: (first[lg.occ][1], first[lg.occ][0]) for lg in legs}

    def price_at(occ, ts, use_open):
        b = bars[occ]
        if ts in b.index:
            r = b.loc[ts]
            return float(r["open"] if use_open else r["close"])
        return last[occ][0]

    def close_debit(ts, reason):
        stp = reason == "stop"
        buy = sum(options.buy_fill(price_at(lg.occ, ts, True)) * (1 + (options.STOP_SLIP_FRAC if stp else 0))
                  for lg in shorts)
        sell = sum(options.sell_fill(price_at(lg.occ, ts, True), stop=stp) for lg in longs)
        debit = max(0.0, buy - sell)
        # A combo can't really cost more than its width plus crossing it;
        # beyond that is a stale print on one leg, so cap it and flag it.
        cap = w + sum(options.half_spread(price_at(lg.occ, ts, True)) for lg in legs) * 2
        if debit > cap:
            t.capped = True
            debit = cap
        return debit

    pending = None
    for ts in day.index[day.index > t.entry_ts]:
        if pending:
            t.exit_ts, t.exit, t.exit_reason = ts, close_debit(ts, pending), pending
            break
        for lg in legs:
            b = bars[lg.occ]
            if ts in b.index:
                last[lg.occ] = (float(b.loc[ts, "close"]), ts)
        fresh = all((ts - last[lg.occ][1]) <= pd.Timedelta(minutes=STALE_MIN) for lg in shorts)
        mark = max(0.0, sum(last[lg.occ][0] for lg in shorts) - sum(last[lg.occ][0] for lg in longs))
        if fresh and stop is not None and mark >= stop:
            pending = "stop"
        elif fresh and target is not None and mark <= target:
            pending = "target"
        elif ts >= exit_by:
            pending = "time"
    if t.exit_ts is None:
        ts = day.index[-1]
        t.exit_ts, t.exit, t.exit_reason = ts, close_debit(ts, pending or "eod"), pending or "eod"

    nlegs = len(legs)
    t.pnl = (t.entry - t.exit) * 100 * contracts - 2 * nlegs * options.FEE_PER_CONTRACT * contracts
    t.ret = t.pnl / t.max_loss if t.max_loss > 0 else np.nan
    return t


# -------------------------------------------------- controls and stress


@contextlib.contextmanager
def cost_scenario(spread_mult=1.0, commission=0.0):
    """Temporarily widen the modeled spread and add a per-contract-per-side
    commission on top of fees. For the pass bar's cost stress only."""
    saved = (options.HALF_SPREAD_FLOOR, options.HALF_SPREAD_FRAC, options.FEE_PER_CONTRACT)
    options.HALF_SPREAD_FLOOR *= spread_mult
    options.HALF_SPREAD_FRAC *= spread_mult
    options.FEE_PER_CONTRACT += commission
    try:
        yield
    finally:
        options.HALF_SPREAD_FLOOR, options.HALF_SPREAD_FRAC, options.FEE_PER_CONTRACT = saved


# ------------------------------------------------------------- selftest


def _synthetic_day(d="2025-03-12", spot=560.0):
    idx = pd.date_range(f"{d} 09:30", f"{d} 15:59", freq="1min", tz=NY)
    px = spot + np.zeros(len(idx))
    return pd.DataFrame({"open": px, "high": px + 0.1, "low": px - 0.1, "close": px, "volume": 1000}, index=idx)


def selftest():
    fails = []

    def check(name, ok):
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            fails.append(name)

    exp = pd.Timestamp("2025-03-12").date()
    legs = build_legs("iron_condor", 560.3, 4.0, 1.0, 2, exp)
    check("condor strikes", [(l.right, l.strike, l.side) for l in legs] ==
          [("P", 556.0, "sell"), ("P", 554.0, "buy"), ("C", 565.0, "sell"), ("C", 567.0, "buy")])
    check("short strike at least 1 OTM", build_legs("put_credit", 560.0, 0.1, 1.0, 1, exp)[0].strike == 559.0)

    P = lambda k: occ_symbol("SPY", exp, "P", k)
    C = lambda k: occ_symbol("SPY", exp, "C", k)
    check("covered put spread", naked_shorts([(P(556), -1), (P(554), 1)]) == [])
    check("covered condor", naked_shorts([(P(556), -1), (P(554), 1), (C(565), -1), (C(567), 1)]) == [])
    check("naked put", naked_shorts([(P(556), -1)]) == [(P(556), 1)])
    check("long on wrong side", naked_shorts([(P(556), -1), (P(558), 1)]) == [(P(556), 1)])
    check("call long doesn't cover a put", naked_shorts([(P(556), -1), (C(567), 1)]) == [(P(556), 1)])
    check("2 shorts, 1 long", naked_shorts([(P(556), -2), (P(554), 1)]) == [(P(556), 1)])
    check("other expiry doesn't cover",
          naked_shorts([(P(556), -1), (occ_symbol("SPY", "2025-03-13", "P", 554), 1)]) == [(P(556), 1)])
    try:
        mleg_order([Leg("P", 556, "sell", P(556))], 1, -0.5, "open")
        check("mleg refuses a naked open", False)
    except ValueError:
        check("mleg refuses a naked open", True)
    body = mleg_order(legs, 1, -0.42, "open")
    check("mleg credit is negative", body["limit_price"] == "-0.42" and body["order_class"] == "mleg")
    check("mleg intents", [l["position_intent"] for l in body["legs"]] ==
          ["sell_to_open", "buy_to_open", "sell_to_open", "buy_to_open"])
    check("mleg close intents", [l["position_intent"] for l in mleg_order(legs, 1, 0.1, "close")["legs"]] ==
          ["buy_to_close", "sell_to_close", "buy_to_close", "sell_to_close"])

    # simulator on synthetic bars, by stubbing the data layer
    day = _synthetic_day()
    d = day.index[0].date()
    ctx = {"date": d}
    from .strategies import Signal

    def run_case(paths, **kw):
        frames = {}
        for occ, series in paths.items():
            idx = day.index
            v = np.asarray(series, dtype=float)
            frames[occ] = pd.DataFrame({"open": v, "high": v, "low": v, "close": v, "volume": 10}, index=idx)
        orig = (data.option_day, data.option_chain_day)
        data.option_day = lambda o, dd: frames.get(o, pd.DataFrame(columns=["open", "high", "low", "close"]))  # RangeIndex, like Alpaca
        data.option_chain_day = lambda dd, occs: None
        try:
            sig = Signal(ts=pd.Timestamp(f"{d} 13:44", tz=NY), direction="long", structure="put_credit",
                         width=2, em_mult=1.0, credit_target=0.5, credit_stop=2.0, exit_by="15:45", **kw)
            return simulate_spread(sig, day, ctx)
        finally:
            data.option_day, data.option_chain_day = orig

    n = len(day)
    flat = lambda v: [v] * n
    base = {C(560): flat(2.0), P(560): flat(2.0)}       # straddle 4.0 -> short put 556
    decay = np.linspace(0.60, 0.02, n)
    t1 = run_case({**base, P(556): decay, P(554): np.linspace(0.20, 0.01, n)})
    check("decaying spread hits target", t1.filled and t1.exit_reason == "target" and t1.pnl > 0)
    check("credit after crossing", abs(t1.entry - ((decay[255] - 0.01) - (np.linspace(0.20, 0.01, n)[255] + 0.01))) < 0.02)
    crash = np.concatenate([flat(0.60)[:300], np.linspace(0.60, 4.5, n - 300)])
    t2 = run_case({**base, P(556): crash, P(554): np.concatenate([flat(0.20)[:300], np.linspace(0.20, 2.5, n - 300)])})
    check("crash hits stop", t2.filled and t2.exit_reason == "stop" and t2.pnl < 0)
    check("loss within max loss + costs", -t2.pnl <= t2.max_loss + 8 * options.FEE_PER_CONTRACT * 100)
    stale = run_case({**base, P(556): np.concatenate([flat(0.60)[:300], flat(5.0)[300:]]), P(554): flat(0.20)})
    check("stale long leg can't cost more than the width", stale.exit <= 2 + 0.2 and stale.capped)
    t3 = run_case({**base, P(556): flat(0.60), P(554): flat(0.20)})
    check("flat by 15:45", t3.exit_reason == "time" and t3.exit_ts <= pd.Timestamp(f"{d} 15:47", tz=NY))
    missing = run_case({**base, P(556): flat(0.60)})
    check("a leg that never traded is unfilled", not missing.filled and missing.exit_reason == "unfilled")
    thin = run_case({**base, P(556): flat(0.06), P(554): flat(0.05)})
    check("thin credit skipped", not thin.filled and thin.exit_reason == "thin_credit")
    with cost_scenario(2.0, 0.65):
        stressed = run_case({**base, P(556): decay, P(554): np.linspace(0.20, 0.01, n)})
    check("stress costs more", stressed.pnl < t1.pnl)
    check("stress restored", options.FEE_PER_CONTRACT == 0.05 and options.HALF_SPREAD_FRAC == 0.01)
    print(f"{len(fails)} failed")
    return not fails


def main(argv=None):
    p = argparse.ArgumentParser(prog="lab.spreads")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args(argv)
    if a.selftest:
        return 0 if selftest() else 1
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
