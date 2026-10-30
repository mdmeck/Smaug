"""Confirmatory test #2 on a second FRESH holdout (SPY 2014-04..2020-03, never examined).

PRE-REGISTERED (2026-10-29, before this data was loaded): only C1, prior-day
POC rejection candle at $0.25 bins — the one lead left after holdout #1.
PASS = pooled real-minus-placebo Good-rate > 0 with 99% CI excluding 0, AND
positive in >= 5 of the 6 Apr-Mar years. C2/C3 are still printed for
reference but are already rejected.

--- original holdout #1 header follows ---
Confirmatory test on a FRESH holdout (SPY 2020-04..2023-03, never examined).

Pre-registered before the holdout was loaded, from the near-misses of the
robustness sweep — rejection-candle ("confirm") entry, $0.25 bins:
  C1 prev-day POC
  C2 naked POC (untouched prior POCs, last 10 sessions)
  C3 developing VAH/VAL
PASS = real minus placebo Good-rate > 0 with the session-clustered 99% CI
excluding 0 on the pooled holdout, AND the same sign in all three holdout
years. 5 placebo draws per level per session (averaged) to cut placebo noise.
Score (3*Good - Bad) is reported alongside, same rule.
"""
import glob, os
import numpy as np, pandas as pd
SP = os.path.dirname(os.path.abspath(__file__)) + "/"
src_full = open(SP + "vp_full.py").read().split("# ================================================================ main")[0]
exec(src_full)
rob = open(SP + "vp_robust.py").read()
exec("def events" + rob.split("def events")[1].split("def top_hvns")[0])
N_PLACEBO = 5
BIN = 0.25; vp_lib.VP_BIN = 0.25


def period_of(d):  # noqa: F811 — holdout years
    d = pd.Timestamp(d)
    y = d.year if d.month >= 4 else d.year - 1
    return f"Y{y}"


def load_holdout():
    df = pd.concat([pd.read_pickle(f) for f in sorted(glob.glob(SP + "hf_SPY_holdout2/*.pkl"))])
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


bars = clean(load_holdout())
print("holdout sessions", bars.index.normalize().nunique(), bars.index.min().date(), bars.index.max().date(), flush=True)
feats = vp_lib.compute_all(sp, bars)
mod = bars.index.hour * 60 + bars.index.minute
rth_m = (mod >= 570) & (mod < 960)
rb, rf = bars[rth_m], feats[rth_m]
days = sorted(set(rb.index.date))
dprof = {d: day_profile(rb[rb.index.date == d]) for d in days}
rows, naked = [], []
for j, d in enumerate(days):
    s = rb[rb.index.date == d]; f = rf.loc[s.index]
    H, L, C, O = s.high.values, s.low.values, s.close.values, s.open.values
    n = len(s); per = period_of(d)
    lv = lambda col: np.r_[np.nan, (C * (1 - f[col].values / 1e4))[:-1]]
    mats = {}
    if j >= 1:
        b, p = dprof[days[j - 1]]
        mats["C1 prev POC"] = np.full((n, 1), px(b, vp_lib._vp_poc_va(p)[0]))
        if naked:
            mats["C2 naked POC"] = np.tile(np.array([x for _, x in naked]), (n, 1))
    dh, dl = lv("dist_dev_vah_bps"), lv("dist_dev_val_bps")
    dh[:30] = np.nan; dl[:30] = np.nan
    mats["C3 dev VAH/VAL"] = np.c_[dh, dl]
    for k, M in list(mats.items()):
        for r_ in range(N_PLACEBO):
            off = rng.choice([-1, 1], M.shape[1]) * rng.uniform(0.40, 1.20, M.shape[1])
            mats[f"{k} ~placebo{r_}"] = M + off
    for k, M in mats.items():
        for t, side in events(H, L, C, O, M, "confirm"):
            if t >= n: continue
            g = auto_grade(H, L, t, O[t], side)
            rows.append((d, per, k.split(" ~")[0], "~placebo" in k, g == "Good", g == "Bad"))
    naked = [(fd, x) for fd, x in naked if not (L.min() <= x <= H.max())]
    b, p = dprof[d]; naked.append((d, px(b, vp_lib._vp_poc_va(p)[0])))
    naked = [(fd, x) for fd, x in naked if days.index(fd) >= j - 9]

df = pd.DataFrame(rows, columns=["day", "period", "fam", "placebo", "good", "bad"])
df["score"] = 3 * df.good - df.bad
df.to_pickle(SP + "confirm2.pkl")
for fam in sorted(df.fam.unique()):
    r = df[df.fam == fam].copy(); r["cond"] = ~r.placebo
    for col in ("good", "score"):
        r["v"] = r[col].astype(float)
        per = {p: round(r[(r.period == p) & r.cond].v.mean() - r[(r.period == p) & ~r.cond].v.mean(), 4)
               for p in sorted(r.period.unique())}
        a, b_ = r[r.cond], r[~r.cond]
        lo, hi = boot_diff(a.day.values, a.v.values, b_.day.values, b_.v.values)
        diff = a.v.mean() - b_.v.mean()
        consistent = sum(v > 0 for v in per.values()) >= 5
        print(f"{fam:16s} {col:5s} n_real={len(a):5d} real={a.v.mean():.4f} placebo={b_.v.mean():.4f} "
              f"diff={diff:+.4f} ci99={lo:+.4f}..{hi:+.4f} by_year={per} "
              f"PASS={bool(diff > 0 and lo > 0 and consistent)}")
