"""Deterministic intraday runner — executes the strategies the Lab routine
promoted to paper (or live), in GitHub Actions.

    python -m lab.runner                 # window chosen from the clock
    python -m lab.runner --once          # one pass, for testing
    python -m lab.runner --dry-run       # evaluate + log signals, never order

No LLM in the loop: each minute it asks every active strategy module the
same question the backtest asked (`signals(day_so_far, ctx, params)`), and
acts on a signal whose ts is the bar that just closed. Exits are managed
here because Alpaca options take no bracket/OCO orders: a stop rests at the
broker as the backstop (so a dead runner still can't lose more than the
stop), and the target / underlying / time exits are watched by this loop.

Actions jobs cap at 6h and RTH is 6.5h, so the day is two jobs — morning
(to 12:50 ET) and afternoon (to the close) — and everything the second needs
lives in lab_trades and at the broker, never in process memory.
"""
import argparse
import traceback

import pandas as pd

from . import alpaca, backtest, broker, clock, data, strategies, supa
from .alpaca import NY

ENTRY_TIMEOUT_S = 90       # unfilled entry limit is canceled after this
FLATTEN_AT = "15:50"       # everything is closed by here, whatever the strategy says
DISASTER_STOP = 0.6        # resting stop when a strategy sets none: -60% premium
MAX_ENTRY_SPREAD = 0.10    # skip if (ask - bid) / mid exceeds this
# Back to back, never overlapping: two runners managing the same trade would
# both try to exit it. The afternoon job is scheduled early and sleeps.
WINDOWS = {"morning": ("09:30", "12:50"), "afternoon": ("12:51", "16:00")}


