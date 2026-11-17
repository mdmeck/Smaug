"""The only code that sends orders. Every limit is checked here, in code,
against `lab_controls` as freshly read — not against anything a strategy or
the routine says about itself.

Allowed order shapes, and nothing else:
  * buy-to-open a single call or put, limit price, TIF day
  * sell-to-close up to the quantity actually held (stop, limit or market)
No selling to open outside a covered combo, no equities — a bug can lose
the premium of one long option, never more than that.

Defined-risk credit spreads (docs/lab-credit-spreads-proposal.md) are being
added paper-only. So far the only spread code here is probe_spread_order(),
which checks the paper account accepts a combo: one order priced where it
can't fill, canceled at once.
"""

import pandas as pd

from . import alpaca, clock, spreads, supa
from .alpaca import NY

ENTRY_WINDOW = ("09:31", "15:30")


class Refused(Exception):
    pass


def controls():
    rows = supa.select("lab_controls")
    if not rows:
        raise Refused("no lab_controls row")
    return rows[0]


def today_trades():
    d = clock.now().normalize()
    return supa.select("lab_trades", signal_ts=f"gte.{d.isoformat()}")


def day_pnl(trades=None):
    """Realized P/L from today's closed trades plus unrealized on open Alpaca
    positions. Broker positions, not our own table, are the authority on what
    is open."""
    trades = today_trades() if trades is None else trades
    realized = sum(t["pnl"] or 0 for t in trades if t["status"] == "closed")
    unrealized = sum(float(p.get("unrealized_pl") or 0) for p in alpaca.positions()
                     if p.get("asset_class") == "us_option")
    return realized + unrealized


def check_mode(c, strategy_status):
    paper = alpaca.is_paper()
    if c["mode"] == "live" and paper:
        raise Refused("controls say live but endpoint is paper")
    if not paper:
        if c["mode"] != "live":
            raise Refused("live endpoint but controls.mode is paper")
        if strategy_status != "live":
            raise Refused("strategy not promoted to live")


def open_position(strategy, occ, contracts, limit_price):
    """Buy-to-open. Raises Refused with the reason if any limit fails."""
    c = controls()
    if not c["trading_enabled"]:
        raise Refused("trading disabled (kill switch)")
    check_mode(c, strategy["status"])
    now = clock.now()
    if not (ENTRY_WINDOW[0] <= now.strftime("%H:%M") <= ENTRY_WINDOW[1]):
        raise Refused(f"outside entry window {ENTRY_WINDOW}")
    if contracts < 1 or contracts > c["max_contracts"]:
        raise Refused(f"contracts {contracts} > max {c['max_contracts']}")
    cost = limit_price * 100 * contracts
    if cost > c["max_premium_per_trade"]:
        raise Refused(f"premium ${cost:.0f} > max ${c['max_premium_per_trade']:.0f}")
    trades = today_trades()
    n_today = sum(1 for t in trades if t["status"] in ("pending", "open", "closed"))
    if n_today >= c["max_trades_per_day"]:
        raise Refused(f"{n_today} trades today >= max {c['max_trades_per_day']}")
    held = [p for p in alpaca.positions() if p.get("asset_class") == "us_option"]
    pending = [t for t in trades if t["status"] == "pending"]
    if len(held) + len(pending) >= c["max_open_positions"]:
        raise Refused(f"{len(held)} open + {len(pending)} pending >= max {c['max_open_positions']}")
    pnl = day_pnl(trades)
    if pnl <= -c["max_daily_loss"]:
        raise Refused(f"daily loss ${pnl:.0f} hit limit ${c['max_daily_loss']:.0f}")
    return alpaca.submit_order(symbol=occ, qty=str(contracts), side="buy", type="limit",
                               limit_price=f"{limit_price:.2f}", time_in_force="day",
                               client_order_id=f"lab-{strategy['name']}-{now:%Y%m%d%H%M%S}"[:48])


def _held_qty(occ):
    for p in alpaca.positions():
        if p["symbol"] == occ:
            return int(float(p["qty"]))
    return 0


