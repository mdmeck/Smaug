#!/usr/bin/env python3
"""Midday review: the morning's break-and-retest setups, found by a script.

Runs once a weekday at about 14:00 ET, after the trader's entry window has
closed but while his charts are still up. Plain Python, no AI: it pulls the
morning's 1-minute bars from Alpaca, applies the *default* rules of
`docs/strategy-break-retest.md` to every symbol, simulates how each setup played
out so far, and matches the day's `journal_entries` against the setups. The
"Smaug Afternoon Review" routine (`docs/afternoon-review-prompt.md`) only reads
what this writes and summarises it.

Writes three tables (see `webapp/supabase/schema.sql`):
  intraday_bars   today's 1-min bars for every symbol, kept INTRADAY_RETENTION_DAYS.
                  Separate from `bars` on purpose: `bars` is keyed on ts alone
                  (SPY only) and the daily pipeline treats every row in it as
                  SPY, so QQQ there would corrupt its features.
  midday_setups   one row per confirmed break and retest (passed every filter or
                  not), replaced for the day on each run.
  midday_runs     one row per day: when it ran, how far the data goes, the
                  journal match, errors. The routine waits for this row.

Data: Alpaca SIP, split-adjusted only (never dividend-adjusted, the repo rule).
The free plan withholds the latest 15 minutes of SIP history, so a 14:00 run
sees bars through about 13:44. That covers every entry (09:35-11:30); outcomes
of trades still open by then are marked to the last bar and labelled "open".

Not the Lab. The Lab's rule is one implementation per strategy in
`lab/strategies/`, and the Lab build is on hold. When it starts, this detector
should move there and this script should call it, so the 2pm check and the Lab
can never disagree about what a setup is.

Known gaps against the spec, all reported in `midday_runs.notes`:
  * FOMC days and the IV-over-60 skip aren't checked (no data source here).
  * Outcomes are in R on the underlying, not option P/L.

Usage (needs ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY, plus SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY unless --dry-run):
    python midday_review.py              # normal run
    python midday_review.py --dry-run    # compute and print, write nothing
    python midday_review.py --selftest   # synthetic scenarios, no network
"""

import argparse
import json
import math
import os
import re
import sys
from datetime import time

import numpy as np
import pandas as pd
import requests

NY = "America/New_York"
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
# The service-role key has no auth.uid(); journal and example reads filter on
# this. Not a secret: see CLAUDE.md.
USER_ID = "c0b48756-5f94-4862-886a-8ecdb7099ef6"

# The spec's universe (its watchlist names plus SMH), and SPY because that's
# what the trader trades day to day. QQQ is the market reference only.
REFERENCE = "QQQ"
SYMBOLS = ["SPY", "TSLA", "AAPL", "NFLX", "INTC", "MSFT", "AMZN", "GOOG",
           "NVDA", "AMD", "MU", "PLTR", "SMH"]

PRIOR_SESSIONS = 10           # 1-min history: prior day/week levels, 1H trend
DAILY_LOOKBACK_DAYS = 150     # calendar days of daily bars (trend + ATR)
SIP_DELAY = pd.Timedelta(minutes=16)
INTRADAY_RETENTION_DAYS = 10
BATCH = 500
TIMEOUT = 30

# ---- spec defaults (docs/strategy-break-retest.md). Names follow the spec.
BUFFER_ATR = 0.1              # break buffer, x ATR(14, 1m)
RETEST_ATR = 0.1              # retest tolerance, x ATR(14, 1m)
RETEST_BARS = 20              # retest must come within this many bars of the break
MERGE_DAILY_ATR = 0.25        # levels this close (x daily ATR) are one zone
PIVOT = 3                     # swing pivots, left = right = 3
EMA_FAST, EMA_SLOW = 9, 21
DAILY_TREND_SESSIONS = 15
BOS_WINDOW = pd.Timedelta(minutes=60)
ENTRY_START, ENTRY_END = time(9, 35), time(11, 30)
FLAT_BY = time(15, 45)
MIN_R_ATR1, MAX_R_ATR5 = 0.5, 2.0
TARGET_R, PARTIAL = 2.5, 0.5
# Not in the spec, needed to make "high/low of day" computable: the old high
# must have stood this many minutes, or every new high would be a "break".
HOD_MIN_AGE = 15
QQQ_EXTREME_BARS = 5          # "QQQ making new lows" = a new LOD in the last 5 minutes
JOURNAL_MATCH = pd.Timedelta(minutes=5)

NOTES = [
    "FOMC days and the IV-over-60 skip aren't checked.",
    "Outcomes are R on the underlying (stop and 2.5R target from the spec), not option P/L.",
    "QQQ confirmation is the VWAP test (spec table #7 default); QQQ's 5-minute EMAs are recorded, not required.",
]


# ---------------------------------------------------------------- helpers

def _num(v, places=4):
    if v is None:
        return None
    v = float(v)
    return round(v, places) if math.isfinite(v) else None


def ema(x, n):
    return pd.Series(x, dtype=float).ewm(span=n, adjust=False).mean().to_numpy()


