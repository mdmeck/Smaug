# Smaug daily routine — prompt

## What to actually paste into the routine

Paste **only this**, once. It fetches everything else at run time, so editing
this file or the spec and committing is enough to change routine behavior —
there is nothing to re-paste, ever:

```
You are the Smaug daily routine.

Fetch https://raw.githubusercontent.com/mdmeck/Smaug/main/docs/routine-prompt.md
and carry out the task list under the "Task list" heading, in order, exactly as
written. That document is the instructions; this message only points at them.

If the fetch fails, STOP and report it. Do not improvise the routine from
memory — writing to the wrong tables is worse than not running.
```

### Why the bootstrap exists

The task list below used to be pasted in full, with only the *spec* fetched at
run time. That fixed spec staleness but not task staleness: on 2026-08-04 the
routine wrote `daily_briefs` and `entry_models` normally while `trade_feedback`
sat empty, because PART 3 had been added to this file a few days earlier and
the pasted copy predated it. Reads and most writes looked healthy, so nothing
surfaced the gap — the same asymmetric failure as the `user_id` bug. Now the
only thing that can go stale is the one paragraph above, which contains no
task-specific detail at all.

---

## Task list

=== PART 0: LOAD THE SPEC ===

Before anything else, fetch:
`https://raw.githubusercontent.com/mdmeck/Smaug/main/docs/smaug-project-knowledge.md`

That document is the authoritative specification for everything below — table schemas, feature and target definitions, how to join training examples, and the PineScript assembly contract. This prompt is the task list; that document is the reference. Where the two disagree, the fetched document wins.

If the fetch fails, STOP and report it. Do not proceed from memory or from a cached copy — this spec changes regularly, and running against a stale reading has previously written broken data and silently failed for days.

**Critical, and the single most common failure:** your Supabase connector authenticates as the `postgres` role, so `auth.uid()` evaluates to NULL. Reads work fine (RLS is bypassed), but every INSERT into `daily_briefs`, `entry_models`, `trade_feedback`, or `training_examples` must pass `user_id` explicitly or it fails a NOT NULL violation:

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
5. **WHALE ACTION**: Where outsized options activity showed up in the last session, across the whole market — not just SPY. Fetch these two, in this order; they were checked and are the free, no-login, non-JavaScript sources that actually return data to a fetch:
   - `https://www.marketbeat.com/market-data/unusual-call-options-volume/` — bullish side
   - `https://www.marketbeat.com/market-data/unusual-put-options-volume/` — bearish side

   Each is a dated table of ticker, current price, option volume, average volume, and percent increase. **Record the date printed on the page** — it is the session the numbers describe, and on a Monday it is the previous Friday. That date goes in `as_of` on every row you take from that page, and it is what the panel displays; do not substitute today's date, and do not leave it out because the run date is "close enough". **Take the rows as ranked and published — no market-cap or price filter.** A $0.40 stock at 900% of average is what the source flagged, and second-guessing the screen means the panel stops matching a table the trader can check. Take up to 8 names total across the two pages. Copy `volume` and `avg_volume` as numbers, exactly as printed — do not convert the percent increase into a multiple yourself; the panel does that.

   Then fetch `https://www.cboe.com/us/options/market_statistics/daily/` for the session's market-wide put/call ratios (total, index, equity) and call vs put volume. That's exchange-primary data and is the lean each name should be read against — use it to write the `note` fields, and say so when a name's flow runs opposite the tape.

   Optionally search the web for news on a name to explain *why* the volume showed up (earnings, guidance, M&A, an analyst move) and put that in `note`. If you find nothing, leave `note` empty — never invent a catalyst, and never write a dollar premium figure: no free source publishes per-print premium, and an estimate here is a number the trader would act on.

   If both MarketBeat pages fail, write `whales` as an empty array and say so in your final report. Do not substitute a paid or JavaScript-only scanner, and do not reconstruct the list from memory.

