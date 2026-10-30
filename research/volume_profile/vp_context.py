"""Context test: condition random entries (every 2nd bar, both sides, graded
+1.50 before -0.50) on profile context. Good-rate by bucket, with a
first-75% / last-25% time split so a one-off cluster can't pass as an effect."""
import os, numpy as np, pandas as pd
SP = os.path.dirname(os.path.abspath(__file__)) + "/"
src = open(SP + "vp_test.py").read()
# reuse profile helpers + grade() without running the event study
exec(src.split("day_prof = {")[0])
exec("def grade" + src.split("def grade")[1].split("def touches")[0])

day_prof = {d: day_profile(rth[rth.day == d]) for d in days}
split_day = days[int(len(days) * 0.75)]

rows = []
for j, d in enumerate(days):
    if j < 5:
        continue
    s = rth[rth.day == d]
    H, L, C, O, V = s.high.values, s.low.values, s.close.values, s.open.values, s.volume.values
    b, p = day_prof[days[j - 1]]
    ppoc, pvah, pval = poc_va(b, p)
    b5, p5 = merge([day_prof[x] for x in days[j - 5:j]])
    lvn5 = np.array(nodes(b5, p5, kind="lvn") or [np.nan])
    hvn5 = np.array(nodes(b5, p5, kind="hvn") or [np.nan])
    open_loc = "above_pVA" if O[0] > pvah else "below_pVA" if O[0] < pval else "inside_pVA"
    # developing profile through bar t-1
    lo = np.floor(L / BIN).astype(int); hi = np.floor(H / BIN).astype(int)
    base = lo.min() - 400
    prof = np.zeros(hi.max() - base + 400)
    tp = (H + L + C) / 3
    cpv = cv = 0.0
    for t in range(len(s)):
        if t >= 15 and t < len(s) - 10 and t % 2 == 0:
            dpoc, dvah, dval = poc_va(base, prof)
            vwap = cpv / cv
            c = C[t]
            for side in (1, -1):
                g = grade(H, L, t + 1, c, side, 1.50, 0.50)
                rows.append(dict(
                    day=d, test=d >= split_day, side=side, good=g == 1, bad=g == -1,
                    vs_dpoc=np.sign(c - dpoc), vs_vwap=np.sign(c - vwap),
                    dpoc_bps=(c - dpoc) / c * 1e4,
                    dVA="above" if c > dvah else "below" if c < dval else "inside",
                    pVA="above" if c > pvah else "below" if c < pval else "inside",
                    open_loc=open_loc,
                    near_lvn=np.nanmin(np.abs(lvn5 - c)) <= 0.25,
                    near_hvn=np.nanmin(np.abs(hvn5 - c)) <= 0.25,
                    rng10=(H[t+1:t+11].max() - L[t+1:t+11].min()),
                ))
        a, bb = lo[t] - base, hi[t] - base
        prof[a:bb + 1] += V[t] / (bb - a + 1)
        cpv += tp[t] * V[t]; cv += V[t]

r = pd.DataFrame(rows)
r["dir"] = np.where(r.side > 0, "long", "short")
pd.set_option("display.width", 200)
print(f"n={len(r)} entries, {r.day.nunique()} sessions; test split from {split_day}")
print(f"baseline good: train {r[~r.test].good.mean():.3f}  test {r[r.test].good.mean():.3f}\n")


def tab(col):
    t = r.groupby([col, "dir", "test"]).good.agg(["mean", "count"]).unstack("test")
    t.columns = [f"{a}_{'test' if b else 'train'}" for a, b in t.columns]
    print(f"--- good rate by {col} x direction ---")
    print(t.round(3).to_string(), "\n")

for c in ["vs_dpoc", "vs_vwap", "dVA", "pVA", "open_loc", "near_lvn", "near_hvn"]:
    tab(c)

# "with the auction": long above dev POC / short below, vs against
r["with_dpoc"] = r.vs_dpoc == r.side
r["with_vwap"] = r.vs_vwap == r.side
print("agreement of dev-POC side with VWAP side:", round((r.vs_dpoc == r.vs_vwap).mean(), 3))
for c in ["with_dpoc", "with_vwap"]:
    print(r.groupby([c, "test"]).good.mean().unstack().round(3), "\n")
# does dev POC add anything once VWAP side is known?
print(r.groupby(["with_vwap", "with_dpoc", "test"]).good.agg(["mean", "count"]).unstack().round(3), "\n")

print("--- LVN speed: 10-bar forward range ($) ---")
print(r[r.side > 0].groupby("near_lvn").rng10.describe()[["count", "mean", "50%"]].round(3))
print(r[r.side > 0].groupby("near_hvn").rng10.describe()[["count", "mean", "50%"]].round(3))

# distance-to-dev-POC deciles, sided (positive = trade direction away from POC)
r["sided_dpoc"] = r.dpoc_bps * r.side
r["dec"] = pd.qcut(r.sided_dpoc, 10, labels=False)
print("\n--- good rate by decile of sided dist to dev POC (low = trading toward POC) ---")
print(r.groupby(["dec", "test"]).good.mean().unstack().round(3).T.to_string())
