"""Volume-profile usefulness study, multi-year.

PRE-REGISTERED DECISION RULE (fixed before looking at multi-year results):
  An effect counts as USEFUL only if
   (1) its sign is the same in every yearly period (P1, P2, P3 — Finnhub data),
   (2) the pooled session-clustered bootstrap 99% CI of the difference
       (condition minus its control) excludes 0  (99% rather than 95% because
       ~40 comparisons are run), and
   (3) it survives its control: a level beats its own placebo; a context
       filter adds over the VWAP side it overlaps with.
  P4 (Jul-Oct 2026, yfinance volume, the period the current rules were fit
  on) is reported as a separate check, not used for the decision.

Data: Rrishab/OHLCV-1m (Finnhub) SPY 1-min, 2023-04..2026-03, plus the
Supabase bars for 2026-07..10. Grading matches the pipeline's auto-grader:
fill = next bar open, Good = +1.50 before -0.50, Bad = -0.50 before +1.00.
"""
import glob, json, os, sys
import numpy as np, pandas as pd

SP = os.path.dirname(os.path.abspath(__file__)) + "/"
sys.path.insert(0, os.path.join(SP, "..", "..")); sys.path.insert(0, SP)
import smaug_pipeline as sp  # noqa: E402
import vp_lib  # noqa: E402

rng = np.random.default_rng(11)
BIN = vp_lib.VP_BIN
ARM, COOLDOWN = 0.15, 15
G_T, G_S, B_S, B_T = 1.50, 0.50, 0.50, 1.00


# ---------------------------------------------------------------- data
def load_hf():
    files = sorted(glob.glob(SP + "hf_SPY/*.pkl"))
    if os.environ.get("VP_SMOKE"): files = files[:3]
    parts = [pd.read_pickle(f) for f in files]
    df = pd.concat([p for p in parts if len(p)])
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def clean(df):
    mod = df.index.hour * 60 + df.index.minute
    df = df[(mod >= 240) & (mod < 960)]
    mod = df.index.hour * 60 + df.index.minute
    rth = (mod >= 570) & (mod < 960)
    n = pd.Series(rth, index=df.index).groupby(df.index.date).sum()
    full = set(n[n >= 385].index)            # drop half days / gappy sessions
    return df[[d in full for d in df.index.date]]


def period_of(d):
    d = pd.Timestamp(d)
    if d < pd.Timestamp("2024-04-01"): return "P1"
    if d < pd.Timestamp("2025-04-01"): return "P2"
    if d < pd.Timestamp("2026-04-01"): return "P3"
    return "P4"


# ---------------------------------------------------------------- grading
def first_touch(H, L, i, entry, side, tgt, stp, touch_bar=None):
    """1 target first, -1 stop first, 0 neither by session end. Ties -> stop.
    touch_bar: limit fill on that bar; only its stop side is checked."""
    if touch_bar is not None:
        if (side > 0 and L[touch_bar] <= entry - stp) or (side < 0 and H[touch_bar] >= entry + stp):
            return -1
    h, l = H[i:], L[i:]
    if side > 0:
        s = np.flatnonzero(l <= entry - stp); t = np.flatnonzero(h >= entry + tgt)
    else:
        s = np.flatnonzero(h >= entry + stp); t = np.flatnonzero(l <= entry - tgt)
    s = s[0] if len(s) else 10**9; t = t[0] if len(t) else 10**9
    if s == t == 10**9: return 0
    return -1 if s <= t else 1


def auto_grade(H, L, i, entry, side):
    """Pipeline auto-grader: 'Bad' / 'Good' / None."""
    if first_touch(H, L, i, entry, side, B_T, B_S) == -1: return "Bad"
    if first_touch(H, L, i, entry, side, G_T, G_S) == 1: return "Good"
    return None


# ---------------------------------------------------------------- profiles
def day_profile(d):
    lo = np.floor(d["low"].values / BIN).astype(int)
    hi = np.floor(d["high"].values / BIN).astype(int)
    base = lo.min(); prof = np.zeros(hi.max() - base + 1)
    for a, b, v in zip(lo - base, hi - base, d["volume"].values):
        prof[a:b + 1] += v / (b - a + 1)
    return base, prof


