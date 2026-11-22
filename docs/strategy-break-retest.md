# Strategy spec: trend-aligned break and retest with QQQ confirmation

**Status: the Lab's starting point, not a rulebook.** Implemented as `lab/strategies/break_retest.py` (strategy track started 2026-11-22, on the trader's go). Every default below is a key of its `DEFAULTS`, and `midday_review.py` calls the same code for the 2pm review, so the two can't drift. A change to a default here should be made there.

The trader, 2026-11-15: "My strategy isn't fully dialed in, so we don't need to translate it 1:1. The lab should study and refine to maximize profits, not be limited to anything we discuss today."

So this file is the **baseline configuration** of one Lab strategy family. Every rule below is something the Lab may test, retune, replace or drop, and it may add rules that aren't here. The trader's own choices are the defaults the Lab starts from and compares against, nothing more. What the Lab may not loosen is how it judges results: see "Guardrails" at the end.

Every rule is tagged by where it came from:
- **[Mike]** — the trader's own rule, as he wrote it on 2026-11-14.
- **[Course]** — from the trader's own notes on Scarface Trades' paid "Mastermind" course (mentor "Tony"): multi-timeframe levels, relative strength against QQQ, 9:30–11:30, stop at the last break of structure.
- **[Scarface]** — from Scarface Trades' public YouTube videos (break and retest, named session levels, candle-close stop, at least 2R). Jdub Trades' own rules weren't readable; his material points to the same community.
- **[Default]** — a starting value chosen here so the rule is computable. The Lab may tune it.

Research behind this: `/mnt/project-files/smaug-audit/youtube-traders-research-2026-11-14.md` (project files, not in the repo). The course notes themselves stay in the trader's Google Doc and aren't copied here.

## The trader's rules, as written [Mike]

1. Identify higher time frame trends.
2. Identify higher time frame support and resistance.
3. Identify lower time frame trend.
4. Identify lower time frame support and resistance.
5. Watch for a stock forming breaks of structure creating a trend to confirm direction.
6. 9/21 EMAs can also help establish bullish/bearish.
7. Look for a break and retest of a key level.
8. Ideally, don't enter for a move going against the trend, or without relative strength. Use QQQ to confirm the market. This is the edge.
9. Enter on the retest with a 2.5 to 1 ratio.
10. Take profits, but leave trailers until the 5 minute EMAs cross.

## Universe and instrument

