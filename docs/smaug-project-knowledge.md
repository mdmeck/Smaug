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
- `daily_briefs` — morning brief (econ calendar, earnings, sentiment, bull/bear case), written by the routine: `generated_at, econ (jsonb), earnings (jsonb), sentiment (jsonb), cases (jsonb)`. The `whales` and `squeezes` columns still exist but are retired — see below. Private. One row per user (`user_id` is unique) — overwritten each run via `upsert` with `on_conflict=user_id`, no history kept. Query with a plain `select` for the current one.
  - **The four jsonb columns have a fixed inner shape — match it exactly.** The webapp renders these fields directly, so the shape written here is a UI contract, not free-form notes. It is not validated on write and no build can catch a break: a wrong shape ships to production the moment the upsert lands. Keys not listed below are ignored (they render as nothing); the wrong *type* under a listed key is the dangerous case. Every value below is a **plain string** unless stated otherwise — never a nested object.
    - `econ` — array of `{date, day, event, time_et, impact, forecast, previous}`. **`date` (`YYYY-MM-DD`) is what places the row on the grid**: a dated row lands on the column with that date or on none. `day` is the matching three-letter weekday (`Mon`…`Fri`) and must agree with `date`; it is only used for placement on legacy rows that have no `date`. **The calendar covers the Monday–Friday that contains the run date** — on a weekday run, today's events must be present. A row dated outside that week is dropped and counted in an amber warning on the grid; that warning is deliberate, see the 2026-09-11 note below. `time_et` is `HH:MM` 24-hour Eastern (`08:30`, not `8:30 AM`). `impact` is `high`/`medium`/`low` — only `high` is flagged hot. `forecast`/`previous` are nullable.
    - `earnings` — array of `{date, day, ticker, company, time, note}`. Same `date`/`day` rule. `time` is descriptive (`before open`, `after close`).
    - `sentiment` — `{tone, futures, vix, fear_greed, overnight, summary}`. `tone` must be exactly `bullish`, `bearish`, or `neutral` — it selects the pill's color, and anything else (`cautiously bullish`) falls back to neutral amber. `futures` and `vix` are single display strings (`"+0.16%"`, `"~18.3"`), **not** per-index objects. `fear_greed` is text with the value first — `"56 — Greed"` — because the leading integer positions the gauge needle. `overnight` is the prose recap.
    - `cases` — `{bull, bear, watch}`, where `bull`/`bear` are arrays of strings and `watch` is a single string.
    - `whales` and `squeezes` — **retired from this table.** Both columns still exist and old values are still in them, but nothing writes them and nothing reads them. Whale flow and short-squeeze setups are Erebor's, not Smaug's: they are whole-market single-name screens rather than SPY work, they failed independently of the brief while sharing its run and its report (2026-09-08: the brief succeeded, both of these went out as `[]` because their sources were unreachable, and the webapp rendered that as "Awaiting run"), and `daily_briefs` keeps no history, so a screen written here could never be scored against what its names went on to do. They now live in `erebor_candidates` under `kind` `whale` and `squeeze`, written by a separate routine — see `docs/erebor-routine-prompt.md` and the Erebor section of `webapp/supabase/schema.sql`. Do not write them here.
    - Regression note: on 2026-08-20 a run wrote `futures` as `{dow, spx, nasdaq100}` and `fear_greed` as `{as_of, label, value}`, and renamed `overnight` to `overnight_summary`. The first two crashed the Morning Brief with React error #31, which unmounted the **entire** SPA — every tab, not just the brief. The webapp now coerces objects to text and isolates each panel behind an error boundary, so this specific break is survivable, but the shapes above are still the contract: coercion produces a flattened line, not the layout the panel was designed for.
    - Regression note: the same 2026-08-20 run wrote `econ` and `earnings` as bare arrays (correct per this spec) while the week calendar was still reading them as `{events: [...]}` / `{earnings: [...]}` wrappers, so a complete brief rendered as five empty days with a fresh LAST RUN stamp — the failure is silent, since there is no error, just "no events" everywhere. The webapp now accepts either shape, normalizes `day` (`Wednesday`/`wed` bucket as `Wed`), and reads `time` in both the descriptive and BMO/AMC forms. Bare arrays with three-letter days remain the contract.
    - Regression note: on 2026-09-11 — a Friday, August CPI at 8:30 — the run wrote the *following* week's calendar (Empire State, Retail Sales, the Sept 16 FOMC) and nothing for Friday. The prompt said "the week", which on a Friday reads as the week ahead. Every row carried a plausible weekday, so the grid placed next week's FOMC on that week's Wednesday and rendered CPI day empty under a fresh LAST RUN stamp; no error, no empty state, nothing to notice. Two changes: rows now carry a `date`, and the grid places dated rows by date only, surfacing any that fall outside the rendered week as a count rather than misfiling them; and the routine prompt anchors the calendar to the week containing today and requires today's prints on a weekday run. A weekday name alone can never say *which* Wednesday — that is the design flaw, not the prompt wording.