def merge(profs):
    base = min(b for b, _ in profs); top = max(b + len(p) for b, p in profs)
    out = np.zeros(top - base)
    for b, p in profs: out[b - base:b - base + len(p)] += p
    return base, out


px = lambda base, k: (base + np.asarray(k) + 0.5) * BIN


def lvns(prof, smooth=5, win=10):
    k = np.ones(2 * smooth + 1) / (2 * smooth + 1); s = np.convolve(prof, k, mode="same")
    med = np.median(s[s > 0])
    return [i for i in range(win, len(s) - win)
            if s[i] == s[i - win:i + win + 1].min() and s[i] < 0.5 * med]


# ---------------------------------------------------------------- stats
def boot_diff(a_day, a_val, b_day, b_val, n=2000, ci=99):
    """Session-clustered bootstrap CI of mean(a) - mean(b). Resamples the
    union of sessions, so a and b computed on the same sessions stay paired."""
    days = np.array(sorted(set(a_day) | set(b_day)))
    ix = {d: i for i, d in enumerate(days)}
    def sums(dd, vv):
        S = np.zeros(len(days)); N = np.zeros(len(days))
        np.add.at(S, [ix[d] for d in dd], vv); np.add.at(N, [ix[d] for d in dd], 1)
        return S, N
    Sa, Na = sums(a_day, a_val); Sb, Nb = sums(b_day, b_val)
    w = rng.multinomial(len(days), np.ones(len(days)) / len(days), size=n)
    da = (w @ Sa) / np.maximum(w @ Na, 1); db = (w @ Sb) / np.maximum(w @ Nb, 1)
    lo, hi = np.percentile(da - db, [(100 - ci) / 2, 100 - (100 - ci) / 2])
    return lo, hi


def verdict(rows, label, cond_col, val_col):
    """rows: DataFrame with day, period, cond (bool), value. Per-period diffs
    + pooled 99% CI over P1-P3. Returns dict."""
    out = {"test": label}
    signs = []
    for p in ["P1", "P2", "P3", "P4"]:
        r = rows[rows.period == p]
        a, b = r[r[cond_col]], r[~r[cond_col]]
        if len(a) < 20 or len(b) < 20:
            out[p] = None; continue
        d = a[val_col].mean() - b[val_col].mean()
        out[p] = round(d, 4); out[p + "_n"] = len(a)
        if p != "P4": signs.append(np.sign(d))
    r = rows[rows.period != "P4"]
    a, b = r[r[cond_col]], r[~r[cond_col]]
    out["pooled"] = round(a[val_col].mean() - b[val_col].mean(), 4)
    out["cond_rate"] = round(a[val_col].mean(), 4); out["ctrl_rate"] = round(b[val_col].mean(), 4)
    lo, hi = boot_diff(a.day.values, a[val_col].values, b.day.values, b[val_col].values)
    out["ci99"] = f"{lo:+.4f}..{hi:+.4f}"
    out["consistent"] = len(signs) == 3 and len(set(signs)) == 1
    out["ci_excl_0"] = bool(lo > 0 or hi < 0)
    out["PASS"] = out["consistent"] and out["ci_excl_0"]
    return out


