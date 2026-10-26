-- Run this once in the Supabase SQL Editor (Project > SQL Editor > New query).
-- Creates the Journal and Training Data tables, with row-level security
-- scoped so only the logged-in user can read/write their own rows.

create table if not exists journal_entries (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  date date not null,
  ticker text not null default 'SPY',
  direction text not null check (direction in ('Long', 'Short')),
  setup text default '',
  result text default '',
  -- premium paid to open, in dollars, EXCLUDING fees (net fill price x 100 x
  -- contracts). Nullable — unknown on hand-entered rows. Lets a day be read as
  -- return on capital, sum(result)/sum(cost_basis), rather than raw P&L: two
  -- $100 positions returning $20 is 10%, which $20 alone doesn't tell you.
  -- Fees are deliberately NOT folded in here — they live in their own column
  -- so gross (what thinkorswim shows) and net stay separable.
  cost_basis double precision,
  -- contracts per leg on the round trip, straight from the qty column of the
  -- broker paste. Nullable on hand-written rows with no fill behind them.
  -- Drives the fee estimate, and is the only place size is queryable — it was
  -- previously trapped in notes prose.
  contracts integer,
  -- actual commissions + regulatory for the round trip, when known from the
  -- transaction export. Normally NULL: thinkorswim order history carries no
  -- fee column, so the UI estimates from contracts instead. Set this to
  -- override that estimate with the real figure.
  fees double precision,
  notes text default '',
  created_at timestamptz not null default now()
);

alter table journal_entries enable row level security;

create policy "journal_entries_owner_all"
  on journal_entries
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Each row is one full labeled trade: an entry moment and an exit moment,
-- so the model can learn from feature values at both points.
-- `strategy` is deliberately unconstrained text, unlike direction/quality: the
-- taxonomy is expected to grow as the trader names new setups, and a CHECK
-- constraint would mean a migration each time. The webapp offers a fixed
-- dropdown (STRATEGIES in App.jsx) — the constraint lives there, not here.
create table if not exists training_examples (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  entry_at timestamptz not null,
  -- nullable, unlike entry_at: a Bad example is a trade that should never have
  -- been entered, so it has no exit. Forcing one would mean inventing a
  -- timestamp, and the routine reads exit snapshots as evidence for `exit`
  -- rules — fabricated exits would train the model on a trade that never was.
  exit_at timestamptz,
  ticker text not null default 'SPY',
  direction text not null check (direction in ('Long', 'Short')),
  quality text not null check (quality in ('Good', 'Bad')),
  strategy text default '',
  key_level double precision,
  notes text default '',
  -- written by the AI routine, never by the trader. Deliberately a separate
  -- column from `notes`: if the routine wrote findings into the field it also
  -- reads as ground truth, later runs would learn from their own output and
  -- the trader's observations would become indistinguishable from inferences.
  analysis_notes text default '',
  created_at timestamptz not null default now()
);

-- One example per entry moment per direction. This is what lets the Indicator
-- Preview upsert instead of insert: re-labeling a signal after a page reload
-- updates the quality rather than filing a second row. Without it the preview's
-- `saved` state (component-local, empty on every mount) silently produced
-- duplicates, which the routine then counted twice when deriving rules.
-- Direction is in the key so an entry and a reversal at the same minute stay
-- distinct; quality deliberately is NOT, since changing Good→Bad must overwrite.
create unique index if not exists training_examples_user_entry_dir_key
  on training_examples (user_id, entry_at, direction);

alter table training_examples enable row level security;

create policy "training_examples_owner_all"
  on training_examples
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Each row is one AI-synthesized "entry model" — a snapshot in time, not
-- overwritten in place, so the Model tab can show how the rules evolve as
-- more Training Data examples accumulate.
create table if not exists entry_models (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  generated_at timestamptz not null default now(),
  bars_analyzed int not null default 0,
  examples_used int not null default 0,
  date_range jsonb,
  rules jsonb not null,
  summary text default '',
  confidence text not null check (confidence in ('low', 'medium', 'high')),
  created_at timestamptz not null default now(),
  pinescript text default ''
);

alter table entry_models enable row level security;