6. **ROARING KITTY**: Names where a short squeeze may be setting up, across the whole market. Fetch these two, in this order; they were checked and are the free, no-login, non-JavaScript sources that actually return data to a fetch:
   - `https://www.tipranks.com/screener/most-shorted-stocks` — the ranking. It is already sorted by short interest as a percent of float, descending, and prints its own settlement date ("Short interest data as of ..."). Take the top 8 **as published — no market-cap, price, or sector filter.** Copy `short_percent_float` and `price` as numbers.
   - `https://www.marketbeat.com/short-interest/` — enrichment only. Its "Days to Cover" column is the number you want; match it to the TipRanks names **by ticker** and fill `days_to_cover`. Leave `days_to_cover` out for any name that isn't on this page — do not compute or estimate it. **Do not rank from this page**: it is sorted by dollar volume sold short, so it leads with SPY, QQQ, IWM, XBI and TLT. Those are ETFs, they create and redeem shares on demand, they cannot squeeze, and their "% of float" readings (XBI showed 118%) are an artifact of that mechanism rather than a signal.

   **Record the settlement date each page prints** — MarketBeat puts it right in the column header ("Shares Sold Short (8/14/2026)"). That date goes in `as_of` on every row, formatted `YYYY-MM-DD`. It is normally two to four weeks old and that is correct: exchange short interest settles on the 15th and at month end and publishes about eight business days later. **Never substitute today's date to make the panel look current** — the trader reads that date to decide whether the number still means anything. Put the date of the price quote in `price_as_of` (usually today) — the panel ages the two separately on purpose.

   Do not write a borrow fee, a float-utilization figure, or a squeeze score. No free source publishes a trustworthy one, and an invented borrow fee is a number the trader would size a position on. Optionally search the web for why a name is heavily shorted (a short report, a failed merger, a busted story) and put one line in `note`; if you find nothing, leave `note` empty rather than inventing a thesis.

   **Then the chatter half — this is what makes the panel more than a short-interest table.** Fetch `https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/1` (free, no key, plain JSON; pages 2-6 exist if you need more of the tail). Each result has `ticker`, `mentions`, `mentions_24h_ago`, `upvotes` and `rank`. Also fetch `https://api.stocktwits.com/api/2/trending/symbols.json` (free, no auth) for its 30 trending symbols. **Do not attempt X/Twitter** — as of February 2026 there is no free tier for new developers and reads are billed per post; it is not a free source and is out of scope.

   For each name already in your list, attach a `buzz` object **only if it has 5 or more mentions**. Below that, leave `buzz` off the row entirely — do not write `mentions: 0`, and do not write a buzz object with a body. This floor is not a nicety: on 2026-08-30, 468 of the 534 tickers apewisdom ranked had exactly one mention, and five of the six heavily shorted names that appeared at all had exactly one. One person typing a ticker is not chatter, and rendering it as such is the same error as inventing a borrow fee.

   Then work the other direction, which is the point of the panel: take the names with real chatter (5+ mentions on apewisdom, or present in the StockTwits trending list) and look up each one's short interest at `https://www.marketbeat.com/stocks/{EXCHANGE}/{TICKER}/short-interest/`. Add up to 4 of them to `squeezes` if they carry **10% or more of float short** — these are the loaded-and-loud names, and they are the ones most likely to actually move. Skip anything below that threshold: chatter on a name with no short base is not a squeeze setup. Skip ETFs and crypto tickers (`SPY`, `DIA`, anything ending `.X`) — they cannot squeeze.

   The panel derives its own two sections from whether `buzz` is present, so do not add a section or category field. Expect the loud section to be empty on many days; that is a correct and useful answer, and an empty one is far better than a padded one.

   If both short-interest pages fail, write `squeezes` as an empty array and say so in your final report. If only the chatter sources fail, still write the rows — short interest alone is the panel's baseline — and say which part is missing. Do not substitute a paid, signup-gated, or JavaScript-only screener, and do not reconstruct the list from memory. Two URLs that look right but are not: `marketbeat.com/market-data/short-interest/` and `marketbeat.com/market-data/highest-short-interest-stocks/` both soft-404 into a generic "Public Companies By Market Cap" table — a plausible table of entirely the wrong data. If you land on market-cap columns, you have the wrong page.

