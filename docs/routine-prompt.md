# Smaug daily routine — prompt

Paste this as the scheduled routine's prompt. It is deliberately a *task list*
that fetches its specification at run time rather than embedding it, so editing
`docs/smaug-project-knowledge.md` and committing is enough to change routine
behavior — nothing needs re-pasting. Keep this file and the routine in sync if
you edit one by hand.

---

=== PART 0: LOAD THE SPEC ===

Before anything else, fetch:
`https://raw.githubusercontent.com/mdmeck/Smaug/main/docs/smaug-project-knowledge.md`

That document is the authoritative specification for everything below — table schemas, feature and target definitions, how to join training examples, and the PineScript assembly contract. This prompt is the task list; that document is the reference. Where the two disagree, the fetched document wins.

If the fetch fails, STOP and report it. Do not proceed from memory or from a cached copy — this spec changes regularly, and running against a stale reading has previously written broken data and silently failed for days.

**Critical, and the single most common failure:** your Supabase connector authenticates as the `postgres` role, so `auth.uid()` evaluates to NULL. Reads work fine (RLS is bypassed), but every INSERT into `daily_briefs`, `entry_models`, or `training_examples` must pass `user_id` explicitly or it fails a NOT NULL violation:

```
user_id = c0b48756-5f94-4862-886a-8ecdb7099ef6
```

Omitting it does not error visibly at the reasoning level — reads keep looking healthy while nothing is ever written. If a write fails, report the error; never report success you did not verify.

=== PART 1: MORNING BRIEF ===

Determine today's date and the current/upcoming trading week (Mon-Fri) yourself. Search the web and research:

1. **ECON CALENDAR**: US economic calendar for the week — the schedule shown on sites like Forex Factory. USD events only, medium and high impact only (Fed speakers, CPI, PPI, jobs data, PMI, FOMC, auctions, consumer sentiment, etc). Max 18 events across the week, each day's events sorted by time.
2. **EARNINGS**: Major companies reporting earnings this week, with special focus on tech and semiconductor companies plus any mega-caps that move the S&P 500. Max 12 across the week, empty array if nothing major. Always include the ticker symbol, not just the company name.
3. **SENTIMENT**: Current pre-market / overnight US market sentiment for today — S&P 500 futures direction and %, VIX level, CNN Fear & Greed index, and any major overnight headlines moving markets.
4. **BULL/BEAR CASE**: Latest news and analyst commentary relevant to SPY / the S&P 500 today. Build a same-day bull case and bear case, each point under 15 words.

Using your Supabase connector, upsert into `daily_briefs` with conflict target `user_id` (the unique constraint is in place, so `on_conflict=user_id` resolves correctly):

- `user_id`: the UUID from Part 0 — required, do not omit
- `econ`: `{"events": [{"day": "Mon|Tue|Wed|Thu|Fri", "time_et": "e.g. 8:30 AM", "event": "name", "impact": "high|medium", "forecast": "or empty string", "previous": "or empty string"}]}`
- `earnings`: `{"earnings": [{"day": "Mon|Tue|Wed|Thu|Fri", "ticker": "e.g. AAPL", "company": "name", "time": "BMO|AMC", "note": "why it matters, under 8 words"}]}`
- `sentiment`: `{"tone": "bullish|bearish|neutral", "futures": "e.g. ES +0.3%", "vix": "e.g. 18.6", "fear_greed": "e.g. 62 - Greed", "overnight": "one line on overnight action", "summary": "2 sentences max on the tape's tone"}`
- `cases`: `{"bull": ["point 1", "point 2", "point 3"], "bear": ["point 1", "point 2", "point 3"], "watch": "single most important thing to watch today, one line"}`
- `generated_at`: current timestamp — this is when the brief was last refreshed, not the row's original insert time

This table holds exactly one row, overwritten each run. No history is kept.

=== PART 2: REASSESS THE ENTRY MODEL ===

1. Fetch `https://raw.githubusercontent.com/mdmeck/Smaug/main/smaug_pipeline.py`. `compute_features()` is the ground truth for what every feature column means, `compute_targets()` for the targets. Cross-reference against the feature/target tables in the spec you fetched in Part 0.
2. Query `analysis_runs` — `order by generated_at desc limit 1` — for the current regression/correlation output (correlations, standardized coefficients, r2_train/r2_test, deciles, per target).
3. Query `training_examples` for the trader's labeled examples. May be empty. Each row is a full round trip: `entry_at`, `exit_at`, `ticker`, `direction` (Long/Short), `quality` (Good/Bad), `strategy`, `key_level`, `notes`, `analysis_notes`. Re-read these fresh every run — the trader edits and deletes them, so yesterday's set may not still apply.

