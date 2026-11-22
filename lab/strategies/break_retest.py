"""The trader's playbook: trend-aligned break and retest with QQQ confirmation.

The one implementation of `docs/strategy-break-retest.md`. Both the Lab
(`signals()`, through backtest.py / research.py / evaluation.py) and the 2pm
review (`midday_review.py`, through `scan_symbol()`) call the code below, so
the two can't disagree about what a setup is.

The spec is a starting point, not a rulebook (the trader, 2026-11-15): every
default lives in DEFAULTS so the Lab can vary it, and every filter can be
switched off by name through `filters_off`, so each filter's contribution is
measurable on its own. PRESETS names the comparisons the spec asks for first.

Shorts run through the long code on mirrored prices (`flip()`), so a rule is
written once. `midday_review.py --selftest` checks a day and its mirror give
the same setup.

What a strategy sees (all causal; `research causal break_retest` checks it):
  day                 the traded symbol's RTH 1-min bars up to now
  ctx["prior_rth"]    the last PRIOR_SESSIONS sessions' RTH 1-min bars
  ctx["premarket"]    today's 04:00-09:30 bars (may be empty)
  ctx["daily"]        prior daily bars
  ctx["ref_day"]      QQQ's RTH 1-min bars today (sliced to `day`'s last bar here)
  ctx["flags"]        skip flags for the day, e.g. ["quad_witching"]
Without ref_day every setup fails `qqq_missing`, so the full spec emits
nothing; switch the QQQ filters off to run on the trigger alone.

Exits in the option backtester: stop on a 1-min close through the stop, a
full exit at target_r x R, flat by flat_by. The spec's half-off-then-trailer
isn't modelled in option P/L yet (backtest.simulate has one exit); it is in
the underlying R that `scan_symbol()` reports, which is what the 2pm review
and the underlying gate use.
"""
import math
from datetime import time

import numpy as np
import pandas as pd

from . import Signal

NY = "America/New_York"
NAME = "break_retest"
DESCRIPTION = ("The trader's playbook: break and retest of a named level, with trend, "
               "5-min EMA, break-of-structure, QQQ and relative-strength filters.")

# Filters, by the name a failing setup reports. Day flags (quad_witching,
# earnings_day, ...) are filters too and are switched off by their own name.
TREND_FILTERS = ["ltf_trend", "htf_trend", "ema5", "bos"]
MARKET_FILTERS = ["qqq_vwap", "relative_strength", "fighting_market", "qqq_missing"]
RISK_FILTERS = ["stop_too_tight", "stop_too_wide"]

DEFAULTS = {
    # break and retest (spec table #5, #6)
    "buffer_atr": 0.1,          # break buffer, x ATR(14, 1m)
    "retest_atr": 0.1,          # retest tolerance, x ATR(14, 1m)
    "retest_bars": 20,          # retest must come within this many bars of the break
    "candle": "hammer",         # "hammer" | "close" (plain close back beyond the level)
    "merge_daily_atr": 0.25,    # levels this close (x daily ATR) are one zone
    "hod_min_age": 15,          # not in the spec: the old high must have stood this long
    # trend (spec #2, #3)
    "pivot": 3,
    "ema_fast": 9,
    "ema_slow": 21,
    "trend_mode": "both",       # "both" | "structure" | "ema"
    "daily_trend_sessions": 15,
    "bos_window_min": 60,
    # market (spec #7, #8)
    "qqq_ema5": False,          # also require QQQ's 5-min 9/21 to agree
    "qqq_extreme_bars": 5,      # "QQQ making new lows" = a new LOD in the last 5 minutes
    # time (spec #12)
    "entry_start": "09:35",
    "entry_end": "11:30",
    "flat_by": "15:45",
    # risk and exits (spec #10, #11)
    "min_r_atr1": 0.5,
    "max_r_atr5": 2.0,
    "target_r": 2.5,
    "partial": 0.5,             # fraction off at target in the underlying R
    # filters switched off by name, see the lists above
    "filters_off": [],
    # instrument (spec #13)
    "otm": 0,
    "dte": 0,
}

PRESETS = {
    "full": {"filters_off": []},
    "trend": {"filters_off": MARKET_FILTERS},
    "trigger": {"filters_off": TREND_FILTERS + MARKET_FILTERS},
}

