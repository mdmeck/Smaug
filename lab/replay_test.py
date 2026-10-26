"""Replay test: drive the real runner + broker through whole sessions in
simulated time, against a fake Alpaca and an in-memory Supabase.

    python -m lab.replay_test                 # last 10 sessions of `bars`
    python -m lab.replay_test --days 30 --kill-at 11:00

No keys needed: SPY minutes come from the pipeline's public `bars` table and
option quotes are modeled (options.model_path) with a realistic spread. The
fake broker enforces the rules that bite in production — notably that a sell
can't exceed the quantity not already reserved by another open sell, which is
what rejects an exit placed while the protective stop still rests.

It checks invariants, not P/L: every entry is closed or canceled by the end
of the day, no position survives the close, nothing errors, no two trades
are open at once, limits refuse what they should.
"""
import argparse
import itertools
import sys

import pandas as pd

from . import alpaca, backtest, broker, clock, data, options, runner, supa
from .alpaca import NY


class FakeClock:
    def __init__(self, t):
        self.t = t
        self.market = None

    def now(self):
        return self.t

    def time(self):
        return self.t.timestamp()

    def sleep(self, s):
        self.t = self.t + pd.Timedelta(seconds=max(s, 0.01))
        if self.market:
            self.market.match()


class FakeMarket:
    def __init__(self, clk, day, sigma):
        self.clk, self.day, self.sigma = clk, day, sigma
        self.d = day.index[0].date()
        self.paths = {}
        self.orders = {}
        self.pos = {}       # occ -> qty
        self.cost = {}      # occ -> avg entry
        self.ids = itertools.count(1)
        self.rejects = []

    # ---- prices
    def _path(self, occ):
        if occ not in self.paths:
            right = occ[9]
            strike = int(occ[10:]) / 1000
            exp = pd.Timestamp(f"20{occ[3:5]}-{occ[5:7]}-{occ[7:9]} 16:00", tz=NY)
            self.paths[occ] = options.model_path(self.day, strike, right, exp, self.sigma)
        return self.paths[occ]

    def mid(self, occ):
        p = self._path(occ)
        p = p[p.index <= self.clk.t - pd.Timedelta(minutes=1)]
        return float(p["close"].iloc[-1]) if len(p) else float(self._path(occ)["open"].iloc[0])

    def quote(self, occ):
        m = self.mid(occ)
        h = options.half_spread(m)
        return {"bp": round(max(0.01, m - h), 2), "ap": round(m + h, 2)}

    # ---- alpaca surface
    def clock(self):
        t = self.clk.t
        o = pd.Timestamp(f"{self.d} 09:30", tz=NY)
        c = pd.Timestamp(f"{self.d} 16:00", tz=NY)
        return {"is_open": o <= t < c, "next_open": o.isoformat(), "next_close": c.isoformat()}

    def calendar(self, start, end):
        return [{"date": str(d.date())} for d in pd.bdate_range(start, end)]

    def stock_bars(self, symbol, start, end, **kw):
        return self.day[self.day.index < pd.Timestamp(end)]

    def option_quotes_latest(self, syms, **kw):
        return {s: self.quote(s) for s in syms}

    def positions(self):
        out = []
        for occ, q in self.pos.items():
            if q:
                out.append({"symbol": occ, "qty": str(q), "asset_class": "us_option",
                            "unrealized_pl": str((self.mid(occ) - self.cost[occ]) * 100 * q)})
        return out

    def orders_list(self, status="open", **kw):
        return [o for o in self.orders.values() if o["status"] in ("new", "accepted", "partially_filled")]

    def get_order(self, oid):
        return dict(self.orders[oid])

    def submit_order(self, **kw):
        qty = int(kw["qty"])
        if kw["side"] == "sell":
            reserved = sum(int(o["qty"]) for o in self.orders_list()
                           if o["symbol"] == kw["symbol"] and o["side"] == "sell")
            if qty + reserved > self.pos.get(kw["symbol"], 0):
                self.rejects.append(kw)
                raise alpaca.AlpacaError(f"403: insufficient qty available for order (requested {qty}, reserved {reserved})")
        oid = f"o{next(self.ids)}"
        o = {"id": oid, "symbol": kw["symbol"], "qty": str(qty), "side": kw["side"], "type": kw["type"],
             "limit_price": kw.get("limit_price"), "stop_price": kw.get("stop_price"), "status": "new",
             "filled_qty": "0", "filled_avg_price": None, "filled_at": None,
             "submitted_at": self.clk.t.isoformat()}
        self.orders[oid] = o
        self.match()
        return dict(o)

    def cancel_order(self, oid):
        o = self.orders[oid]
        if o["status"] in ("filled", "canceled"):
            raise alpaca.AlpacaError("422: order not cancelable")
        o["status"] = "canceled"

    def match(self):
        if not (pd.Timestamp(f"{self.d} 09:30", tz=NY) <= self.clk.t < pd.Timestamp(f"{self.d} 16:00", tz=NY)):
            return
        for o in list(self.orders.values()):
            if o["status"] != "new":
                continue
            q = self.quote(o["symbol"])
            px = None
            if o["side"] == "buy" and o["type"] == "limit" and q["ap"] <= float(o["limit_price"]):
                px = q["ap"]
            elif o["side"] == "sell" and o["type"] == "market":
                px = q["bp"]
            elif o["side"] == "sell" and o["type"] == "limit" and q["bp"] >= float(o["limit_price"]):
                px = float(o["limit_price"])
            elif o["side"] == "sell" and o["type"] == "stop" and self.mid(o["symbol"]) <= float(o["stop_price"]):
                px = q["bp"]
            if px is None:
                continue
            n = int(o["qty"])
            o.update(status="filled", filled_qty=str(n), filled_avg_price=str(px), filled_at=self.clk.t.isoformat())
            s = o["symbol"]
            if o["side"] == "buy":
                self.cost[s] = px
                self.pos[s] = self.pos.get(s, 0) + n
            else:
                self.pos[s] = self.pos.get(s, 0) - n


