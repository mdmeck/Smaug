"""Strategy track (Track A) tooling for the trader's playbook, break_retest.

  python -m lab.playbook scan --symbols SPY,AAPL --start 2016-01-01 --end 2023-12-31 --out rows.pkl
  python -m lab.playbook gate rows1.pkl rows2.pkl ...      # the underlying gate
  python -m lab.playbook setups --symbols SPY,NVDA --start 2026-07-01 --end 2026-11-20

`scan` runs the detector once per symbol-day with every filter evaluated, so
one pass gives every filter combination: a preset or a "full spec minus one
filter" ablation is just a different subset of the same rows. For each setup
it records, on the underlying and under the spec's exits (half off at 2.5R,
trailer on the 5-min 9/21 cross, stop to entry), the R of the taken side and
of the opposite side, and which of +1R / -1R came first on each side.

`gate` is step 5 of the build plan, the free first answer to "does the
playbook have an edge". It compares the trigger alone, trigger + trend, the
full spec, and the matched random control (same symbol, day and entry minute,
random side, same R and exits: its expectation is exactly the mean of the
two sides, so edge per trade = (R taken - R opposite) / 2). It also reports a
timing control (a random minute in the entry window on the same symbol-day,
random side, the same R) and how often each filter changes a decision.
It refuses windows that touch the sealed holdout.

`setups` prints only when and where the detector fired (no outcomes), for the
fidelity check against the trader's own trades. It is allowed inside the
holdout because it reads no P/L.
"""
import argparse
import json
import random
import sys

import numpy as np
import pandas as pd

from . import backtest, data, evaluation
from .alpaca import NY
from .strategies import break_retest as br

FAMILY = "playbook_break_retest"
WATCHLIST = ["TSLA", "AAPL", "NFLX", "INTC", "MSFT", "AMZN", "GOOG", "NVDA", "AMD", "MU", "PLTR", "SMH"]


def _sessions_of(symbol, start, end, p, ref_minutes=None):
    mk = backtest.load_market(symbol, start, end, br.NEEDS, ref_minutes=ref_minutes)
    daily = backtest.daily_bars(mk["rth"])
    days = data.sessions(mk["rth"])
    first = np.searchsorted(daily.index, pd.Timestamp(start))
    last = np.searchsorted(daily.index, pd.Timestamp(end), side="right")
    for i in range(first, last):
        d = daily.index[i].date()
        day = days[d]
        if len(day) < 300:
            continue
        ctx = backtest.session_context(daily, i, None, mk)
        if len(ctx.get("prior_rth") or []) < 2:
            continue
        yield d, day, ctx


