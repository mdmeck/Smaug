"""Event study: does price react at volume-profile levels more than at placebo levels?

For each level family, a "touch" is a bar that reaches the level from one side
(prev close >= ARM away on that side). Approach from above = support test -> long
bounce; from below = resistance test -> short bounce. Entry = level price (limit).
Graded in SPY dollars, same scale as the pipeline's auto-grader:
  good  : +1.50 before -0.50   (1:3, the trader's Good)
  hold  : +0.50 before -0.50   (did the level hold at all)
  break : continuation -0.50 vs ... i.e. 1 - hold (reported implicitly)
Touch bar itself: if it extends STOP past the level -> stop; target never
credited on the touch bar (conservative, intrabar order unknown).
Placebos: each real level shifted by a random +-[0.40,1.20] so they live in the
same price region and volatility regime.
"""
import os, numpy as np, pandas as pd, sys
rng = np.random.default_rng(7)
SP = os.path.dirname(os.path.abspath(__file__)) + "/"
bars = pd.read_pickle(SP + "bars.pkl")
mod = bars.index.hour * 60 + bars.index.minute
rth = bars[(mod >= 570) & (mod < 960)].copy()
rth["day"] = rth.index.date
days = sorted(rth["day"].unique())
BIN = 0.05
ARM = 0.15
COOLDOWN = 15


def day_profile(d):
    """Volume spread uniformly over each bar's [low, high], binned at BIN."""
    lo = np.floor(d["low"].values / BIN).astype(int)
    hi = np.floor(d["high"].values / BIN).astype(int)
    base = lo.min()
    prof = np.zeros(hi.max() - base + 1)
    for a, b, v in zip(lo - base, hi - base, d["volume"].values):
        prof[a:b + 1] += v / (b - a + 1)
    return base, prof


def merge(profs):
    base = min(b for b, _ in profs)
    top = max(b + len(p) for b, p in profs)
    out = np.zeros(top - base)
    for b, p in profs:
        out[b - base:b - base + len(p)] += p
    return base, out


def poc_va(base, prof, frac=0.70):
    i = int(prof.argmax())
    lo = hi = i
    tot, acc = prof.sum(), prof[i]
    while acc < frac * tot:
        up = prof[hi + 1] if hi + 1 < len(prof) else -1
        dn = prof[lo - 1] if lo > 0 else -1
        if up >= dn:
            hi += 1; acc += up
        else:
            lo -= 1; acc += dn
    px = lambda k: (base + k + 0.5) * BIN
    return px(i), px(hi), px(lo)


def nodes(base, prof, smooth=5, win=10, kind="hvn"):
    """Local maxima (HVN) / minima (LVN) of a smoothed profile, +-win bins."""
    k = np.ones(2 * smooth + 1) / (2 * smooth + 1)
    s = np.convolve(prof, k, mode="same")
    out = []
    for i in range(win, len(s) - win):
        w = s[i - win:i + win + 1]
        if kind == "hvn" and s[i] == w.max() and s[i] > np.median(s):
            out.append((base + i + 0.5) * BIN)
        if kind == "lvn" and s[i] == w.min() and s[i] < 0.5 * np.median(s[s > 0]):
            out.append((base + i + 0.5) * BIN)
    return out


day_prof = {d: day_profile(rth[rth.day == d]) for d in days}

# ---- static levels per session (known at the open) ----
levels = {}  # family -> {day: [levels]}
def put(fam, d, lv):
    levels.setdefault(fam, {})[d] = list(lv)

for j, d in enumerate(days):
    if j < 1:
        continue
    pd_ = rth[rth.day == days[j - 1]]
    put("prevday_HL (control)", d, [pd_.high.max(), pd_.low.min()])
    o = rth[rth.day == d].open.iloc[0]
    put("round_$1 (control)", d, [np.floor(o) + k for k in range(-6, 8)])
    b, p = day_prof[days[j - 1]]
    poc, vah, val = poc_va(b, p)
    put("prev POC", d, [poc]); put("prev VAH/VAL", d, [vah, val])
    if j >= 5:
        b, p = merge([day_prof[x] for x in days[j - 5:j]])
        poc, vah, val = poc_va(b, p)
        put("5d POC", d, [poc]); put("5d VAH/VAL", d, [vah, val])
        put("5d HVN", d, nodes(b, p, kind="hvn"))
        put("5d LVN", d, nodes(b, p, kind="lvn"))
    if j >= 20:
        b, p = merge([day_prof[x] for x in days[j - 20:j]])
        put("20d HVN", d, nodes(b, p, kind="hvn"))
        poc, vah, val = poc_va(b, p)
        put("20d POC", d, [poc])

# placebo: same families, levels jittered by +-[0.40,1.20]
for fam in [f for f in levels if "control" not in f]:
    levels[fam + " ~placebo"] = {
        d: [x + rng.choice([-1, 1]) * rng.uniform(0.40, 1.20) for x in lv]
        for d, lv in levels[fam].items()
    }