# ================================================================ main
def main():
    hf = clean(load_hf())
    sb = clean(pd.read_pickle(SP + "bars.pkl"))
    print(f"HF: {hf.index.normalize().nunique()} sessions {hf.index.min().date()}..{hf.index.max().date()}; "
          f"Supabase: {sb.index.normalize().nunique()} sessions", flush=True)

    results = []
    all_level, all_ctx, all_sig, all_80 = [], [], [], []
    for src, bars in (("hf", hf), ("sb", sb)):
        feats = vp_lib.compute_all(sp, bars)
        mod = bars.index.hour * 60 + bars.index.minute
        rth_m = (mod >= 570) & (mod < 960)
        rb, rf = bars[rth_m], feats[rth_m]
        days = sorted(set(rb.index.date))
        dprof = {d: day_profile(rb[rb.index.date == d]) for d in days}
        rules = json.load(open(SP + "rules_2026-10-23.json"))
        sig_all = sp.derive_signals(feats, rules)
        sig_by_day = {}
        for ts, direction in sig_all:
            sig_by_day.setdefault(ts.date(), []).append((ts, 1 if direction == "Long" else -1))
        print(src, "features done", flush=True)

        naked = []  # (formed_day, poc) still untouched
        for j, d in enumerate(days):
            s = rb[rb.index.date == d]; f = rf.loc[s.index]
            H, L, C, O = s.high.values, s.low.values, s.close.values, s.open.values
            n = len(s); per = period_of(d) if src == "hf" else "P4"
            lv = lambda col: C * (1 - f[col].values / 1e4)   # invert bps distance
            # ---------- level families (static: one value per session) ----------
            fam = {}
            if j >= 1:
                pdh, pdl = rb[rb.index.date == days[j - 1]].high.max(), rb[rb.index.date == days[j - 1]].low.min()
                fam["ctl prevday H/L"] = [pdh, pdl]
                fam["ctl round $1"] = [np.floor(O[0]) + k for k in range(-6, 8)]
                b, p = dprof[days[j - 1]]
                i_poc, i_vah, i_val = vp_lib._vp_poc_va(p)
                fam["prev POC"] = [px(b, i_poc)]; fam["prev VAH/VAL"] = [px(b, i_vah), px(b, i_val)]
                fam["naked POC (10d)"] = [x for _, x in naked]
            if j >= 5:
                b, p = merge([dprof[x] for x in days[j - 5:j]])
                i_poc, i_vah, i_val = vp_lib._vp_poc_va(p)
                fam["5d POC"] = [px(b, i_poc)]; fam["5d VAH/VAL"] = [px(b, i_vah), px(b, i_val)]
                fam["5d HVN"] = list(px(b, vp_lib._vp_hvns(p))); fam["5d LVN"] = list(px(b, lvns(p)))
            if j >= 20:
                b, p = merge([dprof[x] for x in days[j - 20:j]])
                fam["20d HVN"] = list(px(b, vp_lib._vp_hvns(p)))
            # per-bar level matrices
            mats = {k: np.tile(np.array(v, float), (n, 1)) for k, v in fam.items() if len(v)}
            if f["dist_dev_poc_bps"].notna().any():
                dp, dh, dl = lv("dist_dev_poc_bps"), lv("dist_dev_vah_bps"), lv("dist_dev_val_bps")
                # level as of the prior bar: the dev profile includes bar t's own
                # volume, so testing a touch at t against it would peek
                dp, dh, dl = (np.r_[np.nan, x[:-1]] for x in (dp, dh, dl))
                dp[:30] = np.nan; dh[:30] = np.nan; dl[:30] = np.nan   # skip first 30 min
                mats["dev POC"] = dp[:, None]; mats["dev VAH/VAL"] = np.c_[dh, dl]
            for k in [k for k in mats if not k.startswith("ctl")]:
                off = rng.choice([-1, 1], mats[k].shape[1]) * rng.uniform(0.40, 1.20, mats[k].shape[1])
                mats[k + " ~placebo"] = mats[k] + off
            for k, M in mats.items():
                last = {}
                for t in range(1, n - 10):
                    for c in range(M.shape[1]):
                        x = M[t, c]
                        if not np.isfinite(x) or t - last.get(c, -99) < COOLDOWN: continue
                        if C[t - 1] >= x + ARM and L[t] <= x: side = 1
                        elif C[t - 1] <= x - ARM and H[t] >= x: side = -1
                        else: continue
                        last[c] = t
                        g = first_touch(H, L, t + 1, x, side, G_T, G_S, touch_bar=t)
                        bk = first_touch(H, L, t + 1, x, -side, G_T, G_S, touch_bar=t)
                        all_level.append((src, d, per, k, side, g == 1, bk == 1))
            # update naked POCs: drop touched today, add today's, cap at 10 sessions
            naked = [(fd, x) for fd, x in naked if not (L.min() <= x <= H.max())]
            b, p = dprof[d]; naked.append((d, px(b, vp_lib._vp_poc_va(p)[0])))
            naked = [(fd, x) for fd, x in naked if days.index(fd) >= j - 9]

            # ---------- random-entry context ----------
            vw = f["dist_vwap_bps"].values; dpoc = f["dist_dev_poc_bps"].values
            dvah, dval = f["dist_dev_vah_bps"].values, f["dist_dev_val_bps"].values
            pvah, pval = f["dist_prev_vah_bps"].values, f["dist_prev_val_bps"].values
            ppoc, hv = f["dist_prev_poc_bps"].values, f["dist_hvn5_bps"].values
            for t in range(15, n - 10, 2):
                for side in (1, -1):
                    g = auto_grade(H, L, t + 1, O[t + 1], side)
                    all_ctx.append(dict(src=src, day=d, period=per, side=side,
                        good=g == "Good", bad=g == "Bad",
                        vwap=np.sign(vw[t]), dpoc=np.sign(dpoc[t]), ppoc=np.sign(ppoc[t]),
                        dva=1 if dvah[t] > 0 else -1 if dval[t] < 0 else 0,
                        pva=1 if pvah[t] > 0 else -1 if pval[t] < 0 else 0,
                        hvn_side=np.sign(hv[t]) if np.isfinite(hv[t]) else np.nan,
                        near_hvn=abs(hv[t]) * C[t] / 1e4 <= 0.25 if np.isfinite(hv[t]) else np.nan,
                        rng10=H[t + 1:t + 11].max() - L[t + 1:t + 11].min()))
            # ---------- indicator signals ----------
            for ts, side in sig_by_day.get(d, []):
                t = s.index.get_loc(ts) if ts in s.index else None
                if t is None or t + 1 >= n: continue
                g = auto_grade(H, L, t + 1, O[t + 1], side)
                all_sig.append(dict(src=src, day=d, period=per, side=side,
                    good=g == "Good", bad=g == "Bad",
                    vwap=np.sign(vw[t]), dpoc=np.sign(dpoc[t]), ppoc=np.sign(ppoc[t]),
                    dva=1 if dvah[t] > 0 else -1 if dval[t] < 0 else 0,
                    pva=1 if pvah[t] > 0 else -1 if pval[t] < 0 else 0,
                    hvn_side=np.sign(hv[t]) if np.isfinite(hv[t]) else np.nan,
                    near_hvn=abs(hv[t]) * C[t] / 1e4 <= 0.25 if np.isfinite(hv[t]) else np.nan))
            # ---------- 80% rule ----------
            if j >= 1 and np.isfinite(pvah[0]):
                vah, val = C[0] * (1 - pvah[0] / 1e4), C[0] * (1 - pval[0] / 1e4)
                for tag, (a, b_) in (("real", (vah, val)),
                                     ("placebo", tuple(np.array([vah, val]) + rng.choice([-1, 1]) * rng.uniform(0.4, 1.2)))):
                    if not (O[0] > a or O[0] < b_): continue
                    above = O[0] > a
                    br = [C[k] for k in range(29, n, 30)]   # 30-min bracket closes
                    for k in range(1, len(br)):
                        if b_ <= br[k - 1] <= a and b_ <= br[k] <= a:
                            t = 30 * k + 29
                            if t + 1 >= n: break
                            target_hit = (L[t + 1:].min() <= b_) if above else (H[t + 1:].max() >= a)
                            side = -1 if above else 1
                            g = auto_grade(H, L, t + 1, O[t + 1], side)
                            all_80.append(dict(src=src, day=d, period=per, kind=tag,
                                               traverse=bool(target_hit), good=g == "Good", bad=g == "Bad"))
                            break
        print(src, "events done", flush=True)

    lev = pd.DataFrame(all_level, columns=["src", "day", "period", "fam", "side", "good", "brk"])
    ctx = pd.DataFrame(all_ctx); sig = pd.DataFrame(all_sig); r80 = pd.DataFrame(all_80)
    for name, df in (("lev", lev), ("ctx", ctx), ("sig", sig), ("r80", r80)):
        df.to_pickle(SP + f"full_{name}.pkl")
    print("saved", len(lev), len(ctx), len(sig), len(r80))


if __name__ == "__main__":
    main()