def place_stop(occ, contracts, stop_price):
    qty = min(contracts, _held_qty(occ))
    if qty <= 0:
        raise Refused(f"no position in {occ} to protect")
    return alpaca.submit_order(symbol=occ, qty=str(qty), side="sell", type="stop",
                               stop_price=f"{max(0.01, stop_price):.2f}", time_in_force="day")


def close_position(occ, contracts, limit_price=None):
    """Sell-to-close. Never more than held, so it can't open a short."""
    qty = min(contracts, _held_qty(occ))
    if qty <= 0:
        return None
    kw = dict(symbol=occ, qty=str(qty), side="sell", time_in_force="day")
    if limit_price:
        kw.update(type="limit", limit_price=f"{limit_price:.2f}")
    else:
        kw.update(type="market")
    return alpaca.submit_order(**kw)


def cancel(order_id):
    if order_id:
        try:
            alpaca.cancel_order(order_id)
        except alpaca.AlpacaError as e:
            # 422 = already filled/canceled; that's the answer we wanted
            if "422" not in str(e) and "404" not in str(e):
                raise


def probe_spread_order():
    """Paper only. Reads the account's options level, then submits one put
    credit spread far out of the money asking a credit of 90% of its width
    (it can't fill), cancels it, and confirms nothing is held. Answers
    whether Alpaca accepts the combo shape the spread path will use."""
    if not alpaca.is_paper():
        raise Refused("spread probe runs on the paper endpoint only")
    res = {"paper": True}
    acct = alpaca.account()
    res["account"] = {k: acct.get(k) for k in ("status", "options_trading_level", "options_approved_level",
                                                "options_buying_power", "buying_power")}
    clk = alpaca.clock()
    res["market_open"] = clk.get("is_open")
    now = clock.now()
    cal = alpaca.calendar(now.date(), (now + pd.Timedelta(days=7)).date())
    expiry = None
    for c in cal:
        close = pd.Timestamp(f"{c['date']} {c['close']}", tz=NY)
        if close > now:
            expiry = pd.Timestamp(c["date"]).date()
            break
    bar = alpaca.latest_stock_bar("SPY")
    spot = float(bar["close"])
    snaps = alpaca.option_snapshots("SPY", expiry)
    puts = sorted({int(o[-8:]) / 1000 for o in snaps if o[-9] == "P" and int(o[-8:]) / 1000 <= 0.92 * spot})
    if len(puts) < 2:
        res["error"] = f"no listed puts below {0.92 * spot:.0f} for {expiry}"
        return res
    short, long_ = puts[-1], puts[-2]
    width = short - long_
    legs = [spreads.Leg("P", short, "sell", alpaca.occ_symbol("SPY", expiry, "P", short)),
            spreads.Leg("P", long_, "buy", alpaca.occ_symbol("SPY", expiry, "P", long_))]
    body = spreads.mleg_order(legs, 1, -0.9 * width, "open")
    body["client_order_id"] = f"lab-spread-probe-{now:%Y%m%d%H%M%S}"
    res.update(spot=spot, expiry=str(expiry), legs=[lg.occ for lg in legs], width=width,
               limit_price=body["limit_price"])
    try:
        o = alpaca.submit_order(**body)
    except alpaca.AlpacaError as e:
        res["accepted"] = False
        res["reject"] = str(e)[:500]
        return res
    res["accepted"] = True
    res["order_status"] = o.get("status")
    res["order_class"] = o.get("order_class")
    try:
        alpaca.cancel_order(o["id"])
    except alpaca.AlpacaError as e:
        res["cancel_error"] = str(e)[:300]
    for _ in range(10):
        clock.sleep(1)
        st = alpaca.get_order(o["id"])
        if st.get("status") in ("canceled", "filled", "rejected", "expired"):
            break
    res["final_status"] = st.get("status")
    res["filled_qty"] = st.get("filled_qty")
    held = [p for p in alpaca.positions() if p["symbol"] in {lg.occ for lg in legs}]
    res["held_after"] = [(p["symbol"], p["qty"]) for p in held]
    if held:
        # can't happen at that price; if it did, close the whole combo at once
        close = spreads.mleg_order(legs, 1, 0, "close")
        close["type"] = "market"
        close.pop("limit_price")
        alpaca.submit_order(**close)
        res["flattened"] = True
    return res
