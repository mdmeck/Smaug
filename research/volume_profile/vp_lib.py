"""Volume-profile feature set used by the research scripts.

Never merged into smaug_pipeline.py: the study found no edge (see README.md).
compute_all() = the pipeline's features + these, at the current VP_BIN.
"""
import numpy as np
import pandas as pd

# Volume-at-price from RTH 1-minute bars, spreading each bar's volume evenly
# across its high-low range (no tick data, so this is the standard approximation).
VP_BIN = 0.05                   # price bin width, SPY dollars; scripts override per run
VP_VALUE_AREA = 0.70            # share of volume inside VAH..VAL
VP_COMPOSITE_DAYS = 5           # prior sessions in the HVN composite
VP_NODE_SMOOTH = 5              # bins each side in the smoothing kernel
VP_NODE_WINDOW = 10             # bins each side a peak must dominate to be an HVN

VP_FEATURES = [
    "dist_prev_poc_bps", "dist_prev_vah_bps", "dist_prev_val_bps",
    "dist_dev_poc_bps", "dist_dev_vah_bps", "dist_dev_val_bps",
    "dist_hvn5_bps",
]


def _vp_poc_va(prof):
    """Bin indices of (POC, VAH, VAL). The value area grows outward from the
    POC one bin at a time toward whichever neighbour holds more volume, until
    it covers VP_VALUE_AREA of the total — the usual contiguous definition,
    not the top-N bins by volume, which can leave holes."""
    i = int(prof.argmax())
    lo = hi = i
    target, acc = VP_VALUE_AREA * prof.sum(), prof[i]
    while acc < target:
        up = prof[hi + 1] if hi + 1 < len(prof) else -1.0
        dn = prof[lo - 1] if lo > 0 else -1.0
        if up >= dn:
            hi += 1
            acc += up
        else:
            lo -= 1
            acc += dn
    return i, hi, lo


def _vp_hvns(prof):
    """Bin indices of high-volume nodes: local maxima of the smoothed profile
    that dominate VP_NODE_WINDOW bins each side and clear the median of the
    traded bins (so a bump in a thin tail doesn't count)."""
    k = np.ones(2 * VP_NODE_SMOOTH + 1) / (2 * VP_NODE_SMOOTH + 1)
    s = np.convolve(prof, k, mode="same")
    traded = s[s > 0]
    if not len(traded):
        return []
    floor = np.median(traded)
    w = VP_NODE_WINDOW
    return [
        i for i in range(w, len(s) - w)
        if s[i] > floor and s[i] == s[i - w:i + w + 1].max()
    ]


def _volume_profile_features(h, l, c, v, day, rth_mask):
    """Distances (bps of close) to volume-profile levels, RTH bars only.

    Causal by construction: the prior-session and composite profiles are
    finished before today's open, and the developing profile includes bars up
    to and including the current one — the same convention as the session
    VWAP, which also counts the bar it is evaluated on."""
    n_rows = len(c)
    out = {name: np.full(n_rows, np.nan) for name in VP_FEATURES}
    pos = np.flatnonzero(np.asarray(rth_mask) & np.isfinite(h.values)
                         & np.isfinite(l.values) & np.isfinite(v.values))
    if not len(pos):
        return out
    H, L, C, V = h.values, l.values, c.values, v.values.astype(float)
    lo_b = np.floor(L / VP_BIN).astype(int)
    hi_b = np.floor(H / VP_BIN).astype(int)
    # one shared price grid for the whole window, so day profiles can be
    # summed into a composite without re-aligning
    base = lo_b[pos].min()
    n_bins = hi_b[pos].max() - base + 1
    px = lambda k: (base + k + 0.5) * VP_BIN

    def bps(t, level):
        return (C[t] - level) / C[t] * 10_000

    days = np.asarray(day.values)[pos]
    finished = []  # completed session profiles, in date order
    for d in pd.unique(days):
        idx = pos[days == d]
        if finished:
            poc, vah, val = (px(k) for k in _vp_poc_va(finished[-1]))
            out["dist_prev_poc_bps"][idx] = bps(idx, poc)
            out["dist_prev_vah_bps"][idx] = bps(idx, vah)
            out["dist_prev_val_bps"][idx] = bps(idx, val)
        if len(finished) >= VP_COMPOSITE_DAYS:
            hvns = px(np.array(_vp_hvns(sum(finished[-VP_COMPOSITE_DAYS:]))))
            if hvns.size:
                near = hvns[np.abs(C[idx][:, None] - hvns).argmin(axis=1)]
                out["dist_hvn5_bps"][idx] = bps(idx, near)
        prof = np.zeros(n_bins)
        for t in idx:
            a, b = lo_b[t] - base, hi_b[t] - base
            prof[a:b + 1] += V[t] / (b - a + 1)
            poc, vah, val = (px(k) for k in _vp_poc_va(prof))
            out["dist_dev_poc_bps"][t] = bps(t, poc)
            out["dist_dev_vah_bps"][t] = bps(t, vah)
            out["dist_dev_val_bps"][t] = bps(t, val)
        finished.append(prof)
    return out




def compute_all(sp, bars):
    """sp.compute_features(bars) plus the full profile set, at the current VP_BIN."""
    feats = sp.compute_features(bars)
    mod = bars.index.hour * 60 + bars.index.minute
    rth_mask = (mod >= 570) & (mod < 960)
    day = pd.Series(bars.index.date, index=bars.index)
    vp = _volume_profile_features(bars["high"], bars["low"], bars["close"],
                                  bars["volume"], day, rth_mask)
    for name, vals in vp.items():
        feats[name] = vals
    return feats
