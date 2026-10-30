"""Decade-scale out-of-sample test for the Smaug indicator.

Runs in GitHub Actions (indicator-research.yml), which holds the Alpaca keys.
Pulls SPY 1-minute SIP bars through lab.data, computes the pipeline's own
compute_features() on them, grades every RTH bar as a hypothetical long and
short with the AUTO_* 1:3 rules, adds per-level break-and-retest flags, and
writes:

  out/features.pkl.gz  derived per-bar frame: stationary features, grades and
                       flags only, never prices — the repo is public and the
                       artifact with it, and raw SIP bars aren't ours to
                       redistribute
  out/summary.json     per-year hit rates for the shipped signals

Dollar thresholds are scaled by price: the AUTO_* stops and the retest margins
were set with SPY near today's level, and $0.50 on a $200 SPY is a 2.5x larger
move. Each session's thresholds are multiplied by (session open / REF), where
REF is the median close of the last 20 sessions, so a 2016 trade is graded on
the same bps scale as a 2026 one. The `*_raw` columns keep the literal
dollars for the recent sessions where the two coincide.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import smaug_pipeline as sp  # noqa: E402
from lab import data  # noqa: E402

LEVELS = ["or15_high", "or15_low", "prev_day_high", "prev_day_low", "vwap"]
DISCOVERY_START = "2026-07-20"   # retest_short was found on 07-20..10-23
BIG = 10**9


def grade_session(o, h, l, sign, k):
    """Grade every bar of one RTH session; k scales the dollar thresholds.
    Fill = next bar's open, path to the close, a bar spanning both levels
    resolves to the stop. Last bar is ungradable (NaN)."""
    n = len(o)
    res = np.full(n, np.nan)
    if n < 2:
        return res
    entry = o[1:][:, None]
    valid = np.arange(n)[None, :] >= np.arange(1, n)[:, None]
    worst = (entry - l[None, :]) if sign > 0 else (h[None, :] - entry)
    best = (h[None, :] - entry) if sign > 0 else (entry - l[None, :])

    def first(m):
        m = m & valid
        return np.where(m.any(1), m.argmax(1), BIG)

    sb, tb = first(worst >= sp.AUTO_BAD_STOP * k), first(best >= sp.AUTO_BAD_TARGET * k)
    sg, tg = first(worst >= sp.AUTO_GOOD_STOP * k), first(best >= sp.AUTO_GOOD_TARGET * k)
    bad = (sb != BIG) & (sb <= tb)
    good = (tg != BIG) & (tg < sg)
    res[:-1] = np.where(bad, -1.0, np.where(good, 1.0, 0.0))
    return res


def retest(c, h, l, level, side, k):
    """Break-and-retest flags for one session, the level captured at the
    break (matters for VWAP, which moves; identical to the pipeline's
    _retest_short for the fixed levels). side=-1 bearish, +1 bullish."""
    m, d = sp.RETEST_MARGIN * k, sp.RETEST_DIST * k
    n = len(c)
    out = np.zeros(n, bool)
    brk = []
    for t in range(1, n):
        lv, lp = level[t], level[t - 1]
        if np.isfinite(lv) and np.isfinite(lp):
            if side < 0 and c[t] <= lv - m and c[t - 1] > lp - m:
                brk.append((t, lv))
            if side > 0 and c[t] >= lv + m and c[t - 1] < lp + m:
                brk.append((t, lv))
        brk = [(j, v) for j, v in brk if t - j <= sp.RETEST_WINDOW]
        for j, v in brk:
            if j == t:
                continue
            seg = slice(j + 1, t + 1)
            if side < 0:
                if (c[seg] > v + m).any():
                    continue
                touch = np.flatnonzero(h[seg] >= v - d)
                ok = c[t] <= v - m
            else:
                if (c[seg] < v - m).any():
                    continue
                touch = np.flatnonzero(l[seg] <= v + d)
                ok = c[t] >= v + m
            if touch.size and (t - (j + 1 + touch[-1])) < sp.RETEST_RECENT and ok:
                out[t] = True
                break
    return out


def build(start, end):
    bars = data.stock_minutes("SPY", start, end, rth=False)
    bars = bars[["open", "high", "low", "close", "volume"]].astype(float)
    print(f"bars {len(bars)} {bars.index[0]} .. {bars.index[-1]}", flush=True)
    feats = sp.compute_features(bars)
    mod = bars.index.hour * 60 + bars.index.minute
    rth = (mod >= 570) & (mod < 960)
    bars, feats = bars[rth], feats[rth].copy()
    day = bars.index.date
    sess_open = bars.groupby(day)["open"].transform("first").to_numpy()
    last_days = sorted(set(day))[-20:]
    ref = float(bars[np.isin(day, last_days)]["close"].median())
    scale = sess_open / ref

    o, h, l, c = (bars[x].to_numpy() for x in ("open", "high", "low", "close"))
    cols = {f"g_{s}{suf}": np.full(len(bars), np.nan)
            for s in ("long", "short") for suf in ("", "_raw", "_x3")}
    for lv in LEVELS:
        for s in ("long", "short"):
            cols[f"rt_{s}_{lv}"] = np.zeros(len(bars))
    # level prices, backed out of the pipeline's own bps distances
    back = lambda f: c * (1 - feats[f].to_numpy() / 10_000)
    L = {"prev_day_high": back("dist_prev_day_high_bps"),
         "prev_day_low": back("dist_prev_day_low_bps"),
         "vwap": back("dist_vwap_bps")}
    formed = feats["min_since_open"].to_numpy() >= 15
    L["or15_high"] = np.where(formed, back("dist_or15_high_bps"), np.nan)
    L["or15_low"] = np.where(formed, back("dist_or15_low_bps"), np.nan)

    pos_by_day = pd.Series(np.arange(len(bars))).groupby(np.asarray(day)).indices
    for i, (d, pos) in enumerate(sorted(pos_by_day.items())):
        k = scale[pos[0]]
        for s, sign in (("long", 1), ("short", -1)):
            cols[f"g_{s}"][pos] = grade_session(o[pos], h[pos], l[pos], sign, k)
            cols[f"g_{s}_raw"][pos] = grade_session(o[pos], h[pos], l[pos], sign, 1.0)
            # same 1:3, 3x wider: $1.50 stop / $4.50 target at today's price
            cols[f"g_{s}_x3"][pos] = grade_session(o[pos], h[pos], l[pos], sign, 3 * k)
            for lv in LEVELS:
                cols[f"rt_{s}_{lv}"][pos] = retest(c[pos], h[pos], l[pos], L[lv][pos], sign, k)
        if i % 250 == 0:
            print(f"  graded {i} sessions ({d})", flush=True)
    for name, v in cols.items():
        feats[name] = v
    feats["scale"] = scale
    feats["atr_bps"] = (sp.atr(bars) / bars["close"] * 10_000).to_numpy()
    feats["ret_30m_bps"] = (bars["close"].groupby(day).pct_change(30) * 10_000).to_numpy()
    prev_close = bars.groupby(day)["close"].last().shift(1)
    feats["gap_bps"] = (sess_open / pd.Series(day).map(prev_close).to_numpy() - 1) * 10_000
    feats["retest_short_scaled"] = ((feats["rt_short_or15_high"] > 0)
                                    | (feats["rt_short_prev_day_low"] > 0)).astype(float)
    feats["day"] = pd.to_datetime(pd.Series(day, index=feats.index))
    return feats, ref


def edge(on, day):
    """Edge-trigger within a session, like markers.pine."""
    same = np.r_[False, day[1:] == day[:-1]]
    return on & ~(np.r_[False, on[:-1]] & same)


def score(f, on, side, grade_col=None):
    g = f[grade_col or f"g_{side}"].to_numpy()
    sig = edge(on, f["day"].to_numpy()) & np.isfinite(g)
    G, B = int((g[sig] == 1).sum()), int((g[sig] == -1).sum())
    return {"n": int(sig.sum()), "good": G, "bad": B,
            "hit": round(G / (G + B), 4) if G + B else None}


def base(f, side, grade_col=None):
    g = f[grade_col or f"g_{side}"].to_numpy()
    G, B = (g == 1).sum(), (g == -1).sum()
    return round(float(G / (G + B)), 4) if G + B else None


def summarize(f, ref):
    year = f["day"].dt.year.to_numpy()
    periods = {str(y): year == y for y in sorted(set(year))}
    periods["pre_discovery"] = (f["day"] < DISCOVERY_START).to_numpy()
    periods["discovery_and_after"] = ~periods["pre_discovery"]
    sigs = {"retest_short_scaled": ("short", f["retest_short_scaled"].to_numpy() > 0, None),
            "retest_short_pipeline_raw": ("short", f["retest_short"].to_numpy() > 0, "g_short_raw")}
    for lv in LEVELS:
        for s in ("long", "short"):
            sigs[f"rt_{s}_{lv}"] = (s, f[f"rt_{s}_{lv}"].to_numpy() > 0, None)
    long_rule = ((f["ema_spread_bps"] > 4.5) & (f["rsi14"] <= 73) & (f["dist_vwap_bps"] <= 20)).to_numpy()
    sigs["routine_long_2026_10_29"] = ("long", long_rule, None)
    out = {"ref_price": ref, "sessions": int(f["day"].nunique()), "periods": {}}
    for pname, sel in periods.items():
        sub = f[sel]
        row = {"sessions": int(sub["day"].nunique()),
               "base_long": base(sub, "long"), "base_short": base(sub, "short"),
               "base_short_raw": base(sub, "short", "g_short_raw"),
               "base_long_x3": base(sub, "long", "g_long_x3"),
               "base_short_x3": base(sub, "short", "g_short_x3")}
        for name, (side, on, gc) in sigs.items():
            row[name] = score(sub, on[sel], side, gc)
            if gc is None:
                row[name + "_x3"] = score(sub, on[sel], side, f"g_{side}_x3")
        out["periods"][pname] = row
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    end = a.end or (pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=1)).date()
    os.makedirs(a.out, exist_ok=True)
    f, ref = build(a.start, end)
    keep = [c for c in f.columns if f[c].dtype != object]
    f32 = f[keep].astype({c: "float32" for c in keep if c != "day"})
    f32.to_pickle(f"{a.out}/features.pkl.gz", compression="gzip")
    s = summarize(f, ref)
    json.dump(s, open(f"{a.out}/summary.json", "w"), indent=1)
    for p in ("pre_discovery", "discovery_and_after"):
        r = s["periods"][p]
        print(p, r["sessions"], "base_short", r["base_short"], "retest_short", r["retest_short_scaled"])


if __name__ == "__main__":
    main()
