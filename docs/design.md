# Morning Brief design system

Imported from the Claude Design project **"Dashboard redesign review"**
(`Market Dashboard.dc.html`, project `05f20b7c-8d4a-4fab-b65c-267facb81f81`) and
implemented in `webapp/src/App.jsx` as the `B` token object plus the
`WeekCalendar` / `TimelineItem` / `BriefPanel` / `SentimentBody` / `CasesBody`
components.

**Scope: the Morning Brief tab only.** The rest of the app still uses `T` (the
"Apex Forge" theme — Bebas Neue display, Lato, warm neutrals). The two are
deliberately not merged. This system is cooler, more spacious, and sets body
copy roughly 10% larger; applying it halfway would read as a bug rather than a
choice. If the other tabs adopt it, `B` should be folded into `T` wholesale.

## Palette

All colors are `oklch()`. The neutrals carry a slight blue cast (hue 260–265,
chroma 0.005–0.008), which is what separates this from the warm grays elsewhere
in the app.

| Token | Value | Used for |
|---|---|---|
| `bg` | `oklch(0.15 0.008 265)` | page canvas |
| `surface` | `oklch(0.19 0.008 265)` | cards |
| `sunken` | `oklch(0.17 0.008 265)` | inset stat tiles, footer strips |
| `edge` | `oklch(0.3 0.008 265)` | card borders, timeline rail |
| `edgeSoft` | `oklch(0.28 0.008 265)` | dividers *inside* a card |

Text is one ramp, applied by descending importance — headings, body, labels,
disclaimer:

`ink 0.95` → `body 0.92` → `muted 0.85` → `dim 0.6` → `faint 0.55` → `ghost 0.45`

Accents:

| Token | Value | Meaning |
|---|---|---|
| `amber` / `amberDim` / `amberBright` | `oklch(0.75 0.15 55)` and neighbors | today, high-impact, WATCH, staleness |
| `green` / `greenText` | `oklch(0.75 0.16 155)` | bullish |
| `red` / `redDeep` | `oklch(0.72 0.18 25)` | bearish |
| `blue` / `blueDot` | `oklch(0.78 0.1 235)` | tickers, ordinary econ events |
| `purple` | `oklch(0.72 0.11 300)` | earnings |

Semantic tints are the accent at **6%** for a full-bleed wash (the bull/bear
halves) and **14%** for a pill background (the tone chip). Never a solid fill —
the only solid accent in the system is the TODAY pill, which inverts to dark
text on amber.

## Type

**Inter** for everything except numbers and labels; **IBM Plex Mono** for those.
Both loaded in `webapp/index.html`.

| Role | Spec |
|---|---|
| Page heading | Inter 36 / 800 / `-0.02em` |
| Card title | Inter 17 / 700 |
| Day number | Inter 18 / 800 / `-0.02em` |
| Body | Inter 14.5 / line-height 1.5–1.65 |
| List item | Inter 13.5 / line-height 1.3 |
| Eyebrow label | Plex Mono 10.5–11.5 / 600 / `0.07em`–`0.14em`, uppercase |
| Data value | Plex Mono 11.5–15 / 500–700 |

The eyebrow is the system's signature: a small, wide-tracked, uppercase mono
label sitting above the thing it names. Every section header and stat label is
one. `eyebrow(color, size)` in `App.jsx` builds it.

Tracking runs opposite to size — display type is negative (`-0.02em`), labels
are strongly positive (up to `0.14em`).

## Shape and space

- **Radius**: 16 on cards, 10 on inset tiles, 20 (pill) on chips.
- **Card padding**: `28px 30px`. Day cards are tighter: `10px 16px` header,
  `4px 18px 14px` body.
- **Gaps**: 28 between major sections, 20 between panels, 18 between day cards,
  16 between tiles, 10 between an item's marker and its text.
- Borders are always 1px. Elevation appears exactly once — the today card's
  `0 16px 40px -16px oklch(0.6 0.14 55 / 0.3)`, an amber-tinted glow rather than
  a neutral shadow.

## Motifs

**Timeline rail.** A day's events are one sequence: a marker per item with a 1px
hairline connecting it to the next, omitted on the last so the rail ends clean.
This replaced three separately-bordered groups divided by dashed rules — same
ordering (BMO → econ → AMC), read as one day unfolding. The source uses a 6px
colored dot as the marker; Smaug uses an emoji (see deviations).

**Gauge.** Fear & Greed is a gradient rail (red → amber → green) with a white
needle at the value. The needle carries a 3px ring in the *card* color, so it
reads as a notch cut through the rail rather than a dot sitting on it.

**Split panel.** Bull and bear are full-bleed tinted halves separated by a
border, not two columns floating in a padded card. The panel's WATCH line is a
footer strip on `sunken`, edge to edge.

**Today.** Three simultaneous signals — warm card background, amber border,
amber glow, plus the inverted pill. Deliberately unmissable; it's the only
element in the design allowed that much emphasis.

## Deviations from the source

The design was drawn against one week of sample data. Four changes were needed
to make it hold real data:

1. **Emoji markers instead of colored dots.** The rail marker is 💸 for
   earnings and 📁 for econ, hue-rotated red when `impact: high` — the Forex
   Factory convention the trader already reads. The source has no emoji and
   encodes category purely as dot color, which can't express impact without
   adding a second label. The marker column is fixed at 16px so the hairline
   still centers under every row.
2. **Company names truncate, event names wrap.** Earnings names ellipsis so they
   stay inline with the ticker, with the full text on `title` — the source lets
   them wrap, which puts the name on its own line and was a specific earlier
   complaint. Econ event names have no ticker to sit beside and are often long,
   so they wrap as drawn.
3. **Staleness coloring.** `LAST RUN` turns amber when the brief wasn't written
   today. The source has no notion of a stale brief.
4. **Bounded height.** Day cards cap at 340px with internal scroll, and the week
   grid scrolls horizontally below ~1060px. The source assumes a wide viewport
   and a light earnings day.

Everything else — colors, type scale, spacing, radii, the four motifs above — is
the source verbatim.