- **Symbols:** the watchlist single names in `watchlist_scan.py` (TSLA, AAPL, NFLX, INTC, MSFT, AMZN, GOOG, NVDA, AMD, MU, PLTR), plus SMH. [Mike, his watchlist] QQQ is the market reference, not a traded symbol. [Mike]
- **Instrument:** ATM 0DTE options, the cheapest per trade for his budget. On days a symbol lists no same-day expiry, the nearest expiry. [Mike, decided 2026-11-15] Shares are a diagnostic only, for the direction test on years before option bars exist.
- **Costs:** P/L always uses real option bars with the backtester's spread crossing, stop slippage and fees (`lab/options.py`), never modeled prices, because spread and theta are a much bigger share of a 0DTE premium.
- **Bars:** 1-minute RTH and premarket, split-adjusted, never dividend-adjusted (the repo's existing rule).

## Definitions

### Timeframes
- **HTF for levels:** weekly and daily bars. [Mike, decided 2026-11-14]
- **HTF for trend:** daily bars (swing trend over the last 10–15 sessions) and 1-hour bars (last 4–10 sessions). [Mike, decided 2026-11-14; lookbacks from the Course]
- 1-hour bars also supply key levels, but only clear ones (see Key levels). [Mike, decided 2026-11-14]
- **LTF (lower time frame):** 5-minute bars, built from 1-minute bars. [Default; the course goes straight from 1H to 1m]
- **Execution:** 1-minute bars. [Scarface uses 1-minute]

### Trend [Mike 1, 3, 5, 6]
**Decided 2026-11-14/15 [Mike]:** on each trend timeframe, swing structure and the 9/21 EMAs must both agree. He hasn't checked this against data, so the Lab compares it with the two simpler versions (below).
- **Structure:** up when the last two confirmed swing highs and the last two swing lows are each higher; down when both are lower; otherwise range. Same pivot logic as `compute_features()` (a pivot is confirmed only `SWING_RIGHT` bars later, so nothing sees the future).
- **EMAs:** up when the 9 EMA is above the 21 EMA on that timeframe's closes; down when below.
- **Trend:** up only when structure is up and the EMAs are up; down only when both are down; anything else is range.
- **Variants to test (Lab strategy track):** structure alone; 9/21 EMAs alone; both agree (the default). Each counts as a try for the family.
- **HTF trend:** structure and 9/21 EMAs on daily bars (last 15 sessions) and on 1-hour bars (last 10 sessions), pivots with left/right = 3. [Course windows; pivot size is Default]
- **LTF trend:** structure on 5-minute bars of the current and prior session, left/right = 3. [Default]
- **Break of structure (BOS):** a 1-minute close beyond the most recent confirmed LTF swing high (long) or low (short) by the break buffer. A BOS in the trade's direction is required within the last 60 minutes. [Mike 5; window is Default]

### 5-minute EMA bias at entry [Mike 6]
**Decided 2026-11-15 [Mike]:** ideally the 5-minute 9/21 EMAs agree with the trade at entry; backtest it and track the alternative.
- **Bullish:** 9 EMA > 21 EMA on 5-minute closes. **Bearish:** the reverse.
- **Default:** must agree with the trade at entry (a long needs bullish, a short bearish).
- **Variant to test (Lab strategy track):** no 5-minute check, daily and 1H trend only. Counts as a try for the family.

### Key levels [Mike 2, 4; named per Scarface]
**Decided 2026-11-15 [Mike]:** track them all and let the data decide. The default set is the named levels (prior-week, prior-day, premarket and first 5-minute candle highs and lows, and high/low of day). Swing levels (weekly, daily, clear 1H) are a variant the Lab adds and tracks.

A level is a price. The candidates, all knowable before or at the moment they're used:
- **Weekly:** prior-week high and low; weekly swing highs and lows from the last 26 weeks. [Mike, decided 2026-11-14; the 26-week lookback is Default]
- **Daily:** prior-day RTH high and low; daily swing highs and lows from the last 15 sessions. [Mike, decided 2026-11-14; lookback from the Course]
- **1-hour, clear ones only:** a 1-hour swing high or low from the last 10 sessions counts only if price turned at it at least twice. [Mike: "key levels if there are ones here"; the two-touch test is Default]
- **Multi-touch:** a level counts double if price turned at it at least twice. [Course: "key levels with multiple touches"; the scoring is Default]
- **Session:** premarket high and low (04:00–09:30); first 5-minute candle high and low (09:30–09:35), usable from 09:35.
- Levels within 0.25 × ATR(14, daily) of each other merge into one zone (their average). [Default]
- **Intraday:** high and low of day so far, once price has broken outside them. [Course: "Looking for a trade after breaking outside these pivot points. Otherwise we're trading chop."]

### Break and retest [Mike 7, 9; mechanics per Scarface]
For a long at level L (short is the mirror):
1. **Break:** a 1-minute close above L by at least the break buffer, 0.1 × ATR(14, 1-minute). [Default]
2. **Hold:** no 1-minute close back below L − buffer after the break. A close back inside cancels the setup. [Scarface: "we don't want the stock to come back into this range"]
3. **Retest:** within 20 bars of the break, a bar's low comes within the retest tolerance of L (0.1 × ATR(14, 1-minute) above or below it). [Default]
4. **Confirmation:** the retest bar, or the next bar, closes above L and is a strong candle: close in the top third of its range, and a lower wick at least as long as its body. [Course: "on retrace look for hammer candle"; Scarface: "strong lower wick"; thresholds are Default]
5. **Entry:** at the confirmation bar's close.
- One setup per level per side per day; first valid retest only. [Scarface takes the first retest]

### Market and relative-strength filter [Mike 8, "the edge"; Course: "Intraday relative strength and weakness by comparing to individual stocks and indexes"]
Both must hold at entry, measured from the 09:30 open to the confirmation bar's close:
- **QQQ confirms:** QQQ is on the trade's side of its session VWAP, and QQQ's 5-minute 9/21 EMA bias agrees. [Course example: a short retest "as QQQ also broke VWAP (down)"; the EMA part is Default]
- **Relative strength:** for a long, the stock's return since the open minus QQQ's return since the open is > 0 (for a short, < 0). [Course: "a stock going down slower than indexes has relative strength"; a beta-adjusted version is an open choice]
- **Never fight the market:** no short while QQQ is making new highs of day, no long while it's making new lows. [Course: shorting a weak TSLA while QQQ rallies "would be fighting against the market"]

### Direction filter [Mike 8]
- The trade must agree with the LTF trend, and the HTF trend must not be against it (up or range for longs, down or range for shorts). [Default reading of "ideally don't go against the trend"]

### Time window [Course]
- Entries 09:35–11:30 ET. Everything flat by 15:45. [Course: "Tony only trades 9:30-11:30 AM ET"; Scarface's videos say 9:30–11:00; Mike didn't state one]

### Stop [Scarface; Mike's 2.5:1 implies one]
- **Stop:** the lower of the retest bar's low and L − buffer (long). Exit on a 1-minute close below it. [Scarface: close below the entry candle; Course: "a break of a key level or a break of the trend or market structure (candle close)"]
- Alternative to test: the last confirmed LTF swing low before the break. [Course: "Stop loss at last break of structure"]
- **R** = entry − stop, in the underlying.
- Skip the trade if R < 0.5 × ATR(14, 1-minute) (stop too tight to survive noise) or R > 2 × ATR(14, 5-minute). [Default]

### Target and trailer [Mike 9, 10]
- **Target 1:** entry + 2.5R. Take half off there. [Mike's 2.5:1; "half" is Default]
- **Trailer:** hold the rest until the 5-minute 9 EMA crosses the 21 EMA against the position (on a closed 5-minute bar), or 15:45. [Mike 10; which EMAs is Default]
- If the stop is hit before Target 1, the whole position exits at −1R.
- After Target 1, the trailer's stop moves to entry. [Course: "Move stop loss as you scale"; break-even is Default]
- Alternative to test: target the next key level or high of day instead of a fixed 2.5R. [Course: "take profit at HOD, LOD, Key Levels"]

### Skips
- Range day: both HTF and LTF trend = range. [Scarface skips choppy days]
- No trade on FOMC days, the symbol's earnings day, or quad-witching days. [Course: avoid "JPOW Days", earnings, quad witching]
- Skip a symbol when its options' implied volatility is over 60. [Course: "If IV is over 60, maybe stay out"]

## Decisions and tunable defaults

Questions 1–4 and 13 were the trader's calls. For 5–13 he asked the Lab to find out from data, so the starting values below are chosen from his rules and his course notes, and each comes with the variants the Lab tries first. Nothing here is fixed.

| # | Question | Starting value | First variants to test | Source |
|---|---|---|---|---|
| 1 | Higher timeframes | Levels: weekly + daily. Trend: daily + 1H, plus clear 1H levels | — | Mike, 2026-11-14 |
| 2 | Trend definition | Structure and 9/21 EMAs both agree | Structure only; EMAs only | Mike, 2026-11-15 |
| 3 | 5-minute EMAs at entry | Must agree | No 5-minute check | Mike, 2026-11-15 |
| 4 | Key levels | Named levels | + swing levels (weekly, daily, clear 1H) | Mike, 2026-11-15 |
| 5 | Break buffer, retest tolerance | 0.1 × ATR(14, 1-minute) each | 0.05 and 0.2 × ATR; 5 bp of price | Default; ATR scales across names |
| 6 | Strong retest candle | Hammer: close in top third, lower wick ≥ body | Plain close back beyond the level | Course ("hammer candle") |
| 7 | QQQ confirms | QQQ on the trade's side of its session VWAP | + QQQ 5-minute 9/21 agrees; QQQ's own daily/1H trend agrees | Course (retest "as QQQ also broke VWAP") |
| 8 | Relative strength | Stock return since 09:30 minus QQQ's, > 0 for longs (< 0 for shorts) | Beta-adjusted (60-day beta); last 30 minutes only | Course ("going down slower than indexes") |
| 9 | "Ideally" (filters) | Every filter is a hard rule | Confluence score: pass at least k of the filters | Default; hard rules make each filter's value measurable |
| 10 | Partial and break-even | Half off at 2.5R, then stop to entry | One third off; no partial; no break-even move | Mike (2.5:1), Course ("move stop loss as you scale") |
| 11 | Trailer | Exit the rest on a 5-minute 9/21 cross against the trade | Exit on a close beyond the last 5-minute swing; target the next key level or HOD | Mike (5-minute EMA cross) |
| 12 | Time window | Entries 09:35–11:30 ET, flat by 15:45 | 09:35–11:00; 09:35–15:00 | Course (Tony, 9:30–11:30) |
| 13 | Instrument | ATM 0DTE options (nearest expiry when none is listed that day); shares only as a direction diagnostic | 1–2 DTE; 1 strike OTM | Mike, 2026-11-15 ("cheapest given our budget") |

The rule that matters most to test is #7 and #8 together, the QQQ filter the trader calls "the edge". The first comparison is the full default against the **same signals with each filter switched off**, one at a time, so each filter's contribution is measured on its own.

## Guardrails (what the Lab may not loosen)

The Lab can change any rule above. It can't change how a result is judged. These come from `/mnt/project-files/smaug-audit/lab-plan-2026-11-11.md` and the failed-strategies review of 2026-11-13.

- **Locked periods.** Discovery 2016-01-01 → 2023-12-31 (stock bars only; unlimited looks). Develop 2024-02-15 → 2026-02-24 (real option bars; every run counted). **Holdout 2026-02-25 → 2026-11-10, sealed:** one look for the whole family, logged. Forward 2026-11-11 onward, shadowed nightly.
- **Every try counts.** Each variant or parameter value tried is counted by code for this family, across all sessions of the Lab. The pass bar rises with the count k: the edge's confidence-interval lower bound must clear zero at one-sided 1 − 0.10/k.
- **Edge against random, not against zero.** Edge = average P/L per trade minus a matched random control (same symbols, days and entry times, random direction, same exits and costs). Only edge decides.
- **Search in stages, not all at once.** The table above has thousands of combinations. Change one row at a time from the defaults on the discovery period (free, underlying only), keep only rows that beat the default there, then tune the survivors on develop. A full grid would push k so high that nothing could pass.
- **Plateaus, not peaks.** A tuned value only counts if its neighbours also beat random.
- **Post-hoc findings are recorded, never built on.** A good year, a good VIX bucket or a good symbol found by looking becomes a new hypothesis, judged on data it wasn't found in.
- **Both gates.** A pass needs the direction test on stock bars and positive edge on real option prices.
- **Symbol-list bias.** The watchlist was picked recently, partly because those names moved. Run the same configuration on a fixed older list as a control universe.

## How this would be tested (proposal only)

See `docs/lab-two-track-proposal.md`. The short version: the break-and-retest trigger alone is already known to be close to random on SPY over a decade (see "No rule has a proven edge" in `docs/smaug-project-knowledge.md`). So the test that matters is the **same signals with the trend and QQQ filters on versus off**, on the watchlist names, against a matched random baseline.
