"""
Smaug daily regression pipeline for SPY 1-minute data.

Run once per day after the close (scheduled). Each run:
  1. Pulls recent SPY 1-min bars from yfinance and upserts into SQLite
     (yfinance serves ~7-8 days of 1-min history, so daily runs never miss).
  2. Computes indicator features per bar.
  3. Builds forward-move targets at several horizons.
  4. Runs correlation, OLS regression (time-based train/test split),
     and decile analysis.
  5. Writes smaug_results.json (paste into the Smaug Technicals tab)
     and a human-readable smaug_report.txt.

Usage:
  python smaug_pipeline.py              # normal daily run
  python smaug_pipeline.py --no-fetch   # re-run analysis on stored data only
  python smaug_pipeline.py --synthetic  # smoke-test with fake data
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime

import numpy as np
import pandas as pd

DB_PATH = "spy_1m.db"
RESULTS_JSON = "smaug_results.json"
REPORT_TXT = "smaug_report.txt"
BARS_JSON = "smaug_bars.json"
TICKER = "SPY"
HORIZONS = [5, 10, 15]          # minutes ahead for forward return targets
MFE_HORIZON = 10                # minutes for max-favorable-excursion target
RTH_ONLY = True                 # keep regular trading hours only (9:30-16:00 ET)
TEST_FRACTION = 0.25            # most recent 25% of data held out for testing
MIN_ROWS = 500                  # refuse to run analysis on less than this
RETENTION_DAYS = 30             # prune bars older than this so the DB stays bounded


# ----------------------------------------------------------------------
# Data layer
# ----------------------------------------------------------------------
def init_db(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bars (
            ts TEXT PRIMARY KEY,          -- ISO timestamp, ET
            open REAL, high REAL, low REAL, close REAL, volume INTEGER
        )"""
    )
    conn.commit()


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
                auto_adjust=False, progress=False,
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
    return df


def upsert_bars(conn, df):
    rows = [
        (ts.isoformat(), float(r.open), float(r.high), float(r.low),
         float(r.close), int(r.volume))
        for ts, r in df.iterrows()
        if not (np.isnan(r.open) or np.isnan(r.close))
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?)", rows
    )
    conn.commit()
    return len(rows)


def prune_old_bars(conn, days=RETENTION_DAYS):
    """Drop bars older than `days` so the DB (and its git history) stay bounded."""
    cutoff = (pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=days))
    conn.execute("DELETE FROM bars WHERE ts < ?", (cutoff.isoformat(),))
    conn.commit()


def load_all_bars(conn):
    df = pd.read_sql("SELECT * FROM bars ORDER BY ts", conn)
    df["ts"] = pd.to_datetime(df["ts"], utc=False)
    # normalize any mixed tz representations
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize("America/New_York",
                                           ambiguous="NaT",
                                           nonexistent="NaT")
    df = df.dropna(subset=["ts"]).set_index("ts")
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
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ticker": TICKER,
        "bars_analyzed": 0,
        "date_range": None,
        "targets": {},
        "notes": [],
    }

    for tcol in target_cols:
        sub = data[feature_cols + [tcol]].dropna()
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
        # correlations
        corrs = {
            f: round(float(sub[f].corr(y)), 4) for f in feature_cols
        }
        ranked = sorted(corrs.items(), key=lambda kv: -abs(kv[1]))

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

        # decile tables for the 3 strongest features
        deciles = {}
        for fname, _ in ranked[:3]:
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
            lines.append(f"    {f:>18}: {r:+.4f}")
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
    """Raw 1-min OHLCV for the retained window, for the webapp's candlestick
    chart. Compact [ts, o, h, l, c, v] rows to keep the file small."""
    if RTH_ONLY:
        mod = bars.index.hour * 60 + bars.index.minute
        mask = (mod >= 9 * 60 + 30) & (mod < 16 * 60)
        bars = bars[mask]
    rows = [
        [ts.isoformat(), round(float(r.open), 4), round(float(r.high), 4),
         round(float(r.low), 4), round(float(r.close), 4), int(r.volume)]
        for ts, r in bars.iterrows()
    ]
    with open(BARS_JSON, "w") as f:
        json.dump({"ticker": TICKER, "bars": rows}, f)


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

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.synthetic:
        n = upsert_bars(conn, synthetic_bars())
        print(f"[synthetic] upserted {n} fake bars")
    elif not args.no_fetch:
        try:
            df = fetch_recent_bars()
            n = upsert_bars(conn, df)
            print(f"fetched + upserted {n} bars from yfinance")
        except Exception as e:
            print(f"WARNING: fetch failed ({e}); analyzing stored data only",
                  file=sys.stderr)

    prune_old_bars(conn)

    bars = load_all_bars(conn)
    if len(bars) < MIN_ROWS:
        print(f"Only {len(bars)} bars stored — need {MIN_ROWS}+. "
              "Run daily to accumulate.", file=sys.stderr)
        sys.exit(1)

    results = run_analysis(bars)
    with open(RESULTS_JSON, "w") as f:
        json.dump(results, f, indent=1)
    print(f"wrote {RESULTS_JSON}")

    write_bars_json(bars)
    print(f"wrote {BARS_JSON}")
    print()
    print(write_report(results))


if __name__ == "__main__":
    main()