def scan_rows(symbol, start, end, p=None, timing_seed=11, ref_minutes=None, outcomes=True):
    """One row per setup with its failed filters and both sides' outcomes."""
    p = br.params(p)
    rng = random.Random(f"{timing_seed}-{symbol}")
    out = []
    for d, day, ctx in _sessions_of(symbol, start, end, p, ref_minutes):
        v = br.view_from_ctx(day, ctx)
        q = ctx.get("ref_day")
        flags = {"*": ["quad_witching"]} if br.is_quad_witching(d) else {}
        flat_by = pd.Timestamp(f"{d} {p['flat_by']}", tz=NY)
        data_end = day.index[-1] + pd.Timedelta(minutes=1)
        try:
            rows = br.scan_symbol(symbol, v, q, flags, data_end, flat_by, p, outcome=outcomes)
        except Exception as e:  # noqa: BLE001 - one bad day never sinks a decade
            print(f"WARNING {symbol} {d}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if not rows:
            continue
        m1 = v["sides"][1]["m1"]
        window = [j for j in range(len(m1["t"])) if br.in_window(m1["t"][j] + pd.Timedelta(minutes=1), p)]
        for r in rows:
            ci, sgn = r["_ci"], (1 if r["side"] == "Long" else -1)
            rec = {"symbol": symbol, "date": d, "side": r["side"], "entry_at": r["entry_at"],
                   "level_tags": "+".join(r["level_tags"]), "failed": r["failed"]}
            if outcomes:
                E, R = sgn * r["entry"], r["risk"]
                opp = -sgn
                so, sp = v["sides"][sgn], v["sides"][opp]
                res_o = br.simulate(sp, ci, -E, -E - R, data_end, flat_by, p)
                rec.update({
                    "r": r["r_multiple"], "r_opp": res_o["r"],
                    "hit": br.first_hit(so, ci, E, R), "hit_opp": br.first_hit(sp, ci, -E, R),
                })
                # timing control: a random minute in the window, random side, same R
                if window:
                    j = rng.choice(window)
                    s2 = rng.choice([1, -1])
                    e2 = s2 * m1["c"][j]
                    res_t = br.simulate(v["sides"][s2], j, e2, e2 - R, data_end, flat_by, p)
                    rec["r_timing"] = res_t["r"]
            out.append(rec)
    return pd.DataFrame(out)


def _selected(rows, off):
    off = set(off)
    return rows[rows["failed"].apply(lambda f: not [x for x in f if x not in off])]


def _block(sel):
    if not len(sel):
        return {"n": 0}
    edge = 0.5 * (sel.r - sel.r_opp)
    days = sel.date.astype(str)
    eci = evaluation.boot_mean_ci(edge, days)
    out = {"n": int(len(sel)), "days": int(days.nunique()),
           "avg_r": round(float(sel.r.mean()), 3),
           "win_rate": round(float((sel.r > 0).mean()), 3),
           "control_avg_r": round(float(0.5 * (sel.r + sel.r_opp).mean()), 3),
           "edge_r": round(float(edge.mean()), 3),
           "edge_ci90": None if eci is None else [round(x, 3) for x in eci],
           "plus1r_first": evaluation.z_vs_half(sel.hit.tolist()),
           "opp_plus1r_first": evaluation.z_vs_half(sel.hit_opp.tolist())}
    if "r_timing" in sel:
        t = sel.r_timing.dropna()
        out["timing_control_avg_r"] = round(float(t.mean()), 3) if len(t) else None
    return out


def gate(rows, by_year=True):
    """The underlying gate on scan rows (filters evaluated, outcomes in R)."""
    out = {"family": FAMILY, "setups_scanned": int(len(rows)), "symbols": sorted(rows.symbol.unique().tolist())}
    for name, pre in br.PRESETS.items():
        out[name] = _block(_selected(rows, pre["filters_off"]))
    # pass bar declared in advance (two-track proposal): full beats trigger and random at z >= 2
    full, trig = _selected(rows, []), _selected(rows, br.PRESETS["trigger"]["filters_off"])
    out["full_vs_trigger_r"] = round(float(full.r.mean() - trig.r.mean()), 3) if len(full) and len(trig) else None
    # each filter's bite: of the trigger setups, how many it alone rejects, and
    # what the full spec looks like without it
    filters = br.TREND_FILTERS + br.MARKET_FILTERS
    bite = {}
    for f in filters:
        rejects = int(trig["failed"].apply(lambda x: f in x).sum()) if len(trig) else 0
        without = _selected(rows, [f])
        bite[f] = {"rejects_of_trigger": rejects,
                   "rejects_share": round(rejects / len(trig), 3) if len(trig) else None,
                   "full_without_it": {k: without_v for k, without_v in _block(without).items()
                                       if k in ("n", "avg_r", "edge_r", "edge_ci90")}}
    out["filter_bite"] = bite
    if by_year and len(full):
        out["full_by_year"] = {str(y): {k: v for k, v in _block(g).items() if k in ("n", "avg_r", "edge_r")}
                               for y, g in full.groupby(pd.to_datetime(full.date).dt.year)}
        out["trend_by_symbol"] = {s: {k: v for k, v in _block(g).items() if k in ("n", "avg_r", "edge_r")}
                                  for s, g in _selected(rows, br.PRESETS["trend"]["filters_off"]).groupby("symbol")}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lab.playbook")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--symbols", required=True)
    s.add_argument("--start", default=evaluation.PERIODS["discovery"][0])
    s.add_argument("--end", default=evaluation.PERIODS["discovery"][1])
    s.add_argument("--params")
    s.add_argument("--out", required=True)
    g = sub.add_parser("gate")
    g.add_argument("files", nargs="+")
    g.add_argument("--record", action="store_true", help="append the result to the scoreboard")
    u = sub.add_parser("setups")
    u.add_argument("--symbols", required=True)
    u.add_argument("--start", required=True)
    u.add_argument("--end", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "scan":
        evaluation.check_window(a.start, a.end, "discovery")
        p = json.loads(a.params or "{}")
        ref = data.stock_minutes("QQQ", pd.Timestamp(a.start) - pd.Timedelta(days=10), a.end)
        frames = []
        for sym in a.symbols.split(","):
            df = scan_rows(sym, a.start, a.end, p, ref_minutes=ref)
            print(f"{sym}: {len(df)} setups", file=sys.stderr, flush=True)
            frames.append(df)
        pd.concat(frames, ignore_index=True).to_pickle(a.out)
        return 0
    if a.cmd == "gate":
        rows = pd.concat([pd.read_pickle(f) for f in a.files], ignore_index=True)
        res = gate(rows)
        if a.record:
            for name, pre in br.PRESETS.items():
                b = res[name]
                if not b.get("n"):
                    continue
                params = br.params({"preset": name})
                evaluation.record({"family": FAMILY, "strategy": f"{br.NAME}:{name}", "module": br.NAME,
                                   "params": params, "params_hash": evaluation.params_hash(params),
                                   "period": "discovery", "pricing": "underlying_r",
                                   "start": evaluation.PERIODS["discovery"][0],
                                   "end": evaluation.PERIODS["discovery"][1], "n": b["n"],
                                   "avg_pnl": b["avg_r"], "baseline_avg_pnl": b["control_avg_r"],
                                   "edge": b["edge_r"],
                                   "edge_ci90_lo": b["edge_ci90"][0] if b.get("edge_ci90") else None,
                                   "edge_ci90_hi": b["edge_ci90"][1] if b.get("edge_ci90") else None,
                                   "win_rate": b["win_rate"], "variants_in_family": evaluation.tries(FAMILY) + 1,
                                   "verdict": "screen", "detail": {"units": "R", "plus1r_first": b["plus1r_first"]}})
        print(json.dumps(res, indent=1, default=str))
        return 0
    if a.cmd == "setups":
        ref = data.stock_minutes("QQQ", pd.Timestamp(a.start) - pd.Timedelta(days=10), a.end)
        for sym in a.symbols.split(","):
            df = scan_rows(sym, a.start, a.end, ref_minutes=ref, outcomes=False)
            for r in df.itertuples():
                print(f"SETUP,{r.symbol},{r.date},{r.side},{r.entry_at},{r.level_tags},{'|'.join(r.failed) or 'PASS'}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