def atr_series(h, l, c, n=14):
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def flip(df):
    """Mirror a bar frame so a short can run through the long logic: highs
    become lows, a hammer becomes a shooting star, higher highs become lower
    lows. Every price the long code returns is negated back on the way out."""
    out = df.copy()
    out["open"], out["close"] = -df["open"], -df["close"]
    out["high"], out["low"] = -df["low"], -df["high"]
    return out


def pivots(h, l, p=PIVOT):
    """[(i, 'H'|'L', price)] for confirmed swings. Bar i is a pivot only once
    bar i+p has closed, which callers enforce by index."""
    out = []
    for i in range(p, len(h) - p):
        if h[i] == h[i - p:i + p + 1].max():
            out.append((i, "H", h[i]))
        if l[i] == l[i - p:i + p + 1].min():
            out.append((i, "L", l[i]))
    return out


def structure(h, l):
    """Up when the last two confirmed swing highs and lows both rose, Down when
    both fell, else Range. Pivots are confirmed inside the window given."""
    piv = pivots(h, l)
    hs = [x for i, k, x in piv if k == "H"]
    ls = [x for i, k, x in piv if k == "L"]
    if len(hs) < 2 or len(ls) < 2:
        return "Range"
    if hs[-1] > hs[-2] and ls[-1] > ls[-2]:
        return "Up"
    if hs[-1] < hs[-2] and ls[-1] < ls[-2]:
        return "Down"
    return "Range"


def trend(f, k, window=None):
    """Spec default: structure and the 9/21 EMAs must both agree, using bars
    [0..k] (structure over the last `window` of them)."""
    if k < 0:
        return "Range"
    lo = 0 if window is None else max(0, k + 1 - window)
    st = structure(f["h"][lo:k + 1], f["l"][lo:k + 1])
    e = "Up" if f["e9"][k] > f["e21"][k] else "Down"
    return st if st == e else "Range"


def arrays(df):
    f = {"h": df["high"].to_numpy(float), "l": df["low"].to_numpy(float),
         "c": df["close"].to_numpy(float), "o": df["open"].to_numpy(float)}
    f["e9"], f["e21"] = ema(f["c"], EMA_FAST), ema(f["c"], EMA_SLOW)
    if "end" in df:
        f["end"] = df["end"].to_numpy()
    return f


def resample(df, minutes, s):
    if df.empty:
        return df.assign(end=pd.Series(index=df.index, dtype=object))
    g = (df.resample(f"{minutes}min", origin=s["open"], label="left", closed="left")
         .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
         .dropna(subset=["close"]))
    g["end"] = [min(t + pd.Timedelta(minutes=minutes), s["close"]) for t in g.index]
    return g


def is_quad_witching(d):
    if d.month not in (3, 6, 9, 12) or d.weekday() != 4:
        return False
    return 15 <= d.day <= 21


# ---------------------------------------------------------------- per-symbol view

EMPTY = pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                     index=pd.DatetimeIndex([], tz=NY), dtype=float)


def build(sym_m1, daily, sessions, data_end):
    """Everything one symbol needs, unflipped. `sessions` ends with today."""
    sym_m1 = EMPTY if sym_m1 is None or sym_m1.empty else sym_m1
    daily = EMPTY if daily is None or daily.empty else daily
    today = sessions[-1]
    prior = sessions[:-1][-PRIOR_SESSIONS:]

    def rth(s, end=None):
        x = sym_m1[(sym_m1.index >= s["open"]) & (sym_m1.index < s["close"])]
        return x if end is None else x[x.index < end]

    prev_rth = [rth(s) for s in prior]
    today_rth = rth(today, data_end)
    pre = sym_m1[(sym_m1.index >= today["open"].normalize() + pd.Timedelta(hours=4))
                 & (sym_m1.index < today["open"])]
    m5 = pd.concat([resample(prev_rth[-1], 5, prior[-1]) if prior else None,
                    resample(today_rth, 5, today)])
    h1 = pd.concat([resample(x, 60, s) for x, s in zip(prev_rth, prior)]
                   + [resample(today_rth, 60, today)])
    d1 = daily[daily.index.date < today["date"]]
    ltf1 = pd.concat([prev_rth[-1] if prior else today_rth.iloc[:0], today_rth])
    return {"today": today, "prior": prior, "prev_rth": prev_rth, "rth": today_rth,
            "pre": pre, "m5": m5, "h1": h1, "d1": d1, "ltf1": ltf1}


def named_levels(v):
    """Spec default level set: prior week, prior day, premarket, first 5-minute
    candle. [(price, tag, usable_from)]. High/low of day is handled separately
    because it moves."""
    today = v["today"]
    out = []
    if v["prior"]:
        pd_ = v["prev_rth"][-1]
        if len(pd_):
            out += [(pd_["high"].max(), "PDH", today["open"]), (pd_["low"].min(), "PDL", today["open"])]
        week = today["date"].isocalendar()[:2]
        prev_weeks = [(s["date"].isocalendar()[:2], x) for s, x in zip(v["prior"], v["prev_rth"])
                      if s["date"].isocalendar()[:2] < week]
        if prev_weeks:
            last = max(w for w, _ in prev_weeks)
            wk = pd.concat([x for w, x in prev_weeks if w == last])
            if len(wk):
                out += [(wk["high"].max(), "PWH", today["open"]), (wk["low"].min(), "PWL", today["open"])]
    if len(v["pre"]):
        out += [(v["pre"]["high"].max(), "PMH", today["open"]), (v["pre"]["low"].min(), "PML", today["open"])]
    first5 = v["rth"][v["rth"].index < today["open"] + pd.Timedelta(minutes=5)]
    if len(first5) == 5:
        t = today["open"] + pd.Timedelta(minutes=5)
        out += [(first5["high"].max(), "OR5H", t), (first5["low"].min(), "OR5L", t)]
    return out