- `entry_models` — each row is one AI-synthesized entry/exit rule set + PineScript indicator, written directly by the routine (append-only, so the trader can see the model evolve day over day). Private: `generated_at, bars_analyzed, examples_used, date_range, rules (jsonb), summary, confidence (low/medium/high), pinescript (text)`.
- `journal_entries` — the trader's own trade log, one row per round trip: `date, ticker, direction (Long/Short), setup, result, notes`. Private. **This is a record of what was actually traded**, unlike `training_examples`, which is a record of what *should* have been traded — the two are different things and a trade can appear in one, both, or neither. `result` is free text the trader types (`+30`, `-$12.50`, `+1.5R`); parse the leading number and skip anything unparseable rather than guessing. `setup` is free text and is often empty, especially on rows bulk-imported from a broker export — an empty `setup` means "not recorded", never "no setup".
- `trade_feedback` — your coaching critique of `journal_entries`, written by the routine and shown on the Dashboard: `generated_at, observations (jsonb array), strengths (jsonb array), risks (jsonb array), focus (text), trades_reviewed (int), date_range (jsonb)`. Private. One row per user (`user_id` is unique) — upsert with `on_conflict=user_id`, no history. The column shape matches what the Journal tab's copy/paste flow asks claude.ai for, so both sources are interchangeable.

### Reviewing the journal

`trade_feedback` is a critique of execution, which is a different question from the one `entry_models` answers. The model asks "where was the edge?"; this asks "did the trader take it, and take it well?" Keep them separate — do not let journal performance quietly retune the rules, or a bad week of discipline will get encoded as a threshold change.

What to actually look for, in rough order of value:

- **Distribution, not average.** Win rate and net P&L hide the thing that usually matters. Compare average win against average loss, and check whether a single day or a single repeated ticker accounts for most of the damage. A 59% win rate that still loses money is a sizing-and-exits story, not a setup story.
- **Repeat entries on one instrument in one session.** Several round trips on the same strike the same day, each worse than the last, is the signature of re-entering a losing idea. Name the date and the count.
- **The two logs against each other.** Where a `journal_entries` date overlaps a `training_examples` date, ask whether the trades taken were the setups labeled. Trading well on days with no labeled setup — or ignoring a labeled Good setup — are both findings.
- **What the trader already said.** `notes` is their own reasoning; a mistake they name repeatedly in their own words is stronger evidence than anything inferred from the numbers.

Say plainly when the data can't support a claim. With `setup` empty there is no setup discipline analysis to give — say so in `observations` instead of substituting something you can measure for something you can't. Be specific and quantitative, cite dates and figures, and skip encouragement that isn't backed by a number. Never give position-sizing prescriptions or anything that reads as financial advice: describe what the record shows and what to watch, not what to trade.

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
| `or15_width_bps` | width of the 15-minute opening range, in bps of close. Day-type context — a break out of a 5 bps range is a different event from a break out of a 40 bps one |
| `dist_swing_high_bps` | distance of close from the most recent **confirmed** swing high, in bps. `> 0` means structure is broken to the upside |
| `dist_swing_low_bps` | distance of close from the most recent confirmed swing low, in bps. `< 0` means structure is broken to the downside |
| `structure_dir` | `+1` uptrend (higher high **and** higher low), `-1` downtrend (lower high **and** lower low), `0` mixed. Null until two of each pivot have confirmed |
| `bos` | break of structure: `+1` broke the swing high with an up/neutral trend, `-1` broke the swing low with a down/neutral trend, else `0` |
| `choch` | change of character: `+1` broke the swing high **while** `structure_dir` was negative, `-1` broke the swing low while it was positive, else `0` |

