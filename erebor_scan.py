#!/usr/bin/env python3
"""Erebor — single-name event screening.

Standalone from smaug_pipeline.py on purpose. Smaug is intraday SPY options
scalping; Erebor screens individual equities for event-driven dislocations and
holds multi-day positions in them. They share a Supabase project and the webapp
shell and nothing else — see the Erebor section of webapp/supabase/schema.sql.

Three screens run here: merger arbitrage (deal terms + price), short squeezes
(chatter universe + short interest), and whale flow (option chain volume vs
open interest). All three were meant to be split between this script and an AI
routine; the routine's environment turned out to block every source it needed
(see docs/erebor-routine-prompt.md), and the data was structured enough not to
need one. Liquidity sweeps come later, off `bars`.

WHAT THE SCREEN ACTUALLY DOES
    A cash-and-stub merger is the rare case where a stock has a *hard,
    published* anchor price. Holders are getting `cash_per_share` in cash plus
    `stub_pct` of the combined company, so every dollar the stock trades above
    the cash is a direct, checkable bet on what that stub is worth:

        stub per share    = price - cash_per_share
        implied combined  = stub per share * shares_out / (stub_pct / 100)

    Compare that against the value the deal was actually struck at and you get
    a number that needs no forecast to interpret. GPRO on 2026-09-03 traded at
    $1.83 against $1.14 cash and a 10% stub on ~159M shares, implying ~$1.1B
    for a company being recapitalised at $285M — a ~3.9x same-week markup on a
    business that had not reported a single day of combined results. It gave
    most of that back within a week.

    Nothing here forecasts. It restates the price as the assumption the price
    is making, and lets that assumption be judged.

Usage (needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in env):
    python erebor_scan.py              # normal run (scan + fill outcomes)
    python erebor_scan.py --dry-run    # compute and print, write nothing
    python erebor_scan.py --outcomes   # only fill forward outcomes on snapshots
    python erebor_scan.py --backtest   # score vs. forward pop, from snapshots
"""

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

# The service-role key bypasses RLS, so `auth.uid()` is NULL on every write
# from here and the NOT NULL DEFAULT auth.uid() columns would fail. Same
# invariant the AI routine's postgres connector lives under (see CLAUDE.md):
# reads look fine while writes silently stop, so user_id is always explicit.
#
# Preferred source is the deal row itself — a candidate derived from a deal
# belongs to whoever owns that deal, so the id propagates rather than being
# assumed. This env fallback only has to cover the case where there are no
# deals at all and a run row still needs an owner.
EREBOR_USER_ID = os.environ.get("EREBOR_USER_ID", "")

REQUEST_TIMEOUT = 30


# ----------------------------------------------------------------------
# Data layer
# ----------------------------------------------------------------------
def _supabase_headers(prefer=None):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in the environment."
        )
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _raise_for_status(resp):
    """requests' raise_for_status() drops the response body, which is exactly
    where PostgREST puts the useful error — surface it."""
    if resp.status_code >= 400:
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} {resp.reason} for {resp.url}: {resp.text}"
        )


def _json_safe(v):
    """None for NaN/inf so json.dumps never emits a bare `NaN` token — that is
    invalid JSON and PostgREST rejects the whole batch on it. Deliberately
    duplicated from smaug_pipeline rather than imported: importing it would
    make this module depend on the SPY pipeline, which is the coupling the
    whole module is arranged to avoid."""
    if v is None:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def load_open_deals():
    """Every announced (not closed, not terminated) deal, with its owner.

    PostgREST caps responses at 1000 rows and pages everything else in this
    project, but deals are hand-entered and there will never be a thousand of
    them; a single request that would silently truncate is still worth
    noticing, so the count is checked rather than assumed.
    """
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/erebor_deals",
        headers=_supabase_headers(),
        params={"select": "*", "status": "eq.announced", "order": "ticker.asc"},
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_status(resp)
    rows = resp.json()
    if len(rows) >= 1000:
        raise RuntimeError(
            "erebor_deals returned a full page — add paging before trusting this."
        )
    return rows


def upsert_candidates(rows):
    """Upsert on (user_id, as_of, ticker, kind).

    Not a plain insert: re-running on the same day must *correct* that day's
    reading rather than file a second one. History lives across days, never
    within one — which is the whole difference from daily_briefs, where a
    single overwritten row is why nobody could tell whether a screen had ever
    worked.
    """
    if not rows:
        return
    headers = _supabase_headers(
        prefer="resolution=merge-duplicates,return=minimal"
    )
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/erebor_candidates",
        headers=headers,
        params={"on_conflict": "user_id,as_of,ticker,kind"},
        json=rows,
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_status(resp)


def prune_candidates(owner, kind, as_ofs, keep_tickers):
    """Delete this kind's rows at `as_ofs` whose ticker is not in the current
    screen. Returns the number of rows removed.

    `erebor_candidates` is keyed on the DATA's date, which for a squeeze is the
    short-interest settlement — the same date for two to four weeks. Upserting
    each run therefore corrected the tickers it re-screened and left every
    ticker it no longer screened sitting there. By 2026-09-23 the panel was
    drawing 52 names under one Aug 31 settlement, 40 of them dropped from the
    universe days earlier and carrying prices up to five sessions stale, and
    the trader had no way to tell them from the 12 the screen actually found.

    So this table means "the current screen" and the pruning is what makes it
    true. History is not lost: `erebor_snapshots` keeps every run's reading
    keyed on the run date, which is what the backtest reads.

    Only ever called with a non-empty `keep_tickers` — a screen whose source
    broke returns no rows, and wiping the panel on a dead feed would turn a
    visible failure into an empty one.
    """
    if not as_ofs or not keep_tickers:
        return 0
    quoted = ",".join(f'"{t}"' for t in sorted(keep_tickers))
    resp = requests.delete(
        f"{SUPABASE_URL}/rest/v1/erebor_candidates",
        headers=_supabase_headers(prefer="return=representation"),
        params={
            "user_id": f"eq.{owner}",
            "kind": f"eq.{kind}",
            "as_of": f"in.({','.join(sorted(as_ofs))})",
            "ticker": f"not.in.({quoted})",
            "select": "ticker",
        },
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_status(resp)
    return len(resp.json())


def upsert_run(row):
    """One row per user per day, recording that a scan happened at all.

    This is the structural fix for the ambiguity found on 2026-09-08, when the
    morning brief ran, wrote empty arrays because its sources were unreachable,
    and the webapp rendered that as "Awaiting run" — a dead scraper and a quiet
    market were indistinguishable. A scan that ran and found nothing writes
    this row with its sources marked ok; a scan that never ran leaves no row.
    An empty day is therefore never ambiguous again.
    """
    headers = _supabase_headers(
        prefer="resolution=merge-duplicates,return=minimal"
    )
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/erebor_runs",
        headers=headers,
        params={"on_conflict": "user_id,as_of"},
        json=[row],
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_status(resp)


def upsert_snapshots(rows):
    """Upsert on (user_id, run_date, ticker, kind).

    `erebor_candidates` is keyed on the data's own date, so two runs that read
    the same settlement figure resolve to one row and the second run's price
    and chatter overwrite the first's. Correct for the panel, useless for a
    backtest: what the screen showed on the 17th is gone by the 18th. This
    table is keyed on the run date instead, one reading per run, and is
    never overwritten by a later day — the row is what the trader saw.
    """
    if not rows:
        return
    headers = _supabase_headers(
        prefer="resolution=merge-duplicates,return=minimal"
    )
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/erebor_snapshots",
        headers=headers,
        params={"on_conflict": "user_id,run_date,ticker,kind"},
        json=rows,
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_status(resp)


def load_snapshots(params):
    """Paged read of erebor_snapshots — PostgREST caps a response at 1000 rows
    and this table grows by a couple of dozen a day forever."""
    out, page = [], 1000
    for start in range(0, 100_000, page):
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/erebor_snapshots",
            headers={**_supabase_headers(), "Range": f"{start}-{start + page - 1}"},
            params={"select": "*", "order": "run_date.asc,ticker.asc", **params},
            timeout=REQUEST_TIMEOUT,
        )
        _raise_for_status(resp)
        rows = resp.json()
        out.extend(rows)
        if len(rows) < page:
            break
    return out


def load_prior_episode_anchors(kind, before_date):
    """Ticker -> episode anchor, from the most recent scan BEFORE `before_date`.

    Streak continuation is defined against the previous *scan*, not the
    previous calendar day: weekends, holidays and a skipped run would
    otherwise each break an episode that never actually lapsed. A ticker
    missing from that scan starts a fresh episode — the chatter universe
    turns over roughly two thirds a day, so a gap is a different setup.
    """
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/erebor_snapshots",
        headers=_supabase_headers(),
        params={
            "select": "run_date", "kind": f"eq.{kind}",
            "run_date": f"lt.{before_date}",
            "order": "run_date.desc", "limit": 1,
        },
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_status(resp)
    rows = resp.json()
    if not rows:
        return {}
    prior = rows[0]["run_date"]
    rows = load_snapshots({"kind": f"eq.{kind}", "run_date": f"eq.{prior}"})
    out = {}
    for r in rows:
        # A row from before these columns existed has no anchor to carry, so
        # it cannot continue an episode; the name simply starts a new one.
        if not r.get("episode_start"):
            continue
        ep = (r.get("metrics") or {}).get("episode") or {}
        out[r["ticker"]] = {
            "episode_start": r["episode_start"],
            "anchor_price": r.get("anchor_price"),
            "anchor_spy": r.get("anchor_spy"),
            "anchor_px_as_of": ep.get("anchor_px_as_of"),
            # Scans the name has been listed for, not calendar days: a holiday
            # or a skipped run should not inflate how long a setup has been
            # sitting there. Carried rather than counted so the panel needs no
            # second query.
            "days": int(ep.get("days") or 1),
        }
    return out


