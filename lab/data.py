"""Historical data with an on-disk cache.

Research pulls years of minute bars straight from Alpaca rather than storing
them in Supabase: a decade of SPY alone is ~1M rows, and the free tier's
500MB would be gone after a handful of tickers. Supabase holds results only.

Cache layout (gitignored, under LAB_CACHE or lab/.cache):
  stocks/<SYM>/split/<YYYY-MM>.pkl  one month of split-adjusted 1-min bars
  options/<YYYY-MM-DD>/<OCC>.pkl one contract's 1-min bars on one day
Completed months/days never change, so a cached file is reused forever; the
current month is always refetched.
"""
import os
from pathlib import Path

import pandas as pd

from . import alpaca

NY = alpaca.NY
CACHE = Path(os.environ.get("LAB_CACHE", Path(__file__).parent / ".cache"))


def _month_range(start, end):
    m = pd.Timestamp(start).to_period("M")
    last = pd.Timestamp(end).to_period("M")
    while m <= last:
        yield m
        m += 1


def stock_minutes(symbol, start, end, rth=True):
    """1-min bars for [start, end] (dates), NY-indexed. RTH-only by default
    (09:30-15:59 bar opens)."""
    today = pd.Timestamp.now(tz=NY).to_period("M")
    frames = []
    for m in _month_range(start, end):
        path = CACHE / "stocks" / symbol / "split" / f"{m}.pkl"
        if path.exists() and m < today:
            df = pd.read_pickle(path)
        else:
            s = m.start_time.tz_localize(NY)
            e = min((m + 1).start_time.tz_localize(NY), pd.Timestamp.now(tz=NY) - pd.Timedelta(minutes=16))
            df = alpaca.stock_bars(symbol, s, e)
            if m < today:
                path.parent.mkdir(parents=True, exist_ok=True)
                df.to_pickle(path)
        frames.append(df)
    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated()]
    df = df.loc[str(pd.Timestamp(start).date()):str(pd.Timestamp(end).date())]
    if rth:
        df = df.between_time("09:30", "15:59")
    return df


def option_day(occ, day):
    """1-min bars for one contract on one session (RTH). Empty frame when the
    contract didn't trade or the day predates Alpaca's options history."""
    day = pd.Timestamp(day).date()
    path = CACHE / "options" / str(day) / f"{occ}.pkl"
    if path.exists():
        return pd.read_pickle(path)
    s = pd.Timestamp(f"{day} 09:30", tz=NY)
    e = pd.Timestamp(f"{day} 16:00", tz=NY)
    df = alpaca.option_bars([occ], s, e)[occ]
    if day < pd.Timestamp.now(tz=NY).date():
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_pickle(path)
    return df


def vix_daily(start, end):
    """Daily VIX closes (yfinance — Alpaca has no index data). Used only to
    seed modeled option prices for pre-2024 periods; see options.py."""
    path = CACHE / "vix.pkl"
    if path.exists():
        v = pd.read_pickle(path)
        if v.index.max() >= pd.Timestamp(end).normalize() - pd.Timedelta(days=4):
            return v.loc[str(pd.Timestamp(start).date()):str(pd.Timestamp(end).date())]
    import yfinance as yf
    v = yf.download("^VIX", start="2015-01-01", progress=False, auto_adjust=False)["Close"]
    if isinstance(v, pd.DataFrame):
        v = v.iloc[:, 0]
    v.index = pd.to_datetime(v.index).normalize()
    path.parent.mkdir(parents=True, exist_ok=True)
    v.to_pickle(path)
    return v.loc[str(pd.Timestamp(start).date()):str(pd.Timestamp(end).date())]


def sessions(df):
    """Split a minute frame into {date: day_frame}."""
    return {d: g for d, g in df.groupby(df.index.date)}
