# Volume-profile study

**Verdict (2026-10-29), with high confidence: volume profile doesn't help the 1:3 grade (+$1.50 before −$0.50).** It wasn't adopted, so `compute_features()` has no volume-profile columns.

## Data
- **SPY 1-min bars, 2014-04 → 2026-03**, from the Hugging Face dataset [Rrishab/OHLCV-1m](https://huggingface.co/datasets/Rrishab/OHLCV-1m) (Finnhub-sourced). `hf_spy.py` range-reads only SPY's row group from each monthly parquet file. Checked against yfinance daily bars: RTH open/high/low match to the cent (unadjusted); volume is ~85% of consolidated volume, 0.98–0.99 correlated day to day.
- **The stored Supabase bars (2026-07 → 10)**, used as period P4: yfinance volume, and the period the current rules were fit on.
- Half days and gappy sessions are dropped (fewer than 385 RTH bars).

| Set | Years | Sessions | Use |
|---|---|---|---|
| discovery | 2023-04 → 2026-03 (+ 2026 Supabase bars) | 785 | all exploratory tests |
| holdout #1 | 2020-04 → 2023-03 | 737 | pre-registered confirmation of 3 near-misses |
| holdout #2 | 2014-04 → 2020-03 | 1,491 | pre-registered confirmation of the 1 survivor |

## Method
- **Grading:** identical to the pipeline's auto-grader. Fill at the next bar's open; Good = +$1.50 before −$0.50; Bad = −$0.50 before +$1.00; same-session; ties go to the stop.
- **Placebo levels:** each real level is shifted ±$0.40–1.20. A level only counts if it beats its own placebo. Osler's FX work found that arbitrary levels "bounce" more than 50% of the time, so 50% is the wrong baseline.
- **Pass rule (fixed before each set was examined):** the same sign in every year, a session-clustered bootstrap 99% CI that excludes 0, and the effect has to survive its control (its placebo, or VWAP side for overlapping filters).

## Results
| Script | Tests | Result |
|---|---|---|
| `vp_full.py` + `vp_verdict.py` | ~50 tests on discovery. (A) Level bounce/break with limit fills. (B) Profile context as a filter on random entries. (C) The 2026-10-23 rule set's L/S signals filtered by profile context. (D) The 80% rule. | **0 passed.** Pooled diffs within ±4 points, CIs about ±3 points. Developing-POC side only repeats VWAP side. The 80% rule traverses 56% of the time, vs 58% for a placebo value area. |
| `vp_robust.py` | Level tests again, with rejection-candle and break-and-retest entries, at $0.05 and $0.25 bins, plus top-3 HVNs | 0 passed. 3 near-misses, all rejection-candle entries at $0.25 bins |
| `vp_confirm.py` | The 3 near-misses on holdout #1, 5 placebo draws each | **0 passed.** Naked POC and developing VAH/VAL ≈ 0. Prior-day POC +3.1 points, but the CI includes 0 |
| `bin_sweep.py` | Prior-day POC rejection candle at bins from $0.05 to $1.00, 5 placebos, on discovery | Noise around 0 (+0.7, −1.0, −1.6, +2.3, −0.6, +1.4 points). The +8-point discovery result came from one low placebo draw |
| `vp_confirm2.py` | Prior-day POC on holdout #2 (pass rule: CI excludes 0, positive in ≥5 of 6 years) | **Failed.** −0.9 points, positive in 2 of 6 years |
| `pool.py` | Prior-day POC pooled across discovery + holdout #1 (not confirmatory) | Superseded by the two rows above |

Side finding from (C): out of sample, the 2026-10-23 rule set's signals came out Good 21.8 / 24.1 / 26.0% in 2023–26, vs 18.6 / 21.8 / 20.6% for random entries. Their Bad rate was about the same as random.

## Re-running
`vp_lib.py` holds the full profile feature set. `compute_all(sp, bars)` returns the pipeline's features plus the profile features.
```bash
SV=/path/to/venv   # pip install pyarrow fsspec aiohttp pandas numpy requests yfinance
$SV/bin/python research/volume_profile/fetch.py                                        # Supabase bars -> bars.pkl
$SV/bin/python research/volume_profile/hf_spy.py SPY                                   # discovery -> hf_SPY/
$SV/bin/python research/volume_profile/hf_spy.py SPY 2020-04 2023-03                   # -> hf_SPY_holdout/
$SV/bin/python research/volume_profile/hf_spy.py SPY 2014-04 2020-03 hf_SPY_holdout2   # -> hf_SPY_holdout2/
$SV/bin/python research/volume_profile/vp_full.py && $SV/bin/python research/volume_profile/vp_verdict.py
$SV/bin/python research/volume_profile/vp_robust.py
$SV/bin/python research/volume_profile/vp_confirm.py
$SV/bin/python research/volume_profile/bin_sweep.py hf_SPY
$SV/bin/python research/volume_profile/vp_confirm2.py
```
`vp_test.py` and `vp_context.py` are the first 55-session pass. Downloaded data and result pickles are gitignored.
