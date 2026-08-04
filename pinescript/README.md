# pinescript/

Static, hand-authored Pine Script v5 fragments for the "Smaug Entry Model" TradingView indicator. The daily routine that synthesizes `entry_models` rows fetches these files **verbatim** from GitHub and concatenates them with one freshly-generated block (the day's entry/exit rule thresholds) to produce the final `pinescript` column. This exists so the deterministic parts of the indicator (candlestick pattern math, RSI, swing-structure detection, the entry/exit marker convention) never drift day to day — only the rule thresholds, which are supposed to change as new data comes in, are LLM-generated.

## Fetch order (fixed — do not reorder)

Concatenate raw fetches of these files, in this exact order, each preceded by a `// === <filename> ===` comment:

1. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/header.pine` — `//@version=5` + the one `indicator()` declaration. Must be first; the version pragma cannot have anything before it.
2. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/rsi_9_21.pine`
3. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/candles_1.pine`
4. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/candles_2.pine`
5. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/candles_3.pine`
6. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/structure.pine` — swing pivots, BOS, CHoCH. Must come *before* the generated block, which consumes its `st_*` values.
7. **Generated block** (not a file here) — `input.float`/`input.int` per rule threshold, feature recomputation, and `longEntry`/`shortEntry`/`exitSignal` boolean definitions. See the "PineScript generation" section of `docs/smaug-project-knowledge.md`.
8. `https://raw.githubusercontent.com/mdmeck/Smaug/main/pinescript/markers.pine` — must be last; it references `longEntry`/`shortEntry`/`exitSignal`, which only exist once step 7 defines them (Pine requires definition-before-use). It also converts those three from state to edge triggers, so the generated block must define them as plain conditions with no once-only guard of their own.

This is fetch-and-paste-verbatim, not a template to fill in — nothing in these files should be edited or "improved" by the routine at generation time.

## Why one script, not five

Pine v5 allows exactly one declaration statement (`indicator()`/`strategy()`/`library()`) per script, and `//@version=5` must be the literal first line. So these files are fragments — variable/function/plot definitions only, no version pragma or declaration of their own — meant to be concatenated under the single header in `header.pine`.

## Naming convention — required, prevents "already declared" errors

Pine has no per-file scoping. Every `var`, function, and local variable defined in a fragment must start with that file's prefix, with no exceptions (even one-off locals inside an `if` block):

| File | Prefix |
|---|---|
| `rsi_9_21.pine` | `rsi_` |
| `candles_1.pine` | `c1_` |
| `candles_2.pine` | `c2_` |
| `candles_3.pine` | `c3_` |
| `structure.pine` | `st_` — and these are the one exception to "the generated block recomputes every feature it references". Pivot confirmation lag is a silent-lookahead trap, so the block **consumes** `st_distSwingHighBps`, `st_distSwingLowBps`, `st_structureDir`, `st_bos`, `st_choch` rather than re-deriving them. |
| `markers.pine` | `mk_` for its own locals; consumes the unprefixed `longEntry`/`shortEntry`/`exitSignal` names the generated block defines. Don't rename those three identifiers anywhere. |

## Pattern label style

Candlestick pattern labels are deliberately distinct from the green/red/orange L/S/X entry/exit markers, so a pattern label is never mistaken for a trade signal:
- `label.new()` (filled bubble), not `plotchar()`.
- Uniform gray background, white text, `size.tiny`, regardless of bullish/bearish — direction is conveyed by placement (`label.style_label_up` below the bar for bullish, `label.style_label_down` above for bearish), not color.
- 2-3 character code + a `tooltip` with the full explanation.
- Only ever drawn on the current/forming candle (`barstate.islast`), never across history — each file deletes its previous label before drawing a new one so labels don't pile up across realtime ticks.
- ATR-scaled vertical offset per file (`candles_1` closest to the bar, `_2` further, `_3` furthest) so up to three simultaneous pattern labels never overlap each other or the L/S/X marker.

## Known limitation: RSI pane

`indicator(..., overlay=true)` is required (the price-based markers/labels need it), so `rsi_9_21.pine`'s plots initially render on the price scale. Pine gives no way for one script to declare mixed panes. After pasting the assembled script into TradingView, drag the RSI line's price-scale label down to move it to its own pane — a one-time manual step, not something fixable in-script.
