"""
Smaug daily regression pipeline for SPY 1-minute data.

Run once per day after the close (scheduled). Each run:
  1. Pulls recent SPY 1-min bars from yfinance and upserts into Supabase
     (yfinance serves ~7-8 days of 1-min history, so daily runs never miss).
  2. Computes indicator features per bar and upserts them alongside the
     bars (full retained window, every run, so the stored features
     self-heal if this file's feature formulas ever change).
  3. Builds forward-move targets at several horizons.
  4. Runs correlation, OLS regression (time-based train/test split),
     and decile analysis, and inserts the result as a new row in
     analysis_runs.
  5. Also writes local smaug_results.json/smaug_bars.json/smaug_report.txt
     for local debugging — these are gitignored, not committed.

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment.
The service-role key bypasses Row Level Security, so it must only ever
be used here (server-side) — never in the browser-facing webapp.

Usage:
  python smaug_pipeline.py              # normal daily run
  python smaug_pipeline.py --no-fetch   # re-run analysis on stored data only
  python smaug_pipeline.py --synthetic  # smoke-test with fake data
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

RESULTS_JSON = "smaug_results.json"
REPORT_TXT = "smaug_report.txt"
BARS_JSON = "smaug_bars.json"
TICKER = "SPY"
HORIZONS = [5, 10, 15]          # minutes ahead for forward return targets
MFE_HORIZON = 10                # minutes for max-favorable-excursion target
RTH_ONLY = True                 # keep regular trading hours only (9:30-16:00 ET)
TEST_FRACTION = 0.25            # most recent 25% of data held out for testing
MIN_ROWS = 500                  # refuse to run analysis on less than this
RETENTION_DAYS = 60             # prune bars older than this so the table stays bounded
SUPABASE_PAGE_SIZE = 1000       # PostgREST's default max rows per request
SUPABASE_BATCH_SIZE = 500       # rows per upsert request

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")


# ----------------------------------------------------------------------
# Data layer (Supabase — bars + analysis_runs, both public-read,
# service-role-write only; see webapp/supabase/schema.sql)
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
    """requests' raise_for_status() drops the response body, which is
    exactly where PostgREST puts the useful error (bad column, failed
    constraint, etc) — surface it instead of a bare '400 Client Error'."""
    if resp.status_code >= 400:
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} {resp.reason} for {resp.url}: {resp.text}"
        )


def _json_safe(v):
    """None for NaN/inf so json.dumps never emits a bare `NaN` token —
    that's invalid JSON and PostgREST rejects the whole batch on it."""
    if v is None:
        return None
    if isinstance(v, float) and not np.isfinite(v):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def upsert_bars_supabase(df):
    """Upsert raw OHLCV only. merge-duplicates only touches columns present
    in the request body, so this never clobbers an existing row's features
    (set separately by upsert_features_supabase)."""
    rows = [
        {
            "ts": ts.isoformat(),
            "ticker": TICKER,
            "open": float(r.open), "high": float(r.high),
            "low": float(r.low), "close": float(r.close),
            "volume": int(r.volume),
        }
        for ts, r in df.iterrows()
        if not (np.isnan(r.open) or np.isnan(r.close))
    ]
    headers = _supabase_headers(prefer="resolution=merge-duplicates,return=minimal")
    for i in range(0, len(rows), SUPABASE_BATCH_SIZE):
        batch = rows[i:i + SUPABASE_BATCH_SIZE]
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/bars", headers=headers, json=batch,
            params={"on_conflict": "ts"}, timeout=30,
        )
        _raise_for_status(resp)
    return len(rows)