def merge(levels, tol):
    groups = []
    for lv in sorted(levels):
        if groups and lv[0] - groups[-1][0][0] <= tol:
            groups[-1].append(lv)
        else:
            groups.append([lv])
    out = []
    for g in groups:
        tags = list(dict.fromkeys(t for _, t, _ in g))
        out.append({"price": sum(p for p, _, _ in g) / len(g), "tags": tags,
                    "from": max(u for _, _, u in g)})
    return out


def side_view(v, sgn):
    """Arrays for one side. sgn=+1 long, -1 short (prices mirrored)."""
    tf = (lambda df: df) if sgn > 0 else flip
    rth, ltf1, m5, h1, d1 = (tf(v[k]) for k in ("rth", "ltf1", "m5", "h1", "d1"))
    m1 = arrays(rth)
    m1["t"] = rth.index
    a1 = atr_series(*(arrays(ltf1)[k] for k in ("h", "l", "c"))) if len(ltf1) else np.array([])
    m1["atr"] = a1[-len(rth):] if len(rth) else a1[:0]
    if len(v["pre"]):
        first_prev = sgn * v["pre"]["close"].iloc[-1]
    elif v["prior"] and len(v["prev_rth"][-1]):
        first_prev = sgn * v["prev_rth"][-1]["close"].iloc[-1]
    else:
        first_prev = np.nan
    m1["pc"] = np.concatenate([[first_prev], m1["c"][:-1]]) if len(rth) else m1["c"]
    f5, fh, fd = arrays(m5), arrays(h1), arrays(d1)
    f5["atr"] = atr_series(f5["h"], f5["l"], f5["c"]) if len(m5) else np.array([])
    # last confirmed 5-min swing high known once bar k has closed (for BOS)
    last_sh = np.full(len(m5), np.nan)
    piv = [(i, x) for i, k, x in pivots(f5["h"], f5["l"]) if k == "H"]
    j = 0
    cur = np.nan
    for k in range(len(m5)):
        while j < len(piv) and piv[j][0] + PIVOT <= k:
            cur = piv[j][1]
            j += 1
        last_sh[k] = cur
    f5["last_sh"] = last_sh
    return {"m1": m1, "m5": f5, "h1": fh, "d1": fd}


def last_complete(f, t):
    """Index of the last bar of `f` that had closed by time t, or -1."""
    if not len(f["c"]):
        return -1
    return int(np.searchsorted(f["end"], t, side="right")) - 1


# ---------------------------------------------------------------- the setup machine

def track(m1, b, L):
    """After a break at bar b (long space). ('entry', retest, confirm) or
    ('cancel'|'expire', next index to resume the break search from)."""
    n = len(m1["c"])
    c, h, l, o = m1["c"], m1["h"], m1["l"], m1["o"]
    buf = BUFFER_ATR * m1["atr"]
    tol = RETEST_ATR * m1["atr"]

    def strong(k):
        rng = h[k] - l[k]
        if rng <= 0:
            return False
        lower_wick = min(o[k], c[k]) - l[k]
        return (c[k] - l[k]) / rng >= 2 / 3 and lower_wick >= abs(c[k] - o[k])

    i = b + 1
    while i < n and i <= b + RETEST_BARS + 1:
        if c[i] < L - buf[i]:
            return ("cancel", i)
        if i <= b + RETEST_BARS and abs(l[i] - L) <= tol[i]:
            if c[i] > L and strong(i):
                return ("entry", i, i)
            if i + 1 < n and c[i + 1] > L and strong(i + 1) and abs(l[i + 1] - L) > tol[i + 1]:
                return ("entry", i, i + 1)
        i += 1
    return ("expire", i)


def in_window(t):
    return ENTRY_START <= t.time() <= ENTRY_END