### Candlestick patterns (binary `pat_*` features)
Each is `1` on a bar where the pattern completed, `0` otherwise, and **null** where the lookback would cross a session boundary (`close[1]` at 9:30 is yesterday's 15:59 — an "engulfing" built across the overnight gap is an artifact, not a pattern). They are independent booleans, not one categorical code: more than one can be `1` on the same bar.

| feature | candles | meaning |
|---|---|---|
| `pat_doji` | 1 | body ≤ 10% of range — indecision |
| `pat_marubozu_bull` / `pat_marubozu_bear` | 1 | full body, wicks ≤ 5% of body |
| `pat_hammer` | 1 | lower wick ≥ 2× body, upper wick ≤ ½ body, body ≤ 40% of range |
| `pat_shooting_star` | 1 | the mirror image — upper wick ≥ 2× body |
| `pat_bull_engulf` / `pat_bear_engulf` | 2 | current body fully covers the prior, opposite-colored body |
| `pat_bull_harami` / `pat_bear_harami` | 2 | current body contained inside the prior, opposite-colored body |
| `pat_morning_star` / `pat_evening_star` | 3 | long candle, small-bodied middle, strong opposite close past the first candle's midpoint. **Gap-free approximation** — the textbook version needs overnight gaps between candles, which a continuous 1-minute RTH chart never produces |
| `pat_three_soldiers` / `pat_three_crows` | 3 | three consecutive same-direction candles, each opening inside the prior body |

These formulas are transcribed from `pinescript/candles_1.pine` / `_2` / `_3` into `compute_features()` and must stay identical in both places — the Python is what scores the edge, the Pine is what fires on the chart, and if they drift the analysis measures something the indicator never draws.

**How they're reported.** A binary that fires on 0.3% of bars can't be split into deciles (`qcut` collapses a mostly-zero column) and its correlation is noise, so patterns are excluded from the decile tables and summarized separately in a `patterns` block per target: `n`, `base_rate`, and then either `avg_move_bps` / `baseline_bps` / `edge_bps` (bps targets) or `outcome` / `baseline` (label targets, both in the same shape as the top-level `outcome`). Read `edge_bps`, or `outcome.win_rate` against `baseline.win_rate` — a pattern's raw win rate means nothing without the all-other-bars number next to it. Patterns that fired fewer than 30 times in the window are omitted rather than reported with a wide error bar.

All `dist_*`/`ret_*`/`range_bps`/`ema_spread_bps` features are causal — computed only from information available at or before that bar (no lookahead). Opening-range features use a running high/low while the window is still forming, then hold the finalized value for the rest of the session. VWAP accumulates through the current bar only, so it likewise never sees the future. Swing pivots are the subtlest case: a bar cannot be known to be a local high until `SWING_RIGHT` later bars have failed to exceed it, so the confirmed pivot is deliberately delayed by that many bars before it becomes visible to any feature.

## Targets
| target | meaning |
|---|---|
| `fwd_5m_bps` / `fwd_10m_bps` / `fwd_15m_bps` | forward return N minutes ahead, in bps, same-session only. **Path-blind** — see the barrier targets below |
| `fwd_max_10m_bps` / `fwd_min_10m_bps` | the best and worst price reached over the next 10 minutes, in bps from the current close. Deliberately **direction-neutral**: for a long, `fwd_max` is the favorable excursion and `fwd_min` the adverse one; for a short they swap and flip sign, so one pair serves both. `fwd_max` is always ≥ 0 and `fwd_min` ≤ 0. Replaces the old long-only `mfe_10m_bps`, which was the same number under a name that presumed a direction — `analysis_runs` rows written before 2026-09-07 still carry `mfe_10m_bps` |
| `barrier_long_10m` / `barrier_short_10m` | **first-touch (triple-barrier) label**: `+1` the profit target was touched first, `-1` the stop was, `0` neither inside the window. `+1` always means *this trade won*, for both directions. Barriers are ATR-scaled — target `BARRIER_TARGET_ATR` (3.0) and stop `BARRIER_STOP_ATR` (1.5) multiples of a 14-period ATR on 1-minute bars, i.e. 2:1 reward:risk — so a label means the same thing in a quiet tape as a fast one |

### Why the barrier targets exist

A forward return says where price ended up and nothing about how it got there. An entry that bled 8 bps against you before running 20 bps in your favor scores **identically** to one that ran straight there — but the first one stops you out and the second is the trade. The barrier labels are the only targets that know a trade can be stopped out before it is right, which makes them the ones to model against for entry timing.

Three things to know when reading them:

- **They are categorical, not a move in bps.** Each target in `analysis_runs` now carries a `kind` field, `"bps"` or `"label"`. For a `label` target the decile table's `avg_move_bps` key is the mean label — roughly (win rate − loss rate) — not a basis-point figure, and the OLS `r2` is a linear probability model, so read the `outcome` block instead.
- **`outcome`** (present only on label targets) reports `wins`/`losses`/`timeouts`, `win_rate` (of *resolved* bars, excluding timeouts), `resolved_rate`, `expectancy_r` (average outcome in units of the stop, timeouts scored flat), and `breakeven_win_rate`. Compare `win_rate` against `breakeven_win_rate` — at 2:1 the breakeven is 0.333, and beating it is the whole question. `expectancy_r` ignores commissions, slippage, and the option-premium path, so treat it as a ranking statistic between setups, never as a P&L forecast.
- **Ties inside one bar resolve to the stop.** When a single bar's range spans both barriers, OHLC cannot say which came first, so the label is made pessimistic for the trade being modeled. Resolving the other way would inflate every win rate the pipeline reports.

**The stop multiple has a floor.** ATR here is the average range of a *single* 1-minute bar (~4–5 bps on SPY), so a stop under about 1× ATR sits inside one bar's own noise and gets touched by nearly every bar. At 1.0/0.5 the labels resolve 99.9% of the time and the win rate reads ~0.40 against a 0.333 breakeven purely from intrabar granularity — that looks like edge and is not. The 3.0/1.5 defaults converge on the driftless 1/(1+RR) value on random-walk data, which is the sign the labels are measuring the tape rather than the bar size. Widen the two together to keep the 2:1 ratio `expectancy_r` assumes.

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
  - `or15_width_bps` — `(orHigh - orLow) / close * 10000`, reusing the same opening-range `var`s as `dist_or15_*`.
  - `pat_*` — **do not recompute these either.** `candles_1/2/3.pine` (fragments #3-5) already define every one of them; reference the fragment identifier and translate `pat_x >= 1` to the bare boolean (and `pat_x < 1` to `not`):

    | feature | Pine identifier | feature | Pine identifier |
    |---|---|---|---|
    | `pat_doji` | `c1_isDoji` | `pat_bull_harami` | `c2_isBullHarami` |
    | `pat_marubozu_bull` | `c1_isMarubozuBull` | `pat_bear_harami` | `c2_isBearHarami` |
    | `pat_marubozu_bear` | `c1_isMarubozuBear` | `pat_morning_star` | `c3_isMorningStar` |
    | `pat_hammer` | `c1_isHammer` | `pat_evening_star` | `c3_isEveningStar` |
    | `pat_shooting_star` | `c1_isShootingStar` | `pat_three_soldiers` | `c3_isThreeSoldiers` |
    | `pat_bull_engulf` | `c2_isBullEngulf` | `pat_three_crows` | `c3_isThreeCrows` |
    | `pat_bear_engulf` | `c2_isBearEngulf` | | |

    A pattern rule takes no `input.float` threshold — there's nothing to tune, the value is always 1. The if/else chain inside those fragments only decides which *label* gets drawn when several patterns match at once; the booleans themselves are independent, so `and`-ing two of them is legal (just usually empty).
  - `dist_swing_high_bps`, `dist_swing_low_bps`, `structure_dir`, `bos`, `choch` — **do not recompute these.** `pinescript/structure.pine` (fragment #6) already defines them as `st_distSwingHighBps`, `st_distSwingLowBps`, `st_structureDir`, `st_bos`, `st_choch`. Reference those names directly. Pivot confirmation lag is a silent-lookahead trap and the fragment is the version that has been verified against the Python; re-deriving it in generated code is how that gets quietly broken.
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
mk_long = longEntry and not longEntry[1] and barstate.isconfirmed
mk_short = shortEntry and not shortEntry[1] and barstate.isconfirmed
mk_exit = exitSignal and not exitSignal[1] and barstate.isconfirmed
plotchar(mk_long, title="Long Entry", char="L", location=location.belowbar, color=color.green, size=size.tiny)
plotchar(mk_short, title="Short Entry", char="S", location=location.abovebar, color=color.red, size=size.tiny)
plotchar(mk_exit ? close : na, title="Exit", char="X", location=location.absolute, color=color.orange, size=size.tiny)
```
Where `longEntry`/`shortEntry`/`exitSignal` are the boolean expressions built from that day's `long_entry`/`short_entry`/`exit` rule conditions (`and`-ed together) — defined in the generated block (step 6), consumed here. Never substitute a different shape, color, character, or location — the trader relies on "L below the bar = long, S above the bar = short, X at price = exit" being stable day over day, even as the thresholds behind them evolve.

**Define those three as plain state, not as events.** `markers.pine` already converts them to edge triggers — do not add `ta.crossover`, `not …[1]`, or any other once-only guard to the generated block, or the signal will require two simultaneous transitions and almost never fire.

**Closed bars only, and that is also `markers.pine`'s job.** The `barstate.isconfirmed` gate above is why a marker prints once, at the candle's close, instead of appearing and vanishing as the bar ticks — the rules are re-evaluated on every tick, and an intrabar signal on a 1-minute chart moves too much to act on. Historical bars are all confirmed, so chart history is unaffected. Don't add a bar-state gate of your own to the generated block either; the consequence to keep in mind when writing rules is that the signal describes the last *closed* candle, so the realistic fill is the next bar's open.

### Rules are state predicates, and that has a failure mode

Every rule list is `and`-ed and evaluated per bar, so it describes a *condition the market is in*, not a moment. "Close above EMA9, EMA9 above EMA21, RSI above 48" is a description of an uptrend — it is true for as long as the uptrend lasts, not once at its start. Edge detection in `markers.pine` turns that into one signal per move, but it cannot rescue a rule set that is simply too loose.

So check the **firing rate**, not just satisfiability. For each list, count the bars it holds on across the analyzed window:

- **0%** — contradictory or impossibly tight. Already covered by the satisfiability check.
- **over ~30%** — too loose to be a signal. A condition that describes a third of the session is describing the market, not an entry. This has happened: an `exit` list of `range_bps >= 1.5` and `vol_z >= -1` held on **382 of 390 bars (98%)** of 2026-07-31, and a `long_entry` list held on 167 (43%).
- **a few percent, clustered** — what a real setup looks like.

Report the per-list firing rate in `summary` every run, as a percentage of bars and as signals-per-session after edge detection. The trader expects **a handful of entries per session**, so a list producing more than ~10 edge-triggered signals a day is still too loose even if the raw rate looks acceptable. Tighten the discriminating threshold — the one the training examples actually support — rather than adding more conditions, since each extra `and` narrows the window without making the rule more specific to the setup.

### Candlestick patterns and RSI
Also fetched verbatim as part of the fragments above — not something to regenerate:
- `candles_1.pine`/`candles_2.pine`/`candles_3.pine` detect a small curated set of 1/2/3-candle patterns (Doji, Hammer, Shooting Star, Marubozu; Bullish/Bearish Engulfing, Harami; Morning/Evening Star, Three White Soldiers/Black Crows) and label them with `label.new()` — gray background, white text, 2-3 char code, `barstate.islast`-gated so a pattern only ever appears on the current/forming candle, never scattered across history. That gate is on the **label** only. The `c1_`/`c2_`/`c3_` booleans themselves are live on every bar and are what the generated rules block consumes for a `pat_*` feature — so a pattern can be part of an entry condition while its bubble is still only drawn on the newest candle.
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
Only use feature names from the tables above — never invent one, since these rules get translated mechanically into `pinescript`. A `pat_*` pattern is a binary, so it takes the same triple shape with a fixed value: `{"feature": "pat_bull_engulf", "op": ">=", "value": 1}` means the pattern fired on this bar, `"op": "<"` means it didn't. Prefer patterns that the `patterns` block actually separates from its baseline — a pattern with no measured edge is a condition that only narrows the firing rate. If there are zero or very few training examples, say so explicitly in the summary and lean on the regression/decile output instead; confidence must be "low" in that case. Never invent a finding the numbers don't support.
