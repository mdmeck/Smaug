# Watchlist routine — prompt

The "Smaug Watchlist" routine fetches this file from `main` each run. It starts
the scan, then adds Claude's own support and resistance levels to the Watchlist
tab. Paste **only this** into the routine, once:

```
You are the Smaug Watchlist routine.

Fetch https://raw.githubusercontent.com/mdmeck/Smaug/main/docs/watchlist-routine-prompt.md
and carry out the task list under the "Task list" heading, in order, exactly as
written. That document is the instructions; this message only points at them.

If the fetch fails, STOP and report it. Do not improvise from memory.
```

Cron: `30 12,13 * * 1-5`. Routine crons are UTC, so this fires at both 12:30 and
13:30 and step 1 stops the one that isn't 08:30 ET. That keeps the time right
across daylight saving.

## Task list

Run each shell command as its own Bash call. Never chain with `&&` or `;`.

Supabase project id: `uslbwanjvritktievkxc`. Your connector runs as `postgres`.
These are updates to existing rows, so they need no `user_id`.

**1. Check the time.** Run `TZ=America/New_York date '+%A %Y-%m-%d %H:%M'`. If the
hour isn't 08, stop and report `not the 08:30 ET slot`. Otherwise keep that date
as RUN_DATE.

**2. Start the scan.** Pushing to this branch starts `watchlist-scan.yml`:

1. `git fetch origin main`
2. `git checkout -B claude/watchlist-trigger origin/main`
3. `git -c user.name="Smaug Watchlist trigger" -c user.email="noreply@anthropic.com" commit --allow-empty -m "watchlist trigger"`
4. `git push --force origin claude/watchlist-trigger`

If the push fails, redo these steps once. If it fails again, stop and report the
error.

**3. Wait for the rows.** Run this query, then `sleep 60`, and repeat up to 12
times until `n` is above 0:

```sql
select count(*) as n from watchlist_snapshots where run_date = 'RUN_DATE';
```

If `n` is still 0, stop and report it. That's either a market holiday, where the
scan writes nothing on purpose, or a failed scan. Don't write anything.

**4. Read the rows.**

```sql
select id, ticker, price, prev_close, htf_trend, ltf_trend, next_earnings,
       support, resistance, context
from watchlist_snapshots
where run_date = 'RUN_DATE' and error is null
order by sort_order;
```

**5. Pick levels for each ticker.** Read its `context` bars (`daily` is
`[date, o, h, l, c, v]`, `weekly` is `[date, o, h, l, c]`) and choose the levels
you think matter most today.

- Up to 3 supports below `price` and up to 3 resistances above it, nearest first.
  Fewer is fine. Don't pad the list.
- Add something the mechanical levels don't: for example unfilled gaps, the edges
  of a consolidation, a high-volume bar's range, or a round number price has
  actually reacted to. Repeating a mechanical level is fine when you think it's
  the one that matters, and the reason should say why.
- Every level must trace to the bars you were given. Your environment can't reach
  market-data sites, so don't try, and don't use prices from memory.
- `reason` is one short sentence naming what happened there, with a date.
- `summary` is one line on how the ticker is set up today.

These are opinions, labeled as AI on the tab. They aren't a trade signal, so don't
turn them into one.

**6. Write them.** Update each row by `id`, setting only these four columns. Don't
insert rows, and don't touch any other column. The scan owns them, and the rest of
the row isn't yours to correct even if it looks wrong. Report anything suspicious
instead.

```sql
update watchlist_snapshots
set ai_support = $j$[{"price": 512.40, "reason": "..."}]$j$::jsonb,
    ai_resistance = $j$[{"price": 530.00, "reason": "..."}]$j$::jsonb,
    ai_summary = $j$...$j$,
    ai_generated_at = now()
where id = '...';
```

**7. Verify and report.** Run:

```sql
select count(*) as total, count(ai_generated_at) as with_ai, count(error) as failed
from watchlist_snapshots
where run_date = 'RUN_DATE';
```

Report those three numbers and name any failed ticker. Don't report a write you
didn't verify.