def find_setups(sv, levels, day_open):
    """[(level_price, tags, break_idx, retest_idx, confirm_idx)] in long space.
    One setup per level per side per day: the first retest that confirms."""
    m1 = sv["m1"]
    n = len(m1["c"])
    if n < 6:
        return []
    c, h = m1["c"], m1["h"]
    buf = BUFFER_ATR * m1["atr"]
    out = []
    for lv in levels:
        L = lv["price"]
        i = 0
        while i < n and m1["t"][i].time() <= ENTRY_END:
            if m1["t"][i] >= lv["from"] and c[i] > L + buf[i] and m1["pc"][i] <= L + buf[i]:
                res = track(m1, i, L)
                if res[0] == "entry":
                    entry_t = m1["t"][res[2]] + pd.Timedelta(minutes=1)
                    if in_window(entry_t):
                        out.append((L, lv["tags"], i, res[1], res[2]))
                        break
                    i = res[2] + 1
                    continue
                i = max(res[1], i + 1)
                continue
            i += 1

    # High of day (in long space; low of day for the short side). The old
    # high must have stood HOD_MIN_AGE minutes and mustn't sit on a named level.
    tol = [x["price"] for x in levels]
    i, hod, hod_at = 1, h[0], 0
    while i < n and m1["t"][i].time() <= ENTRY_END:
        if (i - hod_at >= HOD_MIN_AGE and c[i] > hod + buf[i] and m1["pc"][i] <= hod + buf[i]
                and all(abs(hod - p) > 2 * buf[i] for p in tol)):
            res = track(m1, i, hod)
            if res[0] == "entry" and in_window(m1["t"][res[2]] + pd.Timedelta(minutes=1)):
                out.append((hod, ["HOD"], i, res[1], res[2]))
                break
            nxt = res[2] + 1 if res[0] == "entry" else max(res[1], i + 1)
            for k in range(i, min(nxt, n)):
                if h[k] > hod:
                    hod, hod_at = h[k], k
            i = nxt
            continue
        if h[i] > hod:
            hod, hod_at = h[i], i
        i += 1
    return out


def simulate(sv, ci, entry, stop, data_end, flat_by):
    """Spec exits in long space: -1R on a 1-min close below the stop; half off
    at 2.5R, then the rest runs to a 5-min 9/21 cross against it (stop moved to
    entry) or the flat-by time."""
    m1, m5 = sv["m1"], sv["m5"]
    R = entry - stop
    target = entry + TARGET_R * R
    half, stop_lv = False, stop
    n = len(m1["c"])
    for j in range(ci + 1, n):
        t_close = m1["t"][j] + pd.Timedelta(minutes=1)
        if t_close > flat_by:
            return _result("flat", half, m1["t"][j - 1] + pd.Timedelta(minutes=1), m1["c"][j - 1], entry, R)
        if not half and m1["h"][j] >= target:
            half, stop_lv = True, entry
        if m1["c"][j] < stop_lv:
            return _result("target, then trailer" if half else "stopped", half, t_close, m1["c"][j], entry, R)
        if half:
            k = last_complete(m5, t_close)
            if k >= 0 and m5["end"][k] == t_close and m5["e9"][k] < m5["e21"][k]:
                return _result("target, then trailer", half, t_close, m1["c"][j], entry, R)
    if n:
        return _result("open", half, m1["t"][-1] + pd.Timedelta(minutes=1), m1["c"][-1], entry, R)
    return _result("open", half, None, entry, entry, R)


def _result(label, half, t, px, entry, R):
    rest = (px - entry) / R
    r = PARTIAL * TARGET_R + (1 - PARTIAL) * rest if half else rest
    return {"outcome": label, "exit_at": t, "exit_px": px, "r": r}


def qqq_at(q, t):
    """Index of the QQQ bar that started at or before t."""
    return int(q.index.searchsorted(t, side="right")) - 1


