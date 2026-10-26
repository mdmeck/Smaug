"""Option contract selection, Black-Scholes, and the cost model.

The cost model is deliberately pessimistic. On 0DTE SPY a backtest that
fills at the mid is the single most common way to manufacture an edge that
doesn't exist: the round trip on a $1.00 contract costs ~2-4% of premium in
spread before any fees, and a scalper with a 1:1 target pays that on every
trade. Every number below errs toward a worse fill.
"""
import math

import numpy as np
import pandas as pd

# per-side regulatory/clearing fees per contract. Alpaca charges no
# commission on options; ORF + OCC + TAF come to ~$0.02-0.03. Rounded up.
FEE_PER_CONTRACT = 0.05
# half the bid/ask we expect to cross, per side, as max(floor, frac * price).
# SPY near-ATM short-dated quotes are usually a penny or two wide under $2 and
# widen with price; the floor covers the penny, the fraction the rest.
HALF_SPREAD_FLOOR = 0.01
HALF_SPREAD_FRAC = 0.01
# extra adverse slippage on stop-market exits (they fill into a moving book)
STOP_SLIP_FRAC = 0.02

TRADING_MIN_PER_YEAR = 252 * 390
RATE = 0.04


def half_spread(px):
    return max(HALF_SPREAD_FLOOR, HALF_SPREAD_FRAC * px)


def buy_fill(px):
    return px + half_spread(px)


def sell_fill(px, stop=False):
    f = px - half_spread(px)
    if stop:
        f -= STOP_SLIP_FRAC * px
    return max(0.01, f)


def strike_for(spot, right, otm=0, increment=1.0):
    """`otm` strikes out of the money (0 = nearest ATM, negative = ITM)."""
    atm = round(spot / increment) * increment
    return atm + otm * increment if right == "C" else atm - otm * increment


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot, strike, t_years, sigma, right, r=RATE):
    if t_years <= 0 or sigma <= 0:
        return max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / sq
    d2 = d1 - sq
    if right == "C":
        return spot * _ncdf(d1) - strike * math.exp(-r * t_years) * _ncdf(d2)
    return strike * math.exp(-r * t_years) * _ncdf(-d2) - spot * _ncdf(-d1)


def implied_vol(price, spot, strike, t_years, right, lo=0.01, hi=5.0):
    intrinsic = max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    if price <= intrinsic + 1e-6 or t_years <= 0:
        return np.nan
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t_years, mid, right) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def years_to_expiry(ts, expiry_close):
    """Trading-time years from `ts` to the expiry's 16:00 close, counting only
    RTH minutes (overnight variance is folded into sigma by convention)."""
    mins = (pd.Timestamp(expiry_close) - pd.Timestamp(ts)).total_seconds() / 60.0
    days = (pd.Timestamp(expiry_close).normalize() - pd.Timestamp(ts).normalize()).days
    # strip non-trading overnight time: each calendar day boundary removes 1050 min
    rth_mins = max(0.0, mins - days * (1440 - 390))
    return rth_mins / TRADING_MIN_PER_YEAR


def model_path(underlying, strike, right, expiry_close, sigma):
    """Modeled per-minute OHLC for an option from underlying minute bars.
    High/low come from pricing at the underlying's favourable/adverse
    extremes — an approximation, but on the conservative side for stops."""
    rows = []
    for ts, b in underlying.iterrows():
        t = years_to_expiry(ts + pd.Timedelta(minutes=1), expiry_close)
        p = {k: bs_price(b[k], strike, t, sigma, right) for k in ("open", "high", "low", "close")}
        hi, lo = (p["high"], p["low"]) if right == "C" else (p["low"], p["high"])
        rows.append((ts, p["open"], max(hi, lo), min(hi, lo), p["close"]))
    return pd.DataFrame(rows, columns=["t", "open", "high", "low", "close"]).set_index("t")
