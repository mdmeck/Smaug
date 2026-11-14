# Proposal: a two-track Lab

**Status: proposal for the trader's review. Nothing is built.** The Lab build has been on hold since 2026-11-12; this only describes the idea so it can be judged before any code changes.

## The idea

- **Track A, "the playbook":** encode the trader's own strategy (`docs/strategy-break-retest.md`) as one Lab module family, test whether it has an edge, and tune its few parameters without fooling ourselves.
- **Track B, "open research":** the Lab keeps doing what it does now, looking for any automatable strategy with real expectancy, under the lessons of the six retired rules.

Both tracks share one scoreboard, one set of locked periods and one way of counting tries, so a result in either is comparable to the other.

## Why Track A is worth a slot

- The six retired Lab rules all inferred direction from SPY's own minute bars, and all sat near 50/50. The journal's lesson was that new ideas need **outside information**.
- The trader's QQQ confirmation and relative-strength rule is exactly that: a second tape, and the stock's behaviour relative to it.
- The break-and-retest trigger by itself is already known to be close to random on SPY (2016–2026, every level tested, 25–27% Good vs random; see `docs/smaug-project-knowledge.md`). So Track A's question is narrow and answerable: **do the trend and QQQ filters turn a coin-flip trigger into an edge?**

## Track A: how it would work

1. **Fidelity check first.** Before any tuning, run the module over the sessions in `journal_entries` and compare its signals with the trader's real trades. If it doesn't flag most of what he took, the spec is wrong, not the market. Fix the spec with him, not the parameters.
2. **Underlying gate (free).** On 2016–2023 stock bars for the watchlist names, check P(+1R before −1R) for:
   - the trigger alone (filters off),
   - the trigger with the trend filter,
   - the trigger with trend + QQQ confirmation + relative strength (the full spec),
   - a matched random control (same symbols, days and entry times, random side, same exits).
   Pass bar declared in advance: the full spec beats both the filters-off version and random at z ≥ 2.
3. **Filter vacuity check.** Report how often each filter actually changes the signal. pdhl_break's VWAP filter changed 4 of 255 trades; a filter that rarely bites can't be the edge.
4. **Tune on develop only.** Real option bars, 2024-02-15 → 2026-02-24. A small grid declared before running (at most ~24 cells): break buffer, retest tolerance, target R, time window end. Every cell is counted as a try for the family.
5. **One holdout look.** 2026-02-25 → 2026-11-10, once, for the chosen cell. If it fails, the family's holdout is spent and further variants go to forward shadowing only.
6. **Forward shadow.** Every night, run the module on the latest session with no orders, alongside its random control, so a forward record grows whether or not anything passes.

## Guarding against overfitting (both tracks)

These come from the 2026-11-11 Lab plan and the 2026-11-13 failed-strategies review:

- **Locked periods in code:** discovery 2016–2023 (underlying only, unlimited looks), develop 2024-02-15 → 2026-02-24, holdout 2026-02-25 → 2026-11-10 (sealed, one look per family, logged), forward 2026-11-11 onward.
- **Count every try.** Tries are counted by code per family, not typed into the journal. The pass bar rises with them: the edge CI's lower bound must clear zero at one-sided confidence 1 − 0.10/k, where k is tries in the family.
- **Matched random baseline.** Edge = strategy P/L per trade minus a random-entry control that pays the same costs on the same days and times. Only edge decides.
- **Plateaus, not peaks.** A tuned cell only counts if its neighbours in the grid are also positive. A parameter that only works at one value is noise.
- **Post-hoc cells are recorded, never built on.** That includes the recurring VIX 15–20 bucket and single good years.
- **Both gates.** A pass needs the underlying direction test and the option P/L, because rich premium can lose money on a right call.

## Track A specific risks

- **Survivorship in the symbol list.** The watchlist was picked today, partly because those names have moved. Results on them over 2016–2026 will look better than a fresh list would. Mitigation: also run a fixed, older list (for example the 2016 top-20 Nasdaq-100 weights) as a control universe.
- **Single-name option liquidity.** Option bars start 2024-02-15 and some names trade thin strikes; costs may be worse than SPY's.
- **The runner is SPY-only today.** Paper-trading a single-name strategy would need runner and broker changes later. Not part of this proposal.

## What would need building (later, on the trader's go)

- QQQ minute bars in the strategy context, next to the traded symbol (`lab/data.stock_minutes` already fetches any ticker).
- Backtest support for symbols other than SPY.
- The evaluation pieces from the 2026-11-11 plan: locked periods, matched random control, try counting, the scoreboard table and the nightly shadow job.
- A split of the Lab routine's runs between the two tracks, for example alternate runs, or Track A gets one experiment per week.

## Open choices

1. Should Track A take every run until its underlying gate is decided (it's cheap), then share?
2. Shares or options for the forward record of the playbook?
3. Fixed control universe: which list?
