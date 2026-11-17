# Lab: defined-risk 0DTE credit spreads on paper — proposal

Status: **the trader said go on 2026-11-17. The cheap gate failed, so the
build stopped at step 3, as planned.** What's on this branch: the
simulator, the random control and edge, the research commands, the paper
probe and the gate workflow. The broker and runner trading path, schema, Lab
tab and charter edit were not built. Results are below.

## Why

The Lab's 2026-11-16 `event_straddle` study (journal, scratch script, real
Alpaca option bars, `lab.options` costs, in-sample 2024-02-15 → 2026-02-24,
holdout untouched, 1 variant) found that **buying** the ATM 0DTE straddle
loses on ordinary days, with a 90% CI that excludes zero:

| Window (non-event days) | n | Long straddle avg | 90% CI | Round-trip cost |
|---|---|---|---|---|
| 09:35 → 10:30 | 474 | −$10.9 | [−16.3, −5.3] | $7.0 |
| 13:45 → 15:30 | 484 | −$19.4 | [−26.0, −12.3] | $5.0 |

That money goes to the seller, but the seller pays its own round of costs.
Mirroring the long result and charging costs again gives roughly:

| Window | Short straddle, after costs (approx.) | Approx. 90% CI |
|---|---|---|
| 09:35 → 10:30 | about −$3 | about [−9, +2] |
| 13:45 → 15:30 | about +$9 | about [+2, +16] |

So the edge on the short side **survives costs only in the afternoon
window**, where 0DTE theta is fastest. These are back-of-envelope numbers
from one in-sample study of naked straddles. A straddle is not a spread:
buying wings caps the loss but gives back part of the premium, and the
untested part is whether what's left still beats costs. That is the question
this proposal lets the Lab answer.

The Lab can't test it today. `lab/broker.py` allows only buy-to-open single
options and sell-to-close up to what's held, and the charter keeps the
routine from building toward short premium.

## What the rule change allows

One new order shape, **paper only**, on top of the existing two.

**Allowed structures (SPY, 0DTE, same expiry, equal quantity on every leg):**

1. **Put credit spread:** sell 1 OTM put, buy 1 put at a lower strike.
2. **Call credit spread:** sell 1 OTM call, buy 1 call at a higher strike.
3. **Iron condor:** one put credit spread plus one call credit spread, with the
   short strikes on opposite sides of spot.

**Never allowed, checked in code:**

- A short option without a long option of the same right and expiry, further
  OTM, in equal or larger quantity. No naked shorts, at any moment, including
  between legs.
- Legging in or out. Every open and every close is one atomic multi-leg order
  (Alpaca `order_class: "mleg"`), net-credit limit to open, net-debit limit
  or market to close, TIF day. If the account can't place `mleg` orders, the
  spread path stays backtest-only. There is no fallback to separate legs.
- Closing the long leg while its short leg is open.
- Holding into the close. Flat by 15:45, so there's no pin, assignment or
  exercise risk.
- Live trading of spreads. `broker.open_spread()` refuses unless
  `alpaca.is_paper()` is true, whatever `lab_controls.mode` or the strategy's
  status says. Going live with spreads needs another rule change and the
  trader's call.
- Any other symbol, DTE, ratio, calendar or debit structure.

**New `lab_controls` columns (trader-owned, set from the Lab tab, like the rest):**

| Column | Default | Meaning |
|---|---|---|
| `spreads_enabled` | `false` | Master switch for the spread path. Off means `open_spread()` refuses. |
| `max_spread_width` | `2` | Widest allowed distance between short and long strike, dollars. |
| `max_spread_risk` | `200` | Max loss on one entry: (width − credit) × 100 × contracts, dollars. |

The existing limits apply to spreads as well: kill switch, entry window,
`max_contracts`, `max_trades_per_day`, `max_open_positions` (a spread or
condor counts as one position) and `max_daily_loss` (marked on the combo).

**Why max loss is bounded even if the runner dies:** options take no bracket
or OCO orders, so the runner manages exits itself. For a long option the
resting stop is the backstop. For a spread, the backstop is the structure:
the worst case is width − credit, and that is what `max_spread_risk` limits.

## Code changes it needs (for the later build PR)

