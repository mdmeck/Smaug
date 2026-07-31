# Smaug — reference context

Pin this to the claude.ai Project's knowledge so daily conversations don't need to re-explain it.

## What this is
Smaug is a personal intraday SPY options scalping tool. The trader uses a **break-and-retest methodology with confluence scoring and ATR-based stops**. A daily Python pipeline pulls SPY 1-minute bars (pre-market + regular trading hours, 30-day retention), computes indicator features, and runs regression/correlation analysis against forward-return targets. A companion webapp lets the trader log actual trades (Journal), log labeled good/bad trade examples (Training Data), and displays the "entry model" (structured rule set + PineScript indicator) synthesized daily by a routine from the two combined.

## Data sources

**GitHub (public repo, fetch directly — pipeline source only):**
- `https://raw.githubusercontent.com/mdmeck/Smaug/main/smaug_pipeline.py` — the actual pipeline source. `compute_features()` is the ground truth for exactly how every feature below is calculated; `compute_targets()` for the targets.

**Supabase (query via your Supabase connector, not a plain URL):**
- `bars` — 1-minute SPY bars + computed features, ~30-day retained window (~8,000 rows). Columns: `ts, ticker, open, high, low, close, volume, features` (jsonb — keys are the feature names below). Public read — no auth needed, but there are far more than 1000 rows, so page through with `.range()`/`limit`+`offset` rather than assuming one query returns everything.
- `analysis_runs` — daily regression/correlation output. One row per pipeline run (append-only — query `order by generated_at desc limit 1` for the current one). Columns: `generated_at, ticker, bars_analyzed, date_range, targets (jsonb), notes`. Public read, same as `bars`.
- `training_examples` — the trader's labeled trade examples: `entry_at, exit_at (null on Bad examples — never entered, so never exited), ticker (default 'SPY'), direction (Long/Short), quality (Good/Bad — overall quality of the trade), strategy, key_level, notes`. **Private, RLS-protected** — but see "Writing as the routine" below: the connector reads it fine. May be empty.
  - `strategy` — which setup the trader was playing: currently `Break and Retest`, `Opening Range Break`, or `Bounce`, though it's plain text with no DB constraint (the webapp dropdown is the only thing enforcing the list), so treat unfamiliar values as valid and new rather than as errors. May be empty on older rows. Useful for grouping: rules synthesized from a mix of strategies are weaker than rules that respect the split, so mention in `summary` which strategies the examples covered.
  - `key_level` — the price level the setup was built around (the level broken and retested, the opening-range boundary, the level bounced off). Nullable. Where present it's the anchor the trade was reasoning about, so `close - key_level` at the entry snapshot is usually more meaningful than the raw price, and worth comparing against the `dist_*` features to see which stored level the trader was actually watching.
  - `analysis_notes` — **yours to write, not the trader's.** Record what you found when analyzing this example: which features actually distinguished it, whether it agreed with the others of its `strategy`, anything that makes it an outlier. Write it back with an `update` on the example's `id` (passing `user_id` is unnecessary on an update — the row already has one). Kept separate from `notes` on purpose: `notes` is the trader's own reasoning and is ground truth, `analysis_notes` is your inference. Never write into `notes`, and when reading examples treat only `notes` as evidence — otherwise later runs learn from earlier runs' conclusions and the labels quietly stop meaning anything.

Sessions referenced by a training example are **exempt from the `bars` retention window** — the pipeline pins those whole days so the feature-snapshot join above keeps working indefinitely, even once the session ages past the 60-day cutoff. So an old example is still joinable; don't assume a missing bar means the example is stale. Editable/deletable by the trader in the webapp, so always re-read fresh each run rather than assuming yesterday's set still applies. Note the entry-model synthesis is SPY-specific — if an example has a different ticker, treat it as informational context rather than folding it into the SPY feature-value join.
- `daily_briefs` — morning brief (econ calendar, earnings, sentiment, bull/bear case), written by the routine: `generated_at, econ (jsonb), earnings (jsonb), sentiment (jsonb), cases (jsonb)`. Private. One row per user (`user_id` is unique) — overwritten each run via `upsert` with `on_conflict=user_id`, no history kept. Query with a plain `select` for the current one.
- `entry_models` — each row is one AI-synthesized entry/exit rule set + PineScript indicator, written directly by the routine (append-only, so the trader can see the model evolve day over day). Private: `generated_at, bars_analyzed, examples_used, date_range, rules (jsonb), summary, confidence (low/medium/high), pinescript (text)`.