create policy "entry_models_owner_all"
  on entry_models
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Daily brief (econ calendar, earnings, sentiment, bull/bear case), written
-- by the Claude Code routine via its authenticated Supabase connector.
-- Private like the tables above — the routine authenticates as the owning
-- user, so there's no need to expose this publicly. One row per user,
-- overwritten each run (unique user_id + on_conflict=user_id upsert) —
-- no need to keep historical briefs around.
create table if not exists daily_briefs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique default auth.uid() references auth.users(id) on delete cascade,
  generated_at timestamptz not null default now(),
  econ jsonb,
  earnings jsonb,
  sentiment jsonb,
  cases jsonb,
  -- outsized market-wide options flow, shown on the Dashboard rather than the
  -- brief. Added after the table existed, so it needs the alter below too:
  -- `create table if not exists` is a no-op on an already-created table and
  -- would silently skip the new column.
  whales jsonb,
  -- heavily shorted names that could squeeze, also Dashboard-side. Same
  -- after-the-fact caveat as `whales`: needs the alter below, because
  -- `create table if not exists` is a no-op against the live table and would
  -- silently skip the column.
  squeezes jsonb,
  created_at timestamptz not null default now()
);

alter table daily_briefs add column if not exists whales jsonb;
alter table daily_briefs add column if not exists squeezes jsonb;

alter table daily_briefs enable row level security;

create policy "daily_briefs_owner_all"
  on daily_briefs
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Coaching feedback on the trader's own journal, written by the routine and
-- shown on the Dashboard. Columns mirror the schema the Journal tab's
-- copy/paste flow already asks claude.ai for, so the routine and the manual
-- fallback produce interchangeable output. One row per user, upserted on
-- user_id like daily_briefs — this is "what to work on now", not a log, and
-- keeping every past critique around would just bury the current one.
create table if not exists trade_feedback (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique default auth.uid() references auth.users(id) on delete cascade,
  generated_at timestamptz not null default now(),
  observations jsonb default '[]'::jsonb,
  strengths jsonb default '[]'::jsonb,
  risks jsonb default '[]'::jsonb,
  focus text default '',
  -- what the critique was based on, so the UI can show its scope rather than
  -- implying the feedback covers trades it never saw
  trades_reviewed int not null default 0,
  date_range jsonb,
  created_at timestamptz not null default now()
);

alter table trade_feedback enable row level security;

create policy "trade_feedback_owner_all"
  on trade_feedback
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- 1-minute SPY bars + computed indicator features. Public read (this is
-- just market data, not personal) so both the webapp and the routine can
-- read it without authentication; writes only via the service_role key,
-- used exclusively by the GitHub Actions pipeline — never client-side.
create table if not exists bars (
  ts timestamptz primary key,
  ticker text not null default 'SPY',
  open double precision not null,
  high double precision not null,
  low double precision not null,
  close double precision not null,
  volume bigint not null,
  features jsonb
);

alter table bars enable row level security;

create policy "bars_public_read"
  on bars
  for select
  using (true);

-- Daily regression/correlation output. One row per pipeline run
-- (append-only, like entry_models, so you can see how the analysis
-- evolves) — public read, same reasoning and write restriction as bars.
create table if not exists analysis_runs (
  id uuid primary key default gen_random_uuid(),
  generated_at timestamptz not null default now(),
  ticker text not null default 'SPY',
  bars_analyzed int not null default 0,
  date_range jsonb,
  targets jsonb not null,
  notes jsonb default '[]'::jsonb
);

alter table analysis_runs enable row level security;

create policy "analysis_runs_public_read"
  on analysis_runs
  for select
  using (true);

-- One row per date that has bars, so the webapp can build its day picker in
-- one request. Paging `select ts` over the whole table did the same job but
-- costs one request per 1000 bars, which at a year of retention (~170k rows)
-- is ~170 sequential round trips before the tab can render. UTC date, to
-- match the webapp's `${day}T00:00:00Z` range filters — every stored bar
-- (04:00-20:00 ET) falls on the same date in UTC and ET. security_invoker so
-- the view reads through `bars`' own public-read policy rather than as owner.
create or replace view bar_days with (security_invoker = true) as
  select distinct (ts at time zone 'UTC')::date as day from bars;

