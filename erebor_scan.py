#!/usr/bin/env python3
"""Erebor — single-name event screening.

Standalone from smaug_pipeline.py on purpose. Smaug is intraday SPY options
scalping; Erebor screens individual equities for event-driven dislocations and
holds multi-day positions in them. They share a Supabase project and the webapp
shell and nothing else — see the Erebor section of webapp/supabase/schema.sql.

This run implements the merger-arbitrage screen only. Squeezes stay with the AI
routine (they need a browser and judgment, and a Python scraper of MarketBeat
would rot faster than a prompt does); liquidity sweeps come later off `bars`.

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
    python erebor_scan.py              # normal run
    python erebor_scan.py --dry-run    # compute and print, write nothing
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
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

# ETFs create and redeem shares on demand and cannot squeeze; their short-float
# readings (XBI once showed 118%) are an artifact of that mechanism. Crypto
# tickers on the chatter feeds end in .X and are not equities.
ETF_SKIP = {
    "SPY", "QQQ", "IWM", "DIA", "TLT", "XBI", "VXX", "UVXY", "SQQQ", "TQQQ",
    "SPXU", "SPXL", "SOXL", "SOXS", "TZA", "TNA", "GLD", "SLV", "USO", "ARKK",
    "HYG", "LQD", "EEM", "EFA", "XLF", "XLE", "XLK", "SMH", "KRE", "GDX",
}

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"}


def _is_equity_ticker(t):
    return bool(t) and t.isascii() and t.upper() == t and "." not in t and t not in ETF_SKIP


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


def screen_squeeze(owner):
    """Returns (candidates, sources, errors) for kind='squeeze'."""
    universe, sources = fetch_chatter()
    errors = []
    if not universe:
        return [], sources, errors

    tickers = sorted(universe)
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
        rows.append(
            {
                "user_id": owner,
                "as_of": d["as_of"],
                "ticker": t,
                "kind": "squeeze",
                "company": d["company"],
                "price": _json_safe(q["price"]) if q else None,
                "price_as_of": q["as_of"] if q else None,
                # NULL on purpose: the panel ranks from the raw figures so the
                # trader can check the ordering against the source. A stored
                # composite is the number that would get a position sized on it.
                "score": None,
                "metrics": {k: _json_safe(v) for k, v in metrics.items()},
                "note": "",
            }
        )

    # Two sections, capped separately, both ranked by short float. The loud
    # names are the actionable half and are usually few; the quiet baseline
    # is capped so the panel stays a screen rather than a table.
    rows.sort(key=lambda r: -r["metrics"]["short_percent_float"])
    loud = [r for r in rows if "buzz" in r["metrics"]][:MAX_LOUD]
    quiet = [r for r in rows if "buzz" not in r["metrics"]][:MAX_QUIET]
    return loud + quiet, sources, errors


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
    kinds.append("squeeze")
    sq_rows, sq_sources, sq_errors = screen_squeeze(owner)
    sources.update(sq_sources)
    errors.extend(sq_errors)
    candidates.extend(sq_rows)
    loud = sum(1 for r in sq_rows if "buzz" in r["metrics"])
    print(f"[erebor] squeeze: {len(sq_rows)} name(s), {loud} with chatter")

    for c in candidates:
        if c["kind"] == "squeeze":
            m = c["metrics"]
            tag = "LOUD " if "buzz" in m else "quiet"
            print(f"  [{tag}] {c['ticker']:<6} {m['short_percent_float']:>6.2f}% short"
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

    upsert_candidates(candidates)
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


def main():
    ap = argparse.ArgumentParser(description="Erebor single-name event screen.")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print, write nothing to Supabase",
    )
    args = ap.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
