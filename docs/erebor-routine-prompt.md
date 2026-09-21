# Erebor daily routine — prompt

> **Status (2026-09-18): this routine is DISABLED and cannot currently run.**
> On its first fire the cloud routine environment blocked every one of its
> sources on organisation policy — TipRanks, MarketBeat, apewisdom, StockTwits
> and CBOE all returned `EGRESS_BLOCKED` (confirmed with a direct `curl`: 403 on
> CONNECT). The routine did the right thing — wrote zero candidates and a run
> row with every source marked failed — but it can never produce a row from
> that environment. This is also why `daily_briefs.whales`/`.squeezes` came
> back `[]` on 2026-09-08: the same block, not a flaky source.
>
> **The squeeze screen has moved to Python** (`screen_squeeze()` in
> `erebor_scan.py`, run by `.github/workflows/erebor-daily.yml`, which has
> open egress). yfinance carries short % of float, days to cover and the
> settlement date per ticker, and both chatter feeds are plain JSON, so
> nothing needed a browser after all. One change in meaning: it starts from
> the chatter universe and checks short interest, rather than ranking the
> whole market by short interest and then checking chatter — see the comment
> block above `screen_squeeze()`.
>
> **Whale Action has also moved to Python** (`screen_whale()`), and it is a
> different measurement: MarketBeat compared today's options volume to a
> rolling average; this compares it to open interest on the same contracts,
> over the Erebor universe rather than the whole market, skipping expiries
> under a week out (daily-expiry names churn their front week as routine) and
> requiring a directional skew. The panel labels the multiple "x OI", never
> "x avg", for that reason.
>
> The task list below is kept as the specification the Python screen was
> written against, and for whenever a routine environment with open egress
> becomes available. Do not re-enable it as-is.

Erebor screens **individual equities** for event-driven dislocations. It is a
separate module from Smaug, which trades intraday SPY options; the two share a
Supabase project and the webapp shell and nothing else.

## What to actually paste into the routine

Paste **only this**, once. It fetches everything else at run time, so editing
this file and committing is enough to change routine behavior — there is
nothing to re-paste, ever:

```
You are the Erebor daily routine.

Fetch https://raw.githubusercontent.com/mdmeck/Smaug/main/docs/erebor-routine-prompt.md
and carry out the task list under the "Task list" heading, in order, exactly as
written. That document is the instructions; this message only points at them.

If the fetch fails, STOP and report it. Do not improvise the routine from
memory — writing to the wrong tables is worse than not running.
```

### Why this is a separate routine

These two screens used to be tasks 5 and 6 of the Smaug morning routine, writing
into `daily_briefs.whales` and `daily_briefs.squeezes`. Splitting them out fixes
three things at once:

1. **They failed independently and invisibly.** On 2026-09-08 the brief ran at
   12:27 UTC with `econ` and `cases` populated and both of these columns written
   as `[]` because their sources were unreachable. One run, one report, two
   unrelated failure surfaces — and no way to retry only the broken half. The
   old prompt already conceded this, requiring the routine to name the whales
   and squeezes counts separately in its report because they were "the easiest
   to silently skip while the rest of the brief looks healthy." That was a
   workaround for coupling that no longer has to exist.
2. **`daily_briefs` keeps no history.** It is one row per user, overwritten every
   run. A screen with no history cannot be scored: you can never ask whether the
   names it surfaced went on to do anything, which is the difference between a
   panel you read and a screen you trade.
3. **Domain.** Tasks 1-4 of the Smaug routine are SPY. These two are whole-market
   single-name work that happened to be living in a SPY routine.

---

## Task list

=== PART 0: LOAD THE SPEC ===

Before anything else, fetch:
`https://raw.githubusercontent.com/mdmeck/Smaug/main/webapp/supabase/schema.sql`

Read the section headed "Erebor — single-name event screening". That is the
authoritative definition of every table below. Where this prompt and the schema
disagree, the schema wins.

If the fetch fails, STOP and report it. Do not proceed from memory.

**Critical, and the single most common failure:** your Supabase connector
authenticates as the `postgres` role, so `auth.uid()` evaluates to NULL. Reads
work fine (RLS is bypassed), but every INSERT into `erebor_candidates` or
`erebor_runs` must pass `user_id` explicitly or it fails a NOT NULL violation:

```
user_id = c0b48756-5f94-4862-886a-8ecdb7099ef6
```

Omitting it does not error visibly at the reasoning level — reads keep looking
healthy while nothing is ever written. If a write fails, report the error; never
report success you did not verify.

**Determine today's date yourself.** It is `as_of` on the run row. It is *not*
automatically `as_of` on a candidate — see the dates rule below, which is the
thing this routine gets wrong most often.

=== PART 1: ROARING KITTY (kind = `squeeze`) ===

Names where a short squeeze may be setting up, across the whole market. Fetch
these two, in this order; they were checked and are the free, no-login,
non-JavaScript sources that actually return data to a fetch:

- `https://www.tipranks.com/screener/most-shorted-stocks` — the ranking. It is
  already sorted by short interest as a percent of float, descending, and prints
  its own settlement date ("Short interest data as of ..."). Take the top 8 **as
  published — no market-cap, price, or sector filter.** Copy
  `short_percent_float` and `price` as numbers.
- `https://www.marketbeat.com/short-interest/` — enrichment only. Its "Days to
  Cover" column is the number you want; match it to the TipRanks names **by
  ticker** and fill `days_to_cover`. Leave `days_to_cover` out for any name that
  isn't on this page — do not compute or estimate it. **Do not rank from this
  page**: it is sorted by dollar volume sold short, so it leads with SPY, QQQ,
  IWM, XBI and TLT. Those are ETFs, they create and redeem shares on demand,
  they cannot squeeze, and their "% of float" readings (XBI showed 118%) are an
  artifact of that mechanism rather than a signal.

Two URLs that look right but are not:
`marketbeat.com/market-data/short-interest/` and
`marketbeat.com/market-data/highest-short-interest-stocks/` both soft-404 into a
generic "Public Companies By Market Cap" table — a plausible table of entirely
the wrong data. If you land on market-cap columns, you have the wrong page.

Do not write a borrow fee, a float-utilization figure, or a squeeze score. No
free source publishes a trustworthy one, and an invented borrow fee is a number
the trader would size a position on. `score` stays NULL on every row this
routine writes — the UI derives its own ranking from the raw figures, which is
the only way the trader can check it.

Optionally search the web for why a name is heavily shorted (a short report, a
failed merger, a busted story) and put one line in `note`; if you find nothing,
leave `note` empty rather than inventing a thesis.

**Then the chatter half — this is what makes the screen more than a
short-interest table.** Fetch
`https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/1` (free, no key,
plain JSON; pages 2-6 exist if you need more of the tail). Each result has
`ticker`, `mentions`, `mentions_24h_ago`, `upvotes` and `rank`. Also fetch
`https://api.stocktwits.com/api/2/trending/symbols.json` (free, no auth) for its
30 trending symbols. **Do not attempt X/Twitter** — as of February 2026 there is
no free tier for new developers and reads are billed per post; it is not a free
source and is out of scope.

For each name already in your list, attach a `buzz` object inside `metrics`
**only if it has 5 or more mentions**. Below that, leave `buzz` off the row
entirely — do not write `mentions: 0`, and do not write a buzz object with a
body. This floor is not a nicety: on 2026-08-30, 468 of the 534 tickers apewisdom
ranked had exactly one mention, and five of the six heavily shorted names that
appeared at all had exactly one. One person typing a ticker is not chatter, and
rendering it as such is the same error as inventing a borrow fee.

Then work the other direction, which is the point of the screen: take the names
with real chatter (5+ mentions on apewisdom, or present in the StockTwits
trending list) and look up each one's short interest at
`https://www.marketbeat.com/stocks/{EXCHANGE}/{TICKER}/short-interest/`. Add up
to 4 of them if they carry **10% or more of float short** — these are the
loaded-and-loud names, and they are the ones most likely to actually move. Skip
anything below that threshold: chatter on a name with no short base is not a
squeeze setup. Skip ETFs and crypto tickers (`SPY`, `DIA`, anything ending
`.X`) — they cannot squeeze.

The UI derives its two sections from whether `buzz` is present, so do not add a
section or category field. Expect the loud section to be empty on many days;
that is a correct and useful answer, and an empty one is far better than a
padded one.

=== PART 2: WHALE ACTION (kind = `whale`) ===

Where outsized options activity showed up in the last session, across the whole
market — not just SPY. Fetch these two, in this order; same sourcing note as
above:

- `https://www.marketbeat.com/market-data/unusual-call-options-volume/` — bullish side
- `https://www.marketbeat.com/market-data/unusual-put-options-volume/` — bearish side

Each is a dated table of ticker, current price, option volume, average volume,
and percent increase. **Take the rows as ranked and published — no market-cap or
price filter.** A $0.40 stock at 900% of average is what the source flagged, and
second-guessing the screen means the panel stops matching a table the trader can
check. Take up to 8 names total across the two pages. Copy `volume` and
`avg_volume` as numbers, exactly as printed — **do not convert the percent
increase into a multiple yourself**; the UI does that, and a stored multiple is
one the trader cannot check against the source.

Then fetch `https://www.cboe.com/us/options/market_statistics/daily/` for the
session's market-wide put/call ratios (total, index, equity) and call vs put
volume. That's exchange-primary data and is the lean each name should be read
against — use it to write the `note` fields, and say so when a name's flow runs
opposite the tape.

Optionally search the web for news on a name to explain *why* the volume showed
up (earnings, guidance, M&A, an analyst move) and put that in `note`. If you
find nothing, leave `note` empty — never invent a catalyst, and **never write a
dollar premium figure**: no free source publishes per-print premium, and an
estimate here is a number the trader would act on.

=== PART 3: THE DATES RULE ===

