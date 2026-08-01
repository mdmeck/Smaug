# Smaug

Personal intraday SPY options scalping tool. Three pieces that share one Supabase project:

1. **`smaug_pipeline.py`** — daily Python job (GitHub Actions, weekdays 21:30 UTC). Pulls SPY 1-min bars from yfinance → computes features → runs correlation/OLS/decile analysis → writes `bars` + `analysis_runs` in Supabase.
2. **`webapp/`** — React + Vite SPA deployed to GitHub Pages (`https://mdmeck.github.io/Smaug/`). Trader logs trades (Journal) and labeled examples (Training Data); views analysis (Modeling, Raw Data) and the synthesized entry model (Model).
3. **`pinescript/`** — hand-authored Pine v5 fragments assembled into the TradingView indicator that the daily AI routine emits alongside each `entry_models` row.

There is no server-side app code. The "AI routine" is a Claude Code routine / claude.ai conversation that reads Supabase and writes `daily_briefs`, `entry_models`, and `trade_feedback` rows directly — the webapp also has a copy/paste fallback (`CopyPasteAI`) for pasting a response back in by hand.

## Authoritative reference docs

Read these before changing anything they cover; they are the contract the AI routine is written against, so edits here change routine behavior:

- **`docs/smaug-project-knowledge.md`** — the full spec: data sources, every feature and target definition, how training examples join to bar snapshots, the PineScript generation requirements, and the `entry_models` output schema. This file is pinned into the claude.ai Project knowledge.
- **`pinescript/README.md`** — the fragment assembly contract (fetch order, naming prefixes, label style). See "PineScript fragments" below.

## Commands

```bash
# pipeline (needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in env)
python smaug_pipeline.py              # normal daily run
python smaug_pipeline.py --no-fetch   # re-run analysis on stored bars only
python smaug_pipeline.py --synthetic  # smoke test, fake data, no network

# webapp
cd webapp
npm install
npm run dev       # vite dev server
npm run build     # -> webapp/dist (deployed by CI, gitignored locally)
```

Deps are installed ad hoc (`pip install yfinance pandas numpy requests`) — there is no `requirements.txt`. A local `.venv/` exists. No test suite and no linter configured; verify pipeline changes with `--synthetic` and webapp changes in `npm run dev`.

## Supabase

Schema lives in `webapp/supabase/schema.sql` — run it by hand in the Supabase SQL Editor; there are no migrations. Two access tiers:

| Table | Access |
|---|---|
| `bars`, `analysis_runs` | **Public read** (market data). Writes only via service-role key from the pipeline. |
| `journal_entries`, `training_examples`, `entry_models`, `daily_briefs`, `trade_feedback` | **RLS, owner-only** (`auth.uid() = user_id`). Webapp reads as the logged-in user; the routine reads via an authenticated connector. |

Key invariants:

- **The AI routine's connector runs as `postgres`, so `auth.uid()` is NULL.** RLS is bypassed on reads (the role owns the tables and they aren't `FORCE`d), but every insert into `journal_entries` / `training_examples` / `entry_models` / `daily_briefs` / `trade_feedback` must pass `user_id` explicitly — the columns are `NOT NULL DEFAULT auth.uid()`, so omitting it is a not-null violation. The user id is `c0b48756-5f94-4862-886a-8ecdb7099ef6`. Failure mode is asymmetric and easy to miss: reads look fine while routine writes silently stop.
- **Never put `SUPABASE_SERVICE_ROLE_KEY` anywhere client-side.** It bypasses RLS. It exists only in the GitHub Actions secret and the pipeline env.
- The anon key in `webapp/src/supabaseClient.js` is public by design — RLS is what protects the data, not secrecy. `createClient()` throws synchronously on a malformed URL and would crash the whole app, so keep it a well-formed URL.
- **PostgREST caps responses at 1000 rows.** Always page. Python: `load_all_bars_supabase()`. JS: `fetchAllRows()` in `App.jsx`.
- Write patterns differ per table and are deliberate: `bars` upsert `on_conflict=ts`; `analysis_runs` and `entry_models` are **append-only** (history is the point — you can see the model evolve); `daily_briefs` and `trade_feedback` are one row per user, upserted `on_conflict=user_id` with no history.
- `bars` is pruned to a 30-day window each run.

## Pipeline conventions