def evaluate(sym, v, sgn, setup, q, day_flags, data_end, flat_by):
    sv = v["sides"][sgn]
    m1, m5, h1, d1 = sv["m1"], sv["m5"], sv["h1"], sv["d1"]
    L, tags, b, r, ci = setup
    entry = m1["c"][ci]
    t_entry = m1["t"][ci] + pd.Timedelta(minutes=1)
    buf_b = BUFFER_ATR * m1["atr"][b]
    stop = min(m1["l"][r], L - buf_b)
    R = entry - stop

    k5 = last_complete(m5, t_entry)
    kh = last_complete(h1, t_entry)
    kd = len(d1["c"]) - 1
    ltf = trend(m5, k5)
    htf_d = trend(d1, kd, DAILY_TREND_SESSIONS)
    htf_h = trend(h1, kh)
    ema5 = k5 >= 0 and m5["e9"][k5] > m5["e21"][k5]

    # break of structure in the trade's direction within the last 60 minutes
    bos = False
    for j in range(ci, -1, -1):
        tc = m1["t"][j] + pd.Timedelta(minutes=1)
        if t_entry - tc > BOS_WINDOW:
            break
        kj = last_complete(m5, tc)
        if kj >= 0 and np.isfinite(m5["last_sh"][kj]) and m1["c"][j] > m5["last_sh"][kj] + BUFFER_ATR * m1["atr"][j]:
            bos = True
            break

    # QQQ and relative strength, on real (unflipped) prices
    raw = v["rth"]
    qi = qqq_at(q, m1["t"][ci])
    checks = {"ltf_trend": ltf, "htf_daily": htf_d, "htf_1h": htf_h, "ema5": bool(ema5), "bos": bos}
    failed = []
    up, down = ("Up", "Down")
    if ltf != up:
        failed.append("ltf_trend")
    if htf_d == down or htf_h == down:
        failed.append("htf_trend")
    if not ema5:
        failed.append("ema5")
    if not bos:
        failed.append("bos")
    if qi >= 0 and len(raw):
        qc = q["close"].to_numpy(float)
        tp = (q["high"] + q["low"] + q["close"]).to_numpy(float) / 3
        vol = q["volume"].to_numpy(float)
        vwap = np.cumsum(tp * vol)[qi] / max(np.cumsum(vol)[qi], 1)
        q_side = sgn * (qc[qi] - vwap) > 0
        stock_ret = raw["close"].iloc[ci] / raw["open"].iloc[0] - 1
        q_ret = qc[qi] / q["open"].iloc[0] - 1
        rs = sgn * (stock_ret - q_ret)
        ql = (q["low"] if sgn > 0 else -q["high"]).to_numpy(float)
        lo = max(0, qi - QQQ_EXTREME_BARS + 1)
        fighting = lo > 0 and ql[lo:qi + 1].min() < ql[:lo].min()
        qf = arrays(resample(q, 5, v["today"]) if sgn > 0 else flip(resample(q, 5, v["today"])))
        kq = last_complete(qf, t_entry)
        checks.update({"qqq_vwap": bool(q_side), "qqq_ema5": bool(kq >= 0 and qf["e9"][kq] > qf["e21"][kq]),
                       "relative_strength": _num(rs, 5), "fighting_market": bool(fighting)})
        if not q_side:
            failed.append("qqq_vwap")
        if rs <= 0:
            failed.append("relative_strength")
        if fighting:
            failed.append("fighting_market")
    else:
        failed.append("qqq_missing")
    r_atr1 = R / m1["atr"][ci] if m1["atr"][ci] > 0 else np.inf
    r_atr5 = R / m5["atr"][k5] if k5 >= 0 and m5["atr"][k5] > 0 else 0
    checks.update({"r_atr1m": _num(r_atr1, 2), "r_atr5m": _num(r_atr5, 2)})
    if r_atr1 < MIN_R_ATR1:
        failed.append("stop_too_tight")
    if r_atr5 > MAX_R_ATR5:
        failed.append("stop_too_wide")
    for flag in day_flags.get(sym, []) + day_flags.get("*", []):
        failed.append(flag)

    res = simulate(sv, ci, entry, stop, data_end, flat_by)
    t = lambda i: (m1["t"][i]).isoformat()
    return {
        "ticker": sym,
        "side": "Long" if sgn > 0 else "Short",
        "level": _num(sgn * L),
        "level_key": f"{sgn * L:.2f}",
        "level_tags": tags,
        "break_at": t(b),
        "retest_at": t(r),
        "entry_at": t_entry.isoformat(),
        "entry": _num(sgn * entry),
        "stop": _num(sgn * stop),
        "risk": _num(R),
        "target": _num(sgn * (entry + TARGET_R * R)),
        "passed": not failed,
        "failed": failed,
        "checks": checks,
        "outcome": res["outcome"],
        "exit_at": res["exit_at"].isoformat() if res["exit_at"] is not None else None,
        "exit_price": _num(sgn * res["exit_px"]),
        "r_multiple": _num(res["r"], 2),
    }


def scan(m1_by_sym, daily_by_sym, sessions, data_end, day_flags):
    """All setups for today. Returns (rows, errors)."""
    today = sessions[-1]
    flat_by = min(pd.Timestamp.combine(today["date"], FLAT_BY).tz_localize(NY),
                  today["close"] - pd.Timedelta(minutes=15))
    qv = build(m1_by_sym.get(REFERENCE), daily_by_sym.get(REFERENCE),
               sessions, data_end)
    q = qv["rth"]
    rows, errors = [], {}
    if q.empty:
        return rows, {REFERENCE: "no QQQ bars today"}
    for sym in SYMBOLS:
        try:
            m1 = m1_by_sym.get(sym)
            if m1 is None or m1.empty:
                errors[sym] = "no 1-minute bars"
                continue
            v = build(m1, daily_by_sym.get(sym), sessions, data_end)
            if len(v["rth"]) < 6:
                errors[sym] = "too few bars today"
                continue
            d = v["d1"]
            if len(d) >= 15:
                da = atr_series(d["high"].to_numpy(float), d["low"].to_numpy(float), d["close"].to_numpy(float))[-1]
            else:
                da = 0.0
            levels = merge(named_levels(v), MERGE_DAILY_ATR * da)
            v["sides"] = {1: side_view(v, 1), -1: side_view(v, -1)}
            for sgn in (1, -1):
                lv = [{**x, "price": sgn * x["price"]} for x in levels]
                for setup in find_setups(v["sides"][sgn], lv, today["open"]):
                    rows.append(evaluate(sym, v, sgn, setup, q, day_flags, data_end, flat_by))
        except Exception as e:  # noqa: BLE001 - one symbol never sinks the run
            errors[sym] = f"{type(e).__name__}: {e}"
    rows.sort(key=lambda x: x["entry_at"])
    return rows, errors


# ---------------------------------------------------------------- journal

ENTRY_RE = re.compile(r"Entry (\d{1,2}):(\d{2})(?::(\d{2}))?")


