"""Diagnostic: is the prior-day-POC rejection lead smooth across bin sizes?
A real level effect shouldn't appear at $0.25 and vanish at $0.05/$0.10.
Discovery data only (2023-04..2026-03); 5 placebo draws per level."""
import os, sys
import numpy as np, pandas as pd
SP = os.path.dirname(os.path.abspath(__file__)) + "/"
exec(open(SP + "vp_full.py").read().split("# ================================================================ main")[0])
exec("def events" + open(SP + "vp_robust.py").read().split("def events")[1].split("def top_hvns")[0])

data_dir = sys.argv[1] if len(sys.argv) > 1 else "hf_SPY"
df = pd.concat([pd.read_pickle(f) for f in sorted(glob.glob(SP + data_dir + "/*.pkl"))])
df["ts"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
bars = clean(df.drop_duplicates("ts").set_index("ts").sort_index()[["open", "high", "low", "close", "volume"]].astype(float))
mod = bars.index.hour * 60 + bars.index.minute
rb = bars[(mod >= 570) & (mod < 960)]
days = sorted(set(rb.index.date))
by_day = {d: rb[rb.index.date == d] for d in days}

for bsz in (0.05, 0.10, 0.15, 0.25, 0.50, 1.00):
    rows = []
    for j in range(1, len(days)):
        prev, s = by_day[days[j - 1]], by_day[days[j]]
        lo = np.floor(prev.low.values / bsz).astype(int); hi = np.floor(prev.high.values / bsz).astype(int)
        base = lo.min(); prof = np.zeros(hi.max() - base + 1)
        for a, b, v in zip(lo - base, hi - base, prev.volume.values):
            prof[a:b + 1] += v / (b - a + 1)
        poc = (base + prof.argmax() + 0.5) * bsz
        H, L, C, O = s.high.values, s.low.values, s.close.values, s.open.values
        n = len(s)
        levels = [(poc, False)] + [(poc + rng.choice([-1, 1]) * rng.uniform(0.4, 1.2), True) for _ in range(5)]
        for x, plc in levels:
            for t, side in events(H, L, C, O, np.full((n, 1), x), "confirm"):
                if t >= n: continue
                g = auto_grade(H, L, t, O[t], side)
                rows.append((days[j], plc, g == "Good", g == "Bad"))
    r = pd.DataFrame(rows, columns=["day", "placebo", "good", "bad"])
    a, b = r[~r.placebo], r[r.placebo]
    lo_, hi_ = boot_diff(a.day.values, a.good.astype(float).values, b.day.values, b.good.astype(float).values, ci=95)
    print(f"bin ${bsz:.2f}: n_real={len(a):4d} real={a.good.mean():.3f} placebo={b.good.mean():.3f} "
          f"diff={a.good.mean() - b.good.mean():+.3f} 95ci {lo_:+.3f}..{hi_:+.3f}", flush=True)