- **Features must stay stationary** — returns, spreads, ratios, bps distances. Raw price/EMA levels are deliberately excluded as regressors.
- **No lookahead.** Opening-range features use a running high/low while the window forms, then hold the finalized value. Prev-day and premarket levels are only exposed during RTH (blanked to NaN outside it). Targets are same-session only.
- `compute_features()` is the ground truth for feature definitions — the doc, the PineScript translation, and the webapp all follow it. Changing it means updating `docs/smaug-project-knowledge.md` too.
- Features are recomputed and re-upserted for the **entire** retained window every run, not just new bars, so stored features self-heal when a formula changes.
- The features upsert must resend `open/high/low/close/volume`: Postgres validates NOT NULL against the full candidate row before checking `ON CONFLICT`, so a features-only payload fails even on an existing row.
- Emit `None`, never `NaN`, into JSON — Python's `json` writes a bare `NaN` token that both PostgREST and `JSON.parse` reject. `_json_safe()` and `safe_corr()` handle this.
- Train/test split is **time-based** (last 25% held out). Never shuffle.
- `_raise_for_status()` exists because `requests.raise_for_status()` drops the response body, which is where PostgREST puts the actual error.

## PineScript fragments

`pinescript/README.md` is the authoritative contract — read it before touching any `.pine` file. The load-bearing rules:

- **Fetch order is fixed:** `header.pine` → `rsi_9_21.pine` → `candles_1/2/3.pine` → *generated rules block* → `markers.pine`. `header.pine` must be first (`//@version=5` can have nothing before it); `markers.pine` must be last (it consumes `longEntry`/`shortEntry`/`exitSignal`, defined only by the generated block) and edge-triggers them, so the generated block defines plain state with no once-only guard of its own.
- Fragments are fetched **verbatim** from GitHub raw URLs by the routine and concatenated — they are not templates, and nothing in them should be paraphrased, retyped, or "improved" at generation time. Only the rules block is LLM-generated.
- Pine has no per-file scoping, so every identifier in a fragment carries that file's prefix (`rsi_`, `c1_`, `c2_`, `c3_`) — including one-off locals. `markers.pine` uses `mk_` for its own locals but must keep the three shared names (`longEntry`/`shortEntry`/`exitSignal`) exactly.
- **Never change the marker convention:** `L` green below the bar = long, `S` red above the bar = short, `X` orange at price = exit. The trader relies on these being stable day over day even as thresholds move. Pattern labels are deliberately different (gray `label.new()` bubbles, `barstate.islast`-gated, ATR-offset per file) so they're never mistaken for signals.
- Only feature names from the table in `docs/smaug-project-knowledge.md` may appear in `rules` — they get translated mechanically into Pine.
- Assembled output must contain exactly one `^//@version=` line and exactly one `^indicator(` line; if not, omit `pinescript` from the row rather than storing a broken script.
- Known non-bug: RSI plots on the price scale because the indicator is `overlay=true`. Pine can't mix panes in one script; the trader drags it out once in TradingView.

## Webapp notes

- `webapp/src/App.jsx` is one ~3700-line file holding every component, the theme object `T`, and all Supabase access. Follow the existing inline-style-object convention rather than introducing CSS files.
- **Two themes coexist on purpose.** `T` ("Apex Forge" — Bebas Neue, Lato, warm neutrals) styles the whole app; `B` styles the Morning Brief and Dashboard tabs, imported from a Claude Design project. `docs/design.md` is the contract for `B` — read it before touching either, and don't blend the two palettes into one tab.
- Nav is the `NAV` array — a left sidebar where an item with `children` renders as an expandable group: Morning Brief, Journal, **Modeling** (Charts, Training Data, Indicator), Resources. `tab` state always holds a *leaf* name, never a group name; a group is open exactly when one of its children is active, so there's no separate open/closed state. Component names still use the old labels — Charts renders `TechnicalsTab`, Indicator renders `ModelTab` (backed by the `entry_models` table, so the UI label and schema name diverge by design).
- Charts use `lightweight-charts` v5 (`createChart` + `CandlestickSeries`).
- `vite.config.js` sets `base: "/Smaug/"` for GitHub Pages — don't drop it or asset paths break in production.
- Deploy is automatic on push to `main` touching `webapp/**`.

## Local artifacts

`smaug_results.json`, `smaug_bars.json`, `smaug_report.txt`, `smaug_brief.json` are debug-only leftovers from before the Supabase migration. They're gitignored — Supabase is the source of truth.