PRIOR_SESSIONS = 10           # 1-min history: prior day/week levels, 1H trend
# What backtest.load_market puts in ctx for this strategy.
NEEDS = {"prior_rth": PRIOR_SESSIONS, "premarket": True, "ref": "QQQ"}


def params(p=None):
    """DEFAULTS, then the named preset if p has one, then p's own values."""
    p = dict(p or {})
    preset = p.pop("preset", None)
    return {**DEFAULTS, **(PRESETS[preset] if preset else {}), **p}


def _hm(s):
    h, m = s.split(":")
    return time(int(h), int(m))


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


def pivots(h, l, p=3):
    """[(i, 'H'|'L', price)] for confirmed swings. Bar i is a pivot only once
    bar i+p has closed, which callers enforce by index."""
    out = []
    for i in range(p, len(h) - p):
        if h[i] == h[i - p:i + p + 1].max():
            out.append((i, "H", h[i]))
        if l[i] == l[i - p:i + p + 1].min():
            out.append((i, "L", l[i]))
    return out


def structure(h, l, p=3):
    """Up when the last two confirmed swing highs and lows both rose, Down when
    both fell, else Range. Pivots are confirmed inside the window given."""
    piv = pivots(h, l, p)
    hs = [x for i, k, x in piv if k == "H"]
    ls = [x for i, k, x in piv if k == "L"]
    if len(hs) < 2 or len(ls) < 2:
        return "Range"
    if hs[-1] > hs[-2] and ls[-1] > ls[-2]:
        return "Up"
    if hs[-1] < hs[-2] and ls[-1] < ls[-2]:
        return "Down"
    return "Range"


def trend(f, k, window=None, p=None):
    """Spec default: structure and the 9/21 EMAs must both agree, using bars
    [0..k] (structure over the last `window` of them). trend_mode picks the
    spec's variants: structure alone or EMAs alone."""
    p = p or DEFAULTS
    if k < 0:
        return "Range"
    lo = 0 if window is None else max(0, k + 1 - window)
    st = structure(f["h"][lo:k + 1], f["l"][lo:k + 1], p["pivot"])
    e = "Up" if f["e9"][k] > f["e21"][k] else "Down"
    mode = p["trend_mode"]
    if mode == "structure":
        return st
    if mode == "ema":
        return e
    return st if st == e else "Range"


def arrays(df, p=None):
    p = p or DEFAULTS
    f = {"h": df["high"].to_numpy(float), "l": df["low"].to_numpy(float),
         "c": df["close"].to_numpy(float), "o": df["open"].to_numpy(float)}
    f["e9"], f["e21"] = ema(f["c"], p["ema_fast"]), ema(f["c"], p["ema_slow"])
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
    """Everything one symbol needs, unflipped. `sessions` ends with today:
    [{"date", "open", "close"}]. `sym_m1` holds premarket and RTH minutes."""
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
    return _view(today, prior, prev_rth, today_rth, pre, daily)


def _view(today, prior, prev_rth, today_rth, pre, daily):
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


def side_view(v, sgn, p=None):
    """Arrays for one side. sgn=+1 long, -1 short (prices mirrored)."""
    p = p or DEFAULTS
    tf = (lambda df: df) if sgn > 0 else flip
    rth, ltf1, m5, h1, d1 = (tf(v[k]) for k in ("rth", "ltf1", "m5", "h1", "d1"))
    m1 = arrays(rth, p)
    m1["t"] = rth.index
    a1 = atr_series(*(arrays(ltf1, p)[k] for k in ("h", "l", "c"))) if len(ltf1) else np.array([])
    m1["atr"] = a1[-len(rth):] if len(rth) else a1[:0]
    if len(v["pre"]):
        first_prev = sgn * v["pre"]["close"].iloc[-1]
    elif v["prior"] and len(v["prev_rth"][-1]):
        first_prev = sgn * v["prev_rth"][-1]["close"].iloc[-1]
    else:
        first_prev = np.nan
    m1["pc"] = np.concatenate([[first_prev], m1["c"][:-1]]) if len(rth) else m1["c"]
    f5, fh, fd = arrays(m5, p), arrays(h1, p), arrays(d1, p)
    f5["atr"] = atr_series(f5["h"], f5["l"], f5["c"]) if len(m5) else np.array([])
    # last confirmed 5-min swing high known once bar k has closed (for BOS)
    last_sh = np.full(len(m5), np.nan)
    piv = [(i, x) for i, k, x in pivots(f5["h"], f5["l"], p["pivot"]) if k == "H"]
    j = 0
    cur = np.nan
    for k in range(len(m5)):
        while j < len(piv) and piv[j][0] + p["pivot"] <= k:
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

