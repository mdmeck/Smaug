"""Apply the pre-registered decision rule (see vp_full.py) to the saved events."""
import numpy as np, pandas as pd, os, sys
SP = os.path.dirname(os.path.abspath(__file__)) + "/"
sys.argv = [sys.argv[0]]
exec(open(SP + "vp_full.py").read().split("# ================================================================ main")[0])
lev, ctx, sig, r80 = (pd.read_pickle(SP + f"full_{n}.pkl") for n in ("lev", "ctx", "sig", "r80"))
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
out = []

# ---- A. levels vs their own placebo (bounce = good, break = brk) ----
for fam in sorted(f for f in lev.fam.unique() if not f.endswith("~placebo") and not f.startswith("ctl")):
    r = lev[lev.fam.isin([fam, fam + " ~placebo"])].copy()
    r["cond"] = r.fam == fam
    for col, tag in (("good", "bounce"), ("brk", "break")):
        r["v"] = r[col].astype(float)
        out.append({**verdict(r, f"A {fam} {tag} vs placebo", "cond", "v"), "group": "A"})
# controls vs random-entry baseline, for reference
base = ctx[ctx.src == "hf"].good.mean()

# ---- B. context filters on random entries ----
c = ctx.copy()
c["v"] = c.good.astype(float); c["vb"] = c.bad.astype(float)
tests = {
    "with VWAP side": c.vwap == c.side,
    "with dev-POC side": c.dpoc == c.side,
    "with prev-POC side": c.ppoc == c.side,
    "outside dev VA, trading away": c.dva == c.side,
    "outside prev VA, trading away": c.pva == c.side,
    "with HVN5 side": c.hvn_side == c.side,
    "near 5d HVN": c.near_hvn == True,  # noqa: E712
}
for k, m in tests.items():
    c["cond"] = m
    out.append({**verdict(c, f"B {k} (good)", "cond", "v"), "group": "B"})
    out.append({**verdict(c, f"B {k} (bad)", "cond", "vb"), "group": "B"})
# redundancy: dev-POC side *within* VWAP agreement strata
for vs in (True, False):
    cc = c[(c.vwap == c.side) == vs].copy(); cc["cond"] = cc.dpoc == cc.side
    out.append({**verdict(cc, f"B dev-POC side | VWAP side={vs} (good)", "cond", "v"), "group": "B"})
    cc["cond"] = cc.hvn_side == cc.side
    out.append({**verdict(cc, f"B HVN5 side | VWAP side={vs} (good)", "cond", "v"), "group": "B"})
c["cond"] = c.near_hvn == True  # noqa: E712
c["v2"] = c.rng10
out.append({**verdict(c, "B near 5d HVN -> 10-bar range $", "cond", "v2"), "group": "B"})

# ---- C. current indicator's signals, filtered by profile context ----
s = sig.copy(); s["v"] = s.good.astype(float); s["vb"] = s.bad.astype(float)
s["score"] = 3 * s.good - s.bad
for k, m in {
    "with dev-POC side": s.dpoc == s.side, "with prev-POC side": s.ppoc == s.side,
    "outside dev VA, trading away": s.dva == s.side, "inside dev VA": s.dva == 0,
    "outside prev VA, trading away": s.pva == s.side, "with HVN5 side": s.hvn_side == s.side,
    "near 5d HVN": s.near_hvn == True,  # noqa: E712
}.items():
    s["cond"] = m
    out.append({**verdict(s, f"C signals {k} (score 3G-B)", "cond", "score"), "group": "C"})

# ---- D. 80% rule ----
r = r80.copy(); r["cond"] = r.kind == "real"
r["v"] = r.traverse.astype(float)
out.append({**verdict(r, "D 80% rule traverse vs placebo VA", "cond", "v"), "group": "D"})
r["v"] = (3 * r.good - r.bad).astype(float)
out.append({**verdict(r, "D 80% rule trade score vs placebo VA", "cond", "v"), "group": "D"})

res = pd.DataFrame(out)
cols = ["test", "P1", "P2", "P3", "P4", "pooled", "cond_rate", "ctrl_rate", "ci99", "consistent", "ci_excl_0", "PASS"]
for g in "ABCD":
    print(f"\n===== {g} =====")
    print(res[res.group == g][cols].to_string(index=False))
res.to_pickle(SP + "verdicts.pkl")

print(f"\nrandom-entry baseline good (P1-P3): {base:.3f}")
print("signals per period:", sig.groupby("period").size().to_dict())
print("signal good/bad rate by period:\n", sig.groupby("period")[["good", "bad"]].mean().round(3))
print("80% rule real traverse rate by period:\n", r80[r80.kind == "real"].groupby("period").traverse.agg(["mean", "count"]).round(3))
for p in ["P1", "P2", "P3"]:
    x = ctx[ctx.period == p]
    print(p, "random good", round(x.good.mean(), 3), "bad", round(x.bad.mean(), 3))