def upsert_backtest(row):
    """Upsert on (user_id, as_of, kind, score_version)."""
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/erebor_backtests",
        headers=_supabase_headers(prefer="resolution=merge-duplicates,return=minimal"),
        params={"on_conflict": "user_id,as_of,kind,score_version"},
        json=[row],
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_status(resp)


def patch_snapshot(row_id, fields):
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/erebor_snapshots",
        headers=_supabase_headers(prefer="return=minimal"),
        params={"id": f"eq.{row_id}"},
        json=fields,
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_status(resp)


# ----------------------------------------------------------------------
# Prices
# ----------------------------------------------------------------------
def fetch_prices(tickers):
    """Last close per ticker, plus the session that close is from.

    Returns (prices, failures). Deliberately partial-tolerant: one delisted or
    mistyped ticker must not cost the whole scan, so a name that fails is
    recorded as a failure and every other name still gets screened. The date
    comes from the bar's own index rather than today's date — a stale quote
    that silently wears the run date is precisely the class of lie the
    Roaring Kitty panel was already written to avoid.
    """
    import yfinance as yf

    prices, failures = {}, {}
    for t in tickers:
        try:
            hist = yf.Ticker(t).history(period="5d", interval="1d")
            if hist is None or hist.empty:
                failures[t] = "no price history returned"
                continue
            # Last *usable* row, not last row. After the close yfinance often
            # appends a placeholder bar for the session just ended (or the
            # next one) with a NaN close, and taking iloc[-1] blindly read that
            # as the price. Both scheduled runs on 2026-09-16/17 failed on
            # exactly this while a same-day manual dispatch succeeded, because
            # the scheduler fired hours after the close and the manual run
            # fired during the session. Walk back to the newest finite close.
            closes = hist["Close"]
            ok = closes[closes.notna() & (closes > 0)]
            if ok.empty:
                failures[t] = f"no finite close in {len(hist)} rows (last: {closes.iloc[-1]!r})"
                continue
            prices[t] = {
                # float32 from yfinance prints as 13.5447998046875; four
                # places is more than a quote carries
                "price": round(float(ok.iloc[-1]), 4),
                "as_of": ok.index[-1].date().isoformat(),
            }
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not end the scan
            failures[t] = f"{type(exc).__name__}: {exc}"
    return prices, failures


# ----------------------------------------------------------------------
# The merger-arb screen
# ----------------------------------------------------------------------
def screen_merger_arb(deal, quote):
    """Restate a price as the assumption it is making about the stub.

    Returns (metrics, score, note, skip_reason). `skip_reason` is not an error
    — an all-stock deal is a perfectly valid row that this particular screen
    simply cannot read, and conflating "not applicable" with "broken" is how a
    healthy scan starts looking like a failing one.
    """
    cash = deal.get("cash_per_share")
    stub_pct = deal.get("stub_pct")
    shares = deal.get("shares_outstanding")
    txn = deal.get("transaction_value_usd")
    price = quote["price"]

    if cash is None:
        return None, None, "", "no cash_per_share — all-stock deal, not screenable here"

    # Everything above the cash is the market's price for the stub. Negative is
    # not an error and not a discard: a stock *below* its cash consideration is
    # the classic arb (buy the spread, collect at close), and it is worth
    # surfacing for the opposite reason to an overshoot. The sign carries the
    # meaning, so nothing here takes an absolute value.
    stub_per_share = price - cash
    premium_to_cash_pct = (stub_per_share / cash * 100.0) if cash else None

    metrics = {
        "deal_id": deal.get("id"),
        "cash_per_share": cash,
        "stub_pct": stub_pct,
        "shares_outstanding": shares,
        "transaction_value_usd": txn,
        "stub_per_share": round(stub_per_share, 4),
        "premium_to_cash_pct": (
            round(premium_to_cash_pct, 2) if premium_to_cash_pct is not None else None
        ),
        "implied_combined_value_usd": None,
        "implied_vs_transaction_x": None,
    }

    # The headline number needs both a stub percentage and a share count. With
    # either missing the premium above is still real and still worth a row —
    # a partial reading beats no reading, and the absent fields say which.
    #
    # It also needs a POSITIVE stub. Below the cash consideration the formula
    # keeps returning a number and the number is meaningless: a stub cannot be
    # worth less than nothing, so "implies -$143M for the combined company" is
    # arithmetic run past the point where it describes anything. Below cash the
    # honest reading is the spread itself — buy at a discount, collect at close
    # — so the implied valuation is left absent rather than filled with a
    # figure whose sign is real but whose magnitude means nothing.
    implied_combined = None
    if stub_pct and shares and stub_pct > 0 and stub_per_share > 0:
        implied_combined = stub_per_share * shares / (stub_pct / 100.0)
        metrics["implied_combined_value_usd"] = round(implied_combined, 2)

    # Score is the multiple of the struck price the market is implying, and
    # only ever that. It is the most directly falsifiable framing available:
    # "the tape says this combination is worth N times what the buyer agreed
    # to pay for it, days ago, with full information."
    #
    # There is deliberately NO fallback score for a deal missing a share count.
    # An earlier version scored those as 1 + premium/100 so they would still
    # rank, and it silently produced a wrong ordering: a name 14% above cash
    # scored 0.892 on the multiple while a name AT cash scored 1.000 on the
    # premium, so the richer name sorted below the cheaper one. Two bases on
    # one axis cannot be compared no matter how they are scaled, and a ranking
    # that is quietly wrong is worse than a rank that is honestly absent.
    # A partial reading keeps its premium in `metrics` and its sentence in
    # `note`; it just does not claim a position in the ordering.
    score = None
    if implied_combined is not None and txn:
        score = implied_combined / txn
        metrics["implied_vs_transaction_x"] = round(score, 3)
        metrics["score_basis"] = "implied_vs_transaction"

    note = _describe(deal, price, cash, stub_per_share, premium_to_cash_pct, score, metrics)
    return metrics, score, note, None


def _describe(deal, price, cash, stub_per_share, premium_pct, score, metrics):
    """One line a human can check the arithmetic of, because the whole value of
    this screen is that its claim is verifiable rather than predictive."""
    t = deal.get("ticker", "?")
    bits = [f"{t} ${price:.2f} vs ${cash:.2f} cash"]
    # premium_pct is None only when cash is zero, which makes a percentage of
    # it undefined rather than large. Formatting it anyway would raise, so the
    # clause drops to the dollar figure it can always state.
    pct = "" if premium_pct is None else f" ({premium_pct:+.1f}%)"
    if stub_per_share < 0:
        bits.append(f"trading ${abs(stub_per_share):.2f} BELOW cash{pct} — spread to collect at close")
    else:
        bits.append(f"${stub_per_share:.2f} of stub priced in{pct}")
    implied = metrics.get("implied_combined_value_usd")
    txn = metrics.get("transaction_value_usd")
    if implied is not None and txn:
        bits.append(
            f"implies ${implied / 1e6:.0f}M for the combined co "
            f"vs ${txn / 1e6:.0f}M struck ({score:.1f}x)"
        )
    elif implied is not None:
        bits.append(f"implies ${implied / 1e6:.0f}M for the combined co")
    return " · ".join(bits) + "."


# ----------------------------------------------------------------------
# The squeeze screen (Roaring Kitty)
# ----------------------------------------------------------------------
# This screen was written for the AI routine first, on the theory that web
# reading needs judgement and a Python scraper of MarketBeat would rot faster
# than a prompt. That theory died on 2026-09-18: the cloud routine environment
# blocks outbound fetches to every one of its sources on organisation policy
# (TipRanks, MarketBeat, apewisdom, StockTwits, CBOE all EGRESS_BLOCKED), so
# the routine could never have produced a row. It also turned out the data is
# more structured than the prompt assumed — yfinance carries short interest as
# a percent of float, days to cover AND the settlement date per ticker, and
# both chatter feeds are plain JSON. Nothing here parses HTML.
#
# What changed in the screen's meaning, stated plainly: the old design ranked
# the whole market by short interest (TipRanks) and then checked chatter. This
# one starts from the chatter universe (apewisdom's top pages plus StockTwits
# trending) and checks short interest. "Most shorted, quiet" therefore means
# most shorted *among names retail is at least mentioning*, not most shorted
# on the exchange. That is a narrower baseline and a better-targeted one for a
# squeeze screen — a name nobody mentions is not about to be squeezed — but it
# is a different claim and the panel copy should not pretend otherwise.

BUZZ_FLOOR = 5          # matches the panel's floor; below it there is no buzz object
SHORT_FLOAT_MIN_PCT = 10.0
APEWISDOM_PAGES = 2     # 100 tickers per page; two pages ~40s of yfinance lookups
MAX_LOUD = 4            # names with chatter — the actionable half
MAX_QUIET = 8           # shorted-but-quiet baseline