def track(m1, b, L, p=None):
    """After a break at bar b (long space). ('entry', retest, confirm) or
    ('cancel'|'expire', next index to resume the break search from)."""
    p = p or DEFAULTS
    n = len(m1["c"])
    c, h, l, o = m1["c"], m1["h"], m1["l"], m1["o"]
    buf = p["buffer_atr"] * m1["atr"]
    tol = p["retest_atr"] * m1["atr"]
    hammer = p["candle"] == "hammer"
    rb = p["retest_bars"]

    def strong(k):
        if not hammer:
            return True
        rng = h[k] - l[k]
        if rng <= 0:
            return False
        lower_wick = min(o[k], c[k]) - l[k]
        return (c[k] - l[k]) / rng >= 2 / 3 and lower_wick >= abs(c[k] - o[k])

    i = b + 1
    while i < n and i <= b + rb + 1:
        if c[i] < L - buf[i]:
            return ("cancel", i)
        if i <= b + rb and abs(l[i] - L) <= tol[i]:
            if c[i] > L and strong(i):
                return ("entry", i, i)
            if i + 1 < n and c[i + 1] > L and strong(i + 1) and abs(l[i + 1] - L) > tol[i + 1]:
                return ("entry", i, i + 1)
        i += 1
    return ("expire", i)


def in_window(t, p=None):
    p = p or DEFAULTS
    return _hm(p["entry_start"]) <= t.time() <= _hm(p["entry_end"])


def find_setups(sv, levels, day_open, p=None):
    """[(level_price, tags, break_idx, retest_idx, confirm_idx)] in long space.
    One setup per level per side per day: the first retest that confirms."""
    p = p or DEFAULTS
    m1 = sv["m1"]
    n = len(m1["c"])
    if n < 6:
        return []
    c, h = m1["c"], m1["h"]
    buf = p["buffer_atr"] * m1["atr"]
    end = _hm(p["entry_end"])
    out = []
    for lv in levels:
        L = lv["price"]
        i = 0
        while i < n and m1["t"][i].time() <= end:
            if m1["t"][i] >= lv["from"] and c[i] > L + buf[i] and m1["pc"][i] <= L + buf[i]:
                res = track(m1, i, L, p)
                if res[0] == "entry":
                    entry_t = m1["t"][res[2]] + pd.Timedelta(minutes=1)
                    if in_window(entry_t, p):
                        out.append((L, lv["tags"], i, res[1], res[2]))
                        break
                    i = res[2] + 1
                    continue
                i = max(res[1], i + 1)
                continue
            i += 1

    # High of day (in long space; low of day for the short side). The old
    # high must have stood hod_min_age minutes and mustn't sit on a named level.
    tol = [x["price"] for x in levels]
    i, hod, hod_at = 1, h[0], 0
    while i < n and m1["t"][i].time() <= end:
        if (i - hod_at >= p["hod_min_age"] and c[i] > hod + buf[i] and m1["pc"][i] <= hod + buf[i]
                and all(abs(hod - x) > 2 * buf[i] for x in tol)):
            res = track(m1, i, hod, p)
            if res[0] == "entry" and in_window(m1["t"][res[2]] + pd.Timedelta(minutes=1), p):
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


def simulate(sv, ci, entry, stop, data_end, flat_by, p=None):
    """Spec exits in long space: -1R on a 1-min close below the stop; part off
    at target_r, then the rest runs to a 5-min 9/21 cross against it (stop
    moved to entry) or the flat-by time."""
    p = p or DEFAULTS
    m1, m5 = sv["m1"], sv["m5"]
    R = entry - stop
    target = entry + p["target_r"] * R
    half, stop_lv = False, stop
    n = len(m1["c"])
    for j in range(ci + 1, n):
        t_close = m1["t"][j] + pd.Timedelta(minutes=1)
        if t_close > flat_by:
            return _result("flat", half, m1["t"][j - 1] + pd.Timedelta(minutes=1), m1["c"][j - 1], entry, R, p)
        if not half and m1["h"][j] >= target:
            half, stop_lv = True, entry
        if m1["c"][j] < stop_lv:
            return _result("target, then trailer" if half else "stopped", half, t_close, m1["c"][j], entry, R, p)
        if half:
            k = last_complete(m5, t_close)
            if k >= 0 and m5["end"][k] == t_close and m5["e9"][k] < m5["e21"][k]:
                return _result("target, then trailer", half, t_close, m1["c"][j], entry, R, p)
    if n:
        return _result("open", half, m1["t"][-1] + pd.Timedelta(minutes=1), m1["c"][-1], entry, R, p)
    return _result("open", half, None, entry, entry, R, p)


