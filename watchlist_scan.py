#!/usr/bin/env python3
"""Watchlist — a pre-market planning sheet for a fixed list of tickers.

Standalone from smaug_pipeline.py and erebor_scan.py on purpose: a yfinance
failure on one single name should not be able to touch the SPY pipeline or the
Erebor screens. Writes one `watchlist_snapshots` row per ticker per day; the
Watchlist tab reads the latest day and the watchlist routine
(docs/watchlist-routine-prompt.md) adds its own levels to the same rows.

Everything here is mechanical and re-runnable. The AI levels are deliberately
kept out: this script never sends the `ai_*` columns, and PostgREST's
merge-duplicates only updates the columns in the payload, so a re-run on the
same day cannot erase what the routine wrote.

The routine's environment blocks market-data sites, so `context` carries the
recent bars the routine needs. Dropping it would leave the routine nothing to
read levels from.

Usage (needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in env, except --dry-run):
    python watchlist_scan.py                      # normal run
    python watchlist_scan.py --dry-run            # compute and print, write nothing
    python watchlist_scan.py --require-premarket  # no-op on a market holiday
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

# The service-role key has no auth.uid(), so the NOT NULL DEFAULT auth.uid()
# column would fail without this. Not a secret: see CLAUDE.md.
USER_ID = "c0b48756-5f94-4862-886a-8ecdb7099ef6"

NY = ZoneInfo("America/New_York")
OPEN, CLOSE = time(9, 30), time(16, 0)
REQUEST_TIMEOUT = 30

# Order and grouping follow the trader's TradingView watchlist. SPX is left out
# for now (Yahoo calls it ^SPX and has no earnings for it).
WATCHLIST = [
    ("Tech", ["TSLA", "AAPL", "NFLX", "INTC", "MSFT", "AMZN", "GOOG", "SPCX"]),
    ("Semis", ["NVDA", "AMD", "MU", "PLTR"]),
    ("Indices", ["SMH", "SPY", "QQQ", "USO"]),
]

EMA_LEN = 20          # both trends, so Weekly and Daily read the same way
EMA_SLOPE_BARS = 3    # "rising" = the EMA is above where it was this many bars ago
SWING_LEFT = 5        # same 5/5 pivot as smaug_pipeline.py
SWING_RIGHT = 5
PIVOT_LOOKBACK = 252  # sessions; older swings are rarely what price trades around
ATR_LEN = 14
MERGE_ATR = 0.25      # levels closer than this many ATRs are one level
LEVELS_PER_SIDE = 3
MAX_STRIKE_GAP = 0.015  # of price; further than this isn't at the money
MAX_EXPIRIES = 6      # how far out to look for an ATM strike before giving up
CONTEXT_DAILY = 40    # bars handed to the routine in `context`
CONTEXT_WEEKLY = 26


def _num(v, places=2):
    """A rounded float, or None for NaN/inf: json would otherwise emit a bare
    `NaN`, which PostgREST rejects along with the whole batch."""
    if v is None:
        return None
    v = float(v)
    return round(v, places) if math.isfinite(v) else None


def _headers():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in the environment."
        )
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


def completed_sessions(daily, now):
    """Daily bars for finished sessions only. Before the close, today's row
    (when Yahoo has one) is a partial bar, and treating it as finished would
    make today's own range into "prior day" levels."""
    d = daily.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    d.index = d.index.tz_convert(NY)
    if now.time() < CLOSE:
        d = d[d.index.date < now.date()]
    return d


def to_weekly(d):
    return (
        d.resample("W-FRI")
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        .dropna(subset=["Close"])
    )


def trend(closes):
    """Up, Down or Range from the close against a rising or falling 20 EMA.
    None when there are too few bars for the EMA to mean anything, which is
    SPCX's position on weekly bars until it has been listed a while longer."""
    if len(closes) < EMA_LEN:
        return None
    ema = closes.ewm(span=EMA_LEN, adjust=False).mean()
    close, now, then = closes.iloc[-1], ema.iloc[-1], ema.iloc[-1 - EMA_SLOPE_BARS]
    if close > now and now > then:
        return "Up"
    if close < now and now < then:
        return "Down"
    return "Range"


def atr(d):
    prev = d["Close"].shift(1)
    tr = pd.concat(
        [d["High"] - d["Low"], (d["High"] - prev).abs(), (d["Low"] - prev).abs()], axis=1
    ).max(axis=1)
    return tr.tail(ATR_LEN).mean()


def swing_pivots(d):
    """Confirmed daily swing highs and lows. A bar needs SWING_RIGHT finished
    bars after it before it can count, so nothing here sees the future."""
    d = d.tail(PIVOT_LOOKBACK)
    highs, lows = d["High"].tolist(), d["Low"].tolist()
    out = []
    for i in range(SWING_LEFT, len(d) - SWING_RIGHT):
        hood = range(i - SWING_LEFT, i + SWING_RIGHT + 1)
        if highs[i] == max(highs[k] for k in hood):
            out.append(highs[i])
        if lows[i] == min(lows[k] for k in hood):
            out.append(lows[i])
    return out