### Writing as the routine — `user_id` is required

The Supabase connector authenticates as the **`postgres`** role, not as an end user, so `auth.uid()` evaluates to `NULL` in everything the routine runs. Two consequences:

- **Reads just work.** `postgres` owns these tables and they don't set `FORCE ROW LEVEL SECURITY`, so RLS is bypassed on `select`. No authenticated user session is needed to read `training_examples`, `daily_briefs`, or `entry_models`.
- **Writes must set `user_id` explicitly.** All four private tables declare `user_id uuid NOT NULL DEFAULT auth.uid()`. With `auth.uid()` NULL, any insert that omits the column fails a not-null violation before RLS is ever consulted. Always pass:

  ```
  user_id = c0b48756-5f94-4862-886a-8ecdb7099ef6
  ```

This applies to the `daily_briefs` upsert and every `entry_models` insert. Symptom when it's forgotten: reads look completely healthy while `daily_briefs`/`entry_models` silently stop gaining rows, even though `bars`/`analysis_runs` stay current (those are written by the pipeline's service-role key, which is a separate path and unaffected).

For each training example, join **two** feature snapshots — never a later bar than the timestamp in question (that would be lookahead):
- **Entry snapshot**: the `bars` row with the largest `ts <= entry_at`. Good Long examples' entry snapshots inform `long_entry` rules; Good Short examples' entry snapshots inform `short_entry` rules.
- **Exit snapshot**: the `bars` row with the largest `ts <= exit_at`. All Good examples' exit snapshots (regardless of direction) inform `exit` rules. **`exit_at` is nullable and is null on every Bad example** — join only one snapshot for those. A null exit is correct data, not a missing field to report or work around.

### Bad examples are negative constraints, not just absent positives

A `quality = 'Bad'` row is a setup that looked valid and wasn't. The trade was never entered, so it has no exit — `exit_at` is null by design, and the **entry snapshot is the whole of the evidence**. Never infer, substitute, or borrow an exit for one (from a paired example, the session close, or anywhere else): `exit` rules must come only from Good examples.

Use Bad entry snapshots as constraints the rules must *fail*: a `short_entry` rule set that fires on a Bad Short entry is wrong regardless of how well it fits the Good ones. After drafting the rules from the Good examples, evaluate them against every Bad entry snapshot of the same direction; if one fires, tighten a threshold until it doesn't — while confirming the Good examples still pass.

**Matched pairs are the highest-value data here.** When a Bad and a Good example share a direction, `strategy`, and `key_level` and sit minutes apart, the difference between their entry snapshots is close to a controlled experiment: nearly everything is held constant, so whichever feature separates them is likely the real discriminator. Look for the pairing explicitly, and say in `summary` which feature separated them.

If **no** feature cleanly separates a Bad entry from the Good ones, say so plainly rather than inventing a threshold that happens to split them — with this few examples, a boundary drawn between two nearby points is almost certainly fitting noise. An honest "these two look alike on the stored features" is a real finding, and usually means the distinguishing information isn't in the feature set yet.

### Treat the timestamps and levels as approximate — they are eyeballed off a chart

**This is the most important thing to understand about `training_examples`.** The trader reads these off a chart by eye; they are not tick-accurate records of executed trades. `entry_at`, `exit_at`, and `key_level` mark roughly where a setup was, not precisely when or at what price it was taken. A real example: a `key_level` recorded as `738` when the actual opening-range low was `738.72`, and an exit given as "10:50, or preferably 12:17" — a 87-minute spread the trader considered acceptable either way.

So do not fit rules to the exact minute or the exact price. Concretely:

- **Characterize a window, not a bar.** Look at roughly **±5 minutes** around `entry_at` / `exit_at` and describe what was true across that neighborhood — a condition that holds through the window is real, one that holds only on the labeled bar is noise. The single-bar snapshots above are the anchor for the window, not the sole evidence.
- **Never derive a threshold from one labeled bar's value.** If `rsi14` was 37.8 at the labeled entry, that is not evidence for `rsi14 < 37.8`. Take the range across the window and across all examples of the same `strategy`, then leave margin outside it.
- **Round `key_level` generously.** Treat it as "the level near this price," within a few tenths. It is often a round number standing in for a precise high/low.
- **A missed exit by several minutes is not a labeling error.** If the trader gave two acceptable exits, any rule firing between them is correct. Don't tune to whichever one is in the row.
- **Say so in `summary`** when the examples are too few or too loose to support a tight threshold, and keep `confidence` low. Precision the labels cannot justify is worse than an honest wide rule.

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
| `dist_vwap_bps` | distance of close from the session VWAP, in bps. Anchored at the 9:30 RTH open and reset daily; premarket volume is excluded from the anchor |

All `dist_*`/`ret_*`/`range_bps`/`ema_spread_bps` features are causal — computed only from information available at or before that bar (no lookahead). Opening-range features use a running high/low while the window is still forming, then hold the finalized value for the rest of the session. VWAP accumulates through the current bar only, so it likewise never sees the future.

## Targets
| target | meaning |
|---|---|
| `fwd_5m_bps` / `fwd_10m_bps` / `fwd_15m_bps` | forward return N minutes ahead, in bps, same-session only |
| `mfe_10m_bps` | max favorable excursion (long side) over the next 10 minutes, in bps |

## PineScript generation
Every entry-model run also produces a complete TradingView Pine Script v5 indicator implementing the same `long_entry`/`short_entry`/`exit` rules, so the trader can paste it straight into TradingView. Requirements:
- `//@version=5`, `indicator("Smaug Entry Model", overlay=true)`.
- Expose every rule threshold as an `input.float`/`input.int` (with the synthesized value as the default) so the trader can tune it without waiting for a new pasted script.
- Recompute each referenced feature from Pine primitives, using the same formulas as `compute_features()`:
  - `rsi14` → `ta.rsi(close, 14)`; `ema9`/`ema21` → `ta.ema(close, 9)`/`ta.ema(close, 21)`.
  - `ema_spread_bps`, `dist_ema9_bps`, `dist_ema21_bps`, `ret_1m_bps`, `ret_5m_bps`, `range_bps`, `body_ratio` — same algebra as the Python formulas above, computed directly from `close`/`open`/`high`/`low` and `close[1]`/`close[5]`.
  - `dist_prev_day_high_bps` / `dist_prev_day_low_bps` — previous session's RTH high/low via `request.security(syminfo.tickerid, "D", high[1])` / `low[1]`.
  - `dist_or5_*` / `dist_or15_*` — opening-range high/low tracked with a `var` that resets at each new session and updates for the first 5/15 minutes of RTH, then holds.
  - `dist_vwap_bps` — `ta.vwap` (session-anchored and daily-reset by default in Pine, matching the Python anchor), then `(close - ta.vwap) / close * 10000`. One of the few features that translates exactly rather than approximately, provided the chart is set to regular-hours data — extended-hours charts fold premarket volume into the anchor and will drift from the Python value.
  - `vol_z` — exact minute-of-day historical mean/std isn't practical in Pine; approximate with a rolling z-score (e.g. `(volume - ta.sma(volume, 20)) / ta.stdev(volume, 20)`) and add a comment noting it's an approximation, not an exact match to the Python calc.
  - `dist_premkt_*` — only replicate if the chart has extended-hours data available; otherwise add a comment noting the limitation rather than guessing.
- Self-contained — no external requests beyond `request.security` for prior-session levels.
- No `alertcondition()` calls — visual-only indicator, not wired to TradingView alerts.