def _result(label, half, t, px, entry, R, p=None):
    p = p or DEFAULTS
    rest = (px - entry) / R
    r = p["partial"] * p["target_r"] + (1 - p["partial"]) * rest if half else rest
    return {"outcome": label, "exit_at": t, "exit_px": px, "r": r}


def first_hit(sv, ci, entry, R):
    """+1 if price reaches entry + R before entry - R after bar ci, -1 if the
    other way (or both in one bar: the path is unknown), 0 if neither by the
    last bar given. Long space. The proposal's P(+1R before -1R) direction test."""
    m1 = sv["m1"]
    for j in range(ci + 1, len(m1["c"])):
        dn = m1["l"][j] <= entry - R
        if dn:
            return -1
        if m1["h"][j] >= entry + R:
            return 1
    return 0


def qqq_at(q, t):
    """Index of the QQQ bar that started at or before t."""
    return int(q.index.searchsorted(t, side="right")) - 1


def evaluate(sym, v, sgn, setup, q, day_flags, data_end, flat_by, p=None, outcome=True):
    """One setup's filter checks (all of them, whatever filters_off says) and,
    with outcome=True, how it played out in R under the spec's exits."""
    p = p or DEFAULTS
    sv = v["sides"][sgn]
    m1, m5, h1, d1 = sv["m1"], sv["m5"], sv["h1"], sv["d1"]
    L, tags, b, r, ci = setup
    entry = m1["c"][ci]
    t_entry = m1["t"][ci] + pd.Timedelta(minutes=1)
    buf_b = p["buffer_atr"] * m1["atr"][b]
    stop = min(m1["l"][r], L - buf_b)
    R = entry - stop

    k5 = last_complete(m5, t_entry)
    kh = last_complete(h1, t_entry)
    kd = len(d1["c"]) - 1
    ltf = trend(m5, k5, p=p)
    htf_d = trend(d1, kd, p["daily_trend_sessions"], p=p)
    htf_h = trend(h1, kh, p=p)
    ema5 = k5 >= 0 and m5["e9"][k5] > m5["e21"][k5]

    # break of structure in the trade's direction within the last bos_window_min
    bos = False
    bos_window = pd.Timedelta(minutes=p["bos_window_min"])
    for j in range(ci, -1, -1):
        tc = m1["t"][j] + pd.Timedelta(minutes=1)
        if t_entry - tc > bos_window:
            break
        kj = last_complete(m5, tc)
        if kj >= 0 and np.isfinite(m5["last_sh"][kj]) and m1["c"][j] > m5["last_sh"][kj] + p["buffer_atr"] * m1["atr"][j]:
            bos = True
            break

    # QQQ and relative strength, on real (unflipped) prices
    raw = v["rth"]
    qi = qqq_at(q, m1["t"][ci]) if q is not None and len(q) else -1
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
        lo = max(0, qi - p["qqq_extreme_bars"] + 1)
        fighting = lo > 0 and ql[lo:qi + 1].min() < ql[:lo].min()
        qf = arrays(resample(q, 5, v["today"]) if sgn > 0 else flip(resample(q, 5, v["today"])), p)
        kq = last_complete(qf, t_entry)
        q_ema = bool(kq >= 0 and qf["e9"][kq] > qf["e21"][kq])
        checks.update({"qqq_vwap": bool(q_side), "qqq_ema5": q_ema,
                       "relative_strength": _num(rs, 5), "fighting_market": bool(fighting)})
        if not q_side or (p["qqq_ema5"] and not q_ema):
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
    if r_atr1 < p["min_r_atr1"]:
        failed.append("stop_too_tight")
    if r_atr5 > p["max_r_atr5"]:
        failed.append("stop_too_wide")
    for flag in day_flags.get(sym, []) + day_flags.get("*", []):
        failed.append(flag)

    t = lambda i: (m1["t"][i]).isoformat()
    row = {
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
        "target": _num(sgn * (entry + p["target_r"] * R)),
        "passed": not failed,
        "failed": failed,
        "checks": checks,
        "_ci": ci,
    }
    if outcome:
        res = simulate(sv, ci, entry, stop, data_end, flat_by, p)
        row.update({
            "outcome": res["outcome"],
            "exit_at": res["exit_at"].isoformat() if res["exit_at"] is not None else None,
            "exit_price": _num(sgn * res["exit_px"]),
            "r_multiple": _num(res["r"], 2),
        })
    return row