# Squeeze score. A 0-100 composite over figures the panel already shows, so
# every point of it can be re-derived from the tile it sits on. Every stored
# score carries SCORE_VERSION, so a later formula is never compared against an
# earlier one's outcomes as if they were the same number.
#
# READ THIS BEFORE TRADING OFF THE SCORE. High short interest, on its own,
# predicts *negative* abnormal returns — Asquith & Meulbroek (1995) and
# Asquith, Pathak & Ritter (2005) both find heavily shorted names underperform,
# because short sellers are informed on average. A squeeze is the tail of that
# distribution, not its centre, and "most shorted" read unconditionally is a
# bearish list. Nothing in this score contradicts that; it ranks candidates
# within a screen the trader has already chosen to look at.
#
# sq2 weights follow the evidence rather than intuition. sq1 led with short
# float (40) over days to cover (25), which is backwards:
#   - Hong, Li, Ni, Scheinkman & Yan (NBER w21166): short interest alone is a
#     weak predictor; days-to-cover — short interest over daily volume — is
#     the robust one. The volume denominator carries the signal.
#   - Boehmer, Huszar, Wang & Zhang, 38 countries: days-to-cover is the most
#     robust of eight short-selling variables, and once borrow fees and
#     utilization enter the regression short interest stops predicting at all.
#   - Every historical squeeze (VW 2008, GME 2021 and 2024, AMC, OPEN 2025)
#     needed a dated spark and was accelerated by short-dated call buying
#     forcing dealer hedging. None of them started because short interest was
#     high; that only set how far the move ran.
# So: days to cover leads, short float is demoted to a necessary-but-weak
# condition, and the call-side flow the whale screen already measures enters
# as its own term. Borrow fee and utilization are the two inputs the evidence
# rates highest that no free source publishes — their absence is the main
# known gap in this score, not an oversight.
#
# Each input is clamped to a 0-1 ramp between a floor and a ceiling:
#   trapped     days to cover           1  -> 0, 10  -> 1
#   fuel        short % of float       10% -> 0, 50% -> 1
#   spark       chatter present         0 or 1
#   accelerant  call volume / call OI  0.2 -> 0, 1.0 -> 1; zero if flow is
#               leaning bearish, None when the name has no readable chain
#   pressing    shares short vs prior -20% -> 0, +20% -> 1 (flat = 0.5)
SCORE_VERSION = "sq2"
SCORE_WEIGHTS = {"trapped": 30, "fuel": 20, "spark": 20, "accelerant": 15, "pressing": 15}
SCORE_RAMPS = {
    "fuel": (10.0, 50.0),
    "trapped": (1.0, 10.0),
    "pressing": (-0.20, 0.20),
    "accelerant": (0.2, 1.0),
}

# Outcome window for the backtest: a "pop" is the max high over the next
# POP_WINDOW sessions clearing POP_THRESHOLD_PCT above the snapshot price.
POP_WINDOW = 5
POP_THRESHOLD_PCT = 15.0

# Below this many scored-and-resolved snapshots, the backtest is plumbing
# rather than evidence, and both the terminal and the panel say so. Roughly a
# quarter of daily scans — picked to be honest about how long this takes, not
# to be reachable soon.
BACKTEST_MIN_N = 30


def _ramp(x, lo, hi):
    if x is None:
        return None
    return max(0.0, min(1.0, (float(x) - lo) / (hi - lo)))


def score_squeeze(metrics):
    """Returns (score, parts) for a squeeze row's metrics, or (None, {}) when
    the one mandatory input (short float) is missing. A missing optional input
    scores zero for its part rather than dropping the row — a name with no
    days-to-cover figure is still a candidate, just an unproven one, and the
    part is recorded as None rather than 0 so the backtest can tell "absent"
    from "measured and low"."""
    pct = metrics.get("short_percent_float")
    if pct is None:
        return None, {}
    ss, prior = metrics.get("shares_short"), metrics.get("shares_short_prior")
    growth = (ss / prior - 1.0) if ss and prior else None
    # Call volume against standing call OI: new call buying is what forces a
    # dealer to hedge by buying stock, which is the accelerant every historical
    # squeeze had. Flow leaning bearish scores zero rather than None — that is
    # a real reading that the accelerant is absent, not a missing one.
    accel = None
    cv, coi = metrics.get("call_vol"), metrics.get("call_oi")
    if cv is not None and coi:
        accel = (
            0.0 if metrics.get("flow_lean") == "bearish"
            else _ramp(cv / coi, *SCORE_RAMPS["accelerant"])
        )
    parts = {
        "trapped": _ramp(metrics.get("days_to_cover"), *SCORE_RAMPS["trapped"]),
        "fuel": _ramp(pct, *SCORE_RAMPS["fuel"]),
        "spark": 1.0 if metrics.get("buzz") else 0.0,
        "accelerant": accel,
        "pressing": _ramp(growth, *SCORE_RAMPS["pressing"]),
    }
    total = sum(SCORE_WEIGHTS[k] * (v or 0.0) for k, v in parts.items())
    parts = {k: (None if v is None else round(v, 3)) for k, v in parts.items()}
    return round(total, 1), parts

# ETFs create and redeem shares on demand and cannot squeeze; their short-float
# readings (XBI once showed 118%) are an artifact of that mechanism. Crypto
# tickers on the chatter feeds end in .X and are not equities.
ETF_SKIP = {
    "SPY", "QQQ", "IWM", "DIA", "TLT", "XBI", "VXX", "UVXY", "SQQQ", "TQQQ",
    "SPXU", "SPXL", "SOXL", "SOXS", "TZA", "TNA", "GLD", "SLV", "USO", "ARKK",
    "HYG", "LQD", "EEM", "EFA", "XLF", "XLE", "XLK", "SMH", "KRE", "GDX",
    "IBIT", "FBTC", "ETHA", "GBTC", "BITO", "MSTY", "TSLL", "NVDL", "XLV", "XLI",
}

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"}


def _is_equity_ticker(t):
    # Drops crypto (.X) and junk. ETFs stay in the universe: they cannot
    # squeeze, so the squeeze screen drops them, but options flow on IBIT or
    # SMH is real flow and the whale screen wants it.
    return bool(t) and t.isascii() and t.upper() == t and "." not in t