| File | Change |
|---|---|
| `lab/strategies/__init__.py` | `Signal` gains `structure` (`single` default, `put_credit`, `call_credit`, `iron_condor`), `short_otm` (strikes, or a distance rule in `meta`) and `width`. Single-leg strategies are untouched. |
| `lab/backtest.py` | `simulate_spread()`: each leg priced on its own real option bars. Entry needs a bar on **every** leg within `ENTRY_GRACE`, or the trade is unfilled. Credit = Σ `sell_fill(short)` − Σ `buy_fill(long)`. Exits (profit target as a fraction of credit, stop as a multiple of credit, underlying touching the short strike, time) are decided on a bar's close and filled at the next bar's open, crossing the spread on every leg, with `STOP_SLIP_FRAC` on stop exits. Fees = `FEE_PER_CONTRACT` × legs × 2 sides. A short leg with no trade for over 3 minutes doesn't trigger an exit on a stale price. A close that would cost more than the width plus crossing it is a stale print on one leg, so it's capped there and flagged (`capped`), and the evaluate report counts those. |
| `lab/broker.py` | `open_spread()` and `close_spread()`, the only new calls to `alpaca.submit_order`. Both re-read `lab_controls`. A `no_naked_shorts(positions)` invariant runs before every order and after every fill. If it ever fails, the runner buys the uncovered short to close, sets nothing in `lab_controls` (that's the trader's), stops entering for the day and logs an error. |
| `lab/runner.py` | Routes `structure != single` signals to the spread path. Quote check on every leg, with the existing `MAX_ENTRY_SPREAD` applied to the combo's net bid/ask. |
| `lab/replay_test.py` | Fake-broker sessions for spreads: a fill, a stop, a time exit, a partial fill (must be impossible with `mleg`), and an injected naked-short state, which must be flattened. |
| `webapp/supabase/schema.sql` | The three `lab_controls` columns. `lab_trades` gains `legs jsonb` and `max_loss`. |
| Lab tab (`App.jsx`) | Controls for the three columns. Spread trades shown as one row with their legs. |
| `docs/lab-routine-prompt.md` | Charter edit below. |

**Charter edit** (hard rules section), shipped with the code:

> 8. **Spreads are paper only and defined-risk only.** You may write strategy
>    modules that emit `put_credit`, `call_credit` or `iron_condor` signals.
>    You may never write code that sells an option without a matching long
>    leg, legs into a spread, or lets a spread trade live. Changes to
>    `simulate_spread`, `broker.py` or the cost model still go on a branch for
>    the trader.

And under "Research discipline": modeled (Black-Scholes) pricing is never
used for short OTM legs, even for exploration, because a flat-vol model
misses the put skew that the seller is being paid for.

## Test plan

Written before any test, the way the charter asks for hypotheses.

**Hypothesis.** Short 0DTE SPY premium earns the variance risk premium, and
it's largest in the afternoon, when time decay is fastest. A defined-risk
spread keeps enough of it to beat costs.

**Family `credit_spread`, frozen grid (24 variants, all counted):**

- Structure: put credit, call credit, iron condor (3)
- Entry: 10:00 or 13:45 (2). The morning cell is expected to fail, given the
  straddle numbers. It stays in as a check that the finding is real and not
  an artifact of the window.
- Short strike: 1.0× or 1.5× the expected move to the close, taken from the
  ATM straddle mid at entry (2)
- Width: $1 or $2 (2)
- Exits fixed, not tuned: take profit at 50% of credit, stop at a loss of 2×
  credit, time exit 15:45, one trade per day.

Anything outside this grid is a new variant and raises the bar.

**Periods.** In-sample 2024-02-15 → 2026-02-24, real option bars only.
Holdout: the last 25% of sessions as `backtest.stats()` cuts them (from
2026-02-25), **looked at once**, for the one variant chosen in-sample, and
logged. If it fails, the family's holdout is spent and further variants go
to forward shadowing only.

**Cheap gate first.** Before the full grid, the iron condor at 13:45, 1.0×,
$2 on in-sample. **Falsified if** avg P/L after costs ≤ 0. Then the
family stops, and the journal says the straddle finding didn't survive the
wings.

**Random baseline.** The matched random control from the 2026-11-11 Lab plan
isn't in code yet, so the build adds it, adapted for spreads: same days,
same structure, same strike rule, same exits, entry at a random minute
between 09:45 and 14:30, 20 draws, seed 7. **Edge = strategy avg − control
avg.** For a fixed-time rule this asks whether the entry time matters. A
second, plainer baseline is the same spread entered every day at 10:00 with
no filter.

**Pass bar to `paper`** (all of these):

- The charter bar: ≥ 100 filled real-priced trades; in-sample and holdout
  avg P/L > 0; holdout profit factor ≥ **1.20** (1.15 + 0.05 for the 14
  variants beyond the first 10); bootstrap 90% CI lower bound on avg P/L
  > −$5; positive in at least 2 of 2024, 2025, 2026; `causal` passes.
- Edge over the random control > 0, with its CI lower bound above zero at
  one-sided confidence 1 − 0.10/k (k = 24 → 99.6%), the two-track proposal's
  rising bar.
- **Cost stress:** avg P/L still > 0 at 2× `HALF_SPREAD_FRAC` and
  `HALF_SPREAD_FLOOR`, and at $0.65 per contract per side (a typical retail
  commission, for when this ever runs somewhere that charges one).
- **Tail report, not a gate:** the five worst days, plus 2024-08-05 and the
  April 2025 tariff week, each shown alone with their loss against
  `max_spread_risk`. A spread seller wins often and loses big, so the trader
  should see the big days.