# developing session POC/VAH/VAL, causal (built through bar t-1), from 10:00 on
dev = {}
for d in days[1:]:
    s = rth[rth.day == d]
    lo = np.floor(s["low"].values / BIN).astype(int)
    hi = np.floor(s["high"].values / BIN).astype(int)
    base = lo.min() - 400
    prof = np.zeros(hi.max() - base + 400)
    rows = []
    for t in range(len(s)):
        if t >= 30:
            poc, vah, val = poc_va(base, prof)
            rows.append((s.index[t], poc, vah, val))
        a, b = lo[t] - base, hi[t] - base
        prof[a:b + 1] += s["volume"].values[t] / (b - a + 1)
    dev[d] = pd.DataFrame(rows, columns=["ts", "poc", "vah", "val"]).set_index("ts")


def grade(H, L, i, entry, side, tgt, stp):
    """side +1 long, -1 short. From bar i (touch bar) onward. 1 win, -1 loss, 0 timeout."""
    if side > 0 and L[i] <= entry - stp: return -1
    if side < 0 and H[i] >= entry + stp: return -1
    for k in range(i + 1, len(H)):
        if side > 0:
            if L[k] <= entry - stp: return -1
            if H[k] >= entry + tgt: return 1
        else:
            if H[k] >= entry + stp: return -1
            if L[k] <= entry - tgt: return 1
    return 0


def touches(s, lvl_at):
    """lvl_at(t) -> list of levels at bar t. Yields (t, level, side)."""
    H, L, C = s.high.values, s.low.values, s.close.values
    last = {}
    for t in range(1, len(s)):
        for lv in lvl_at(t):
            key = round(lv, 2)
            if t - last.get(key, -999) < COOLDOWN:
                continue
            if C[t - 1] >= lv + ARM and L[t] <= lv:
                last[key] = t; yield t, lv, +1      # support test -> long
            elif C[t - 1] <= lv - ARM and H[t] >= lv:
                last[key] = t; yield t, lv, -1      # resistance test -> short


def study(fam, getter):
    rows = []
    for d in days:
        s = rth[rth.day == d]
        lv_at = getter(d, s)
        if lv_at is None:
            continue
        H, L = s.high.values, s.low.values
        for t, lv, side in touches(s, lv_at):
            if t > len(s) - 10:          # no trades in last 10 min
                continue
            rows.append(dict(day=d, t=t, side=side,
                             good=grade(H, L, t, lv, side, 1.50, 0.50),
                             hold=grade(H, L, t, lv, side, 0.50, 0.50),
                             brk=grade(H, L, t, lv, -side, 1.50, 0.50)))
    return pd.DataFrame(rows)


def static_getter(fam):
    def g(d, s):
        lv = levels[fam].get(d)
        return None if lv is None else (lambda t: lv)
    return g


def dev_getter(cols):
    def g(d, s):
        if d not in dev:
            return None
        df = dev[d]
        def at(t):
            ts = s.index[t]
            return [] if ts not in df.index else [df.at[ts, c] for c in cols]
        return at
    return g


def dev_placebo(cols):
    off = {}
    def g(d, s):
        if d not in dev:
            return None
        df = dev[d]
        o = off.setdefault(d, [rng.choice([-1, 1]) * rng.uniform(0.4, 1.2) for _ in cols])
        def at(t):
            ts = s.index[t]
            return [] if ts not in df.index else [df.at[ts, c] + x for c, x in zip(cols, o)]
        return at
    return g


def boot_ci(df, col, val=1, n=2000):
    """Session-clustered bootstrap CI of P(col == val)."""
    g = df.groupby("day")[col].agg(lambda x: ((x == val).sum(), len(x)))
    arr = np.array(g.tolist())
    idx = rng.integers(0, len(arr), (n, len(arr)))
    k, m = arr[idx, 0].sum(1), arr[idx, 1].sum(1)
    r = k / np.maximum(m, 1)
    return np.percentile(r, 5), np.percentile(r, 95)


fams = {f: static_getter(f) for f in levels}
fams["dev POC"] = dev_getter(["poc"])
fams["dev VAH/VAL"] = dev_getter(["vah", "val"])
fams["dev POC ~placebo"] = dev_placebo(["poc"])
fams["dev VAH/VAL ~placebo"] = dev_placebo(["vah", "val"])

out = []
allrows = {}
for f, g in sorted(fams.items()):
    r = study(f, g)
    if r.empty:
        continue
    allrows[f] = r
    lo, hi = boot_ci(r, "good")
    out.append(dict(family=f, touches=len(r), sessions=r.day.nunique(),
                    good=(r.good == 1).mean(), good_90ci=f"{lo:.2f}-{hi:.2f}",
                    bad=(r.good == -1).mean(),
                    hold=(r.hold == 1).mean(),
                    brk_good=(r.brk == 1).mean(),
                    exp_R=((r.good == 1) * 3 - (r.good == -1)).mean()))
res = pd.DataFrame(out).set_index("family")
pd.set_option("display.width", 200)
print(res.round(3).to_string())
pd.to_pickle(allrows, SP + "rows.pkl")

# random-entry baseline: every RTH bar, enter at close, both sides
base = []
for d in days[1:]:
    s = rth[rth.day == d]; H, L, C = s.high.values, s.low.values, s.close.values
    for t in range(0, len(s) - 10, 3):
        for side in (1, -1):
            base.append(grade(H, L, t + 1, C[t], side, 1.50, 0.50) if t + 1 < len(s) else 0)
base = np.array(base)
print(f"\nrandom entry baseline: good={np.mean(base==1):.3f}  bad={np.mean(base==-1):.3f}  n={len(base)}")