def upsert_features_supabase(bars, feats):
    """Rewrites bars + features for the *entire* retained window every run
    (not just new bars) — deliberately, so stored features self-heal if
    compute_features() ever changes, rather than accumulating drift.

    Must include open/high/low/close/volume here even though
    upsert_bars_supabase already wrote them: Postgres validates a full
    candidate row against NOT NULL constraints before it even checks
    ON CONFLICT, so a features-only payload fails that check immediately —
    even when the row already exists and this would just be an update."""
    rows = []
    for ts, r in bars.iterrows():
        if ts not in feats.index or np.isnan(r.open) or np.isnan(r.close):
            continue
        rows.append({
            "ts": ts.isoformat(),
            "ticker": TICKER,
            "open": float(r.open), "high": float(r.high),
            "low": float(r.low), "close": float(r.close),
            "volume": int(r.volume),
            "features": {k: _json_safe(v) for k, v in feats.loc[ts].items()},
        })
    headers = _supabase_headers(prefer="resolution=merge-duplicates,return=minimal")
    for i in range(0, len(rows), SUPABASE_BATCH_SIZE):
        batch = rows[i:i + SUPABASE_BATCH_SIZE]
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/bars", headers=headers, json=batch,
            params={"on_conflict": "ts"}, timeout=30,
        )
        _raise_for_status(resp)
    return len(rows)


def _protected_session_dates():
    """ET dates referenced by any training_examples row (entry or exit).

    Those sessions are exempt from pruning: the routine joins each labeled
    example to the `bars` row at its timestamp to recover the feature snapshot,
    so dropping the bars silently makes the example useless. The trader's hand
    labels are the scarcest data here and can't be regenerated, unlike bars.

    Read with the service-role key, which bypasses the RLS on this table."""
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/training_examples",
        headers=_supabase_headers(),
        params={"select": "entry_at,exit_at"},
        timeout=30,
    )
    _raise_for_status(resp)
    dates = set()
    for row in resp.json():
        for key in ("entry_at", "exit_at"):
            if row.get(key):
                dates.add(pd.Timestamp(row[key]).tz_convert("America/New_York").date())
    return dates


def prune_old_bars_supabase(days=RETENTION_DAYS):
    """Drop bars older than `days`, except whole sessions pinned by a training
    example. Deletes a day at a time rather than one `ts < cutoff` sweep, since
    the protected dates punch holes in the range that a single filter can't
    express. In steady state only one day ages out per run."""
    cutoff = (pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=days))
    protected = _protected_session_dates()
    headers = _supabase_headers()

    stale_dates = set()
    offset = 0
    while True:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/bars",
            headers=headers,
            params={
                "select": "ts",
                "ts": f"lt.{cutoff.isoformat()}",
                "order": "ts.asc",
                "limit": SUPABASE_PAGE_SIZE,
                "offset": offset,
            },
            timeout=30,
        )
        _raise_for_status(resp)
        page = resp.json()
        for row in page:
            stale_dates.add(
                pd.Timestamp(row["ts"]).tz_convert("America/New_York").date()
            )
        if len(page) < SUPABASE_PAGE_SIZE:
            break
        offset += SUPABASE_PAGE_SIZE

    for d in sorted(stale_dates - protected):
        start = pd.Timestamp(d, tz="America/New_York")
        end = start + pd.Timedelta(days=1)
        resp = requests.delete(
            f"{SUPABASE_URL}/rest/v1/bars",
            headers=headers,
            params={"ts": [f"gte.{start.isoformat()}", f"lt.{end.isoformat()}"]},
            timeout=30,
        )
        _raise_for_status(resp)

    return len(stale_dates - protected), len(stale_dates & protected)


def load_all_bars_supabase():
    headers = _supabase_headers()
    all_rows = []
    offset = 0
    while True:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/bars",
            headers=headers,
            params={
                "select": "ts,open,high,low,close,volume",
                "order": "ts.asc",
                "limit": SUPABASE_PAGE_SIZE,
                "offset": offset,
            },
            timeout=30,
        )
        _raise_for_status(resp)
        page = resp.json()
        all_rows.extend(page)
        if len(page) < SUPABASE_PAGE_SIZE:
            break
        offset += SUPABASE_PAGE_SIZE

    if not all_rows:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
            index=pd.DatetimeIndex([], tz="America/New_York"),
        )
    df = pd.DataFrame(all_rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("America/New_York")
    return df.set_index("ts").sort_index()[["open", "high", "low", "close", "volume"]]


def insert_analysis_run_supabase(results):
    headers = _supabase_headers(prefer="return=minimal")
    payload = {
        "generated_at": results["generated_at"],
        "ticker": results["ticker"],
        "bars_analyzed": results["bars_analyzed"],
        "date_range": results["date_range"],
        "targets": results["targets"],
        "notes": results["notes"],
    }
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/analysis_runs", headers=headers, json=payload, timeout=30
    )
    _raise_for_status(resp)