def match_journal(journal, examples, rows, day):
    """Each of today's journal trades against the setups: same ticker and
    direction, entry within JOURNAL_MATCH of a setup's entry. A match with a
    passing setup is a candidate Good example, unless one is already saved."""
    out = []
    for j in journal:
        m = ENTRY_RE.search(j.get("notes") or "")
        t = (pd.Timestamp.combine(day, time(int(m[1]), int(m[2]), int(m[3] or 0))).tz_localize(NY)
             if m else None)
        best = None
        if t is not None:
            for r in rows:
                if r["ticker"] != j["ticker"] or r["side"] != j["direction"]:
                    continue
                gap = abs(pd.Timestamp(r["entry_at"]) - t)
                if gap <= JOURNAL_MATCH and (best is None or (r["passed"], -gap) > (best[0]["passed"], -best[1])):
                    best = (r, gap)
        saved = None
        if t is not None:
            for e in examples:
                if (e["ticker"] == j["ticker"] and e["direction"] == j["direction"]
                        and abs(pd.Timestamp(e["entry_at"]).tz_convert(NY) - t) <= pd.Timedelta(minutes=2)):
                    saved = e.get("quality")
        out.append({
            "journal_id": j["id"],
            "ticker": j["ticker"],
            "direction": j["direction"],
            "entry_time": t.strftime("%H:%M:%S") if t is not None else None,
            "result": j.get("result"),
            "in_window": bool(t is not None and in_window(t)),
            "setup": None if best is None else {
                "entry_at": best[0]["entry_at"], "level": best[0]["level"],
                "level_tags": best[0]["level_tags"], "passed": best[0]["passed"],
                "failed": best[0]["failed"], "minutes_apart": round(best[1].total_seconds() / 60, 1)},
            "already_saved_as": saved,
            "example_candidate": bool(best and best[0]["passed"] and saved is None),
        })
    return out


# ---------------------------------------------------------------- I/O

def _headers(prefer=None):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
    h = {"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
         "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


def _check(resp):
    if resp.status_code >= 400:
        raise RuntimeError(f"{resp.request.method} {resp.url} -> {resp.status_code}: {resp.text[:500]}")


def sb_get(table, params):
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=_headers(), params=params, timeout=TIMEOUT)
    _check(resp)
    return resp.json()


def sb_upsert(table, rows, on_conflict):
    h = _headers("resolution=merge-duplicates,return=minimal")
    for i in range(0, len(rows), BATCH):
        resp = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=h, json=rows[i:i + BATCH],
                             params={"on_conflict": on_conflict}, timeout=TIMEOUT)
        _check(resp)


def sb_delete(table, params):
    resp = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}", headers=_headers(), params=params, timeout=TIMEOUT)
    _check(resp)


def fetch(symbols, sessions, now):
    from lab import alpaca
    start = sessions[0]["open"].normalize()
    end = now - SIP_DELAY
    rows = alpaca._paged_bars("/v2/stocks/bars", {
        "symbols": ",".join(symbols), "timeframe": "1Min", "start": alpaca._iso(start),
        "end": alpaca._iso(end), "feed": "sip", "adjustment": "split"})
    m1 = {s: alpaca._to_frame(rows.get(s, [])) for s in symbols}
    drows = alpaca._paged_bars("/v2/stocks/bars", {
        "symbols": ",".join(symbols), "timeframe": "1Day",
        "start": alpaca._iso((now - pd.Timedelta(days=DAILY_LOOKBACK_DAYS)).date()),
        "end": alpaca._iso(sessions[-1]["date"]), "feed": "sip", "adjustment": "split"})
    daily = {}
    for s in symbols:
        d = alpaca._to_frame(drows.get(s, []))
        d.index = pd.DatetimeIndex([pd.Timestamp(x.date()).tz_localize(NY) for x in d.index])
        daily[s] = d
    return m1, daily, end


def load_sessions(now):
    from lab import alpaca
    cal = alpaca.calendar((now - pd.Timedelta(days=25)).date(), now.date())
    out = []
    for c in cal or []:
        d = pd.Timestamp(c["date"]).date()
        out.append({"date": d,
                    "open": pd.Timestamp(f"{c['date']} {c['open']}").tz_localize(NY),
                    "close": pd.Timestamp(f"{c['date']} {c['close']}").tz_localize(NY)})
    return out


def day_flags_for(day, write):
    flags = {}
    if is_quad_witching(day):
        flags["*"] = ["quad_witching"]
    if write:
        try:
            for r in sb_get("watchlist_snapshots", {"select": "ticker,next_earnings",
                                                    "run_date": f"eq.{day}", "user_id": f"eq.{USER_ID}"}):
                if r.get("next_earnings") == day.isoformat():
                    flags.setdefault(r["ticker"], []).append("earnings_day")
        except Exception as e:  # noqa: BLE001 - optional
            print(f"WARNING: earnings check skipped ({e})", file=sys.stderr)
    return flags


