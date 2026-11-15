# Strategy spec: trend-aligned break and retest with QQQ confirmation

**Status: draft for the trader's review.** Nothing in the pipeline, the Lab or the routines reads this file yet. It turns the trader's manual watchlist strategy into rules a program can follow, so it can be tested and, if it earns it, tuned.

Every rule is tagged by where it came from:
- **[Mike]** — the trader's own rule, as he wrote it on 2026-11-14.
- **[Course]** — from the trader's own notes on Scarface Trades' paid "Mastermind" course (mentor "Tony"): multi-timeframe levels, relative strength against QQQ, 9:30–11:30, stop at the last break of structure.
- **[Scarface]** — from Scarface Trades' public YouTube videos (break and retest, named session levels, candle-close stop, at least 2R). Jdub Trades' own rules weren't readable; his material points to the same community.
- **[Default]** — a placeholder chosen here so the rule is computable. Each one is listed under "Open definitions" for the trader to confirm or change.

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
- **Instrument for testing:** the underlying stock first (direction only), then short-dated options on it priced on real option bars. [Default]
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

### EMA bias [Mike 6]
- **Bullish:** 9 EMA > 21 EMA on 5-minute closes. **Bearish:** the reverse.
- Used as a **filter** (must agree with the trade) in the base spec. [Default; Mike wrote "can also help", so it may instead be a tie-breaker. See open definitions.]

### Key levels [Mike 2, 4; named per Scarface]
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

## Open definitions (the trader's call)

Each has a default above so the rule can run. Change any of them before a test is declared.

1. ~~**HTF:** daily and 1-hour, daily only, or Daily > 4H > 1H?~~ **Decided 2026-11-14:** weekly and daily for levels; daily and 1H for trend, plus clear 1H levels.
2. ~~**Trend:** swing structure (HH/HL), the 9/21 EMAs, or both?~~ **Decided 2026-11-15:** both must agree, as the default; structure alone and EMAs alone are tested as variants.
3. **5-minute EMAs at entry:** with the EMAs now part of the daily and 1H trend, must the 5-minute 9/21 also agree when entering?
4. **Which key levels count**, and is a daily swing level as good as prior-day high/low?
5. **Break buffer and retest tolerance:** fraction of ATR, a fixed cents value, or a percent of price?
6. **"Strong" retest candle:** wick and close rules above, or simply a close back beyond the level?
7. **QQQ "confirms":** above/below VWAP, EMA bias, QQQ's own trend, or QQQ breaking its own level at the same time?
8. **Relative strength:** raw return vs QQQ since the open, beta-adjusted, or over the last 30 minutes?
9. **"Ideally":** are the trend and RS rules hard filters, or does a setup score points (a confluence score) and need a minimum?
10. **Partial size at 2.5R**, and does the trailer's stop move to break-even?
11. **5-minute EMAs for the trailer:** 9/21, or another pair?
12. **Time window:** 9:30–11:30 like the course, 9:30–11:00 like the videos, or all day?
13. **Instrument:** shares, or options (which DTE and strike)?

## How this would be tested (proposal only)

See `docs/lab-two-track-proposal.md`. The short version: the break-and-retest trigger alone is already known to be close to random on SPY over a decade (see "No rule has a proven edge" in `docs/smaug-project-knowledge.md`). So the test that matters is the **same signals with the trend and QQQ filters on versus off**, on the watchlist names, against a matched random baseline.