class FakeSupa:
    def __init__(self):
        self.t = {"lab_controls": [], "lab_strategies": [], "lab_trades": [], "lab_runs": []}
        self.ids = itertools.count(1)

    @staticmethod
    def _match(row, k, v):
        if k in ("order", "limit", "select"):
            return True
        op, _, val = v.partition(".")
        x = row.get(k)
        if op == "eq":
            return str(x) == val
        if op == "gte":
            return x is not None and str(x) >= val
        if op == "in":
            return str(x) in val.strip("()").split(",")
        raise ValueError(v)

    def select(self, table, **params):
        params.pop("user_id", None)
        return [dict(r) for r in self.t[table] if all(self._match(r, k, v) for k, v in params.items())]

    def insert(self, table, row, upsert_on=None, ignore_duplicates=False):
        defaults = {"lab_trades": {"status": "pending", "entry_order_id": None, "stop_order_id": None,
                                   "exit_order_id": None, "entry_price": None, "exit_price": None,
                                   "target_price": None, "stop_price": None, "pnl": None,
                                   "entry_ts": None, "exit_reason": "", "notes": ""}}
        if upsert_on:
            keys = [k for k in upsert_on.split(",") if k != "user_id"]
            for r in self.t[table]:
                if all(str(r.get(k)) == str(row.get(k)) for k in keys):
                    if ignore_duplicates:
                        return None
                    r.update(row)
                    return dict(r)
        r = {"id": f"r{next(self.ids)}", **defaults.get(table, {}), **row}
        self.t[table].append(r)
        return dict(r)

    def update(self, table, match, patch):
        out = []
        for r in self.t[table]:
            if all(str(r.get(k)) == str(v) for k, v in match.items()):
                r.update(patch)
                out.append(dict(r))
        return out


def install(market, fs, clk, history, vix):
    for name in ("clock", "calendar", "stock_bars", "option_quotes_latest", "positions",
                 "get_order", "submit_order", "cancel_order"):
        setattr(alpaca, name, getattr(market, name))
    alpaca.orders = market.orders_list
    alpaca.is_paper = lambda: True
    for name in ("select", "insert", "update"):
        setattr(supa, name, getattr(fs, name))
    clock.now, clock.time, clock.sleep = clk.now, clk.time, clk.sleep
    data.stock_minutes = lambda sym, s, e, rth=True: history[history.index.date <= pd.Timestamp(e).date()]
    data.vix_daily = lambda s, e: vix