grant select on bar_days to anon, authenticated;


-- =====================================================================
-- Erebor — single-name event screening
-- =====================================================================
-- Deliberately a separate module from everything above. Smaug is intraday
-- SPY options scalping; Erebor screens individual equities for event-driven
-- dislocations (merger arbitrage first, squeezes and liquidity sweeps later)
-- and holds multi-day positions in them. They share this Supabase project and
-- the webapp shell, and nothing else: no foreign keys reach across, no Erebor
-- row joins to `bars` / `analysis_runs` / `entry_models`, and Erebor trades do
-- NOT go in `journal_entries`. That last one is a correctness matter, not
-- tidiness — `journal_entries` carries 0DTE round-trip semantics and a daily
-- P/L reconciliation against the broker, and multi-day equity holds dropped
-- into it would corrupt both.
--
-- Owner-only tier, like every other routine-written table. The routine's
-- connector runs as `postgres` with `auth.uid()` NULL, so every insert here
-- must pass `user_id` explicitly or hit a not-null violation — the same
-- asymmetric failure the rest of the schema warns about (reads look fine
-- while writes silently stop).

-- Announced deals and their terms. This is the input the merger-arb screen
-- cannot compute for itself: terms live in an 8-K or a press release, not in
-- any free structured feed, so a human or the routine enters them once. That
-- is the whole reason merger arb is the cheapest of the three screens to
-- build — the terms change essentially never while the price moves daily, so
-- one hand-entered row supports an indefinite series of automated readings.
--
-- Upserted on (user_id, ticker, announced_on) rather than append-only: unlike
-- a price, a correction to a deal's terms supersedes the old value outright
-- and keeping both would just be two contradictory rows.
create table if not exists erebor_deals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  ticker text not null,
  company text default '',
  acquirer text default '',
  announced_on date not null,
  -- Terms. Both are nullable and mean different things when absent:
  -- cash_per_share is NULL in an all-stock deal, stub_pct is NULL when
  -- holders are cashed out entirely. A deal with neither is not screenable
  -- and the scanner skips it rather than assuming a zero.
  cash_per_share double precision,
  stub_pct double precision check (stub_pct is null or (stub_pct >= 0 and stub_pct <= 100)),
  -- Headline transaction value, and the denominator the screen exists to
  -- compare against: when the market's implied value of the combined company
  -- runs to several times the price the deal was actually struck at, that gap
  -- is the signal. GPRO on 2026-09-03 implied ~$1.1B against a $285M deal.
  transaction_value_usd double precision,
  shares_outstanding double precision,
  status text not null default 'announced'
    check (status in ('announced', 'closed', 'terminated')),
  expected_close date,
  -- the filing or release the terms were read from, so a number that looks
  -- wrong six weeks later can be re-checked against its source instead of
  -- re-derived from memory
  source_url text default '',
  notes text default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists erebor_deals_user_ticker_announced_key
  on erebor_deals (user_id, ticker, announced_on);

alter table erebor_deals enable row level security;