def scan_symbol(sym, v, q, day_flags, data_end, flat_by, p=None, outcome=True):
    """Every confirmed break and retest for one symbol today, both sides,
    each with its filter checks. `v` comes from build(); `q` is QQQ's RTH
    bars today (or None)."""
    p = p or DEFAULTS
    d = v["d1"]
    if len(d) >= 15:
        da = atr_series(d["high"].to_numpy(float), d["low"].to_numpy(float), d["close"].to_numpy(float))[-1]
    else:
        da = 0.0
    levels = merge(named_levels(v), p["merge_daily_atr"] * da)
    v["sides"] = {1: side_view(v, 1, p), -1: side_view(v, -1, p)}
    rows = []
    for sgn in (1, -1):
        lv = [{**x, "price": sgn * x["price"]} for x in levels]
        for setup in find_setups(v["sides"][sgn], lv, v["today"]["open"], p):
            rows.append(evaluate(sym, v, sgn, setup, q, day_flags, data_end, flat_by, p, outcome))
    rows.sort(key=lambda x: x["entry_at"])
    return rows


def blocking(row, p=None):
    """The failed filters that count under p's filters_off."""
    off = set((p or DEFAULTS).get("filters_off") or [])
    return [f for f in row["failed"] if f not in off]


# ---------------------------------------------------------------- Lab entry point

def view_from_ctx(day, ctx):
    """build()'s view from the Lab's (day, ctx), causal: today's bars are
    whatever `day` holds, and today's close is the fixed 16:00 bell so no
    session boundary depends on bars not yet seen."""
    d = ctx["date"]
    today = {"date": d, "open": pd.Timestamp(f"{d} 09:30", tz=NY), "close": pd.Timestamp(f"{d} 16:00", tz=NY)}
    prior_rth = ctx.get("prior_rth") or []
    prior = []
    for x in prior_rth:
        pdt = x.index[0].date()
        prior.append({"date": pdt, "open": pd.Timestamp(f"{pdt} 09:30", tz=NY),
                      "close": x.index[-1] + pd.Timedelta(minutes=1)})
    pre = ctx.get("premarket")
    pre = EMPTY if pre is None else pre
    daily = ctx.get("daily")
    if daily is None or not len(daily):
        daily = EMPTY
    elif daily.index.tz is None:
        daily = daily.tz_localize(NY)
    return _view(today, prior, prior_rth, day, pre, daily)


def signals(day, ctx, params_=None):
    p = params(params_)
    if len(day) < 6:
        return []
    v = view_from_ctx(day, ctx)
    q = ctx.get("ref_day")
    if q is not None:
        q = q[q.index <= day.index[-1]]
    flags = {"*": list(ctx.get("flags") or [])}
    if is_quad_witching(ctx["date"]):
        flags["*"].append("quad_witching")
    flat_by = pd.Timestamp(f"{ctx['date']} {p['flat_by']}", tz=NY)
    rows = scan_symbol(ctx.get("symbol", "SPY"), v, q, flags, day.index[-1] + pd.Timedelta(minutes=1),
                       flat_by, p, outcome=False)
    out = []
    for r in rows:
        if blocking(r, p):
            continue
        ts = day.index[r["_ci"]]
        direction = "long" if r["side"] == "Long" else "short"
        out.append(Signal(ts, direction, dte=p["dte"], otm=p["otm"], u_stop=r["stop"], u_target=r["target"],
                          exit_by=p["flat_by"], tag="br_" + "+".join(r["level_tags"]),
                          meta={"level": r["level"], "risk": r["risk"], "failed": r["failed"]}))
    return out
