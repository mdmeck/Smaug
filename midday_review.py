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

The detector itself is the Lab's `lab/strategies/break_retest.py` (the
one-implementation rule); this script only fetches, calls it, matches the
journal and writes.

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

# ---- the detector. One implementation, shared with the Lab: every rule and
# spec default lives in lab/strategies/break_retest.py (DEFAULTS there), so the
# 2pm review and the Lab can't disagree about what a setup is.
from lab.strategies import break_retest as br  # noqa: E402

P = br.DEFAULTS
PRIOR_SESSIONS = br.PRIOR_SESSIONS
FLAT_BY = br._hm(P["flat_by"])
JOURNAL_MATCH = pd.Timedelta(minutes=5)
flip = br.flip
is_quad_witching = br.is_quad_witching

NOTES = [
    "FOMC days and the IV-over-60 skip aren't checked.",
    "Outcomes are R on the underlying (stop and 2.5R target from the spec), not option P/L.",
    "QQQ confirmation is the VWAP test (spec table #7 default); QQQ's 5-minute EMAs are recorded, not required.",
]


def in_window(t):
    return br.in_window(t, P)


def scan(m1_by_sym, daily_by_sym, sessions, data_end, day_flags):
    """All setups for today. Returns (rows, errors)."""
    today = sessions[-1]
    flat_by = min(pd.Timestamp.combine(today["date"], FLAT_BY).tz_localize(NY),
                  today["close"] - pd.Timedelta(minutes=15))
    qv = br.build(m1_by_sym.get(REFERENCE), daily_by_sym.get(REFERENCE), sessions, data_end)
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
            v = br.build(m1, daily_by_sym.get(sym), sessions, data_end)
            if len(v["rth"]) < 6:
                errors[sym] = "too few bars today"
                continue
            rows += br.scan_symbol(sym, v, q, day_flags, data_end, flat_by, P)
        except Exception as e:  # noqa: BLE001 - one symbol never sinks the run
            errors[sym] = f"{type(e).__name__}: {e}"
    for r in rows:
        r.pop("_ci", None)
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
    # Daily bars stop just before today's midnight ET. Only prior sessions are
    # used (build() drops today), and asking for today's still-forming daily bar
    # is "recent SIP data", which the free plan refuses with a 403.
    daily_end = sessions[-1]["open"].normalize() - pd.Timedelta(seconds=1)
    drows = alpaca._paged_bars("/v2/stocks/bars", {
        "symbols": ",".join(symbols), "timeframe": "1Day",
        "start": alpaca._iso((now - pd.Timedelta(days=DAILY_LOOKBACK_DAYS)).date()),
        "end": alpaca._iso(daily_end), "feed": "sip", "adjustment": "split"})
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