def bar_rows(m1_by_sym, day):
    out = []
    for sym, df in m1_by_sym.items():
        for ts, r in df[df.index.date == day].iterrows():
            out.append({"ticker": sym, "ts": ts.isoformat(), "open": float(r.open), "high": float(r.high),
                        "low": float(r.low), "close": float(r.close), "volume": int(r.volume)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    ap.add_argument("--selftest", action="store_true", help="synthetic scenarios, no network")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    write = not args.dry_run
    now = pd.Timestamp.now(tz=NY)
    day = now.date()
    generated_at = pd.Timestamp.now(tz="UTC").isoformat()
    sessions = load_sessions(now)
    if not sessions or sessions[-1]["date"] != day:
        print(f"{day}: market closed, nothing to scan.")
        if write:
            sb_upsert("midday_runs", [{"run_date": day.isoformat(), "generated_at": generated_at,
                                       "market_open": False, "setups": 0, "passed": 0,
                                       "journal": [], "errors": {}, "notes": []}], "run_date")
        return 0

    symbols = SYMBOLS + [REFERENCE]
    m1, daily, data_end = fetch(symbols, sessions[-(PRIOR_SESSIONS + 1):], now)
    rows, errors = scan(m1, daily, sessions[-(PRIOR_SESSIONS + 1):], data_end, day_flags_for(day, write))

    journal, examples = [], []
    if write:
        start = pd.Timestamp.combine(day, time(0)).tz_localize(NY)
        journal = sb_get("journal_entries", {"select": "id,ticker,direction,result,notes",
                                             "date": f"eq.{day}", "user_id": f"eq.{USER_ID}"})
        examples = sb_get("training_examples", {
            "select": "ticker,direction,quality,entry_at", "user_id": f"eq.{USER_ID}",
            "and": f"(entry_at.gte.{start.isoformat()},entry_at.lt.{(start + pd.Timedelta(days=1)).isoformat()})"})
    jm = match_journal(journal, examples, rows, day)

    run = {"run_date": day.isoformat(), "generated_at": generated_at, "market_open": True,
           "data_through": (data_end.floor("min")).isoformat(), "symbols": symbols,
           "setups": len(rows), "passed": sum(r["passed"] for r in rows),
           "journal": jm, "errors": errors, "notes": NOTES}
    for r in rows:
        r.update({"run_date": day.isoformat(), "generated_at": generated_at})

    if not write:
        print(json.dumps({"run": run, "setups": rows}, indent=1, default=str))
        return 0

    sb_upsert("intraday_bars", bar_rows(m1, day), "ticker,ts")
    sb_delete("intraday_bars", {"ts": f"lt.{(now - pd.Timedelta(days=INTRADAY_RETENTION_DAYS)).isoformat()}"})
    sb_delete("midday_setups", {"run_date": f"eq.{day}"})
    if rows:
        sb_upsert("midday_setups", rows, "run_date,ticker,side,level_key")
    # the run row last: the routine waits for it, so it must mean "all written"
    sb_upsert("midday_runs", [run], "run_date")
    print(f"{day}: {len(rows)} setup(s), {run['passed']} passed every filter; "
          f"{len(jm)} journal trade(s); data through {run['data_through']}; errors: {errors or 'none'}")
    return 0


# ---------------------------------------------------------------- selftest

def _synthetic(day, path, prev_hl=(99.0, 98.0), start="09:30"):
    """1-min frame for `day` following `path` (a list of closes); highs/lows a
    few cents around open/close unless given as (o,h,l,c) tuples."""
    t0 = pd.Timestamp(f"{day} {start}").tz_localize(NY)
    rows, prev = [], path[0] if not isinstance(path[0], tuple) else path[0][0]
    for i, p in enumerate(path):
        if isinstance(p, tuple):
            o, h, l, c = p
        else:
            o, c = prev, p
            h, l = max(o, c) + 0.02, min(o, c) - 0.02
        rows.append({"t": t0 + pd.Timedelta(minutes=i), "open": o, "high": h, "low": l, "close": c, "volume": 1000})
        prev = c
    return pd.DataFrame(rows).set_index("t")


def selftest():
    """A clean long break-and-retest of PDH, its mirror for a short, and a
    break that falls back inside (cancelled)."""
    d0, d1 = pd.Timestamp("2026-11-12").date(), pd.Timestamp("2026-11-13").date()
    sessions = [{"date": d, "open": pd.Timestamp(f"{d} 09:30").tz_localize(NY),
                 "close": pd.Timestamp(f"{d} 16:00").tz_localize(NY)} for d in (d0, d1)]

    def prev_day(lo, hi):
        # oscillate between lo and hi for a full session; exact extremes
        mid, amp = (lo + hi) / 2, (hi - lo) / 2
        bars = []
        for i in range(390):
            a, b = mid + amp * math.sin(i / 7), mid + amp * math.sin((i + 1) / 7)
            bars.append((a, max(a, b), min(a, b), b))
        return _synthetic(d0, bars)

    def rising_path(pdh):
        # trend up through the morning, break PDH, pull back to it, hammer
        p, x = [], pdh - 1.0
        for i in range(40):  # 09:30-10:09, staircase up with pullbacks
            x += 0.03 if i % 6 < 4 else -0.02
            p.append(round(x, 2))
        while x < pdh - 0.06:
            x += 0.04
            p.append(round(x, 2))
        p += [pdh + 0.25, pdh + 0.35, pdh + 0.30, pdh + 0.20, pdh + 0.10]       # break, drift back
        p.append((pdh + 0.06, pdh + 0.08, pdh, pdh + 0.075))                    # hammer retest
        x = pdh + 0.075
        for i in range(200):                                                     # run up
            x += 0.03 if i % 5 < 4 else -0.02
            p.append(round(x, 2))
        return p

    def daily(trend_up=True):
        idx = pd.DatetimeIndex([pd.Timestamp(d0) - pd.Timedelta(days=k) for k in range(60, 0, -1)]).tz_localize(NY)
        base = np.linspace(90, 99, 60) if trend_up else np.linspace(110, 101, 60)
        wig = np.sin(np.arange(60) / 2.5)
        c = base + wig
        return pd.DataFrame({"open": c - 0.2, "high": c + 1, "low": c - 1, "close": c, "volume": 1e6}, index=idx)

    pdh = 100.0
    spy_today = rising_path(pdh)
    m1 = {"SPY": pd.concat([prev_day(98.0, pdh), _synthetic(d1, spy_today)]),
          "QQQ": pd.concat([prev_day(398.0, 400.0), _synthetic(d1, [400 + 0.5 * (v - spy_today[0] if not isinstance(v, tuple) else v[3] - spy_today[0]) * 0.5 for v in spy_today])])}
    daily_ = {"SPY": daily(True), "QQQ": daily(True)}
    data_end = pd.Timestamp(f"{d1} 13:44").tz_localize(NY)

    global SYMBOLS
    saved = SYMBOLS
    SYMBOLS = ["SPY"]
    try:
        rows, errors = scan(m1, daily_, sessions, data_end, {})
        longs = [r for r in rows if r["side"] == "Long" and "PDH" in r["level_tags"]]
        assert not errors, errors
        assert longs, f"expected a PDH long, got {rows}"
        r = longs[0]
        print("long:", json.dumps({k: r[k] for k in ("entry_at", "level", "entry", "stop", "target", "passed",
                                                     "failed", "outcome", "r_multiple")}, default=str))
        assert r["entry"] > r["level"] > r["stop"], r
        assert r["outcome"] in ("target, then trailer", "open"), r

        # mirror: the same day upside down must give the same setup as a short
        mir = {s: flip(df) for s, df in m1.items()}
        mir = {s: df.assign(open=df.open + 400, high=df.high + 400, low=df.low + 400, close=df.close + 400)
               if s == "SPY" else df.assign(open=df.open + 1200, high=df.high + 1200, low=df.low + 1200, close=df.close + 1200)
               for s, df in mir.items()}
        dmir = {s: flip(df).assign(**{c: flip(df)[c] + (400 if s == "SPY" else 1200) for c in ("open", "high", "low", "close")})
                for s, df in daily_.items()}
        rows2, errors2 = scan(mir, dmir, sessions, data_end, {})
        shorts = [x for x in rows2 if x["side"] == "Short" and "PDL" in x["level_tags"]]
        assert shorts, f"expected a PDL short, got {rows2}"
        s = shorts[0]
        print("short:", json.dumps({k: s[k] for k in ("entry_at", "level", "entry", "stop", "passed", "failed",
                                                      "outcome", "r_multiple")}, default=str))
        assert s["entry_at"] == r["entry_at"] and abs(s["r_multiple"] - r["r_multiple"]) < 1e-6, (s, r)
        assert sorted(s["failed"]) == sorted(r["failed"]), (s["failed"], r["failed"])

        # a break that closes back inside the level is cancelled: no PDH setup
        bad = list(spy_today)
        k = next(i for i, v in enumerate(bad) if not isinstance(v, tuple) and v >= pdh + 0.2)
        bad[k + 2] = pdh - 0.30   # close back well inside
        bad[k + 3: k + 6] = [pdh - 0.25, pdh - 0.28, pdh - 0.30]
        m1b = {**m1, "SPY": pd.concat([prev_day(98.0, pdh), _synthetic(d1, bad)])}
        rows3, _ = scan(m1b, daily_, sessions, data_end, {})
        early = [x for x in rows3 if "PDH" in x["level_tags"] and x["break_at"] < r["entry_at"]
                 and x["entry_at"] == r["entry_at"]]
        assert not early, f"cancelled break still produced {early}"

        # journal match
        j = [{"id": "j1", "ticker": "SPY", "direction": "Long", "result": "+20.00",
              "notes": f"SPY 100C 0DTE · 1x · in 1.00 / out 1.20. Entry {pd.Timestamp(r['entry_at']).strftime('%H:%M:%S')}, exit 11:00:00."}]
        jm = match_journal(j, [], rows, d1)
        assert jm[0]["setup"] is not None and jm[0]["example_candidate"] == r["passed"], jm
        jm2 = match_journal(j, [{"ticker": "SPY", "direction": "Long", "quality": "Good",
                                 "entry_at": r["entry_at"]}], rows, d1)
        assert jm2[0]["already_saved_as"] == "Good" and not jm2[0]["example_candidate"], jm2
        assert is_quad_witching(pd.Timestamp("2026-12-18").date()) and not is_quad_witching(pd.Timestamp("2026-12-11").date())
    finally:
        SYMBOLS = saved
    print("selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