def replay_day(d, minutes, controls, kill_at=None, module="orb", params=None):
    day = minutes[minutes.index.date == d]
    history = minutes[minutes.index.date < d]
    clk = FakeClock(pd.Timestamp(f"{d} 09:05", tz=NY))
    vix = pd.Series(16.0, index=pd.bdate_range(pd.Timestamp(d) - pd.Timedelta(days=30), d))
    market = FakeMarket(clk, day, 0.16 * backtest.IV_MULT)
    clk.market = market
    fs = FakeSupa()
    fs.t["lab_controls"].append({"user_id": supa.USER_ID, **controls})
    fs.t["lab_strategies"].append({"id": "s1", "name": f"{module}_test", "module": module, "params": params or {},
                                   "status": "paper", "contracts": 1})
    install(market, fs, clk, history, vix)

    # the kill switch flips mid-session when asked to
    if kill_at:
        orig_sleep = clk.sleep

        def sleep(s):
            orig_sleep(s)
            if clk.t.strftime("%H:%M") >= kill_at:
                fs.t["lab_controls"][0]["trading_enabled"] = False
        clk.sleep = sleep
        clock.sleep = sleep

    for w in ("morning", "afternoon"):
        runner.Runner(w).run()
        # the afternoon job starts ~when the morning one ends
    return market, fs


def check(d, market, fs, kill_at):
    problems = []
    trades = fs.t["lab_trades"]
    for t in trades:
        if t["status"] in ("pending", "open"):
            problems.append(f"{t['occ']} left {t['status']}")
    left = {k: v for k, v in market.pos.items() if v}
    if left:
        problems.append(f"positions survived the close: {left}")
    errs = [e for r in fs.t["lab_runs"] for e in r.get("errors", [])]
    if errs:
        problems.append(f"runner errors: {errs[:3]}")
    if market.rejects:
        problems.append(f"broker rejected sells: {market.rejects[:2]}")
    live = [o for o in market.orders.values() if o["status"] == "new"]
    if live:
        problems.append(f"orders left working: {[(o['side'], o['type']) for o in live]}")
    if kill_at:
        late = [t for t in trades if t["status"] in ("closed", "pending", "open")
                and pd.Timestamp(t["signal_ts"]).strftime("%H:%M") >= kill_at]
        late += [t for t in trades if t["status"] == "closed" and t.get("exit_ts")
                 and pd.Timestamp(t["exit_ts"]).tz_convert(NY).strftime("%H:%M") > kill_at
                 and pd.Timestamp(t["exit_ts"]).tz_convert(NY) > pd.Timestamp(f"{d} {kill_at}", tz=NY) + pd.Timedelta(minutes=1)]
        if late:
            problems.append(f"entered after kill switch: {[t['signal_ts'] for t in late]}")
    closed = [t for t in trades if t["status"] == "closed"]
    return {"date": str(d), "signals": len(trades), "closed": len(closed),
            "rejected": sum(t["status"] == "rejected" for t in trades),
            "pnl": round(sum(t["pnl"] for t in closed), 2),
            "exits": [t["exit_reason"] for t in closed], "problems": problems}


def main(argv=None):
    from .research import supabase_bars
    p = argparse.ArgumentParser(prog="lab.replay_test")
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--kill-at")
    p.add_argument("--module", default="orb")
    p.add_argument("--max-trades", type=int, default=4)
    p.add_argument("--max-loss", type=float, default=200)
    a = p.parse_args(argv)
    # quiet the runner's per-event prints
    runner.print = lambda *x, **k: None
    minutes = supabase_bars()
    days = sorted({d for d in minutes.index.date if (minutes.index.date == d).sum() >= 380})[-a.days:]
    controls = {"trading_enabled": True, "mode": "paper", "max_contracts": 1, "max_open_positions": 1,
                "max_trades_per_day": a.max_trades, "max_daily_loss": a.max_loss, "max_premium_per_trade": 1000}
    bad = 0
    for d in days:
        market, fs = replay_day(d, minutes, dict(controls), kill_at=a.kill_at, module=a.module)
        r = check(d, market, fs, a.kill_at)
        bad += bool(r["problems"])
        print(r, flush=True)
    print("FAIL" if bad else "OK", f"{bad}/{len(days)} days with problems")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