**Paper phase.** On a pass, at most 1 contract and the default limits above.
Forward review per the charter (≥ 40 trades over ≥ 4 weeks), plus a fill
check: average paper credit versus the backtest's modeled credit on the same
signals. If paper fills are worse than modeled by more than the modeled cost,
the cost model is wrong, and that's fixed on a branch before anything else.
Promotion past paper isn't possible under this change.

## Build order, once the trader says go

1. Probe (read-only plus one paper order): confirm the paper account's
   options level allows spreads and that an `mleg` order is accepted. Submit
   one far-from-market combo limit on **paper** and cancel it. If either
   fails, stop at backtest-only.
2. `simulate_spread()`, the random control and the replay tests.
3. The cheap gate. If it fails, stop; the broker and runner work isn't needed.
4. `broker.py` / `runner.py` / schema / Lab tab and the charter edit, on a
   branch for review.
5. The full grid and the holdout look, by the routine, under the new charter.

## Open questions for the trader

- Are the defaults right: $2 max width, $200 max loss per trade, off until
  you switch it on?
- Iron condors in the first family, or verticals only to start?

## Results, 2026-11-17

**Paper probe (step 1): passed.** The paper account is at options level 3
(approved 3). One put credit spread far out of the money, priced at a $4.50
credit on a $5 width so it couldn't fill, was accepted as an `mleg` order,
canceled, filled 0, and nothing was held afterward.

**Cheap gate (step 3): failed.** Iron condor at 13:45, short strikes 1.0×
the straddle, $2 wings, 50% target, 2× stop, flat by 15:45. In-sample
2024-02-15 → 2026-02-24, real option bars, locked holdout (from 2026-02-25)
not touched. Run `Lab spreads` #2, `research evaluate`.

| | Value |
|---|---|
| Filled trades | 487 (20 skipped: 7 thin credit, 7 no leg trade, 6 no straddle) |
| Win rate | 70.6% |
| Avg P/L per condor | **−$6.08**, 90% CI [−8.27, −4.09] |
| Profit factor | 0.48 |
| By year | 2024 −$2.97 · 2025 −$8.55 · 2026 −$7.99 |
| Edge over random entry time (20 draws) | +$0.15, 90% CI [−1.86, +2.09] |
| At 2× spread + $0.65 commission | −$18.51 |
| Worst day | 2025-04-08, −$153 (avg max loss $177) |

The falsifier (avg ≤ 0) is met, so the family stops here and nothing goes
further without the trader.

**Reading.** The condor wins 7 times in 10 and still loses, because the
average loser is about 5× the average winner (from the 70.6% win rate and
0.48 profit factor). Costs decide it. Eight leg crossings at the
model's 1¢ minimum plus fees come to about $8.40 a condor, so before costs it
makes roughly +$2. Even if real fills were twice as good as the model, it
would still be about −$2. The entry time adds nothing over random: edge
≈ 0. The afternoon seller's premium the straddle study pointed at is there,
but it's about $2 on a condor, which isn't enough to pay for four legs.

**What could change the verdict, and what can't.** The model's half-spread
floor (1¢ a leg) was set for single options. If real 0DTE SPY legs this far
out trade a penny wide and `mleg` orders fill near mid, the true cost could
be well under $8. That's a question about the cost model, which the charter
says to settle against real fills or quotes, never by loosening it to
rescue a result. Widening the grid (other structures, strikes, times) would
spend tries and raise the bar without touching the cost problem.

## Grid 2, frozen 2026-11-17 before it was run

The trader's point: a profitable setup combines a position type with entry
signals, confirmation, stops, profit taking and exits. The cheap gate tested
the condor bare. Grid 2 adds a day filter and compares management, still on
the in-sample years only (2024-02-15 → 2026-02-24). The locked holdout stays
untouched. Backtest only: no orders, no settings.

12 variants, all iron condors at 13:45 with short strikes 1.0× the straddle,
flat by 15:45 at the latest (workflow `Lab condor grid`):

| Knob | Values |
|---|---|
| Wing width | $2 (the cheap gate's), $5 |
| Day filter | none · `range` (spot inside the first-hour range and the day's range so far ≤ 0.75× the prior 20-session mean range) · `iv_rich` (prior VIX close ≥ 1.25× SPY's 20-session realized vol) |
| Management | managed (take profit at 50% of credit, stop at a 2× credit loss) · held to 15:45 |

Every closing leg pays the model's crossing and fee, whichever exit fires.
Each variant gets the matched random control (same days, random entry
minute 09:45–14:30, 20 draws) and the 2× spread + $0.65 cost stress. The
family now counts 12 tries, so the edge must clear zero at 1 − 0.10/12
≈ 99.2% one-sided. A filter is judged two ways: against the random-minute
control and against the same width and management with no filter.

**What would count as worth a holdout look**, all on in-sample data:
avg P/L > 0 with its 90% CI lower bound above −$5, PF ≥ 1.20, positive in
at least 2 of 3 years, an edge lower bound above zero at 12 tries, and avg
P/L above zero under the cost stress, or a written case for why the stress
is too harsh. Anything short of that is logged and stops. Nothing goes to
the holdout, paper or live without the trader's go.