Join two feature snapshots per example from `bars` exactly as the spec describes — never a later bar than the timestamp in question, which would be lookahead. Sessions referenced by an example are exempt from the bars retention window, so old examples remain joinable; a missing bar is a problem to report, not a stale example to skip.

**Treat the labels as approximate.** The trader reads these off a chart by eye; they are not tick-accurate execution records. Follow the spec's guidance in full — characterize a roughly ±5 minute window around each timestamp rather than the single labeled bar, never derive a threshold from one bar's value, and round `key_level` generously. Precision the labels cannot justify is worse than an honest wide rule.

Group by `strategy` where the examples support it (currently `Break and Retest`, `Opening Range Break`, `Bounce` — plain text, so treat an unfamiliar value as new rather than as an error). Rules blended across strategies are weaker than rules that respect the split; say in `summary` which strategies the examples covered. Where `key_level` is present, compare `close - key_level` at the entry snapshot against the `dist_*` features to identify which stored level the trader was actually watching.

Combine the regression signal with the labeled examples to build `long_entry`, `short_entry`, and `exit` rules. Only use feature names that actually appear in `analysis_runs` / `compute_features()` — never invent one, since these are translated mechanically into the indicator. `dist_vwap_bps` (session VWAP distance) is part of the feature set; use it if present in the data you read, and don't assume it exists if it isn't.

**Verify every rule list is satisfiable before writing it.** Conditions within a list are AND-ed together. A list containing mutually exclusive conditions on the same feature — e.g. `rsi14 >= 70` alongside `rsi14 <= 41` — can never fire, which silently produces a model that never signals in the app or in TradingView. This has happened. If you want alternatives, express them as a band that can actually hold (`ret_1m_bps < 2` with `ret_1m_bps > -2`), or scope the condition so it isn't self-contradictory. Sanity-check each list against real bars: if it fires zero times across the analyzed window, say so in `summary` rather than shipping it silently.

If there are no training examples, say so explicitly in the summary and derive rules from the regression alone; `confidence` must be `low`. With few examples, still lean `low` or `medium` and say why. Never invent a finding the numbers don't support.

Also produce a complete TradingView Pine Script v5 indicator. Follow the spec's "PineScript generation" and "Static fragments" sections exactly: fetch each file under `pinescript/` verbatim from raw GitHub and concatenate them in the documented order around your generated rules block. Do not paraphrase, retype, or improve the fetched fragments. Before writing, self-check that the assembled text contains exactly one line matching `^//@version=` and exactly one matching `^(indicator|strategy|library)\(` — if either check fails, omit `pinescript` and say so in `summary` rather than storing a broken script. This is not a separate step; it is one column in the row below.

Insert ONE new row into `entry_models` with all of:

- `user_id`: the UUID from Part 0 — required, do not omit
- `bars_analyzed`: from `analysis_runs`
- `examples_used`: count of `training_examples` actually used
- `date_range`: `date_range` from `analysis_runs`
- `rules`: `{"long_entry": [{"feature": "name", "op": "<"|"<="|">"|">=", "value": number, "note": "under 15 words"}], "short_entry": [...], "exit": [...]}` — max 5 conditions per list
- `summary`: 3-5 sentences, plain language
- `confidence`: `"low"|"medium"|"high"`
- `pinescript`: the full Pine Script v5 source, as a single string

Leave `generated_at` to its default. This table is append-only — always insert, never update.

=== PART 3: RECORD WHAT YOU FOUND ===

For each training example that materially informed the rules, write your findings back to that row's `analysis_notes` via an `update` on its `id` (no `user_id` needed on an update — the row already has one). Note which features actually distinguished it, whether it agreed with others of its `strategy`, and anything that makes it an outlier.

Never write into `notes` — that field is the trader's own reasoning and is ground truth. When reading examples, treat only `notes` as evidence. If you were to write inferences into the field you also read as ground truth, later runs would learn from earlier runs' conclusions and the labels would stop meaning anything.

=== FINALLY ===

Report what you actually did: which tables you wrote, the row counts, and any step that failed or was skipped. If a write errored, say so plainly with the error — do not report success you did not verify.
