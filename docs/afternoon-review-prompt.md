# Afternoon review routine — prompt

The "Smaug Afternoon Review" routine runs at 14:00 ET on weekdays. It starts
`midday_review.py` (a plain script in GitHub Actions, no AI), waits for it, and
turns its rows into one short message for the trader: which setups the
strategy's default rules would have taken this morning, how they played out,
and which of his own journal trades are worth saving as examples.

The one write, the push that starts the script, lives in the routine's **own
prompt**, not in this file. A scheduled run won't take a write step because a
fetched file says to, so when the push lived in this file the 2026-11-16 run only
read, found no midday run and stopped. Everything this file asks is read-only.

Why a push and not Actions' `schedule`: those crons ran 3.5 to 6 hours late in
Nov 2026 (`smaug-daily.yml`, set for 21:30 UTC, ran between 01:03 and 03:43 UTC),
so a schedule can't be trusted to land before 2pm. A push starts the workflow
within seconds.

Paste **only this** into the routine, once:

```
You are the Smaug Afternoon Review routine.

First, start today's midday script. In the mdmeck/Smaug checkout, run each
command as its own Bash call (never chain with && or ;):

1. date -u '+%Y-%m-%dT%H:%M:%SZ'   (keep the output as TRIGGER_AT)
2. git fetch origin main
3. git checkout -B claude/midday-trigger origin/main
4. git -c user.name="Smaug Midday trigger" -c user.email="noreply@anthropic.com" commit --allow-empty -m "midday trigger"
5. git push --force origin claude/midday-trigger

If step 5 fails, redo steps 2 to 5 once. If it fails again, your whole message
is "The 2pm review didn't run today: I couldn't start the midday script." and
you stop.

Then fetch https://raw.githubusercontent.com/mdmeck/Smaug/main/docs/afternoon-review-prompt.md
and carry out the task list under the "Task list" heading, in order, exactly as
written, using TRIGGER_AT. That task list only reads Supabase and writes your
message. If the fetch fails, STOP and report it. Do not improvise from memory.
```

Schedule: weekdays 14:00 America/New_York. The routine needs the mdmeck/Smaug
repo (push to `claude/midday-trigger` only) and the Supabase connector.

This routine is the only part of the 2pm review that uses AI. Finding setups,
grading them and matching the journal are all done by the script, so this
routine reads a handful of rows and writes a few lines.

## Task list

Run each shell command as its own Bash call. Never chain with `&&` or `;`.

Supabase project id: `uslbwanjvritktievkxc`. Your connector runs as `postgres`.
**This task list only reads.** Write nothing to any table, push nothing, open no
PR, place no order, and send nothing anywhere but your final message. The push
that started the script was your prompt's step, not this file's.

**1. Get the date.** Run `TZ=America/New_York date '+%Y-%m-%d'` and keep it as
RUN_DATE. If your prompt gave you no TRIGGER_AT, leave the `generated_at`
condition out of the next query, so any run from today counts.

**2. Wait for today's run.** Run this query, then `sleep 60`, and repeat up to 10
times until it returns a row:

```sql
select market_open, errors
from midday_runs
where run_date = 'RUN_DATE' and generated_at >= 'TRIGGER_AT';
```

"No run today" means this query still returns no row after the 10th try. Then
your whole message is: `The 2pm review didn't run today: the midday script didn't finish.` Stop.

If `market_open` is false, your whole message is: `Market's closed today, so there's no review.` Stop.

**3. Read the setups.** Times come back already in ET.

```sql
select ticker, side, round(level::numeric, 2) as level, level_tags,
       to_char(entry_at at time zone 'America/New_York', 'FMHH12:MI AM') as entry_time,
       round(entry::numeric, 2) as entry, round(stop::numeric, 2) as stop,
       round(target::numeric, 2) as target, passed, failed, outcome,
       to_char(exit_at at time zone 'America/New_York', 'FMHH12:MI AM') as exit_time,
       r_multiple
from midday_setups
where run_date = 'RUN_DATE'
order by passed desc, entry_at;
```

**4. Read the run.**

```sql
select to_char(data_through at time zone 'America/New_York', 'FMHH12:MI AM') as data_through,
       journal, errors
from midday_runs
where run_date = 'RUN_DATE';
```

`journal` is a list, one entry per journal trade logged today, each with
`ticker`, `direction`, `entry_time` (ET, 24-hour), `result` (dollars),
`in_window`, `setup` (the matched setup, or null), `already_saved_as` and
`example_candidate`.

**5. Write the message.** It reads as a text on a phone, so keep it short. Use
only what steps 3 and 4 returned. Don't add market commentary, levels or prices
from anywhere else.

- **First line:** the count and the headline, e.g. `2 setups this morning, one hit its target.` If no row has `passed` true, say so plainly.
- **Then one line per passed setup**, entry time first: ticker and side, the level by its tags (PDH, PMH, OR5H, PWL, HOD…) and price, and how it went in R. Example: `- 10:14 AM SPY long, retest of PDH 757.80: hit 2.5R, trailer still open at +1.9R.` For `outcome`:
  - `stopped`: "stopped out at −1.0R" (use `r_multiple`).
  - `target, then trailer`: "hit 2.5R, trailer out at 1:05 PM, +1.9R total".
  - `open`: "still open at DATA_THROUGH, +0.4R so far" (or "−0.3R so far").
  - `flat`: "flat at the 3:45 cutoff, +0.6R".
- **Near misses, one line at most**, and only when a non-passing setup would have made at least +1R: name it and the one filter that stopped it, in plain words from the table below. Skip this line otherwise.
- **Journal, one line per trade where `example_candidate` is true:** `Your 10:08 AM SPY put matches the PDL retest, a good Break and Retest example to save.` Then, if there were other trades, one line for them together, e.g. `Your other 2 trades didn't match a setup.` (or say they were after 11:30 when `in_window` is false). If `journal` is empty, add: `No trades logged for today yet.`
- **Errors:** if `errors` names symbols, one closing clause: `TSLA and MU had no data today.`
- **Nothing at all** (no setups of any kind, and `journal` empty): your whole message is `No setups this morning.`

Plain words for `failed`:

| value | say |
|---|---|
| `ltf_trend` | the 5-minute trend wasn't with it |
| `htf_trend` | the daily or 1-hour trend was against it |
| `ema5` | the 5-minute 9/21 EMAs disagreed |
| `bos` | no break of structure in the last hour |
| `qqq_vwap` | QQQ was on the wrong side of VWAP |
| `relative_strength` | no relative strength against QQQ |
| `fighting_market` | QQQ was making new highs/lows against it |
| `stop_too_tight` / `stop_too_wide` | the stop was too tight / too wide |
| `earnings_day` | earnings day |
| `quad_witching` | quad witching |
| `qqq_missing` | QQQ data was missing |

R is on the underlying, from the spec's stop and 2.5R target. It isn't option
P/L, so don't put dollar figures on the setups.

Don't save examples, label trades or change anything. Saving an example is the
trader's call; the line in your message is the suggestion.