Using your Supabase connector, upsert into `daily_briefs` with conflict target `user_id` (the unique constraint is in place, so `on_conflict=user_id` resolves correctly):

- `user_id`: the UUID from Part 0 — required, do not omit
- `econ`: `{"events": [{"day": "Mon|Tue|Wed|Thu|Fri", "time_et": "e.g. 8:30 AM", "event": "name", "impact": "high|medium", "forecast": "or empty string", "previous": "or empty string"}]}`
- `earnings`: `{"earnings": [{"day": "Mon|Tue|Wed|Thu|Fri", "ticker": "e.g. AAPL", "company": "name", "time": "BMO|AMC", "note": "why it matters, under 8 words"}]}`
- `sentiment`: `{"tone": "bullish|bearish|neutral", "futures": "e.g. ES +0.3%", "vix": "e.g. 18.6", "fear_greed": "e.g. 62 - Greed", "overnight": "one line on overnight action", "summary": "2 sentences max on the tape's tone"}`
- `cases`: `{"bull": ["point 1", "point 2", "point 3"], "bear": ["point 1", "point 2", "point 3"], "watch": "single most important thing to watch today, one line"}`
- `whales`: a **bare array** (not wrapped in an object) — `[{"ticker": "AFRM", "lean": "bullish|bearish|mixed", "volume": 34696, "avg_volume": 18557, "as_of": "2026-08-28", "flow": "one sentence on what the volume was", "note": "why it matters, or empty"}]`. `volume`/`avg_volume` are numbers, not strings; `as_of` is `YYYY-MM-DD`, the date printed on the source page, not the date of this run. Empty array `[]` if the sources were unreachable or nothing was unusual. This renders on the Morning Brief's Whale Action panel.
- `squeezes`: a **bare array** (not wrapped in an object) — `[{"ticker": "CPB", "company": "Campbell Soup", "short_percent_float": 93.74, "days_to_cover": 4.2, "price": 23.47, "as_of": "2026-08-14", "price_as_of": "2026-08-30", "note": "why it could move, or empty", "buzz": {"mentions": 38, "mentions_prev": 4, "upvotes": 512, "source": "wallstreetbets", "as_of": "2026-08-30"}}]`. `buzz` is **optional and omitted entirely below 5 mentions**; `mentions`/`mentions_prev` are numbers and the panel computes the change. The three figures are numbers, not strings, on the source's own scale (`93.74` means 93.74%). `as_of` is the short-interest **settlement date** printed by the source; `price_as_of` is the quote's date. No borrow fee, no utilization, no squeeze score. Empty array `[]` if the sources were unreachable. This renders on the Morning Brief's Roaring Kitty panel.
- `generated_at`: current timestamp — this is when the brief was last refreshed, not the row's original insert time

This table holds exactly one row, overwritten each run. No history is kept.

=== PART 2: REASSESS THE ENTRY MODEL ===

1. Fetch `https://raw.githubusercontent.com/mdmeck/Smaug/main/smaug_pipeline.py`. `compute_features()` is the ground truth for what every feature column means, `compute_targets()` for the targets. Cross-reference against the feature/target tables in the spec you fetched in Part 0.
2. Query `analysis_runs` — `order by generated_at desc limit 1` — for the current regression/correlation output (correlations, standardized coefficients, r2_train/r2_test, deciles, per target).
3. Query `training_examples` for the trader's labeled examples. May be empty. Each row has `entry_at`, `exit_at`, `ticker`, `direction` (Long/Short), `quality` (Good/Bad), `strategy`, `key_level`, `notes`, `analysis_notes`. `exit_at` is null on Bad examples — those trades were never entered, so they have no exit; join one snapshot, not two, and never substitute an exit for them. Re-read these fresh every run — the trader edits and deletes them, so yesterday's set may not still apply.

Join two feature snapshots per example from `bars` exactly as the spec describes — never a later bar than the timestamp in question, which would be lookahead. Sessions referenced by an example are exempt from the bars retention window, so old examples remain joinable; a missing bar is a problem to report, not a stale example to skip.