def named_levels(d, today, premarket):
    """(price, tag) pairs. MH/ML is the prior calendar month; PMH/PML is
    today's pre-market, the sense traders mean by PMH."""
    out = []

    def add(hi, lo, hi_tag, lo_tag):
        out.append((float(hi), hi_tag))
        out.append((float(lo), lo_tag))

    last = d.iloc[-1]
    add(last["High"], last["Low"], "PDH", "PDL")

    # Week and month in progress are left out, or their "prior" high would
    # just be the last few days over again.
    weeks = to_weekly(d)
    weeks = weeks[weeks.index.date < today]
    if len(weeks):
        add(weeks["High"].iloc[-1], weeks["Low"].iloc[-1], "PWH", "PWL")

    month_key = d.index.year * 12 + d.index.month
    prior = month_key[month_key < today.year * 12 + today.month]
    if len(prior):
        m = d[month_key == prior.max()]
        add(m["High"].max(), m["Low"].min(), "MH", "ML")

    if premarket is not None and len(premarket):
        add(premarket["High"].max(), premarket["Low"].min(), "PMH", "PML")
    return out


def merge_levels(cands, tol):
    """Cluster (price, tag) pairs into levels. Each cluster is measured from
    its first member, so a long ladder of near-neighbors can't chain into one
    level wider than `tol`."""
    groups = []
    for price, tag in sorted(cands):
        if groups and price - groups[-1][0][0] <= tol:
            groups[-1].append((price, tag))
        else:
            groups.append([(price, tag)])
    levels = []
    for g in groups:
        tags = []
        for _, tag in g:
            if tag not in tags:
                tags.append(tag)
        levels.append({"price": _num(sum(p for p, _ in g) / len(g)), "tags": tags})
    return levels


def split_levels(levels, price):
    """Nearest LEVELS_PER_SIDE on each side of price, nearest first. Side is
    decided by where price is now, not what the level used to be: a broken
    swing high below price is support."""
    above = sorted((x for x in levels if x["price"] > price), key=lambda x: x["price"])
    below = sorted((x for x in levels if x["price"] < price), key=lambda x: -x["price"])
    return below[:LEVELS_PER_SIDE], above[:LEVELS_PER_SIDE]


def _mid(row):
    """(price, basis). Before the open Yahoo often has no live two-sided
    quote, so a mid may be impossible; fall back to the last trade and say so."""
    bid, ask, last = row.get("bid"), row.get("ask"), row.get("lastPrice")
    if bid and ask and bid > 0 and ask > 0 and math.isfinite(bid) and math.isfinite(ask):
        return _num((bid + ask) / 2), "mid"
    if last and math.isfinite(last) and last > 0:
        return _num(last), "last"
    return None, None


def option_quote(tk, price, today):
    """ATM call and put at the nearest expiry that really has an ATM strike.
    Options don't trade pre-market, so these are the prior session's quotes on
    a strike chosen from today's price; the tab labels them that way.

    Yahoo's chains have holes: on 2026-11-06, with MU at 695, its nearest
    expiry started at strike 805, and the "ATM" call read 0.19 against a 113
    put. Hence MAX_STRIKE_GAP, and walking out to a later expiry rather than
    printing a contract nowhere near the money. The tab always shows the
    expiry, so a later one is visible rather than implied.
    """
    expiries = [e for e in (tk.options or ()) if e >= today.isoformat()]
    for expiry in expiries[:MAX_EXPIRIES]:
        chain = tk.option_chain(expiry)
        strikes = set(chain.calls["strike"]) & set(chain.puts["strike"])
        if not strikes:
            continue
        strike = min(strikes, key=lambda s: abs(s - price))
        if abs(strike - price) / price > MAX_STRIKE_GAP:
            continue
        call = chain.calls[chain.calls["strike"] == strike].iloc[0].to_dict()
        put = chain.puts[chain.puts["strike"] == strike].iloc[0].to_dict()
        call_px, call_basis = _mid(call)
        put_px, put_basis = _mid(put)
        return {
            "expiry": expiry,
            "strike": _num(strike),
            "call": call_px,
            "call_basis": call_basis,
            "put": put_px,
            "put_basis": put_basis,
        }
    return None


def next_earnings(tk, today):
    """Next date on or after today. Yahoo keeps a past date until it has a new
    one (SPCX on 2026-11-06), and ETFs have none: both mean None, never a stale
    date, and neither is allowed to fail the row."""
    try:
        cal = tk.calendar or {}
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        upcoming = sorted(x for x in (dates or []) if x >= today)
        return upcoming[0].isoformat() if upcoming else None
    except Exception:  # noqa: BLE001 - optional context, never fatal
        return None