class Runner:
    def __init__(self, window, dry_run=False):
        self.window = window
        self.dry_run = dry_run
        self.today = clock.now().date()
        self.events = []
        self.errors = []
        self.run_id = None
        self.strats = []
        self.strats_loaded = 0
        self.last_bar = None

    # ------------------------------------------------------------ logging
    def log(self, kind, **kw):
        e = {"t": clock.now().strftime("%H:%M:%S"), "kind": kind, **kw}
        print(e, flush=True)
        self.events.append(e)

    def error(self, where, exc):
        msg = f"{where}: {exc}"
        print("ERROR", msg, traceback.format_exc(), flush=True)
        self.errors.append({"t": clock.now().strftime("%H:%M:%S"), "where": where, "error": str(exc)[:500]})

    def heartbeat(self, status="running", end=False):
        patch = {"heartbeat_at": clock.now().tz_convert("UTC").isoformat(), "status": status,
                 "events": self.events[-400:], "errors": self.errors[-100:],
                 "strategies": [s["name"] for s in self.strats]}
        if end:
            patch["ended_at"] = patch["heartbeat_at"]
        try:
            if self.run_id is None:
                row = supa.insert("lab_runs", {"as_of": str(self.today), "session_window": self.window, **patch})
                self.run_id = row["id"]
            else:
                supa.update("lab_runs", {"id": self.run_id}, patch)
        except Exception as e:  # logging must never stop trading logic
            print("heartbeat failed", e, flush=True)

    # ------------------------------------------------------------ context
    def load_context(self):
        start = pd.Timestamp(self.today) - pd.Timedelta(days=400)
        yday = pd.Timestamp(self.today) - pd.Timedelta(days=1)
        hist = data.stock_minutes("SPY", start, yday)
        daily = backtest.daily_bars(hist)
        today_row = pd.DataFrame([{c: float("nan") for c in daily.columns}], index=[pd.Timestamp(self.today)])
        self.daily = pd.concat([daily, today_row])
        self.vix = data.vix_daily(start, pd.Timestamp(self.today))
        cal = alpaca.calendar(self.today, pd.Timestamp(self.today) + pd.Timedelta(days=20))
        self.future = [pd.Timestamp(c["date"]).date() for c in cal]

    def ctx(self):
        c = backtest.session_context(self.daily, len(self.daily) - 1, self.vix)
        c["sessions"] = [d for d in c["sessions"] if d < self.today] + self.future
        c["session_idx"] = c["sessions"].index(self.today)
        return c

    def load_strategies(self, force=False):
        if not force and clock.time() - self.strats_loaded < 300:
            return
        rows = supa.select("lab_strategies", status="in.(paper,live)")
        self.strats = []
        for r in rows:
            try:
                r["mod"] = strategies.load(r["module"])
                r["params"] = {**r["mod"].DEFAULTS, **(r["params"] or {})}
                self.strats.append(r)
            except Exception as e:
                self.error(f"load {r['name']}", e)
        self.strats_loaded = clock.time()

    # ------------------------------------------------------------ signals
    def today_bars(self):
        now = clock.now()
        df = alpaca.stock_bars("SPY", pd.Timestamp(f"{self.today} 09:30", tz=NY), now, feed="iex")
        df = df.between_time("09:30", "15:59")
        # only completed bars: a bar stamped t covers [t, t+1min)
        return df[df.index <= now.floor("min") - pd.Timedelta(minutes=1)]

    def evaluate(self, day):
        last = day.index[-1]
        ctx = self.ctx()
        for s in self.strats:
            try:
                sigs = [x for x in s["mod"].signals(day, ctx, s["params"]) if x.ts == last]
            except Exception as e:
                self.error(f"signals {s['name']}", e)
                continue
            for sig in sigs[:1]:
                self.enter(s, sig, day)

    def enter(self, strat, sig, day):
        right = "C" if sig.direction == "long" else "P"
        spot = float(day.loc[sig.ts, "close"])
        from .options import strike_for
        strike = strike_for(spot, right, sig.otm)
        ctx = self.ctx()
        expiry = ctx["sessions"][ctx["session_idx"] + sig.dte]
        occ = alpaca.occ_symbol("SPY", expiry, right, strike)
        row = {"strategy": strat["name"], "mode": "paper" if alpaca.is_paper() else "live",
               "signal_ts": sig.ts.isoformat(), "signal": sig.to_json(), "occ": occ,
               "direction": sig.direction, "contracts": strat["contracts"]}
        self.log("signal", strategy=strat["name"], occ=occ, direction=sig.direction, tag=sig.tag)
        if self.dry_run:
            return
        try:
            q = alpaca.option_quotes_latest([occ]).get(occ) or {}
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            if ask <= 0 or bid <= 0:
                raise broker.Refused(f"no quote for {occ}")
            mid = (bid + ask) / 2
            if (ask - bid) / mid > MAX_ENTRY_SPREAD:
                raise broker.Refused(f"spread {bid:.2f}/{ask:.2f} too wide")
            order = broker.open_position(strat, occ, strat["contracts"], ask)
            row.update(status="pending", entry_order_id=order["id"])
            self.log("entry_sent", strategy=strat["name"], occ=occ, limit=ask)
        except broker.Refused as e:
            row.update(status="rejected", notes=str(e))
            self.log("refused", strategy=strat["name"], reason=str(e))
        except Exception as e:
            row.update(status="error", notes=str(e)[:500])
            self.error(f"enter {strat['name']}", e)
        try:
            supa.insert("lab_trades", row, upsert_on="user_id,strategy,signal_ts", ignore_duplicates=True)
        except Exception as e:
            self.error("record trade", e)

    # ------------------------------------------------------------ management
    def manage(self, ctl, day):
        trades = [t for t in broker.today_trades() if t["status"] in ("pending", "open")]
        now = clock.now()
        halt = None
        if not ctl["trading_enabled"]:
            halt = "kill_switch"
        elif broker.day_pnl() <= -ctl["max_daily_loss"]:
            halt = "daily_loss"
        if now.strftime("%H:%M") >= FLATTEN_AT:
            halt = halt or "flatten_eod"
        for t in trades:
            try:
                if t["status"] == "pending":
                    self.manage_pending(t, now)
                else:
                    self.manage_open(t, now, day, halt)
            except Exception as e:
                self.error(f"manage {t['occ']}", e)
        if halt in ("kill_switch", "daily_loss", "flatten_eod"):
            self.flatten_orphans(halt)
        return halt

    def manage_pending(self, t, now):
        o = alpaca.get_order(t["entry_order_id"])
        filled = int(float(o.get("filled_qty") or 0))
        age = (now - pd.Timestamp(o["submitted_at"]).tz_convert(NY)).total_seconds()
        if o["status"] == "filled" or (filled and age > ENTRY_TIMEOUT_S):
            if o["status"] != "filled":
                broker.cancel(o["id"])
            entry = float(o["filled_avg_price"])
            sig = t["signal"] or {}
            stop = entry * (1 - (sig.get("stop_pct") or DISASTER_STOP))
            target = entry * (1 + sig["target_pct"]) if sig.get("target_pct") else None
            so = broker.place_stop(t["occ"], filled, stop)
            supa.update("lab_trades", {"id": t["id"]}, {
                "status": "open", "contracts": filled, "entry_price": entry,
                "entry_ts": o["filled_at"], "stop_price": round(stop, 2),
                "target_price": round(target, 2) if target else None, "stop_order_id": so["id"],
                "updated_at": now.isoformat()})
            self.log("filled", occ=t["occ"], entry=entry, stop=round(stop, 2), target=target)
        elif o["status"] in ("canceled", "expired", "rejected"):
            supa.update("lab_trades", {"id": t["id"]}, {"status": "canceled", "notes": f"entry {o['status']}",
                                                         "updated_at": now.isoformat()})
        elif age > ENTRY_TIMEOUT_S:
            broker.cancel(o["id"])
            self.log("entry_timeout", occ=t["occ"])

    def manage_open(self, t, now, day, halt):
        # an exit already in flight: just wait for it
        if t.get("exit_order_id"):
            o = alpaca.get_order(t["exit_order_id"])
            if o["status"] == "filled":
                self.record_close(t, o, t["exit_reason"])
            elif o["status"] in ("canceled", "expired", "rejected"):
                supa.update("lab_trades", {"id": t["id"]}, {"exit_order_id": None})
            return
        so = alpaca.get_order(t["stop_order_id"]) if t.get("stop_order_id") else None
        if so and so["status"] == "filled":
            self.record_close(t, so, "stop")
            return
        sig = t["signal"] or {}
        reason = halt
        if not reason and sig.get("exit_by") and now.strftime("%H:%M") >= sig["exit_by"]:
            reason = "time"
        if not reason and sig.get("max_hold_min") and t.get("entry_ts"):
            if now >= pd.Timestamp(t["entry_ts"]).tz_convert(NY) + pd.Timedelta(minutes=sig["max_hold_min"]):
                reason = "max_hold"
        if not reason and len(day):
            c = float(day["close"].iloc[-1])
            long = t["direction"] == "long"
            if sig.get("u_stop") is not None and (c <= sig["u_stop"] if long else c >= sig["u_stop"]):
                reason = "u_stop"
            elif sig.get("u_target") is not None and (c >= sig["u_target"] if long else c <= sig["u_target"]):
                reason = "u_target"
        limit = None
        if not reason and t.get("target_price"):
            q = alpaca.option_quotes_latest([t["occ"]]).get(t["occ"]) or {}
            bid = float(q.get("bp") or 0)
            if bid >= t["target_price"]:
                reason, limit = "target", bid
        if reason:
            self.exit(t, reason, so, limit)

    def exit(self, t, reason, stop_order, limit=None):
        if stop_order and stop_order["status"] not in ("filled", "canceled", "expired"):
            broker.cancel(stop_order["id"])
            for _ in range(20):
                stop_order = alpaca.get_order(stop_order["id"])
                if stop_order["status"] in ("canceled", "filled", "expired"):
                    break
                clock.sleep(0.5)
            if stop_order["status"] == "filled":
                self.record_close(t, stop_order, "stop")
                return
        o = broker.close_position(t["occ"], t["contracts"], limit)
        if o is None:
            self.log("exit_nothing_held", occ=t["occ"], reason=reason)
            supa.update("lab_trades", {"id": t["id"]}, {"status": "error", "notes": "position missing at exit"})
            return
        supa.update("lab_trades", {"id": t["id"]}, {"exit_order_id": o["id"], "exit_reason": reason})
        self.log("exit_sent", occ=t["occ"], reason=reason, limit=limit)
        for _ in range(20):
            o = alpaca.get_order(o["id"])
            if o["status"] == "filled":
                self.record_close(t, o, reason)
                return
            clock.sleep(1)
        if limit:   # target limit didn't fill: pull it and let the next pass re-decide
            broker.cancel(o["id"])
            # re-arm the backstop
            so = broker.place_stop(t["occ"], t["contracts"], t["stop_price"])
            supa.update("lab_trades", {"id": t["id"]}, {"exit_order_id": None, "stop_order_id": so["id"]})

    def record_close(self, t, o, reason):
        px = float(o["filled_avg_price"])
        qty = int(float(o["filled_qty"]))
        pnl = (px - t["entry_price"]) * 100 * qty
        supa.update("lab_trades", {"id": t["id"]}, {
            "status": "closed", "exit_price": px, "exit_ts": o["filled_at"], "exit_reason": reason,
            "pnl": round(pnl, 2), "updated_at": clock.now().tz_convert("UTC").isoformat()})
        self.log("closed", occ=t["occ"], reason=reason, pnl=round(pnl, 2))

    def flatten_orphans(self, why):
        """Close any option position the trade table doesn't account for —
        a crash between order and insert, or a manual order in the account."""
        tracked = {t["occ"] for t in broker.today_trades() if t["status"] == "open"}
        for p in alpaca.positions():
            if p.get("asset_class") == "us_option" and p["symbol"] not in tracked:
                for o in alpaca.orders("open"):
                    if o["symbol"] == p["symbol"]:
                        broker.cancel(o["id"])
                clock.sleep(1)
                broker.close_position(p["symbol"], int(float(p["qty"])))
                self.log("orphan_closed", occ=p["symbol"], why=why)

    # ------------------------------------------------------------ main loop
    def another_runner_alive(self):
        fresh = (clock.now().tz_convert("UTC") - pd.Timedelta(minutes=3)).isoformat()
        rows = supa.select("lab_runs", as_of=f"eq.{self.today}", status="eq.running",
                           heartbeat_at=f"gte.{fresh}")
        return bool(rows)

    def run(self, once=False):
        clk = alpaca.clock()
        now = clock.now()
        start, end = WINDOWS[self.window]
        start_ts = pd.Timestamp(f"{self.today} {start}", tz=NY)
        end_ts = pd.Timestamp(f"{self.today} {end}", tz=NY)
        if now >= end_ts - pd.Timedelta(minutes=5):
            print("window already over; exiting", flush=True)
            return
        if not clk["is_open"]:
            nxt = pd.Timestamp(clk["next_open"]).tz_convert(NY)
            if nxt.date() != self.today or nxt > end_ts:
                print("market not open in this window; exiting", flush=True)
                return
            start_ts = max(start_ts, nxt)
        if not once and now < start_ts:
            print(f"sleeping until {start_ts}", flush=True)
            clock.sleep(max(0, (start_ts - now).total_seconds()))
        # the DST-spare cron entry, or a manual dispatch, while a runner is up
        if not once and self.another_runner_alive():
            print("another runner is heartbeating; exiting", flush=True)
            return
        # early close days: the clock's next_close wins over the window
        close = pd.Timestamp(alpaca.clock()["next_close"]).tz_convert(NY)
        end_ts = min(end_ts, close - pd.Timedelta(minutes=5))
        self.load_strategies(force=True)
        open_trades = [t for t in broker.today_trades() if t["status"] in ("pending", "open")]
        if not self.strats and not open_trades and not once:
            print("no strategies at paper/live and nothing open; exiting", flush=True)
            return
        self.load_context()
        self.heartbeat()
        self.log("start", window=self.window, end=str(end_ts), strategies=[s["name"] for s in self.strats],
                 paper=alpaca.is_paper(), dry_run=self.dry_run)
        last_beat = clock.time()
        while True:
            try:
                self.load_strategies()
                ctl = broker.controls()
                day = self.today_bars()
                self.manage(ctl, day)
                if len(day) and day.index[-1] != self.last_bar:
                    self.last_bar = day.index[-1]
                    # evaluated even when halted: broker refuses the entry and
                    # the signal is kept as `rejected`, the shadow record
                    self.evaluate(day)
            except Exception as e:
                self.error("loop", e)
            if clock.time() - last_beat > 60:
                self.heartbeat()
                last_beat = clock.time()
            if once or clock.now() >= end_ts:
                break
            # wake ~4s after each minute boundary, when the bar has printed,
            # and every 15s between for exit checks
            clock.sleep(15 - (clock.time() % 15) + (4 if clock.time() % 60 > 55 else 0))
        self.heartbeat("done", end=True)


def pick_window():
    hhmm = clock.now().strftime("%H:%M")
    return "morning" if hhmm < "12:45" else "afternoon"


def main(argv=None):
    p = argparse.ArgumentParser(prog="lab.runner")
    p.add_argument("--window", choices=["morning", "afternoon", "auto"], default="auto")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--once", action="store_true")
    a = p.parse_args(argv)
    w = pick_window() if a.window == "auto" else a.window
    Runner(w, dry_run=a.dry_run).run(once=a.once)


if __name__ == "__main__":
    main()
