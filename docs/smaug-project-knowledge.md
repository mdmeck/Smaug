# Smaug — reference context

Pin this to the claude.ai Project's knowledge so daily conversations don't need to re-explain it.

## What this is
Smaug is a personal intraday SPY options scalping tool. The trader uses a **break-and-retest methodology with confluence scoring and ATR-based stops**. A daily Python pipeline pulls SPY 1-minute bars (pre-market + regular trading hours, 30-day retention), computes indicator features, and runs regression/correlation analysis against forward-return targets. A companion webapp lets the trader log actual trades (Journal), log labeled good/bad entry & exit examples (Training Data), and synthesize an "entry model" (structured rule set) from the two combined.

## Data sources (public GitHub repo — fetch directly, always current)
- `https://raw.githubusercontent.com/mdmeck/Smaug/main/smaug_bars.json` — 30-day window of 1-min OHLCV + every computed feature, per bar. Shape: `{"ticker": "SPY", "feature_cols": [...], "bars": [[ts, open, high, low, close, volume, ...feature values in feature_cols order], ...]}`.
- `https://raw.githubusercontent.com/mdmeck/Smaug/main/smaug_results.json` — daily regression/correlation output. Shape: `{"generated_at", "ticker", "bars_analyzed", "date_range": [start, end], "targets": {"<target_key>": {"n", "correlations": [[feature, corr_or_null], ...], "regression": {"intercept_bps", "std_coefficients_bps": {feature: coef}, "r2_train", "r2_test"}, "deciles": {feature: [{"decile", "avg_move_bps", "n"}]}}}}`.
- `https://raw.githubusercontent.com/mdmeck/Smaug/main/smaug_pipeline.py` — the actual pipeline source. `compute_features()` is the ground truth for exactly how every feature below is calculated; `compute_targets()` for the targets.

## Feature columns (all stationary — returns/spreads/ratios/bps-distances, never raw price levels)
| feature | meaning |
|---|---|
| `rsi14` | RSI, 14-period, 0–100 |
| `ema_spread_bps` | (EMA9 − EMA21) / close, in bps |
| `dist_ema9_bps` | distance of close from EMA9, in bps |
| `dist_ema21_bps` | distance of close from EMA21, in bps |
| `ret_1m_bps` | 1-minute return, in bps |
| `ret_5m_bps` | 5-minute return, in bps |
| `range_bps` | (high − low) / close, in bps |
| `body_ratio` | candle body / candle range, 0–1 |
| `vol_z` | volume z-score vs. the same minute-of-day's historical average |
| `min_since_open` | minutes elapsed since 9:30 ET open |
| `dist_prev_day_high_bps` / `dist_prev_day_low_bps` | distance of close from the **previous session's** RTH high/low, in bps |
| `dist_premkt_high_bps` / `dist_premkt_low_bps` | distance of close from **today's** pre-market high/low, in bps |
| `dist_or5_high_bps` / `dist_or5_low_bps` | distance of close from the 5-minute opening-range high/low (first 5 min of RTH), in bps |
| `dist_or15_high_bps` / `dist_or15_low_bps` | distance of close from the 15-minute opening-range high/low, in bps |

All `dist_*`/`ret_*`/`range_bps`/`ema_spread_bps` features are causal — computed only from information available at or before that bar (no lookahead). Opening-range features use a running high/low while the window is still forming, then hold the finalized value for the rest of the session.

## Targets
| target | meaning |
|---|---|
| `fwd_5m_bps` / `fwd_10m_bps` / `fwd_15m_bps` | forward return N minutes ahead, in bps, same-session only |
| `mfe_10m_bps` | max favorable excursion (long side) over the next 10 minutes, in bps |

## Supabase tables (RLS-protected — NOT fetchable via a plain URL; the trader's session data must be pasted in, not linked)
- `journal_entries` — actual trades taken: `date, ticker, direction (Long/Short), setup, result, notes`.
- `training_examples` — labeled good/bad entry & exit examples for model training (distinct from the journal — this is training data, not trades taken): `occurred_at, type (Entry/Exit), direction (Long/Short), quality (Good/Bad), notes`.
- `entry_models` — each row is one AI-synthesized entry/exit rule set, versioned over time (not overwritten): `generated_at, bars_analyzed, examples_used, date_range, rules jsonb, summary, confidence (low/medium/high)`.

## Entry-model output schema
When asked to synthesize/update the entry model, respond with ONLY this JSON shape:
```json
{
  "long_entry": [{"feature": "name", "op": "<|<=|>|>=", "value": number, "note": "under 15 words"}],
  "short_entry": [...],
  "exit": [...],
  "summary": "3-5 sentences, plain language",
  "confidence": "low|medium|high"
}
```
Only use feature names from the table above — never invent one, since these rules get translated mechanically into a PineScript trading indicator. If there are zero or very few training examples, say so explicitly in the summary and lean on the regression/decile output instead; confidence must be "low" in that case. Never invent a finding the numbers don't support.