def _bars(df, with_volume):
    rows = []
    for ts, r in df.iterrows():
        row = [ts.date().isoformat(), _num(r["Open"]), _num(r["High"]), _num(r["Low"]), _num(r["Close"])]
        if with_volume:
            row.append(int(r["Volume"]) if math.isfinite(r["Volume"]) else None)
        rows.append(row)
    return rows


def intraday_today(tk, now):
    df = tk.history(period="1d", interval="5m", prepost=True, auto_adjust=False)
    if df.empty:
        return None
    df = df.dropna(subset=["High", "Low", "Close"])
    df.index = df.index.tz_convert(NY)
    return df[df.index.date == now.date()]


def scan_ticker(symbol, now):
    today = now.date()
    tk = yf.Ticker(symbol)

    # auto_adjust=False: levels have to be prices that really printed. Split
    # adjustment still applies; dividend adjustment would shift them.
    daily = tk.history(period="2y", interval="1d", auto_adjust=False)
    d = completed_sessions(daily, now)
    if d.empty:
        raise RuntimeError("no daily bars")

    intra = intraday_today(tk, now)
    premarket = None
    if intra is not None and len(intra):
        premarket = intra[intra.index.time < OPEN]

    prev_close = float(d["Close"].iloc[-1])
    if intra is not None and len(intra):
        price = float(intra["Close"].iloc[-1])
        price_as_of = intra.index[-1]
        source = "premarket" if now.time() < OPEN else ("session" if now.time() < CLOSE else "postmarket")
    else:
        price, price_as_of, source = prev_close, d.index[-1], "close"

    weekly = to_weekly(d)
    cands = [(p, "pivot") for p in swing_pivots(d)] + named_levels(d, today, premarket)
    support, resistance = split_levels(merge_levels(cands, MERGE_ATR * atr(d)), price)

    return {
        "price": _num(price),
        "price_source": source,
        "price_as_of": price_as_of.isoformat(),
        "prev_close": _num(prev_close),
        "htf_trend": trend(weekly["Close"]),
        "ltf_trend": trend(d["Close"]),
        "next_earnings": next_earnings(tk, today),
        "option": option_quote(tk, price, today),
        "support": support,
        "resistance": resistance,
        "context": {
            "atr14": _num(atr(d)),
            "daily": _bars(d.tail(CONTEXT_DAILY), True),
            "weekly": _bars(weekly.tail(CONTEXT_WEEKLY), False),
        },
        "error": None,
    }


def blank_row(error):
    return {
        "price": None,
        "price_source": None,
        "price_as_of": None,
        "prev_close": None,
        "htf_trend": None,
        "ltf_trend": None,
        "next_earnings": None,
        "option": None,
        "support": [],
        "resistance": [],
        "context": None,
        "error": error,
    }


def is_market_day(now):
    """Holidays have no pre-market prints, which saves keeping a calendar."""
    intra = intraday_today(yf.Ticker("SPY"), now)
    return intra is not None and len(intra) > 0


def scan_all(now):
    rows = []
    order = 0
    for group, tickers in WATCHLIST:
        for symbol in tickers:
            try:
                row = scan_ticker(symbol, now)
            except Exception as e:  # noqa: BLE001 - one bad name mustn't cost the sheet
                print(f"{symbol}: {e}", file=sys.stderr)
                row = blank_row(str(e)[:200])
            rows.append(
                {
                    **row,
                    "user_id": USER_ID,
                    "run_date": now.date().isoformat(),
                    "ticker": symbol,
                    "grp": group,
                    "sort_order": order,
                }
            )
            order += 1
    return rows


def write_rows(rows):
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/watchlist_snapshots",
        headers=_headers(),
        params={"on_conflict": "user_id,run_date,ticker"},
        data=json.dumps(rows, allow_nan=False),
        timeout=REQUEST_TIMEOUT,
    )
    # raise_for_status() would drop the body, which is where PostgREST explains itself.
    if resp.status_code >= 400:
        raise RuntimeError(f"{resp.status_code} {resp.reason}: {resp.text}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    ap.add_argument("--require-premarket", action="store_true", help="exit quietly on a market holiday")
    args = ap.parse_args()

    now = datetime.now(NY)
    if args.require_premarket and not is_market_day(now):
        print("No pre-market prints for SPY today: market holiday, nothing to do.")
        return 0

    rows = scan_all(now)
    failed = [r["ticker"] for r in rows if r["error"]]
    print(f"{len(rows) - len(failed)}/{len(rows)} tickers ok", f"failed: {failed}" if failed else "")

    if args.dry_run:
        for r in rows:
            print(json.dumps({k: v for k, v in r.items() if k not in ("context", "user_id")}))
        print("context bytes per row:", max(len(json.dumps(r["context"])) for r in rows))
        return 0

    if len(failed) == len(rows):
        print("Every ticker failed: leaving yesterday's rows in place.", file=sys.stderr)
        return 1
    write_rows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