**Treat the labels as approximate.** The trader reads these off a chart by eye; they are not tick-accurate execution records. Follow the spec's guidance in full — characterize a roughly ±5 minute window around each timestamp rather than the single labeled bar, never derive a threshold from one bar's value, and round `key_level` generously. Precision the labels cannot justify is worse than an honest wide rule.

Group by `strategy` where the examples support it (currently `Break and Retest`, `Opening Range Break`, `Bounce` — plain text, so treat an unfamiliar value as new rather than as an error). Rules blended across strategies are weaker than rules that respect the split; say in `summary` which strategies the examples covered. Where `key_level` is present, compare `close - key_level` at the entry snapshot against the `dist_*` features to identify which stored level the trader was actually watching.

Combine the regression signal with the labeled examples to build `long_entry`, `short_entry`, and `exit` rules. Only use feature names that actually appear in `analysis_runs` / `compute_features()` — never invent one, since these are translated mechanically into the indicator. `dist_vwap_bps` (session VWAP distance) is part of the feature set; use it if present in the data you read, and don't assume it exists if it isn't.

**Check each rule list's firing rate against real bars before writing it, in both directions.** A list that fires on ~0% of bars is contradictory; a list that fires on more than ~30% is too loose to be a signal at all. Both are broken, and the second is the one that has actually shipped — see "Rules are state predicates, and that has a failure mode" in the spec, which gives the thresholds and what to report in `summary`. The trader's standard is a handful of entries per session.

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

=== PART 3: REVIEW THE TRADES ===

Query `journal_entries` — the trader's log of what they actually traded, which is a different question from what they should have traded. Review the most recent 60 rows (or all of them, if fewer). Follow the spec's "Reviewing the journal" section for what to look for and what not to claim.

Upsert into `trade_feedback` with conflict target `user_id`:

- `user_id`: the UUID from Part 0 — required, do not omit
- `observations`: up to 3 strings, each under 20 words — patterns in the record, each citing a date, count, or figure
- `strengths`: up to 3 strings, each under 15 words — only what a number actually supports
- `risks`: up to 3 strings, each under 15 words — habits or leaks, not market predictions
- `focus`: one sentence, the single most important adjustment for the next session
- `trades_reviewed`: how many rows you actually read
- `date_range`: `{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}` covering those rows
- `generated_at`: current timestamp

This table holds exactly one row, overwritten each run. If `journal_entries` is empty, say so in `observations` and write the row anyway with `trades_reviewed: 0` — an honest empty state is more useful than a stale critique left in place.

Keep this strictly separate from Part 2: journal performance must not feed back into the entry rules. Describe what the record shows; never prescribe position sizing or anything that reads as financial advice.

=== PART 4: RECORD WHAT YOU FOUND ===

For each training example that materially informed the rules, write your findings back to that row's `analysis_notes` via an `update` on its `id` (no `user_id` needed on an update — the row already has one). Note which features actually distinguished it, whether it agreed with others of its `strategy`, and anything that makes it an outlier.

Never write into `notes` — that field is the trader's own reasoning and is ground truth. When reading examples, treat only `notes` as evidence. If you were to write inferences into the field you also read as ground truth, later runs would learn from earlier runs' conclusions and the labels would stop meaning anything.

=== FINALLY ===

Report what you actually did: which tables you wrote, the row counts, and any step that failed or was skipped. If a write errored, say so plainly with the error — do not report success you did not verify.

Name all four of `daily_briefs`, `entry_models`, `trade_feedback`, and `training_examples.analysis_notes` explicitly, each with what you wrote or why you didn't. For `daily_briefs`, state the `whales` and `squeezes` counts separately, how many `squeezes` rows carried a `buzz` object, and give the `as_of` date you wrote for each — they are the newest columns and the easiest to silently skip while the rest of the brief looks healthy, and a stale-but-present `as_of` is invisible unless you say it out loud. A part that was silently skipped looks identical to a part that succeeded unless you list them one by one — this is how `trade_feedback` stayed empty for days without anyone noticing.