def fetch_recent_bars(retries=3, wait=45):
    """Pull ~7 days of 1-min bars from yfinance, with retries —
    Yahoo sometimes rate-limits cloud/datacenter IPs (e.g. GitHub
    Actions runners), and a pause usually clears it."""
    import time
    import yfinance as yf

    last_err = None
    for attempt in range(retries):
        try:
            df = yf.download(
                TICKER, period="7d", interval="1m",
                auto_adjust=False, progress=False, prepost=True,
            )
            if not df.empty:
                break
            last_err = RuntimeError("yfinance returned no data")
        except Exception as e:  # noqa: BLE001
            last_err = e
        if attempt < retries - 1:
            time.sleep(wait)
    else:
        raise last_err
    # yfinance may return multi-level columns for single tickers
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(
        columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        }
    )[["open", "high", "low", "close", "volume"]]
    df.index = df.index.tz_convert("America/New_York")
    # prepost=True also pulls post-market bars, which nothing here uses —
    # drop them so the retained window only covers what the pipeline
    # actually needs: premarket through the RTH close.
    minute_of_day = df.index.hour * 60 + df.index.minute
    df = df[minute_of_day < 16 * 60]
    return df


def synthetic_bars(days=6):
    """Fake SPY-like 1-min data for smoke testing (no network)."""
    rng = np.random.default_rng(42)
    frames = []
    price = 620.0
    base = pd.Timestamp("2026-06-26 09:30", tz="America/New_York")
    for d in range(days):
        day_start = base + pd.Timedelta(days=d)
        if day_start.weekday() >= 5:
            day_start += pd.Timedelta(days=2)
        idx = pd.date_range(day_start, periods=390, freq="1min")
        rets = rng.normal(0, 0.0004, 390)
        # plant a weak, learnable effect: mild mean reversion
        for i in range(5, 390):
            rets[i] -= 0.05 * rets[i - 5:i].sum() / 5
        closes = price * np.exp(np.cumsum(rets))
        price = closes[-1]
        opens = np.concatenate([[closes[0]], closes[:-1]])
        spread = np.abs(rng.normal(0, 0.05, 390))
        df = pd.DataFrame(
            {
                "open": opens,
                "high": np.maximum(opens, closes) + spread,
                "low": np.minimum(opens, closes) - spread,
                "close": closes,
                "volume": rng.integers(50_000, 500_000, 390),
            },
            index=idx,
        )
        frames.append(df)
    return pd.concat(frames)


