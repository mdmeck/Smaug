"""Thin Alpaca REST client: market data + paper/live trading.

Plain `requests`, no SDK — same reasoning as the pipeline's Supabase access:
one less dependency to pin, and the error body (where Alpaca says *why*) is
never swallowed.

Keys come from ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY. The trading base
URL defaults to paper; live is opt-in via ALPACA_TRADING_URL, and broker.py
refuses to trade live unless `lab_controls.mode` says so as well — two
independent switches, so one stray env var can't spend real money.

Free-plan constraints that shape everything downstream:
  * historical stock + option bars exclude the most recent 15 minutes
    (SIP). Intraday decisions read the real-time IEX feed instead.
  * options history starts in early 2024. Older periods are priced by the
    model in options.py.
  * options take market / limit / stop / stop_limit, TIF day|gtc — no
    bracket or OCO. Exits are managed by runner.py, with a resting stop at
    the broker as the backstop if the runner dies.
"""
import os
import time
from datetime import date, datetime

import pandas as pd
import requests

DATA_URL = "https://data.alpaca.markets"
PAPER_URL = "https://paper-api.alpaca.markets"
NY = "America/New_York"


class AlpacaError(RuntimeError):
    pass


def _headers():
    key = os.environ.get("ALPACA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET_KEY")
    if not key or not secret:
        raise AlpacaError("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def trading_url():
    return os.environ.get("ALPACA_TRADING_URL", PAPER_URL).rstrip("/")


def is_paper():
    return trading_url() == PAPER_URL


def _request(method, url, *, params=None, json=None, retries=5):
    for attempt in range(retries):
        r = requests.request(method, url, headers=_headers(), params=params,
                             json=json, timeout=30)
        # 429 = rate limit (200/min on the free plan); 5xx = transient
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(60, 2 ** attempt * 2))
            continue
        if r.status_code >= 400:
            raise AlpacaError(f"{method} {url} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else None
    raise AlpacaError(f"{method} {url} failed after {retries} retries ({r.status_code}): {r.text[:300]}")


def _iso(x):
    if isinstance(x, (datetime, pd.Timestamp)):
        ts = pd.Timestamp(x)
        if ts.tzinfo is None:
            ts = ts.tz_localize(NY)
        return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(x, date):
        return x.isoformat()
    return str(x)


def _paged_bars(path, params):
    out = {}
    params = {**params, "limit": 10000}
    while True:
        j = _request("GET", DATA_URL + path, params=params)
        for sym, rows in (j.get("bars") or {}).items():
            out.setdefault(sym, []).extend(rows)
        tok = j.get("next_page_token")
        if not tok:
            return out
        params["page_token"] = tok


def _to_frame(rows):
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap", "trades"])
    df = pd.DataFrame(rows).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close",
                                            "v": "volume", "vw": "vwap", "n": "trades"})
    df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(NY)
    return df.set_index("t").sort_index()[["open", "high", "low", "close", "volume", "vwap", "trades"]]


# ---------------------------------------------------------------- market data

def stock_bars(symbol, start, end, timeframe="1Min", feed="sip", adjustment="all"):
    """Bars for one symbol as a NY-indexed DataFrame. Includes extended hours;
    callers filter to RTH. `adjustment='all'` so a split or dividend doesn't
    read as a gap in a multi-year backtest."""
    rows = _paged_bars("/v2/stocks/bars", {
        "symbols": symbol, "timeframe": timeframe, "start": _iso(start), "end": _iso(end),
        "feed": feed, "adjustment": adjustment,
    })
    return _to_frame(rows.get(symbol, []))


def option_bars(symbols, start, end, timeframe="1Min"):
    """{occ_symbol: DataFrame}. Up to 100 symbols per request."""
    symbols = list(symbols)
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        rows = _paged_bars("/v1beta1/options/bars", {
            "symbols": ",".join(chunk), "timeframe": timeframe,
            "start": _iso(start), "end": _iso(end),
        })
        for s in chunk:
            out[s] = _to_frame(rows.get(s, []))
    return out


def latest_stock_bar(symbol, feed="iex"):
    j = _request("GET", DATA_URL + "/v2/stocks/bars/latest", params={"symbols": symbol, "feed": feed})
    b = (j.get("bars") or {}).get(symbol)
    return _to_frame([b]).iloc[0] if b else None


def option_snapshots(underlying, expiration, feed="indicative"):
    """Chain snapshot for one expiry: {occ: {quote, greeks, impliedVolatility, ...}}."""
    out, params = {}, {"feed": feed, "expiration_date": _iso(expiration), "limit": 1000}
    while True:
        j = _request("GET", f"{DATA_URL}/v1beta1/options/snapshots/{underlying}", params=params)
        out.update(j.get("snapshots") or {})
        tok = j.get("next_page_token")
        if not tok:
            return out
        params["page_token"] = tok


def option_quotes_latest(symbols, feed="indicative"):
    j = _request("GET", DATA_URL + "/v1beta1/options/quotes/latest",
                 params={"symbols": ",".join(symbols), "feed": feed})
    return j.get("quotes") or {}


def occ_symbol(underlying, expiry, right, strike):
    """OCC/OSI option symbol, e.g. SPY260116C00580000."""
    d = pd.Timestamp(expiry)
    return f"{underlying}{d:%y%m%d}{right.upper()[0]}{int(round(strike * 1000)):08d}"


# -------------------------------------------------------------------- trading

def clock():
    return _request("GET", trading_url() + "/v2/clock")


def calendar(start, end):
    return _request("GET", trading_url() + "/v2/calendar", params={"start": _iso(start), "end": _iso(end)})


def account():
    return _request("GET", trading_url() + "/v2/account")


def positions():
    return _request("GET", trading_url() + "/v2/positions")


def orders(status="open", after=None, limit=500):
    p = {"status": status, "limit": limit, "direction": "asc"}
    if after:
        p["after"] = _iso(after)
    return _request("GET", trading_url() + "/v2/orders", params=p)


def get_order(order_id):
    return _request("GET", f"{trading_url()}/v2/orders/{order_id}")


def submit_order(**kw):
    """Raw order submit. Only broker.py should call this — it is the one
    place limits are enforced."""
    return _request("POST", trading_url() + "/v2/orders", json=kw)


def cancel_order(order_id):
    return _request("DELETE", f"{trading_url()}/v2/orders/{order_id}")
