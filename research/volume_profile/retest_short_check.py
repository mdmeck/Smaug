"""Independent replication of the retest_short decade verdict on Finnhub data.
Uses the pipeline's compute_features() unchanged (fixed-dollar thresholds, as defined),
the auto-grader (next-open fill, Good +1.50/-0.50, Bad -0.50/+1.00), and a
random-short baseline graded identically on the same sessions.
Needs hf_SPY_holdout2/, hf_SPY_holdout/, hf_SPY/ and bars.pkl (see README)."""
import glob, os, sys
import numpy as np, pandas as pd
SP = os.path.dirname(os.path.abspath(__file__)) + "/"
sys.path.insert(0, os.path.abspath(SP + "../.."))
import smaug_pipeline as sp  # noqa: E402
exec(open(SP + "vp_full.py").read().split("# ================================================================ main")[0]
     .replace("import smaug_pipeline as sp  # noqa: E402", "").replace("BIN = sp.VP_BIN", "BIN = 0.05"))


def load(dirname):
    df = pd.concat([pd.read_pickle(f) for f in sorted(glob.glob(SP + dirname + "/*.pkl"))])
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    return clean(df.drop_duplicates("ts").set_index("ts").sort_index()[["open", "high", "low", "close", "volume"]].astype(float))


sets = [(d, load(d)) for d in ("hf_SPY_holdout2", "hf_SPY_holdout", "hf_SPY")]
sets.append(("supabase", clean(pd.read_pickle(SP + "bars.pkl"))))
rows = []
for name, bars in sets:
    f = sp.compute_features(bars)
    mod = bars.index.hour * 60 + bars.index.minute
    rth = (mod >= 570) & (mod < 960)
    rb, rs = bars[rth], f["retest_short"][rth]
    for d, s in rb.groupby(rb.index.date):
        H, L, O = s.high.values, s.low.values, s.open.values
        sig = rs.loc[s.index].values
        n = len(s)
        for t in range(n - 1):
            is_sig = sig[t] == 1 and (t == 0 or sig[t - 1] != 1)   # edge-triggered, like markers.pine
            if not is_sig and t % 3:            # random baseline: every 3rd bar
                continue
            g = auto_grade(H, L, t + 1, O[t + 1], -1)
            rows.append((name, d, pd.Timestamp(d).year, is_sig, g == "Good", g == "Bad"))
    print(name, "done", flush=True)

r = pd.DataFrame(rows, columns=["set", "day", "year", "signal", "good", "bad"])
r.to_pickle(SP + "retest_check.pkl")


def summary(label, r):
    sig, base = r[r.signal], r[~r.signal]
    lo, hi = boot_diff(sig.day.values, sig.good.astype(float).values, base.day.values, base.good.astype(float).values, ci=95)
    score = lambda x: 3 * x.good.mean() - x.bad.mean()
    print(f"{label}: signals={len(sig)} ({len(sig) / r.day.nunique():.2f}/session) good {sig.good.mean():.3f} vs random {base.good.mean():.3f} "
          f"diff {sig.good.mean() - base.good.mean():+.3f} 95ci {lo:+.3f}..{hi:+.3f} | score 3G-B {score(sig):+.3f} vs {score(base):+.3f}")


summary("ALL", r)
summary("2021-2026", r[r.year >= 2021])
summary("2021-2025 (excl. tuning year)", r[(r.year >= 2021) & (r.year <= 2025)])
t = r.groupby(["year", "signal"]).good.mean().unstack()
t["n_sig"] = r[r.signal].groupby("year").size()
t["diff"] = t[True] - t[False]
print(t.round(3).rename(columns={True: "signal", False: "random"}).to_string())
print("years signal > random:", int((t["diff"] > 0).sum()), "of", len(t))