# ----------------------------------------------------------------------
# Features
# ----------------------------------------------------------------------
def rsi(series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def compute_features(df):
    """All features are stationary-ish (returns, spreads, ratios) —
    raw price/EMA levels are deliberately excluded as regressors."""
    out = pd.DataFrame(index=df.index)
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    ema9 = c.ewm(span=9, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()

    out["rsi14"] = rsi(c, 14)
    out["ema_spread_bps"] = (ema9 - ema21) / c * 10_000
    out["dist_ema9_bps"] = (c - ema9) / c * 10_000
    out["dist_ema21_bps"] = (c - ema21) / c * 10_000
    out["ret_1m_bps"] = c.pct_change() * 10_000
    out["ret_5m_bps"] = c.pct_change(5) * 10_000
    out["range_bps"] = (h - l) / c * 10_000
    body = (c - df["open"]).abs()
    out["body_ratio"] = (body / (h - l).replace(0, np.nan)).clip(0, 1)

    # volume vs. same-minute-of-day average (captures the U-shape)
    grp = df.groupby([df.index.date])
    out["vol_z"] = np.nan
    minute_of_day = df.index.hour * 60 + df.index.minute
    vol_mean = v.groupby(minute_of_day).transform("mean")
    vol_std = v.groupby(minute_of_day).transform("std").replace(0, np.nan)
    out["vol_z"] = (v - vol_mean) / vol_std

    out["min_since_open"] = (minute_of_day - (9 * 60 + 30)).astype(float)

    # --- reference levels: prev-day RTH H/L, today's premarket H/L, and
    # 5/15-min opening range H/L, all expressed as bps distance from close
    # so they stay stationary like the rest of the feature set. Each is
    # computed causally (no lookahead): prev-day and premarket levels are
    # fully known once RTH starts; the opening-range levels use a running
    # high/low while the window is still forming, then hold the finalized
    # value for the rest of the session.
    rth_open_min, rth_close_min = 9 * 60 + 30, 16 * 60
    day = pd.Series(df.index.date, index=df.index)
    rth_mask = (minute_of_day >= rth_open_min) & (minute_of_day < rth_close_min)
    premkt_mask = minute_of_day < rth_open_min

    by_date_high = h[rth_mask].groupby(day[rth_mask]).max()
    by_date_low = l[rth_mask].groupby(day[rth_mask]).min()
    prev_high = day.map(by_date_high.shift(1))
    prev_low = day.map(by_date_low.shift(1))
    out["dist_prev_day_high_bps"] = (c - prev_high) / c * 10_000
    out["dist_prev_day_low_bps"] = (c - prev_low) / c * 10_000

    premkt_high = day.map(h[premkt_mask].groupby(day[premkt_mask]).max())
    premkt_low = day.map(l[premkt_mask].groupby(day[premkt_mask]).min())
    out["dist_premkt_high_bps"] = (c - premkt_high) / c * 10_000
    out["dist_premkt_low_bps"] = (c - premkt_low) / c * 10_000

    rth_min_since_open = (minute_of_day - rth_open_min).where(rth_mask)
    run_high = h.where(rth_mask).groupby(day).cummax()
    run_low = l.where(rth_mask).groupby(day).cummin()
    for window, tag in ((5, "or5"), (15, "or15")):
        forming = rth_min_since_open < window
        final_high = h.where(rth_mask & forming).groupby(day).transform("max")
        final_low = l.where(rth_mask & forming).groupby(day).transform("min")
        or_high = np.where(forming, run_high, final_high)
        or_low = np.where(forming, run_low, final_low)
        out[f"dist_{tag}_high_bps"] = (c - or_high) / c * 10_000
        out[f"dist_{tag}_low_bps"] = (c - or_low) / c * 10_000

    # session VWAP, anchored at the RTH open and reset each day. Cumulative
    # through the current bar only, so it's causal like everything else; the
    # premarket bars are masked out first, which both keeps the anchor at 9:30
    # and leaves those rows NaN (cumsum skips them without breaking the total).
    typical = (h + l + c) / 3
    cum_pv = (typical * v).where(rth_mask).groupby(day).cumsum()
    cum_v = v.where(rth_mask).groupby(day).cumsum().replace(0, np.nan)
    out["dist_vwap_bps"] = (c - cum_pv / cum_v) / c * 10_000

    # only meaningful during RTH — blank these out for pre/post-market bars
    out.loc[~rth_mask, [
        "dist_prev_day_high_bps", "dist_prev_day_low_bps",
        "dist_premkt_high_bps", "dist_premkt_low_bps",
        "dist_or5_high_bps", "dist_or5_low_bps",
        "dist_or15_high_bps", "dist_or15_low_bps",
        "dist_vwap_bps",
    ]] = np.nan

    return out


def compute_targets(df):
    """Forward moves in bps. Only valid within the same session —
    rows whose horizon crosses a day boundary are dropped later."""
    out = pd.DataFrame(index=df.index)
    c, h = df["close"], df["high"]
    day = pd.Series(df.index.date, index=df.index)
    for hz in HORIZONS:
        fwd = c.shift(-hz) / c - 1
        same_day = day.shift(-hz) == day
        out[f"fwd_{hz}m_bps"] = np.where(same_day, fwd * 10_000, np.nan)
    # max favorable excursion (long side): best high within window
    fwd_max = h.rolling(MFE_HORIZON).max().shift(-MFE_HORIZON)
    same_day = day.shift(-MFE_HORIZON) == day
    out[f"mfe_{MFE_HORIZON}m_bps"] = np.where(
        same_day, (fwd_max / c - 1) * 10_000, np.nan
    )
    return out


# ----------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------
def ols(X, y):
    """OLS via lstsq. Returns coefficients (incl. intercept) and R^2 fn."""
    Xd = np.column_stack([np.ones(len(X)), X])
    coef, *_ = np.linalg.lstsq(Xd, y, rcond=None)

    def r2(Xe, ye):
        Xe = np.column_stack([np.ones(len(Xe)), Xe])
        pred = Xe @ coef
        ss_res = np.sum((ye - pred) ** 2)
        ss_tot = np.sum((ye - ye.mean()) ** 2)
        return 1 - ss_res / ss_tot if ss_tot > 0 else np.nan

    return coef, r2


def decile_table(feature, target, n=10):
    q = pd.qcut(feature, n, labels=False, duplicates="drop")
    tbl = target.groupby(q).agg(["mean", "count"])
    return [
        {"decile": int(d) + 1,
         "avg_move_bps": round(float(row["mean"]), 2),
         "n": int(row["count"])}
        for d, row in tbl.iterrows()
    ]


def run_analysis(bars):
    feats = compute_features(bars)
    targs = compute_targets(bars)
    if RTH_ONLY:
        mod = feats.index.hour * 60 + feats.index.minute
        mask = (mod >= 9 * 60 + 30) & (mod < 16 * 60)
        feats, targs = feats[mask], targs[mask]

    feature_cols = list(feats.columns)
    target_cols = list(targs.columns)
    data = pd.concat([feats, targs], axis=1).replace(
        [np.inf, -np.inf], np.nan
    )

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ticker": TICKER,
        "bars_analyzed": 0,
        "date_range": None,
        "targets": {},
        "notes": [],
    }

    for tcol in target_cols:
        # only require the target to be present — some features (e.g. the
        # premarket-based ones) are NaN until enough days have accumulated
        # post-market-hours data, and requiring every feature to be non-null
        # would silently shrink the usable window to just those days.
        # Missing feature values are zero-imputed after standardization below.
        sub = data[feature_cols + [tcol]].dropna(subset=[tcol])
        if len(sub) < MIN_ROWS:
            results["notes"].append(
                f"{tcol}: only {len(sub)} rows — skipped (min {MIN_ROWS})."
            )
            continue
        results["bars_analyzed"] = max(results["bars_analyzed"], len(sub))
        results["date_range"] = [
            str(sub.index.min().date()), str(sub.index.max().date())
        ]

        y = sub[tcol]
        # correlations — NaN (e.g. a feature that's still all-missing, like
        # premarket levels before enough days have accumulated) becomes
        # None rather than a bare NaN, since Python's json module emits
        # non-standard `NaN` tokens that JS's JSON.parse can't read.
        def safe_corr(f):
            r = sub[f].corr(y)
            return round(float(r), 4) if pd.notna(r) else None

        corrs = {f: safe_corr(f) for f in feature_cols}
        ranked = sorted(
            corrs.items(), key=lambda kv: -abs(kv[1]) if kv[1] is not None else 0
        )

        # time-based train/test split (never shuffle time series)
        split = int(len(sub) * (1 - TEST_FRACTION))
        train, test = sub.iloc[:split], sub.iloc[split:]

        # standardize on train stats so coefficients are comparable
        mu, sd = train[feature_cols].mean(), train[feature_cols].std()
        sd = sd.replace(0, np.nan)
        Xtr = ((train[feature_cols] - mu) / sd).fillna(0).values
        Xte = ((test[feature_cols] - mu) / sd).fillna(0).values

        coef, r2fn = ols(Xtr, train[tcol].values)
        r2_train = r2fn(Xtr, train[tcol].values)
        r2_test = r2fn(Xte, test[tcol].values)

        # decile tables for the 3 strongest features (skip anything with no
        # correlation at all — e.g. a feature that's still all-missing)
        deciles = {}
        top3 = [f for f, corr in ranked if corr is not None][:3]
        for fname in top3:
            deciles[fname] = decile_table(sub[fname], y)

        results["targets"][tcol] = {
            "n": len(sub),
            "correlations": ranked,
            "regression": {
                "intercept_bps": round(float(coef[0]), 3),
                "std_coefficients_bps": {
                    f: round(float(c), 3)
                    for f, c in zip(feature_cols, coef[1:])
                },
                "r2_train": round(float(r2_train), 5),
                "r2_test": round(float(r2_test), 5),
            },
            "deciles": deciles,
        }

    return results


# ----------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------
def write_report(results):
    lines = [
        f"SMAUG PIPELINE REPORT — {results['generated_at']}",
        f"Ticker: {results['ticker']}  |  Bars: {results['bars_analyzed']}"
        f"  |  Range: {results['date_range']}",
        "",
    ]
    for tcol, t in results["targets"].items():
        lines.append(f"=== TARGET: {tcol} (n={t['n']}) ===")
        reg = t["regression"]
        lines.append(
            f"  R2 train {reg['r2_train']:.4f} | R2 TEST {reg['r2_test']:.4f}"
            "   (test is what matters)"
        )
        lines.append("  Correlations (|r| ranked):")
        for f, r in t["correlations"]:
            lines.append(f"    {f:>18}: {r:+.4f}" if r is not None else f"    {f:>18}:      n/a")
        lines.append("  Std. coefficients (bps per 1-sigma of feature):")
        for f, c in reg["std_coefficients_bps"].items():
            lines.append(f"    {f:>18}: {c:+.3f}")
        for fname, tbl in t["deciles"].items():
            lines.append(f"  Deciles of {fname} -> avg {tcol}:")
            for row in tbl:
                lines.append(
                    f"    D{row['decile']:>2}: {row['avg_move_bps']:+7.2f} bps"
                    f"  (n={row['n']})"
                )
        lines.append("")
    if results["notes"]:
        lines.append("Notes:")
        lines += [f"  - {n}" for n in results["notes"]]
    text = "\n".join(lines)
    with open(REPORT_TXT, "w") as f:
        f.write(text)
    return text


def write_bars_json(bars):
    """Raw 1-min OHLCV + computed indicator features for the retained
    window, for the webapp's candlestick chart and raw-data table.
    Features are computed on the full series first (EMA/RSI need
    warmup) then filtered to RTH, same order as run_analysis()."""
    feats = compute_features(bars)
    feature_cols = list(feats.columns)
    combined = bars.join(feats)
    if RTH_ONLY:
        mod = combined.index.hour * 60 + combined.index.minute
        mask = (mod >= 9 * 60 + 30) & (mod < 16 * 60)
        combined = combined[mask]
    rows = []
    for ts, r in combined.iterrows():
        row = [ts.isoformat(), round(float(r.open), 4), round(float(r.high), 4),
               round(float(r.low), 4), round(float(r.close), 4), int(r.volume)]
        for f in feature_cols:
            v = r[f]
            row.append(None if pd.isna(v) else round(float(v), 4))
        rows.append(row)
    with open(BARS_JSON, "w") as f:
        json.dump({"ticker": TICKER, "feature_cols": feature_cols, "bars": rows}, f)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip yfinance, analyze stored data only")
    ap.add_argument("--synthetic", action="store_true",
                    help="use fake data (smoke test, no network)")
    args = ap.parse_args()

    if args.synthetic:
        n = upsert_bars_supabase(synthetic_bars())
        print(f"[synthetic] upserted {n} fake bars")
    elif not args.no_fetch:
        try:
            df = fetch_recent_bars()
            n = upsert_bars_supabase(df)
            print(f"fetched + upserted {n} bars to Supabase")
        except Exception as e:
            print(f"WARNING: fetch failed ({e}); analyzing stored data only",
                  file=sys.stderr)

    dropped, pinned = prune_old_bars_supabase()
    print(f"pruned {dropped} stale session(s); kept {pinned} pinned by training examples")

    bars = load_all_bars_supabase()
    if len(bars) < MIN_ROWS:
        print(f"Only {len(bars)} bars stored — need {MIN_ROWS}+. "
              "Run daily to accumulate.", file=sys.stderr)
        sys.exit(1)

    feats = compute_features(bars)
    n_feat = upsert_features_supabase(bars, feats)
    print(f"upserted features for {n_feat} bars")

    results = run_analysis(bars)
    insert_analysis_run_supabase(results)
    print("inserted analysis_runs row")

    with open(RESULTS_JSON, "w") as f:
        json.dump(results, f, indent=1)
    print(f"wrote local {RESULTS_JSON} (debug only, not committed)")

    write_bars_json(bars)
    print(f"wrote local {BARS_JSON} (debug only, not committed)")
    print()
    print(write_report(results))


if __name__ == "__main__":
    main()