### Static fragments — fetch verbatim, never regenerate
Candlestick pattern detection, RSI, and the entry/exit marker convention are deterministic — they don't need to change day to day, so they're NOT something to regenerate from this prose spec. They're hand-authored, version-controlled Pine fragments in the `pinescript/` folder of the Smaug repo (see `pinescript/README.md` for the full contract). Fetch each raw file and use its exact text — do not paraphrase, retype, or "improve" it from memory. Assemble the final script by concatenating, in this exact order, each preceded by a `// === <filename> ===` comment:

1. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/header.pine`
2. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/rsi_9_21.pine`
3. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/candles_1.pine`
4. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/candles_2.pine`
5. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/candles_3.pine`
6. **The generated block** — the only dynamic part, described below.
7. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/markers.pine`

`header.pine` must be first (`//@version=5` can't have anything before it) and `markers.pine` must be last (it references `longEntry`/`shortEntry`/`exitSignal`, which only exist once step 6 defines them). This is fetch-and-paste-verbatim, not a template — nothing in the fetched files should be edited at generation time.

Before writing `pinescript` to `entry_models`, self-check the assembled text: it must contain exactly one line matching `^//@version=` and exactly one matching `^(indicator|strategy|library)\(`. If either check fails, don't insert a broken script — omit `pinescript` for that row and say so in `summary`.

### Signal markers — fixed convention, never vary this
The chart marks must look and mean the same thing every single day, regardless of how the underlying rule thresholds change. This is `pinescript/markers.pine` (fragment #7 above) verbatim:
```pine
plotchar(longEntry, title="Long Entry", char="L", location=location.belowbar, color=color.green, size=size.tiny)
plotchar(shortEntry, title="Short Entry", char="S", location=location.abovebar, color=color.red, size=size.tiny)
plotchar(exitSignal ? close : na, title="Exit", char="X", location=location.absolute, color=color.orange, size=size.tiny)
```
Where `longEntry`/`shortEntry`/`exitSignal` are the boolean expressions built from that day's `long_entry`/`short_entry`/`exit` rule conditions (`and`-ed together) — defined in the generated block (step 6), consumed here. Never substitute a different shape, color, character, or location — the trader relies on "L below the bar = long, S above the bar = short, X at price = exit" being stable day over day, even as the thresholds behind them evolve.

### Candlestick patterns and RSI
Also fetched verbatim as part of the fragments above — not something to regenerate:
- `candles_1.pine`/`candles_2.pine`/`candles_3.pine` detect a small curated set of 1/2/3-candle patterns (Doji, Hammer, Shooting Star, Marubozu; Bullish/Bearish Engulfing, Harami; Morning/Evening Star, Three White Soldiers/Black Crows) and label them with `label.new()` — gray background, white text, 2-3 char code, `barstate.islast`-gated so a pattern only ever appears on the current/forming candle, never scattered across history.
- `rsi_9_21.pine` plots a 9-period and 21-period RSI. Because the shared indicator is `overlay=true`, RSI initially renders on the price scale — this is a known Pine limitation (one script can't declare mixed panes), not a bug; the trader drags it to its own pane once in TradingView's UI after pasting.

## Entry-model output schema
When asked to synthesize/update the entry model, write a new row to `entry_models` with:
```json
{
  "rules": {
    "long_entry": [{"feature": "name", "op": "<|<=|>|>=", "value": number, "note": "under 15 words"}],
    "short_entry": [...],
    "exit": [...]
  },
  "summary": "3-5 sentences, plain language",
  "confidence": "low|medium|high",
  "bars_analyzed": number,
  "examples_used": number,
  "date_range": [start, end],
  "pinescript": "full Pine Script v5 source, assembled from the pinescript/ static fragments plus the generated rules block — see 'Static fragments' above"
}
```
`bars_analyzed`, `examples_used`, and `date_range` should echo whatever you actually read from `analysis_runs`/`training_examples` — reflect what was really used, not omitted or guessed.
Only use feature names from the table above — never invent one, since these rules get translated mechanically into `pinescript`. If there are zero or very few training examples, say so explicitly in the summary and lean on the regression/decile output instead; confidence must be "low" in that case. Never invent a finding the numbers don't support.