def fetch_chatter():
    """The universe, with per-name buzz. Returns (universe, sources).

    universe: {ticker: buzz-or-None}. A name is in the universe if either feed
    mentions it; it carries a buzz object only if apewisdom counts it at or
    above BUZZ_FLOOR. Below the floor there is no buzz object at all, not a
    zero — one person typing a ticker is not chatter, and the panel renders
    the presence of the object as "loud". Same rule the prompt enforced.
    """
    universe, sources = {}, {}
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

    got_any = False
    for page in range(1, APEWISDOM_PAGES + 1):
        try:
            r = requests.get(
                f"https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/{page}",
                headers=UA, timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            for row in r.json().get("results", []):
                t = str(row.get("ticker", "")).strip().upper()
                if not _is_equity_ticker(t):
                    continue
                got_any = True
                mentions = row.get("mentions")
                buzz = None
                if isinstance(mentions, (int, float)) and mentions >= BUZZ_FLOOR:
                    buzz = {
                        "mentions": int(mentions),
                        "mentions_prev": row.get("mentions_24h_ago"),
                        "upvotes": row.get("upvotes"),
                        "source": "wallstreetbets",
                        # chatter's own vintage — a rolling 24h window ending
                        # now — kept separate from the settlement date
                        "as_of": today,
                    }
                # keep the louder reading if a name appears on two pages
                if t not in universe or (buzz and not universe[t]):
                    universe[t] = buzz
        except Exception as exc:  # noqa: BLE001
            sources["apewisdom"] = f"failed: {type(exc).__name__}"
            break
    sources.setdefault("apewisdom", "ok" if got_any else "failed")

    try:
        r = requests.get(
            "https://api.stocktwits.com/api/2/trending/symbols.json",
            headers=UA, timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        syms = [str(x.get("symbol", "")).strip().upper() for x in r.json().get("symbols", [])]
        # Trending is membership, not a count: a name on the list is loud by
        # the source's own definition, so it gets a buzz object with the
        # source named and no mention figure invented for it.
        for t in syms:
            if not _is_equity_ticker(t):
                continue
            if not universe.get(t):
                universe[t] = universe.get(t) or {
                    "mentions": None, "mentions_prev": None, "upvotes": None,
                    "source": "stocktwits", "as_of": today,
                }
        sources["stocktwits"] = "ok" if syms else "failed"
    except Exception as exc:  # noqa: BLE001
        sources["stocktwits"] = f"failed: {type(exc).__name__}"

    return universe, sources


def fetch_short_interest(tickers):
    """Per-ticker short interest from yfinance. Returns (data, failures).

    yfinance reports shortPercentOfFloat as a FRACTION (0.1741). The schema
    and the panel want the source's own printed scale — 17.41 means 17.41% —
    so it is multiplied here, exactly once, and nowhere downstream. The panel
    deliberately does no fraction detection (0.4 is a real reading), so getting
    this wrong would render every name as under 1% short.
    """
    import yfinance as yf

    data, failures = {}, {}
    for t in tickers:
        try:
            info = yf.Ticker(t).info or {}
            frac = info.get("shortPercentOfFloat")
            if frac is None:
                failures[t] = "no short interest field"
                continue
            settle = info.get("dateShortInterest")
            data[t] = {
                "short_percent_float": round(float(frac) * 100.0, 2),
                "days_to_cover": info.get("shortRatio"),
                "shares_short": info.get("sharesShort"),
                "shares_short_prior": info.get("sharesShortPriorMonth"),
                "float_shares": info.get("floatShares"),
                "company": info.get("shortName") or info.get("longName") or "",
                # the settlement date the figure describes — normally two to
                # four weeks old, and that is correct, never today's date
                "as_of": (
                    datetime.fromtimestamp(settle, tz=timezone.utc).date().isoformat()
                    if isinstance(settle, (int, float)) else None
                ),
            }
        except Exception as exc:  # noqa: BLE001
            failures[t] = f"{type(exc).__name__}: {exc}"
    return data, failures


def screen_squeeze(owner, universe, flow=None):
    """Returns (candidates, sources, errors) for kind='squeeze'.
    `universe` comes from fetch_chatter() and `flow` from fetch_option_flow();
    both are shared with the whale screen so neither source is fetched twice."""
    sources, errors = {}, []
    flow = flow or {}
    if not universe:
        return [], sources, errors

    tickers = sorted(t for t in universe if t not in ETF_SKIP)
    si, si_failures = fetch_short_interest(tickers)
    sources["yfinance_short"] = (
        "ok" if not si_failures else ("failed" if not si else "partial")
    )
    # Names with no short-interest field are common (ADRs, tiny floats) and
    # not worth an error line each; the count is what matters.
    if si_failures:
        errors.append({"stage": "short_interest", "missing": len(si_failures)})

    prices, px_failures = fetch_prices([t for t in tickers if t in si])
    sources["yfinance"] = "ok" if not px_failures else ("failed" if not prices else "partial")

    rows = []
    for t, d in si.items():
        if t in ETF_SKIP:
            continue
        if d["short_percent_float"] < SHORT_FLOAT_MIN_PCT or not d["as_of"]:
            continue
        q = prices.get(t)
        buzz = universe.get(t)
        metrics = {
            "short_percent_float": d["short_percent_float"],
            "days_to_cover": d["days_to_cover"],
            "shares_short": d["shares_short"],
            "shares_short_prior": d["shares_short_prior"],
            "float_shares": d["float_shares"],
        }
        if buzz:
            metrics["buzz"] = buzz
        # The accelerant inputs, copied in raw so the score stays re-derivable
        # from the row. Absent for any name with no readable option chain,
        # which is common down the chatter universe and is not an error.
        f = flow.get(t)
        if f:
            metrics["call_vol"] = f["call_volume"]
            metrics["call_oi"] = f["call_oi"]
            metrics["put_vol"] = f["put_volume"]
            metrics["put_oi"] = f["put_oi"]
            lean, skew, basis = _lean(
                f["call_volume"], f["put_volume"], f["call_oi"], f["put_oi"]
            )
            metrics["flow_lean"] = lean
            metrics["flow_skew"] = skew
            metrics["flow_basis"] = basis
        score, parts = score_squeeze(metrics)
        metrics["score_version"] = SCORE_VERSION
        metrics["score_parts"] = parts
        rows.append(
            {
                "user_id": owner,
                "as_of": d["as_of"],
                "ticker": t,
                "kind": "squeeze",
                "company": d["company"],
                "price": _json_safe(q["price"]) if q else None,
                "price_as_of": q["as_of"] if q else None,
                # Versioned composite over the figures in `metrics`, so the
                # trader can re-derive it from the tile. This used to be NULL
                # on principle (a stored number nobody can check is one a
                # position gets sized on); it is stored now because the
                # backtest needs the score exactly as the panel showed it.
                "score": score,
                "metrics": {k: _json_safe(v) for k, v in metrics.items()},
                "note": "",
            }
        )

    # Two sections, capped separately, both ranked by score. The loud names
    # are the actionable half and are usually few; the quiet baseline is
    # capped so the panel stays a screen rather than a table.
    rows.sort(key=lambda r: -(r["score"] or 0))
    loud = [r for r in rows if "buzz" in r["metrics"]][:MAX_LOUD]
    quiet = [r for r in rows if "buzz" not in r["metrics"]][:MAX_QUIET]
    return loud + quiet, sources, errors


# ----------------------------------------------------------------------
# The whale screen (Whale Action)
# ----------------------------------------------------------------------
# MarketBeat's unusual-options table — the source the panel was designed
# around — renders a single row without JavaScript, so it is not reachable from
# plain HTTP any more than it was from the cloud routine. This derives the
# same idea from yfinance option chains instead, and the idea is NOT the same
# measurement, which the panel copy is careful about:
#
#   MarketBeat: today's options volume vs a rolling AVERAGE of daily volume
#   Here:       today's options volume vs OPEN INTEREST on the same contracts
#
# Volume over open interest is the standard "new positioning" read — a name
# trading more contracts today than exist open across its front expiries is
# seeing flow that was not there yesterday. It is a same-day signal rather than
# a vs-history one, and it is stored under its own key (`open_interest`) rather
# than aliased into `avg_volume`, so the tile labels it "x OI" and never
# "x avg". A multiple against the wrong denominator, correctly labelled, is
# still a wrong multiple; the label is what stops it being read as the other.
#
# The universe is the chatter universe plus any open deals — the names Erebor
# is already watching — not the whole market. That is a real narrowing versus
# the source's market-wide scan and is stated in the panel's subtitle.

WHALE_EXPIRIES = 3        # expiries aggregated per name, after the skip below
WHALE_MIN_DAYS = 7        # skip expiries closer than this
WHALE_MIN_VOLUME = 2000   # contracts; below this vol/OI is noise on a tiny book
MAX_WHALES = 8
# Screens whose names carry an episode (first-flagged date, anchor price and
# SPY) from scan to scan. Merger arb doesn't: its anchor is the deal terms.
EPISODE_KINDS = ("squeeze", "whale")

# Whale score. Same contract as the squeeze score — 0-100, versioned, derived
# only from figures stored on the row — but the evidence behind it is THINNER
# and the weights should be trusted less until the backtest speaks.
#
# What the literature actually supports: Pan & Poteshman (2006) find that
# put-call ratios built from option volume *initiated by buyers to open new
# positions* predict returns — low P/C beat high P/C by ~40bp the next day and
# over 1% the next week, and the effect is strongest where leverage is highest.
# That is a real result, and it is not quite what this screen measures.
# yfinance publishes total contract volume, with no buy/sell initiation and no
# open/close flag, so:
#   - the call share here is a crude stand-in for their P/C, polluted by
#     sellers and by closing trades;
#   - volume over open interest is a proxy for "these are NEW positions",
#     which is the part their result actually turns on.
# Both proxies point the right way, neither is the measured quantity. That gap
# is the known weakness of this score, the same way a missing borrow fee is
# the squeeze score's.
#
# NOTE ON DIRECTION: this scores signal STRENGTH, not bullishness. A bearish
# name with a high score is a strong bearish read. `lean` carries the sign and
# the backtest scores each row in its own direction — a bearish flag that fell
# is a hit, not a miss.
#
#   newness     volume / open interest   0.2 -> 0, 1.5 -> 1
#   conviction  |lean skew - 0.5| * 2    0.3 -> 0, 0.9 -> 1  (0.65/0.35 bands
#               are already required upstream, so this grades past the gate).
#               wh2: the skew is each side's volume over its OWN open interest,
#               not the raw call share — see _lean().
#   size        total contracts          2k  -> 0, 50k -> 1  (log-scaled: the
#               step from 2k to 10k means far more than 42k to 50k)
WHALE_SCORE_VERSION = "wh2"
WHALE_SCORE_WEIGHTS = {"newness": 40, "conviction": 35, "size": 25}
WHALE_SCORE_RAMPS = {
    "newness": (0.2, 1.5),
    "conviction": (0.3, 0.9),
}
WHALE_SIZE_RAMP = (2_000.0, 50_000.0)


def score_whale(metrics):
    """Returns (score, parts) for a whale row's metrics, or (None, {}) when the
    volume/OI reading that the whole screen rests on is missing."""
    vol, oi = metrics.get("volume"), metrics.get("open_interest")
    if not vol or not oi:
        return None, {}
    # Grade the same quantity the lean was judged on, or the two disagree:
    # under wh1 this used the raw volume share while the lean used it too, and
    # both carried the call-heavy bias. `lean_skew` is already normalised by
    # open interest where that was possible, and says so in `lean_basis`.
    skew = metrics.get("lean_skew")
    if skew is None:
        cv, pv = metrics.get("call_volume") or 0, metrics.get("put_volume") or 0
        skew = cv / (cv + pv) if (cv + pv) else None
    conviction = abs(skew - 0.5) * 2 if skew is not None else None
    parts = {
        "newness": _ramp(vol / oi, *WHALE_SCORE_RAMPS["newness"]),
        "conviction": _ramp(conviction, *WHALE_SCORE_RAMPS["conviction"]),
        # Log-scaled: doubling a small book is a real change, adding the same
        # contracts to an already-huge one is not.
        "size": _ramp(
            math.log10(max(vol, 1.0)),
            math.log10(WHALE_SIZE_RAMP[0]),
            math.log10(WHALE_SIZE_RAMP[1]),
        ),
    }
    total_score = sum(WHALE_SCORE_WEIGHTS[k] * (v or 0.0) for k, v in parts.items())
    parts = {k: (None if v is None else round(v, 3)) for k, v in parts.items()}
    return round(total_score, 1), parts

# WHALE_MIN_DAYS exists because the first version of this screen ranked TSLA,
# AAPL, AMZN, GOOGL and META at the top every day. Names with daily expiries
# turn over more than their open interest in the front week as a matter of
# routine — 0DTE gamma trade, not positioning — so vol/OI over the nearest
# expiries measures how heavily a name is day-traded, which is the opposite
# of unusual. Measured on 2026-09-21: TSLA 1.69x over the front three
# expiries, 0.61x once anything under a week out was skipped; AAPL 1.06x to
# 0.38x. The panel's own comment warns that "a mega-cap's ordinary million
# contracts" must not outrank the name that did something, and this is the
# line that enforces it. Directional skew is required for the same reason:
# two-way churn on a big book is not a whale, however large.


def fetch_option_flow(tickers):
    """Aggregate call/put volume and open interest over the front expiries.

    Returns (data, failures). Names with no options listed are common and are
    counted rather than itemised; a name with chains but under the volume
    floor is not a failure, it is a quiet name, and is simply not returned.
    """
    import yfinance as yf

    today = datetime.now(ZoneInfo("America/New_York")).date()
    data, failures = {}, {}
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            exps = [
                e for e in (tk.options or [])
                if (date.fromisoformat(e) - today).days >= WHALE_MIN_DAYS
            ][:WHALE_EXPIRIES]
            if not exps:
                failures[t] = "no options listed beyond the front week"
                continue
            cv = pv = co = po = 0
            top = None
            for e in exps:
                ch = tk.option_chain(e)
                cv += int(ch.calls["volume"].fillna(0).sum())
                co += int(ch.calls["openInterest"].fillna(0).sum())
                pv += int(ch.puts["volume"].fillna(0).sum())
                po += int(ch.puts["openInterest"].fillna(0).sum())
                for side, df in (("C", ch.calls), ("P", ch.puts)):
                    c = _top_contract(df, side, e)
                    if c and (top is None or c["volume"] > top["volume"]):
                        top = c
            data[t] = {
                "call_volume": cv, "put_volume": pv,
                "call_oi": co, "put_oi": po,
                "expiries": exps,
                "top": top,
            }
        except Exception as exc:  # noqa: BLE001
            failures[t] = f"{type(exc).__name__}: {exc}"
    return data, failures


def _top_contract(df, side, expiry):
    """The single busiest contract in one side of one expiry, or None.

    The chain totals say a name is busy; this says where. On 2026-09-23 U
    printed 190K calls, and a quarter of them were one Oct 16 $46 sweep —
    the strike and expiry are what decide whether following it is still a
    trade, and the totals cannot show them. Raw figures only: the % out of
    the money is left to the panel, which has the price beside it.
    """
    if df is None or df.empty:
        return None
    vol = df["volume"].fillna(0)
    i = vol.idxmax()
    if vol[i] <= 0:
        return None
    row = df.loc[i]

    def num(col, places=4):
        v = row.get(col)
        return None if v is None else _json_safe(round(float(v), places))

    return {
        "side": side,
        "strike": num("strike"),
        "expiry": expiry,
        "volume": int(vol[i]),
        "open_interest": int(num("openInterest", 0) or 0),
        # yfinance's IV is a fraction and is unreliable on illiquid strikes;
        # stored as printed and labelled as yfinance's in the panel tooltip.
        "iv": num("impliedVolatility"),
        "last": num("lastPrice"),
    }


def _lean(cv, pv, coi=None, poi=None):
    """(lean, skew, basis) from call/put volume, normalised by open interest.

    The first version compared raw volume: calls >= 65% of contracts was
    bullish. Single-name equity option volume is structurally call-heavy, so
    that band mostly detected the baseline — over the first days of readings
    it returned bullish 19 times, mixed 9 and bearish 3, and the whale panel
    was bullish on every row it ever drew. A directional screen that only ever
    says one direction is not discriminating between names, it is reporting
    what the asset class does.

    So each side is divided by its OWN open interest first, and the sides are
    compared on that. The question becomes "which side is opening more new
    positions relative to what is already standing there", which is both free
    of the level bias and much closer to the quantity with actual evidence
    behind it — Pan & Poteshman's result is about volume that OPENS positions,
    not volume in total.

    Falls back to the raw share when either side has no open interest to
    divide by, and says which rule judged it so a row is never ambiguous.
    """
    tot = cv + pv
    if tot <= 0:
        return "mixed", None, "none"
    if coi and poi:
        ct, pt = cv / coi, pv / poi
        skew = ct / (ct + pt) if (ct + pt) > 0 else None
        basis = "oi_turnover"
    else:
        skew, basis = cv / tot, "volume_share"
    if skew is None:
        return "mixed", None, basis
    # Wide bands on purpose: 60/40 is ordinary two-way trade, not a lean.
    lean = "bullish" if skew >= 0.65 else ("bearish" if skew <= 0.35 else "mixed")
    return lean, round(skew, 4), basis


def screen_whale(owner, flow, failures, session_date):
    """Returns (candidates, sources, errors) for kind='whale'.
    `flow` comes from fetch_option_flow(), fetched once in run() and shared
    with the squeeze screen — the chains are the slowest fetch in the module
    and the two screens look at overlapping tickers."""
    sources, errors = {}, []
    sources["yfinance_options"] = (
        "ok" if not failures else ("failed" if not flow else "partial")
    )
    if failures:
        errors.append({"stage": "option_flow", "missing": len(failures)})

    rows = []
    for t, d in flow.items():
        vol = d["call_volume"] + d["put_volume"]
        oi = d["call_oi"] + d["put_oi"]
        if vol < WHALE_MIN_VOLUME or oi <= 0:
            continue
        ratio = vol / oi
        lean, skew, lean_basis = _lean(
            d["call_volume"], d["put_volume"], d["call_oi"], d["put_oi"]
        )
        if lean == "mixed":
            continue
        exps = d["expiries"]
        window = exps[0] if len(exps) == 1 else f"{exps[0]} to {exps[-1]}"
        flow_text = (
            f"{d['call_volume']:,} calls vs {d['put_volume']:,} puts, "
            f"expiries {window} \u00b7 {ratio:.2f}\u00d7 open interest"
        )
        rows.append(
            {
                "user_id": owner,
                "as_of": session_date,
                "ticker": t,
                "kind": "whale",
                "company": "",
                "price": None,
                "price_as_of": None,
                "score": None,   # filled just below, once metrics exist
                "metrics": {
                    "lean": lean,
                    # What the lean was judged on, and by which rule. Stored so
                    # the call is checkable against the four raw figures beside
                    # it rather than having to be taken on trust.
                    "lean_skew": skew,
                    "lean_basis": lean_basis,
                    "volume": vol,
                    "call_volume": d["call_volume"],
                    "put_volume": d["put_volume"],
                    "call_oi": d["call_oi"],
                    "put_oi": d["put_oi"],
                    "open_interest": oi,
                    "vol_oi_ratio": round(ratio, 3),
                    "expiries": d["expiries"],
                    "top": d.get("top"),
                    "flow": flow_text,
                },
                "note": "",
            }
        )

    for r in rows:
        score, parts = score_whale(r["metrics"])
        r["score"] = score
        r["metrics"]["score_version"] = WHALE_SCORE_VERSION
        r["metrics"]["score_parts"] = parts

    # Ranked by score rather than by the raw multiple: a 3x on 2,100 contracts
    # and a 3x on 60,000 are not the same event, and the multiple alone put
    # them level.
    rows.sort(key=lambda r: -(r["score"] or 0))
    return rows[:MAX_WHALES], sources, errors


def fetch_earnings_dates(tickers):
    """Ticker -> next earnings date (ISO) on or after today, where yfinance has
    one. Best-effort and silent on a miss: ETFs have none, and an absent date
    must never cost the scan. It matters because earnings inside the flow's
    expiry window turn a directional bet into an IV-crush bet."""
    import yfinance as yf

    today = datetime.now(ZoneInfo("America/New_York")).date()
    out = {}
    for t in tickers:
        try:
            cal = yf.Ticker(t).calendar or {}
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            upcoming = sorted(d for d in (dates or []) if d >= today)
            if upcoming:
                out[t] = upcoming[0].isoformat()
        except Exception:  # noqa: BLE001 - optional context, never fatal
            continue
    return out


# ----------------------------------------------------------------------
# Outcomes and backtest
# ----------------------------------------------------------------------
def snapshot_rows(candidates, run_date, anchors=None, spy=None):
    """The day's candidates, re-keyed on the run date for `erebor_snapshots`.
    Same figures, same score — nothing recomputed, so the snapshot is exactly
    the reading the panel rendered.

    `anchors` maps kind -> ticker -> the prior scan's episode anchor. Present
    means the streak continues and the original anchor is carried forward
    unchanged; absent means this row opens a new episode anchored on today.
    """
    anchors = anchors or {}
    rows = []
    for c in candidates:
        prev = (anchors.get(c["kind"]) or {}).get(c["ticker"])
        if prev:
            start = prev["episode_start"]
            anchor_price = prev["anchor_price"]
            anchor_spy = prev["anchor_spy"]
        else:
            start = run_date
            anchor_price = c["price"]
            anchor_spy = spy
        rows.append(
            {
                "user_id": c["user_id"],
                "run_date": run_date,
                "ticker": c["ticker"],
                "kind": c["kind"],
                "price": c["price"],
                "price_as_of": c["price_as_of"],
                "score": c["score"],
                "score_version": c["metrics"].get("score_version"),
                "metrics": c["metrics"],
                "episode_start": start,
                "anchor_price": _json_safe(anchor_price),
                "anchor_spy": _json_safe(anchor_spy),
                "spy": _json_safe(spy),
            }
        )
    return rows


def compute_outcome(hist, base_date, base_price, spy_hist=None):
    """Forward result over the POP_WINDOW sessions strictly after `base_date`.

    Returns None until the full window has printed — a partial window would
    understate every max and make early rows look like duds. `base_date` is
    the price's own session, not the run date: a scan that ran at 6pm read
    that day's close, and the window starts the next morning.

    When `spy_hist` is supplied the same window is measured on SPY and the
    difference recorded, because a name that rose while the whole tape rose
    is not squeezing. That subtraction assumes a beta of one and is a crude
    adjustment, not a risk model — it is stored beside the raw figure rather
    than replacing it so the unadjusted number stays checkable.
    """
    if not base_price or base_price <= 0:
        return None
    fwd = hist[hist.index.date > date.fromisoformat(base_date)].head(POP_WINDOW)
    if len(fwd) < POP_WINDOW:
        return None
    max_high = float(fwd["High"].max())
    min_low = float(fwd["Low"].min())
    last_close = float(fwd["Close"].iloc[-1])
    ret_max = (max_high / base_price - 1.0) * 100.0
    spy_ret = None
    if spy_hist is not None and not spy_hist.empty:
        sfwd = spy_hist[spy_hist.index.date > date.fromisoformat(base_date)].head(POP_WINDOW)
        prior = spy_hist[spy_hist.index.date <= date.fromisoformat(base_date)]
        if len(sfwd) == POP_WINDOW and not prior.empty:
            base_spy = float(prior["Close"].iloc[-1])
            if base_spy > 0:
                spy_ret = (float(sfwd["Close"].iloc[-1]) / base_spy - 1.0) * 100.0
    return {
        "window": POP_WINDOW,
        "threshold_pct": POP_THRESHOLD_PCT,
        "base_price": base_price,
        "max_high": round(max_high, 4),
        "min_low": round(min_low, 4),
        "last_close": round(last_close, 4),
        "ret_max_pct": round(ret_max, 2),
        "ret_min_pct": round((min_low / base_price - 1.0) * 100.0, 2),
        "ret_close_pct": round((last_close / base_price - 1.0) * 100.0, 2),
        "pop": ret_max >= POP_THRESHOLD_PCT,
        "spy_ret_close_pct": None if spy_ret is None else round(spy_ret, 2),
        "ret_max_vs_spy_pct": None if spy_ret is None else round(ret_max - spy_ret, 2),
        "pop_vs_spy": None if spy_ret is None else (ret_max - spy_ret) >= POP_THRESHOLD_PCT,
        "through": fwd.index[-1].date().isoformat(),
    }


def fill_outcomes(dry_run=False):
    """Fill `outcome` on every snapshot old enough to have a full window.

    One history fetch per ticker covering every pending row for it, rather
    than one per row — the same name shows up day after day, and yfinance
    rate-limits. Rows whose window has not finished are left for a later run.
    """
    import yfinance as yf

    pending = load_snapshots({"outcome": "is.null"})
    if not pending:
        print("[erebor] outcomes: nothing pending")
        return 0

    # A row with no base price can never be scored — compute_outcome() has
    # nothing to measure a return against — so it would otherwise sit in the
    # pending set forever, costing a yfinance fetch every run and adding a
    # permanent floor to the "still pending" count. A count with junk under it
    # stops being a number anyone reads. These get a terminal marker instead:
    # the row is kept, because it is a true record of what the panel showed,
    # but it stops being asked a question it cannot answer.
    unscorable = [r for r in pending if not r.get("price")]
    pending = [r for r in pending if r.get("price")]
    if unscorable and not dry_run:
        for r in unscorable:
            patch_snapshot(
                r["id"],
                {
                    "outcome": {"unscorable": True, "reason": "no base price"},
                    "outcome_as_of": datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
                },
            )
        print(f"[erebor] outcomes: {len(unscorable)} row(s) marked unscorable (no base price)")
    if not pending:
        print("[erebor] outcomes: nothing left pending")
        return 0

    by_ticker = {}
    for r in pending:
        by_ticker.setdefault(r["ticker"], []).append(r)

    filled, today = 0, datetime.now(ZoneInfo("America/New_York")).date()
    # One SPY pull covering every pending row, so each outcome can be stated
    # against the tape it happened in. A failure here degrades to unbenchmarked
    # outcomes rather than costing the pass — the raw figures still stand.
    spy_hist = None
    try:
        earliest_all = min(r.get("price_as_of") or r["run_date"] for r in pending)
        spy_hist = yf.Ticker("SPY").history(start=earliest_all, interval="1d")
    except Exception as exc:  # noqa: BLE001
        print(f"  [outcome] SPY benchmark unavailable: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    for t, rows in sorted(by_ticker.items()):
        earliest = min(r.get("price_as_of") or r["run_date"] for r in rows)
        # Window can't have finished yet — skip the fetch entirely.
        if (today - date.fromisoformat(earliest)).days < POP_WINDOW:
            continue
        try:
            hist = yf.Ticker(t).history(start=earliest, interval="1d")
        except Exception as exc:  # noqa: BLE001
            print(f"  [outcome] {t}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if hist is None or hist.empty:
            continue
        for r in rows:
            base_date = r.get("price_as_of") or r["run_date"]
            out = compute_outcome(hist, base_date, r.get("price"), spy_hist)
            if out is None:
                continue
            filled += 1
            tag = "POP " if out["pop"] else "    "
            vs = ("" if out["ret_max_vs_spy_pct"] is None
                  else f"  vs SPY {out['ret_max_vs_spy_pct']:+6.1f}%")
            print(f"  [{tag}] {t:<6} {base_date}  max {out['ret_max_pct']:+6.1f}%"
                  f"  close {out['ret_close_pct']:+6.1f}%{vs}")
            if not dry_run:
                patch_snapshot(r["id"], {"outcome": out, "outcome_as_of": today.isoformat()})
    print(f"[erebor] outcomes: {filled} filled, {len(pending) - filled} still pending")
    return filled


def _spearman(xs, ys):
    """Rank correlation without numpy — the workflow deliberately installs
    only yfinance and requests. Average ranks for ties."""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r, i = [0.0] * len(v), 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0 + 1.0
            i = j + 1
        return r
    n = len(xs)
    if n < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    return cov / math.sqrt(vx * vy) if vx and vy else None


# Which raw inputs the per-kind correlation table reports alongside the score.
BACKTEST_RAW_FIELDS = {
    "squeeze": ("short_percent_float", "days_to_cover"),
    "whale": ("vol_oi_ratio", "volume"),
}


def _directional(outcome, kind, metrics):
    """The forward return in the direction the screen actually flagged.

    A squeeze flag is always a bet on up. A whale flag is not: a bearish name
    that fell is a correct call, and scoring it on its maximum HIGH would file
    every good bearish read as a miss. `lean` carries the sign, so it is
    applied here rather than in compute_outcome() — the outcome stays a plain
    record of what the price did, and each screen interprets it.

    Returns (return_pct, return_vs_spy_pct_or_None).
    """
    spy = outcome.get("spy_ret_close_pct")
    if kind == "whale" and metrics.get("lean") == "bearish":
        r = -outcome["ret_min_pct"]
        return r, (None if spy is None else r + spy)
    r = outcome["ret_max_pct"]
    return r, (None if spy is None else r - spy)


def backtest(kind="squeeze", quiet=False):
    """Did the score predict a move in the direction it flagged?

    Reads every scored snapshot with an outcome and reports the hit rate by
    score tercile, the rank correlation of score (and each part) with the
    forward directional return, and the same for the raw inputs. Terciles
    rather than deciles because the table will be small for months; splitting
    40 rows ten ways would just be reading noise.

    Returns the report as a dict so the daily scan can store it, and prints it
    unless `quiet`. Writes nothing itself.
    """
    say = (lambda *a: None) if quiet else print
    rows = [
        r for r in load_snapshots({"kind": f"eq.{kind}", "outcome": "not.is.null"})
        # `ret_max_pct` rather than a truthy outcome: an unscorable row carries
        # a terminal marker, not a reading, and _directional() would raise on
        # it. Checking for the field keeps the two apart by what is actually
        # there rather than by a flag that has to be remembered.
        if r.get("score") is not None and (r.get("outcome") or {}).get("ret_max_pct") is not None
    ]
    if not rows:
        say(f"[erebor] backtest: no scored {kind} snapshots with outcomes yet")
        return None
    versions = sorted({r.get("score_version") or "?" for r in rows})
    say(f"[erebor] backtest: {len(rows)} {kind} snapshot(s), score version(s) {versions}")
    if len(versions) > 1:
        say("  WARNING: mixed score versions — the formula changed mid-series;"
            " read per-version numbers, not the pooled ones.")

    scores = [r["score"] for r in rows]
    pairs = [_directional(r["outcome"], kind, r.get("metrics") or {}) for r in rows]
    ret = [p[0] for p in pairs]
    ret_spy = [p[1] for p in pairs]
    thr = rows[0]["outcome"].get("threshold_pct", POP_THRESHOLD_PCT)
    win = rows[0]["outcome"].get("window", POP_WINDOW)
    hits = [1 if r >= thr else 0 for r in ret]
    base = sum(hits) / len(hits)
    direction = "in the flagged direction" if kind == "whale" else "max high"
    say(f"  hit = {direction} >= +{thr:.0f}% within {win} sessions;"
        f" base rate {base:.0%}")

    order = sorted(range(len(rows)), key=lambda i: scores[i])
    thirds = [order[: len(order) // 3], order[len(order) // 3: 2 * len(order) // 3],
              order[2 * len(order) // 3:]]
    terciles = []
    say("  score tercile      n   hit rate   median ret   vs SPY")
    for label, idx in zip(("low", "mid", "high"), thirds):
        if not idx:
            continue
        rr = sorted(ret[i] for i in idx)
        med = rr[len(rr) // 2]
        adj = [ret_spy[i] for i in idx if ret_spy[i] is not None]
        med_adj = sorted(adj)[len(adj) // 2] if adj else None
        rate = sum(hits[i] for i in idx) / len(idx)
        lo, hi = scores[idx[0]], scores[idx[-1]]
        terciles.append({"band": label, "score_lo": lo, "score_hi": hi, "n": len(idx),
                         "hit_rate": round(rate, 3), "median_ret_pct": round(med, 2),
                         "median_ret_vs_spy_pct": None if med_adj is None else round(med_adj, 2)})
        say(f"  {label:<5} {lo:5.1f}-{hi:5.1f} {len(idx):4d}   {rate:7.0%}   {med:+8.1f}%   "
            + ("     —" if med_adj is None else f"{med_adj:+6.1f}%"))

    def rho_of(xs):
        keep = [i for i, x in enumerate(xs) if x is not None]
        return _spearman([xs[i] for i in keep], [ret[i] for i in keep]), len(keep)

    say("  spearman vs directional return:")
    corr = {}
    r0, n0 = rho_of(scores)
    corr["score"] = {"rho": None if r0 is None else round(r0, 3), "n": n0}
    say(f"    score            {r0:+.2f}" if r0 is not None else "    score            n/a")
    weights = WHALE_SCORE_WEIGHTS if kind == "whale" else SCORE_WEIGHTS
    for part in weights:
        r1, n1 = rho_of([(r["metrics"].get("score_parts") or {}).get(part) for r in rows])
        corr[f"part.{part}"] = {"rho": None if r1 is None else round(r1, 3), "n": n1}
        say(f"    part {part:<12}{r1:+.2f}  (n={n1})" if r1 is not None
            else f"    part {part:<12}n/a")
    for raw in BACKTEST_RAW_FIELDS.get(kind, ()):
        r2, n2 = rho_of([r["metrics"].get(raw) for r in rows])
        corr[f"raw.{raw}"] = {"rho": None if r2 is None else round(r2, 3), "n": n2}
        say(f"    raw  {raw:<20}{r2:+.2f}  (n={n2})" if r2 is not None
            else f"    raw  {raw:<20}n/a")

    # Stated in the report itself, not just at the terminal, because the panel
    # renders this and "n=14" has to arrive with its own health warning.
    trustworthy = len(rows) >= BACKTEST_MIN_N
    if not trustworthy:
        say(f"  ({len(rows)} rows is too few to trust any of this; it is a smoke test"
            f" of the plumbing until there are {BACKTEST_MIN_N}+)")

    episodes = report_episodes(kind, quiet=quiet) if kind == "squeeze" else None
    return {
        "kind": kind,
        "n": len(rows),
        "score_versions": versions,
        "mixed_versions": len(versions) > 1,
        "window": win,
        "threshold_pct": thr,
        "base_rate": round(base, 3),
        "min_n": BACKTEST_MIN_N,
        "trustworthy": trustworthy,
        "terciles": terciles,
        "spearman": corr,
        "episodes": episodes,
    }


def report_episodes(kind="squeeze", quiet=False):
    """Per listing episode: how long a name stayed on the screen and what it
    did from its anchor — including after it dropped off.

    Derived from the snapshots rather than stored, so it cannot drift out of
    step with them. This is the half that answers "did we flag that one
    correctly": the panel can only ever show names still listed, and names
    leave the screen partly *because* they worked, so a reading taken over
    current members is biased toward the ones that did nothing.
    """
    say = (lambda *a: None) if quiet else print
    rows = [r for r in load_snapshots({"kind": f"eq.{kind}"}) if r.get("episode_start")]
    if not rows:
        say("  episodes: no anchored snapshots yet")
        return []
    eps = {}
    for r in rows:
        eps.setdefault((r["ticker"], r["episode_start"]), []).append(r)

    say(f"  {len(eps)} listing episode(s):")
    say("  ticker  anchored     days  peak score   since anchor   vs SPY   fwd max")
    out = []
    for (t, start), rs in sorted(eps.items(), key=lambda kv: kv[0][1], reverse=True)[:25]:
        rs.sort(key=lambda r: r["run_date"])
        anchor, last = rs[0].get("anchor_price"), rs[-1]
        drift = vs_spy = None
        if anchor and last.get("price"):
            drift = (last["price"] / anchor - 1.0) * 100.0
            a_spy, l_spy = rs[0].get("anchor_spy"), last.get("spy")
            if a_spy and l_spy:
                vs_spy = drift - (l_spy / a_spy - 1.0) * 100.0
        peak = max((r["score"] for r in rs if r.get("score") is not None), default=None)
        # The forward window on the LAST listed day is what carries past the
        # end of the episode, which is exactly where a squeeze tends to land.
        # None for an unscorable last row, which is correct: the episode has
        # no measured forward move, as distinct from one measured at zero.
        fwd = (last.get("outcome") or {}).get("ret_max_pct")
        fmt = lambda v, suf="%": "     —" if v is None else f"{v:+6.1f}{suf}"
        say(f"  {t:<7} {start}  {len(rs):4d}  "
            f"{'    —' if peak is None else f'{peak:9.1f}'}   "
            f"{fmt(drift)}  {fmt(vs_spy)}  {fmt(fwd)}")
        out.append({
            "ticker": t, "episode_start": start, "scans": len(rs),
            "peak_score": peak,
            "drift_pct": None if drift is None else round(drift, 2),
            "drift_vs_spy_pct": None if vs_spy is None else round(vs_spy, 2),
            "forward_max_pct": fwd,
            "last_run_date": last["run_date"],
        })
    return out


def store_backtests(owner, as_of, kinds=("squeeze", "whale")):
    """Compute and store the report card for each scored kind.

    Keyed by score_version as well as by day, so switching formulas leaves the
    old version's row standing rather than overwriting it with numbers built
    from a different score. A run whose snapshots span two versions is stored
    under a joined key and flagged `mixed_versions` in the report — the panel
    is expected to say so rather than draw the pooled figure as if it meant
    something.
    """
    stored = 0
    for kind in kinds:
        report = backtest(kind, quiet=True)
        if not report:
            continue
        upsert_backtest(
            {
                "user_id": owner,
                "as_of": as_of,
                "kind": kind,
                "score_version": "+".join(report["score_versions"]),
                "n": report["n"],
                "base_rate": report["base_rate"],
                "trustworthy": report["trustworthy"],
                "report": _json_safe_deep(report),
            }
        )
        stored += 1
        print(f"[erebor] backtest stored: {kind} n={report['n']} "
              f"base {report['base_rate']:.0%} "
              f"{'' if report['trustworthy'] else '(below trust threshold)'}")
    if not stored:
        print("[erebor] backtest: nothing resolved yet, no report stored")
    return stored


def _json_safe_deep(v):
    """_json_safe over a nested structure. The report is built from measured
    floats and a stray inf or NaN anywhere in it would have PostgREST reject
    the whole row, which is the same failure mode _json_safe exists for."""
    if isinstance(v, dict):
        return {k: _json_safe_deep(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe_deep(x) for x in v]
    return _json_safe(v)


# ----------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------
def run(dry_run=False):
    # The run's own date, in market time. GitHub's scheduler fired the 22:00
    # UTC cron at 00:13 UTC on 2026-09-17, and date.today() on the runner
    # called that the 17th — labelling the 16th's close as a session that had
    # not opened yet. Every date in this module is an ET trading date.
    as_of = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    errors, skipped = [], []

    deals = load_open_deals()
    print(f"[erebor] {len(deals)} announced deal(s)")

    # Owner resolves from a deal row when there is one, else the env fallback.
    # Resolved here rather than at write time because the squeeze screen
    # needs it before a single row exists.
    owner = deals[0]["user_id"] if deals else EREBOR_USER_ID
    if not owner:
        # No deals and no configured owner means there is nothing to attribute
        # a run row to. Say so rather than writing under a guessed id — a run
        # row with the wrong owner is invisible to the trader and would make
        # an unmonitored scan look monitored.
        print("[erebor] no deals and no EREBOR_USER_ID — nothing written", file=sys.stderr)
        return

    sources = {"erebor_deals": "ok"}
    kinds = ["merger_arb"]
    candidates = []

    if deals:
        tickers = sorted({d["ticker"] for d in deals if d.get("ticker")})
        prices, price_failures = fetch_prices(tickers)
        # Partial success is its own state and is recorded as one. "ok" when
        # every ticker priced, "partial" when some did, "failed" when none did
        # — so a reader can tell a quiet screen from a broken feed without
        # having to infer it from an empty result, which is the mistake this
        # module exists downstream of.
        sources["yfinance"] = (
            "ok" if not price_failures
            else ("failed" if not prices else "partial")
        )
        for t, why in sorted(price_failures.items()):
            errors.append({"ticker": t, "stage": "price", "error": why})

        for deal in deals:
            t = deal.get("ticker")
            quote = prices.get(t)
            if not quote:
                continue  # already recorded as a price failure above
            metrics, score, note, skip_reason = screen_merger_arb(deal, quote)
            if skip_reason:
                skipped.append({"ticker": t, "reason": skip_reason})
                continue
            candidates.append(
                {
                    "user_id": deal["user_id"],
                    # The session the price is from, NOT the run date: the
                    # deal terms are static, so a merger-arb reading *is* a
                    # price reading, and it carries that price's own date the
                    # same way a squeeze row carries its settlement date.
                    # Keeps a late cron from filing yesterday's close under
                    # today, and makes the (as_of, ticker, kind) key mean
                    # "one reading per session" rather than "one per run".
                    "as_of": quote["as_of"],
                    "ticker": t,
                    "kind": "merger_arb",
                    "company": deal.get("company") or "",
                    "price": _json_safe(quote["price"]),
                    "price_as_of": quote["as_of"],
                    "score": _json_safe(score),
                    "metrics": {k: _json_safe(v) for k, v in metrics.items()},
                    "note": note,
                }
            )

    # Rank richest-first: the overshoots are what the screen is for, and a name
    # below its cash consideration sorts to the bottom where it reads as the
    # different (and much rarer) opportunity it is.
    candidates.sort(key=lambda c: (c["score"] is None, -(c["score"] or 0)))

    # Squeeze screen. Its sources are recorded under their own keys so a dead
    # chatter feed cannot be mistaken for a dead price feed, and its kind is
    # listed so the panel can tell "attempted, found nothing" from "not run".
    # One chatter fetch feeds both screens: it is the universe for the squeeze
    # screen and, with the deal tickers added, for the whale screen too.
    universe, chatter_sources = fetch_chatter()
    sources.update(chatter_sources)

    # One option-chain pass feeds both screens, same as the chatter fetch: it
    # is the slowest thing in the module, and the squeeze score now uses the
    # call-side flow that the whale screen was already reading for these very
    # tickers. Fetched before the squeeze screen so it can be scored with it.
    wh_tickers = list(universe) + [d["ticker"] for d in deals if d.get("ticker")]
    flow, flow_failures = fetch_option_flow(sorted(set(wh_tickers)))

    kinds.append("squeeze")
    sq_rows, sq_sources, sq_errors = screen_squeeze(owner, universe, flow)
    sources.update(sq_sources)
    errors.extend(sq_errors)
    candidates.extend(sq_rows)
    loud = sum(1 for r in sq_rows if "buzz" in r["metrics"])
    print(f"[erebor] squeeze: {len(sq_rows)} name(s), {loud} with chatter")

    kinds.append("whale")
    wh_rows, wh_sources, wh_errors = screen_whale(owner, flow, flow_failures, as_of)
    sources.update(wh_sources)
    errors.extend(wh_errors)
    # Priced after the cut, so only the names that made the panel cost a
    # lookup. Without a price a whale snapshot has no base for
    # compute_outcome(), and every whale row through 2026-09-23 was written
    # unpriced — the whale report card could never grade a single one.
    if wh_rows:
        wh_prices, wh_px_failures = fetch_prices([r["ticker"] for r in wh_rows])
        sources["yfinance_whale_prices"] = (
            "ok" if not wh_px_failures
            else ("failed" if not wh_prices else "partial")
        )
        for t, why in sorted(wh_px_failures.items()):
            errors.append({"ticker": t, "stage": "whale_price", "error": why})
        earnings = fetch_earnings_dates([r["ticker"] for r in wh_rows])
        for r in wh_rows:
            q = wh_prices.get(r["ticker"])
            if q:
                r["price"] = _json_safe(q["price"])
                r["price_as_of"] = q["as_of"]
            r["metrics"]["earnings_date"] = earnings.get(r["ticker"])
    candidates.extend(wh_rows)
    print(f"[erebor] whale: {len(wh_rows)} name(s) over {len(set(wh_tickers))} looked up")

    for c in candidates:
        if c["kind"] == "whale":
            m = c["metrics"]
            print(f"  [whale] {c['ticker']:<6} {m['lean']:<8} {m['flow']}")
        elif c["kind"] == "squeeze":
            m = c["metrics"]
            tag = "LOUD " if "buzz" in m else "quiet"
            print(f"  [{tag}] {c['ticker']:<6} score {c['score'] or 0:>5.1f}"
                  f"  {m['short_percent_float']:>6.2f}% short"
                  f"  dtc {m.get('days_to_cover') or '-'}  settled {c['as_of']}")
        else:
            print(f"  {c['note']}")
    for s in skipped:
        print(f"  [skip] {s['ticker']}: {s['reason']}")
    for e in errors:
        print(f"  [error] {e}", file=sys.stderr)

    if dry_run:
        print("[erebor] --dry-run: nothing written")
        return

    # SPY's own close for this session, so every row can be read against the
    # tape it was taken in — a name that rose while the whole market rose is
    # not squeezing. Best-effort: a missing benchmark leaves the field NULL
    # rather than failing the scan.
    spy_prices, _ = fetch_prices(["SPY"])
    spy_close = (spy_prices.get("SPY") or {}).get("price")
    # Whales get episodes too, so the panel can say how far a name has run
    # since it was first flagged. That is the "am I late?" question, and the
    # flow columns alone can't answer it.
    anchors = {k: load_prior_episode_anchors(k, as_of) for k in EPISODE_KINDS}

    # The episode rides in `metrics` as well as its own columns, because the
    # panel reads `erebor_candidates` and would otherwise need a second query
    # against the snapshots to draw a drift the scan already knows.
    for c in candidates:
        if c["kind"] not in EPISODE_KINDS:
            continue
        prev = anchors[c["kind"]].get(c["ticker"])
        ep = {
            "start": prev["episode_start"] if prev else as_of,
            "anchor_price": _json_safe(prev["anchor_price"] if prev else c["price"]),
            "anchor_spy": _json_safe(prev["anchor_spy"] if prev else spy_close),
            "spy": _json_safe(spy_close),
            "days": (prev["days"] + 1) if prev else 1,
        }
        # The SESSION the anchor price came from, not the day it was recorded.
        # A scan run before the open re-reads the previous close, so a name on
        # its second scan can be priced off the same session it was anchored
        # on — and a drift computed across that is structurally 0.0%, which
        # reads as "went nowhere" when it means "no new close yet".
        ep["anchor_px_as_of"] = (
            prev.get("anchor_px_as_of") if prev else c.get("price_as_of")
        )
        c["metrics"]["episode"] = ep

    upsert_candidates(candidates)

    # Now that this run's rows are in, drop the ones it superseded. After the
    # write rather than before, so a failure here leaves a panel with extra
    # names rather than no names.
    for kind in ("squeeze", "whale", "merger_arb"):
        mine = [c for c in candidates if c["kind"] == kind]
        if not mine:
            continue  # source broke or screen was clean; never wipe on that
        removed = prune_candidates(
            owner, kind, {c["as_of"] for c in mine}, {c["ticker"] for c in mine}
        )
        if removed:
            print(f"[erebor] pruned {removed} superseded {kind} row(s)")

    snaps = snapshot_rows(candidates, as_of, anchors, spy_close)
    upsert_snapshots(snaps)
    fresh = sum(1 for r in snaps if r["kind"] == "squeeze" and r["episode_start"] == as_of)
    held = sum(1 for r in snaps if r["kind"] == "squeeze") - fresh
    print(f"[erebor] episodes: {fresh} new, {held} continuing")
    upsert_run(
        {
            "user_id": owner,
            "as_of": as_of,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "kinds": kinds,
            "sources": sources,
            "candidates_written": len(candidates),
            # Skips ride along with errors but stay labelled as skips: an
            # all-stock deal this screen cannot read is not a malfunction, and
            # folding the two together would make a healthy scan look sick.
            "errors": errors + ([{"skipped": skipped}] if skipped else []),
        }
    )
    print(f"[erebor] wrote {len(candidates)} candidate(s) for {as_of}")

    # After the day's write, not before: a yfinance failure here should never
    # cost the scan, and the outcome pass is idempotent so a miss today is
    # simply picked up tomorrow.
    try:
        fill_outcomes()
    except Exception as exc:  # noqa: BLE001
        print(f"[erebor] outcomes failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    # The report card, recomputed and stored every run so the panel can show it
    # without anyone opening a terminal. Runs after the outcome pass so it sees
    # today's fills. Wrapped for the same reason: a broken report is not worth
    # a failed scan, and the row simply stays at yesterday's version.
    try:
        store_backtests(owner, as_of)
    except Exception as exc:  # noqa: BLE001
        print(f"[erebor] backtest store failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Erebor single-name event screen.")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print, write nothing to Supabase",
    )
    ap.add_argument(
        "--outcomes",
        action="store_true",
        help="only fill forward outcomes on pending snapshots, no scan",
    )
    ap.add_argument(
        "--backtest",
        action="store_true",
        help="print how the squeeze score has related to forward pops, plus a "
             "per-episode report of what each listed name went on to do; "
             "writes nothing",
    )
    args = ap.parse_args()
    if args.backtest:
        backtest("squeeze")
        backtest("whale")
    elif args.outcomes:
        fill_outcomes(dry_run=args.dry_run)
    else:
        run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