Read this before writing anything. It is the rule this routine is most likely to
break, and breaking it is invisible.

`erebor_candidates` has two date columns and they are not interchangeable:

- **`as_of`** — the date the *data* describes. For a `squeeze` row this is the
  **short-interest settlement date** the source prints (MarketBeat puts it right
  in the column header: "Shares Sold Short (8/14/2026)"). For a `whale` row it is
  the **session date printed on the MarketBeat page** — on a Monday that is the
  previous Friday. It is normally two to four weeks old for squeezes and that is
  correct: exchange short interest settles on the 15th and at month end and
  publishes about eight business days later.
- **`price_as_of`** — the date of the price quote, usually today.

**Never substitute today's date to make a row look current.** The trader reads
`as_of` to decide whether the number still means anything, and the UI ages the
two on separate clocks precisely so a fresh price cannot lend credibility to a
three-week-old settlement figure. A chatter count inside `buzz` carries its own
`as_of` for the same reason — three vintages, three clocks, never one standing
in for another.

Because `as_of` is the data's own date and the table's uniqueness is
`(user_id, as_of, ticker, kind)`, two runs on different days that both read the
same settlement figure will correctly resolve to **one** row. That is intended:
the row describes a settlement, not a run. What proves the routine ran is the
`erebor_runs` row in Part 5, not a new candidate row.

=== PART 4: WRITE THE CANDIDATES ===

Upsert every row from Parts 1 and 2 into `erebor_candidates` with conflict
target `(user_id, as_of, ticker, kind)`. Columns:

- `user_id` — the UUID from Part 0. Required, do not omit.
- `as_of` — per the dates rule above. `YYYY-MM-DD`.
- `ticker` — uppercase, no exchange prefix.
- `kind` — `squeeze` or `whale`. Nothing else in this routine.
- `company` — string, or empty.
- `price` — number, or omit.
- `price_as_of` — `YYYY-MM-DD`, the quote's date.
- `score` — **always NULL from this routine.** See Part 1.
- `metrics` — jsonb, numbers as numbers, on the source's own scale
  (`93.74` means 93.74%, never `0.9374`):
  - `squeeze`: `{"short_percent_float": 93.74, "days_to_cover": 4.2,
    "buzz": {"mentions": 38, "mentions_prev": 4, "upvotes": 512,
    "source": "wallstreetbets", "as_of": "2026-08-30"}}` — `days_to_cover` and
    `buzz` are each omitted entirely when unavailable.
  - `whale`: `{"lean": "bullish|bearish|mixed", "volume": 34696,
    "avg_volume": 18557, "flow": "one sentence on what the volume was"}`
- `note` — one line, or empty. Never a fabricated catalyst or thesis.

If a source is unreachable, **write no rows for that kind** and record it in
Part 5. Do not substitute a paid, signup-gated, or JavaScript-only screener, and
do not reconstruct a list from memory.

=== PART 5: WRITE THE RUN ROW — DO NOT SKIP THIS ===

Upsert exactly one row into `erebor_runs` with conflict target
`(user_id, as_of)`, where `as_of` here is **today's date**, not a data date.

- `user_id` — the UUID from Part 0.
- `as_of` — today, `YYYY-MM-DD`.
- `ran_at` — current timestamp.
- `kinds` — `["squeeze", "whale"]`, or only the ones you actually attempted.
- `sources` — per-source status, one key per URL family you fetched:
  `{"tipranks": "ok", "marketbeat_short": "ok", "apewisdom": "ok",
  "stocktwits": "failed", "marketbeat_options": "ok", "cboe": "ok"}`.
  Use `ok`, `partial`, or `failed`. Be accurate: this is the only record of
  whether a quiet screen was quiet or broken.
- `candidates_written` — the count you actually wrote.
- `errors` — array of `{"source": "...", "error": "what happened"}`, empty if none.

**Why this row is mandatory, including on a day you find nothing.** An empty
screen is ambiguous: it can mean the market is quiet, or that every source
broke. On 2026-09-08 the old routine wrote empty arrays because its sources were
unreachable, and the webapp rendered that as "Awaiting run" — a dead scraper and
a quiet market were indistinguishable, and with no history nobody could tell it
had been happening. A run row resolves that structurally: a scan that ran and
found nothing leaves this row with its sources marked, and a scan that never ran
leaves no row at all. Writing zero candidates is a valid outcome. Writing zero
candidates *and* no run row is a silent failure.

=== PART 6: REPORT ===

State each of these explicitly, one by one:

- `erebor_candidates`: how many `squeeze` rows and how many `whale` rows, given
  separately, and the `as_of` you wrote for each kind.
- How many `squeeze` rows carried a `buzz` object.
- `erebor_runs`: written or not, and the `sources` map verbatim.
- Any source that failed, and what you did about it.

A part that was silently skipped looks identical to a part that succeeded unless
you list them one by one, and a stale-but-present `as_of` is invisible unless you
say it out loud. Do not report a write you did not verify.
