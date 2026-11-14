# Strategy spec: trend-aligned break and retest with QQQ confirmation

**Status: draft for the trader's review.** Nothing in the pipeline, the Lab or the routines reads this file yet. It turns the trader's manual watchlist strategy into rules a program can follow, so it can be tested and, if it earns it, tuned.

Every rule is tagged by where it came from:
- **[Mike]** — the trader's own rule, as he wrote it on 2026-11-14.
- **[Scarface]** — borrowed from Scarface Trades' public videos (break and retest, 9:30–11:00, named levels, candle-close stop). Jdub Trades' own rules weren't readable; his material points to the same community.
- **[Default]** — a placeholder chosen here so the rule is computable. Each one is listed under "Open definitions" for the trader to confirm or change.

Research behind this: `/mnt/project-files/smaug-audit/youtube-traders-research-2026-11-14.md` (project files, not in the repo).

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
- **HTF (higher time frame):** daily bars. [Default]
- **LTF (lower time frame):** 5-minute bars, built from 1-minute bars. [Default]
- **Execution:** 1-minute bars. [Scarface uses 1-minute]

### Trend [Mike 1, 3, 5]
Trend comes from swing structure, using the same pivot logic as `compute_features()` (a pivot is confirmed only `SWING_RIGHT` bars later, so nothing sees the future).
- **Up:** the last two confirmed swing highs and the last two swing lows are each higher. **Down:** both lower. Otherwise **range**.
- **HTF trend:** structure on daily bars, pivots with left/right = 3. [Default]
- **LTF trend:** structure on 5-minute bars of the current and prior session, left/right = 3. [Default]
- **Break of structure (BOS):** a 1-minute close beyond the most recent confirmed LTF swing high (long) or low (short) by the break buffer. A BOS in the trade's direction is required within the last 60 minutes. [Mike 5; window is Default]

### EMA bias [Mike 6]
- **Bullish:** 9 EMA > 21 EMA on 5-minute closes. **Bearish:** the reverse.
- Used as a **filter** (must agree with the trade) in the base spec. [Default; Mike wrote "can also help", so it may instead be a tie-breaker. See open definitions.]

### Key levels [Mike 2, 4; named per Scarface]
A level is a price. The candidates, all knowable before or at the moment they're used:
- **HTF:** prior-day RTH high and low; daily swing highs and lows from the last 20 sessions.
- **Session:** premarket high and low (04:00–09:30); first 5-minute candle high and low (09:30–09:35), usable from 09:35.
- Levels within 0.25 × ATR(14, daily) of each other merge into one zone (their average). [Default]

### Break and retest [Mike 7, 9; mechanics per Scarface]
For a long at level L (short is the mirror):
1. **Break:** a 1-minute close above L by at least the break buffer, 0.1 × ATR(14, 1-minute). [Default]
2. **Hold:** no 1-minute close back below L − buffer after the break. A close back inside cancels the setup. [Scarface: "we don't want the stock to come back into this range"]
3. **Retest:** within 20 bars of the break, a bar's low comes within the retest tolerance of L (0.1 × ATR(14, 1-minute) above or below it). [Default]
4. **Confirmation:** the retest bar, or the next bar, closes above L and is a strong candle: close in the top third of its range, and a lower wick at least as long as its body. [Scarface's "strong lower wick"; thresholds are Default]
5. **Entry:** at the confirmation bar's close.
- One setup per level per side per day; first valid retest only. [Scarface takes the first retest]

### Market and relative-strength filter [Mike 8, "the edge"]
Both must hold at entry, measured from the 09:30 open to the confirmation bar's close:
- **QQQ confirms:** QQQ is on the trade's side of its session VWAP, and QQQ's 5-minute 9/21 EMA bias agrees. [Default definition of "confirm"]
- **Relative strength:** for a long, the stock's return since the open minus QQQ's return since the open is > 0 (for a short, < 0). [Default; a beta-adjusted version is an open choice]

### Direction filter [Mike 8]
- The trade must agree with the LTF trend, and the HTF trend must not be against it (up or range for longs, down or range for shorts). [Default reading of "ideally don't go against the trend"]

### Time window [Scarface]
- Entries 09:35–11:00 ET. Everything flat by 15:45. [Scarface window; Mike didn't state one]

### Stop [Scarface; Mike's 2.5:1 implies one]
- **Stop:** the lower of the retest bar's low and L − buffer (long). Exit on a 1-minute close below it. [Scarface: close below the entry candle]
- **R** = entry − stop, in the underlying.
- Skip the trade if R < 0.5 × ATR(14, 1-minute) (stop too tight to survive noise) or R > 2 × ATR(14, 5-minute). [Default]

### Target and trailer [Mike 9, 10]
- **Target 1:** entry + 2.5R. Take half off there. [Mike's 2.5:1; "half" is Default]
- **Trailer:** hold the rest until the 5-minute 9 EMA crosses the 21 EMA against the position (on a closed 5-minute bar), or 15:45. [Mike 10; which EMAs is Default]
- If the stop is hit before Target 1, the whole position exits at −1R.
- After Target 1, the trailer's stop moves to entry. [Default]

### Skips
- Range day: both HTF and LTF trend = range. [Scarface skips choppy days]
- No trade within 5 minutes of a scheduled macro release (CPI, FOMC, NFP) if that calendar is available. [Default; optional]

## Open definitions (the trader's call)

Each has a default above so the rule can run. Change any of them before a test is declared.

1. **HTF:** daily, or 1-hour? (Scarface uses the 1H candle before the open as bias.)
2. **Trend:** swing structure (HH/HL), the 9/21 EMAs, or both?
3. **EMA role:** a hard filter, or only a tie-breaker when structure is unclear?
4. **Which key levels count**, and is a daily swing level as good as prior-day high/low?
5. **Break buffer and retest tolerance:** fraction of ATR, a fixed cents value, or a percent of price?
6. **"Strong" retest candle:** wick and close rules above, or simply a close back beyond the level?
7. **QQQ "confirms":** above/below VWAP, EMA bias, QQQ's own trend, or QQQ breaking its own level at the same time?
8. **Relative strength:** raw return vs QQQ since the open, beta-adjusted, or over the last 30 minutes?
9. **"Ideally":** are the trend and RS rules hard filters, or does a setup score points (a confluence score) and need a minimum?
10. **Partial size at 2.5R**, and does the trailer's stop move to break-even?
11. **5-minute EMAs for the trailer:** 9/21, or another pair?
12. **Time window:** first 90 minutes like Scarface, or all day?
13. **Instrument:** shares, or options (which DTE and strike)?

## How this would be tested (proposal only)

See `docs/lab-two-track-proposal.md`. The short version: the break-and-retest trigger alone is already known to be close to random on SPY over a decade (see "No rule has a proven edge" in `docs/smaug-project-knowledge.md`). So the test that matters is the **same signals with the trend and QQQ filters on versus off**, on the watchlist names, against a matched random baseline.
