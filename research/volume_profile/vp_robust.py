"""Robustness sweep for the level tests: is the null an artifact of entry style,
bin size, or weak nodes? Same decision rule as vp_full.py.

Entry modes (all fill at the next bar's open and use the auto-grader):
  confirm : bar touches the level from one side and CLOSES back on that side
            (a rejection candle) -> trade the bounce
  retest  : a close breaks the level by >= ARM, then within 30 bars price comes
            back to touch it and the bar closes on the breakout side -> trade
            the breakout direction (the trader's break-and-retest)
Controls with the same modes: prev-day H/L and 15-min OR H/L (levels the
indicator already knows about), plus a placebo per family.
"""
import os, sys
import numpy as np, pandas as pd
SP = os.path.dirname(os.path.abspath(__file__)) + "/"
exec(open(SP + "vp_full.py").read().split("# ================================================================ main")[0])
RETEST_WIN = 30


def events(H, L, C, O, M, mode):
    """Yield (t_entry_bar, side) for every level column in M (n x k)."""
    n = len(C)
    for c in range(M.shape[1]):
        x = M[:, c]
        last = -99
        if mode == "confirm":
            for t in range(1, n - 10):
                lv = x[t]
                if not np.isfinite(lv) or t - last < COOLDOWN: continue
                if C[t - 1] >= lv + ARM and L[t] <= lv and C[t] > lv: side = 1
                elif C[t - 1] <= lv - ARM and H[t] >= lv and C[t] < lv: side = -1
                else: continue
                last = t; yield t + 1, side
        else:  # retest
            t = 1
            while t < n - 10:
                lv = x[t]
                if not np.isfinite(lv) or t - last < COOLDOWN or not np.isfinite(x[t - 1]):
                    t += 1; continue
                if C[t - 1] <= lv and C[t] >= lv + ARM: side = 1
                elif C[t - 1] >= lv and C[t] <= lv - ARM: side = -1
                else:
                    t += 1; continue
                for k in range(t + 1, min(n - 10, t + RETEST_WIN)):
                    lk = x[k]
                    if not np.isfinite(lk): break
                    if side > 0 and L[k] <= lk:
                        if C[k] > lk: last = k; yield k + 1, side
                        break
                    if side < 0 and H[k] >= lk:
                        if C[k] < lk: last = k; yield k + 1, side
                        break
                t += 1


def top_hvns(prof, k=3):
    idx = vp_lib._vp_hvns(prof)
    kern = np.ones(2 * vp_lib.VP_NODE_SMOOTH + 1) / (2 * vp_lib.VP_NODE_SMOOTH + 1)
    s = np.convolve(prof, kern, mode="same")
    return sorted(idx, key=lambda i: -s[i])[:k]


def run(bars, src, bin_size):
    global BIN
    BIN = bin_size; vp_lib.VP_BIN = bin_size
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
        n = len(s); per = period_of(d) if src == "hf" else "P4"
        lv = lambda col: np.r_[np.nan, (C * (1 - f[col].values / 1e4))[:-1]]
        fam = {}
        if j >= 1:
            prev = rb[rb.index.date == days[j - 1]]
            fam["ctl prevday H/L"] = [prev.high.max(), prev.low.min()]
            b, p = dprof[days[j - 1]]
            i_poc, i_vah, i_val = vp_lib._vp_poc_va(p)
            fam["prev POC"] = [px(b, i_poc)]; fam["prev VAH/VAL"] = [px(b, i_vah), px(b, i_val)]
            fam["naked POC"] = [x for _, x in naked]
        if j >= 5:
            b, p = merge([dprof[x] for x in days[j - 5:j]])
            fam["5d top3 HVN"] = list(px(b, top_hvns(p)))
        mats = {k: np.tile(np.array(v, float), (n, 1)) for k, v in fam.items() if len(v)}
        oh, ol = lv("dist_or15_high_bps"), lv("dist_or15_low_bps")
        oh[:16] = np.nan; ol[:16] = np.nan          # only once the OR is final
        mats["ctl OR15 H/L"] = np.c_[oh, ol]
        if f["dist_dev_poc_bps"].notna().any():
            dp, dh, dl = lv("dist_dev_poc_bps"), lv("dist_dev_vah_bps"), lv("dist_dev_val_bps")
            for a in (dp, dh, dl): a[:30] = np.nan
            mats["dev POC"] = dp[:, None]; mats["dev VAH/VAL"] = np.c_[dh, dl]
        for k in list(mats):
            off = rng.choice([-1, 1], mats[k].shape[1]) * rng.uniform(0.40, 1.20, mats[k].shape[1])
            mats[k + " ~placebo"] = mats[k] + off
        for k, M in mats.items():
            for mode in ("confirm", "retest"):
                for t, side in events(H, L, C, O, M, mode):
                    if t >= n: continue
                    g = auto_grade(H, L, t, O[t], side)
                    rows.append((src, bin_size, d, per, k, mode, g == "Good", g == "Bad"))
        naked = [(fd, x) for fd, x in naked if not (L.min() <= x <= H.max())]
        b, p = dprof[d]; naked.append((d, px(b, vp_lib._vp_poc_va(p)[0])))
        naked = [(fd, x) for fd, x in naked if days.index(fd) >= j - 9]
    return rows


if __name__ == "__main__":
    hf = clean(load_hf())
    sb = clean(pd.read_pickle(SP + "bars.pkl"))
    rows = []
    for bsz in (0.05, 0.25):
        for src, bars in (("hf", hf), ("sb", sb)):
            rows += run(bars, src, bsz)
            print("done", bsz, src, len(rows), flush=True)
    df = pd.DataFrame(rows, columns=["src", "bin", "day", "period", "fam", "mode", "good", "bad"])
    df.to_pickle(SP + "robust.pkl")
    df["score"] = 3 * df.good - df.bad
    out = []
    for (bsz, mode), g in df.groupby(["bin", "mode"]):
        for fam in sorted(f for f in g.fam.unique() if not f.endswith("~placebo")):
            r = g[g.fam.isin([fam, fam + " ~placebo"])].copy(); r["cond"] = r.fam == fam
            for col in ("good", "score"):
                r["v"] = r[col].astype(float)
                v = verdict(r, f"{fam} [{mode}, bin {bsz}] {col}", "cond", "v")
                v["n"] = int(r.cond.sum()); out.append(v)
    res = pd.DataFrame(out)
    pd.set_option("display.width", 250)
    print(res[["test", "n", "P1", "P2", "P3", "P4", "cond_rate", "ctrl_rate", "ci99", "consistent", "PASS"]].to_string(index=False))
    res.to_pickle(SP + "robust_verdicts.pkl")