create policy "erebor_deals_owner_all"
  on erebor_deals
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- One row per name per screen per day. This is the table that makes the
-- module tradeable rather than merely readable, and it is deliberately the
-- opposite of `daily_briefs`: that one keeps a single row per user with no
-- history, which is why the Roaring Kitty and Whale Action panels can show a
-- screen without anyone being able to ask whether the screen has ever worked.
-- Here every day accumulates, so a rule can be backtested, a skipped name can
-- be checked against what it went on to do, and a silently broken source
-- shows up as a gap in a series instead of vanishing.
--
-- `kind` partitions the screens rather than splitting them into three tables:
-- they share every column that matters (what, when, how much, why) and differ
-- only in `metrics`, which is jsonb precisely so each screen can carry its own
-- fields without a migration per screen.
--
-- Upsert on (user_id, as_of, ticker, kind), not plain insert: a re-run on the
-- same day should correct that day's reading, not file a second one. History
-- lives across days, never within one.
create table if not exists erebor_candidates (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  as_of date not null,
  ticker text not null,
  kind text not null check (kind in ('merger_arb', 'squeeze', 'whale', 'sweep')),
  company text default '',
  price double precision,
  -- The quote's own date, kept separate from `as_of` for the same reason the
  -- squeeze panel separates its three vintages: a fresh price must never lend
  -- credibility to a stale input sitting next to it.
  price_as_of date,
  -- Comparable within a `kind` and meaningless across them. Nothing should
  -- ever sort the whole table by this column.
  --
  -- Only ever written by code that can be re-run, never by an AI routine: a
  -- composite number nobody can re-derive is one a trader would size a
  -- position on. `merger_arb` is arithmetic over published deal terms;
  -- `squeeze` is `score_squeeze()` in erebor_scan.py, a fixed-weight 0-100
  -- over the figures in `metrics`, tagged with `metrics.score_version` so a
  -- formula change never gets read against an older formula's outcomes —
  -- sq1 led with short float, sq2 leads with days to cover on the evidence
  -- cited there, and the two are not the same number.
  -- `whale` is NULL — the panel shows the volume/OI multiple directly.
  score double precision,
  -- Screen-specific figures, as the source printed them.
  --   merger_arb: stub_per_share, implied_combined_value_usd,
  --               premium_to_cash_pct, implied_vs_transaction_x, deal_id
  --   squeeze:    short_percent_float, days_to_cover, shares_short,
  --               shares_short_prior, float_shares, buzz {...},
  --               call_vol, call_oi, flow_lean (from the shared option-chain
  --               pass, absent when the name has no readable chain),
  --               score_version, score_parts {trapped, fuel, spark,
  --               accelerant, pressing}
  --   whale:      lean, lean_skew, lean_basis, volume, call_volume,
  --               put_volume, call_oi, put_oi, open_interest, vol_oi_ratio,
  --               expiries, flow, score_version, score_parts
  --               {newness, conviction, size}. `lean_skew` is each side's
  --               volume over its OWN open interest (wh2), not the raw call
  --               share — single-name option volume is structurally
  --               call-heavy and the raw share made the panel bullish on
  --               every row. `lean_basis` says which rule judged the row.
  --               Also top {side, strike, expiry, volume, open_interest, iv,
  --               last} (the single busiest contract), earnings_date (next
  --               report, absent for ETFs), and episode (same shape as
  --               squeeze's) so the panel can show the move since flagged.
  metrics jsonb not null default '{}'::jsonb,
  note text default '',
  created_at timestamptz not null default now()
);

create unique index if not exists erebor_candidates_user_day_ticker_kind_key
  on erebor_candidates (user_id, as_of, ticker, kind);

create index if not exists erebor_candidates_ticker_idx
  on erebor_candidates (user_id, ticker, as_of desc);

alter table erebor_candidates enable row level security;

create policy "erebor_candidates_owner_all"
  on erebor_candidates
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- What the screen showed on each run, kept forever. `erebor_candidates` is
-- keyed on the data's own date (a settlement, a session), which is right for
-- the panel and wrong for a backtest: a Thursday run and a Friday run that
-- read the same settlement figure collapse into one row, and Friday's price
-- and chatter overwrite Thursday's. This table is keyed on the run date, so
-- every day's reading survives as the trader saw it — same figures, same
-- score, nothing recomputed.
--
-- `outcome` is filled in later by the scan once the forward window has fully
-- printed (see `compute_outcome()` in erebor_scan.py): the max high, min low
-- and close over the next POP_WINDOW sessions after `price_as_of`, as
-- percent returns off `price`, and whether the max cleared the pop threshold.
-- NULL until then — never a partial window, which would make every young
-- row look like a dud. `python erebor_scan.py --backtest` reads this table.
--
-- The forward window keeps running after a name leaves the screen, which is
-- the whole reason the question "did we flag that correctly?" is answerable
-- here and not from the panel. Names drop off the screen partly *because*
-- they worked — the settlement figure updates, the chatter moves on — so any
-- reading taken only over names still listed is biased toward the ones that
-- did nothing. Nothing in this table is ever deleted when a name drops off.
create table if not exists erebor_snapshots (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  run_date date not null,
  ticker text not null,
  kind text not null check (kind in ('merger_arb', 'squeeze', 'whale', 'sweep')),
  price double precision,
  price_as_of date,
  score double precision,
  score_version text,
  metrics jsonb not null default '{}'::jsonb,
  -- The listing episode this reading belongs to: the first run_date of the
  -- current UNBROKEN streak of scans that carried this ticker, and the price
  -- and SPY close on that day. A name that drops off the screen and comes
  -- back starts a new episode rather than resuming the old one — the chatter
  -- universe turns over about two thirds a day, so a gap is a different
  -- setup, not a continuation of the same one.
  --
  -- Carried forward from the prior row rather than recomputed, so the anchor
  -- a tile shows is the price that was actually on screen the day the name
  -- appeared. NULL on rows written before this existed, and on day one of an
  -- episode `episode_start` equals `run_date`.
  episode_start date,
  anchor_price double precision,
  anchor_spy double precision,
  -- Whether this reading was one the trader actually saw. The screen keeps
  -- every name that clears its thresholds but shows only the top few, and the
  -- discarded ones were costing nothing to compute and everything to throw
  -- away: with five positive events in the first fortnight, the binding
  -- constraint on learning anything is sample size, not data access.
  --
  -- Both questions stay answerable because the flag is stored rather than
  -- implied. "Does the score rank names correctly" wants every row, including
  -- the low scores that populate the bottom of the range. "Did the screen as
  -- shown work" wants displayed only. Conflating them would quietly answer
  -- the easier one.
  displayed boolean not null default true,
  -- SPY's close on this run's session. Stored on every row because a name
  -- that rose while the whole tape rose is not squeezing, and that comparison
  -- cannot be reconstructed later once the panel only has the name's price.
  spy double precision,
  outcome jsonb,
  outcome_as_of date,
  created_at timestamptz not null default now()
);

create unique index if not exists erebor_snapshots_user_run_ticker_kind_key
  on erebor_snapshots (user_id, run_date, ticker, kind);

create index if not exists erebor_snapshots_pending_idx
  on erebor_snapshots (user_id, kind, run_date)
  where outcome is null;

alter table erebor_snapshots enable row level security;

create policy "erebor_snapshots_owner_all"
  on erebor_snapshots
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- The screen's own report card, recomputed by every scan and stored so the
-- webapp can render it without anyone running a script. One row per
-- (user_id, as_of, kind, score_version): upserted, because a re-run on the
-- same day corrects that day's reading rather than filing a second one, and
-- versioned because sq1 and sq2 are different formulas whose outcomes must
-- never be pooled.
--
-- `report` holds the whole thing as computed — hit rate by score tercile,
-- Spearman of the score and of each part against the forward directional
-- return, and for squeezes the per-episode table. `trustworthy` is false
-- until `n` clears BACKTEST_MIN_N, and the panel is required to say so
-- rather than drawing a confident chart over fourteen rows.
--
-- "Directional" matters for whales: a bearish flag that fell is a hit. The
-- sign lives in `metrics.lean` and is applied when the report is built, not
-- when the outcome is recorded — see _directional() in erebor_scan.py.
create table if not exists erebor_backtests (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  as_of date not null,
  kind text not null check (kind in ('merger_arb', 'squeeze', 'whale', 'sweep')),
  score_version text not null default '',
  n int not null default 0,
  base_rate double precision,
  trustworthy boolean not null default false,
  report jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create unique index if not exists erebor_backtests_user_day_kind_ver_key
  on erebor_backtests (user_id, as_of, kind, score_version);

alter table erebor_backtests enable row level security;

create policy "erebor_backtests_owner_all"
  on erebor_backtests
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Run health, one row per scan. This exists because of a bug found on
-- 2026-09-08: the brief ran, both market panels were written as empty arrays
-- because their sources were unreachable, and the webapp rendered that as
-- "Awaiting run" — a broken scraper and a quiet market looked identical, and
-- with no history there was no way to notice it had been happening.
--
-- An empty `erebor_candidates` day is ambiguous for exactly the same reason,
-- so the ambiguity is resolved structurally instead of by inference: a scan
-- that ran and found nothing writes a row here with its sources marked ok,
-- and a scan that never ran leaves no row at all. `sources` is per-source
-- status so one dead feed doesn't get read as a clean screen.
create table if not exists erebor_runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  as_of date not null,
  ran_at timestamptz not null default now(),
  kinds jsonb not null default '[]'::jsonb,
  sources jsonb not null default '{}'::jsonb,
  candidates_written int not null default 0,
  errors jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

create unique index if not exists erebor_runs_user_day_key
  on erebor_runs (user_id, as_of);

alter table erebor_runs enable row level security;

create policy "erebor_runs_owner_all"
  on erebor_runs
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Erebor's own trade log. Separate from `journal_entries` for the reason given
-- at the top of this section, and shaped differently because the trades are
-- different: multi-day rather than same-session, shares or longer-dated
-- options rather than 0DTE, and closed in pieces often enough that a single
-- open/close pair would misreport them.
--
-- `opened_at`/`closed_at` are timestamps, not a `date` — an Erebor position
-- spans days, so a single date column could not say which one it meant.
-- `closed_at` NULL means the position is still open, which is a state
-- `journal_entries` never has to represent.
create table if not exists erebor_positions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  ticker text not null,
  direction text not null check (direction in ('Long', 'Short')),
  -- what put the name on the radar, so the screen can be scored by outcome
  -- rather than by how good its candidates looked on the day
  thesis text not null default '' check (thesis in ('', 'merger_arb', 'squeeze', 'sweep', 'discretionary')),
  opened_at timestamptz not null,
  closed_at timestamptz,
  quantity double precision,
  avg_entry double precision,
  avg_exit double precision,
  -- Gross of fees, matching the hand-entered convention in journal_entries.
  -- Signed dollars; NULL while the position is open rather than 0, so an
  -- unrealised position can never be summed into a realised total.
  result double precision,
  cost_basis double precision,
  notes text default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists erebor_positions_user_opened_idx
  on erebor_positions (user_id, opened_at desc);

alter table erebor_positions enable row level security;

create policy "erebor_positions_owner_all"
  on erebor_positions
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);


-- =====================================================================
-- Lab — Claude's autonomous strategy research + paper/live trading
-- =====================================================================
-- Separate module, like Erebor: nothing here joins to the Smaug or Erebor
-- tables, and Lab trades never go in `journal_entries` (that table is the
-- trader's own fills, reconciled against thinkorswim).
--
-- Who writes what:
--   lab_journal, lab_strategies   the Lab routine (connector runs as
--                                 postgres -> pass user_id explicitly)
--   lab_trades, lab_runs          lab/runner.py in GitHub Actions
--                                 (service-role key, user_id explicit)
--   lab_controls                  the trader, from the webapp. The runner
--                                 reads it before every order. The routine's
--                                 charter forbids it from writing this table
--                                 — its connector runs as postgres, so that
--                                 rule is policy, not a permission. Limits
--                                 are only limits if the thing they limit
--                                 doesn't move them.
-- All owner-only RLS.

-- The research notebook and the routine's memory between runs. Append-only:
-- a wrong conclusion is corrected by a later entry, not by editing the old
-- one, so the reasoning that led somewhere stays readable.
create table if not exists lab_journal (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  run_kind text not null default 'manual'
    check (run_kind in ('premarket', 'postmarket', 'intraday', 'manual')),
  kind text not null default 'note'
    check (kind in ('hypothesis', 'experiment', 'result', 'decision', 'review', 'plan', 'note')),
  title text not null default '',
  -- markdown
  body text not null default '',
  strategy text default '',
  -- raw command output (research.py JSON), so a number quoted in `body` can
  -- be checked against what the backtest actually printed
  data jsonb
);

create index if not exists lab_journal_user_created_idx on lab_journal (user_id, created_at desc);

alter table lab_journal enable row level security;

create policy "lab_journal_owner_all"
  on lab_journal for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- One row per strategy. `module` names a file in lab/strategies/ and
-- `params` its tunables, which is the whole definition: the runner executes
-- exactly what the backtest measured, never a paraphrase.
--
-- status: idea -> backtest -> paper -> live -> retired. Only 'paper' and
-- 'live' rows trade. 'live' additionally needs lab_controls.mode = 'live'
-- AND a live Alpaca endpoint; promotion past paper is the trader's call.
create table if not exists lab_strategies (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  name text not null,
  module text not null,
  params jsonb not null default '{}'::jsonb,
  status text not null default 'idea'
    check (status in ('idea', 'backtest', 'paper', 'live', 'retired')),
  hypothesis text not null default '',
  -- latest research.py backtest output (stats block), and the forward
  -- paper record computed from lab_trades. Kept apart because only one of
  -- them can have been overfit.
  backtest jsonb,
  forward jsonb,
  contracts int not null default 1 check (contracts between 1 and 10),
  notes text default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists lab_strategies_user_name_key on lab_strategies (user_id, name);

alter table lab_strategies enable row level security;

create policy "lab_strategies_owner_all"
  on lab_strategies for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- Every order the runner places, one row per round trip. Unique on
-- (strategy, signal_ts) so a runner that restarts mid-session — the
-- morning/afternoon job handoff does this daily — can never act on the same
-- signal twice.
create table if not exists lab_trades (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  strategy text not null,
  mode text not null default 'paper' check (mode in ('paper', 'live')),
  signal_ts timestamptz not null,
  signal jsonb,
  occ text not null,
  direction text not null check (direction in ('long', 'short')),
  contracts int not null default 1,
  status text not null default 'pending'
    check (status in ('pending', 'open', 'closed', 'canceled', 'rejected', 'error')),
  entry_order_id text,
  stop_order_id text,
  exit_order_id text,
  entry_ts timestamptz,
  entry_price double precision,
  stop_price double precision,
  target_price double precision,
  exit_ts timestamptz,
  exit_price double precision,
  exit_reason text default '',
  -- dollars, net of the fees Alpaca reports (none on options) — so gross in
  -- practice. NULL until closed.
  pnl double precision,
  notes text default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists lab_trades_user_strategy_signal_key
  on lab_trades (user_id, strategy, signal_ts);
create index if not exists lab_trades_user_created_idx on lab_trades (user_id, created_at desc);

alter table lab_trades enable row level security;

create policy "lab_trades_owner_all"
  on lab_trades for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- The trader's hard limits and kill switch. One row per user. The runner
-- re-reads it every loop, so flipping trading_enabled off takes effect within
-- a minute: open entry orders are canceled and positions are flattened.
create table if not exists lab_controls (
  user_id uuid primary key default auth.uid() references auth.users(id) on delete cascade,
  trading_enabled boolean not null default false,
  mode text not null default 'paper' check (mode in ('paper', 'live')),
  max_contracts int not null default 1 check (max_contracts between 1 and 10),
  max_open_positions int not null default 1 check (max_open_positions between 1 and 5),
  max_trades_per_day int not null default 4 check (max_trades_per_day between 0 and 20),
  -- realized + unrealized, dollars. Hitting it flattens and stops for the day.
  max_daily_loss double precision not null default 200 check (max_daily_loss > 0),
  -- premium at risk on one entry (fill x 100 x contracts), dollars
  max_premium_per_trade double precision not null default 400 check (max_premium_per_trade > 0),
  updated_at timestamptz not null default now()
);

alter table lab_controls enable row level security;

create policy "lab_controls_owner_all"
  on lab_controls for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- One row per runner session (morning / afternoon job). A day with no row
-- means the runner never started — distinguishable from a day it ran and
-- found nothing, the same lesson as erebor_runs.
create table if not exists lab_runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  as_of date not null,
  session_window text not null default '',
  started_at timestamptz not null default now(),
  heartbeat_at timestamptz,
  ended_at timestamptz,
  status text not null default 'running',
  strategies jsonb not null default '[]'::jsonb,
  events jsonb not null default '[]'::jsonb,
  errors jsonb not null default '[]'::jsonb
);

create index if not exists lab_runs_user_day_idx on lab_runs (user_id, as_of desc);

alter table lab_runs enable row level security;

create policy "lab_runs_owner_all"
  on lab_runs for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
