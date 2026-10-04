# Handoff: Zapflix "Floating glass window" redesign (3a Home, 3b Series detail)

## Overview
A redesign of the Zapflix single-page web UI (`templates/index.html`, FastAPI + Alpine.js + Tailwind 2.2 CDN). It targets a 3440×1440 ultrawide, dark theme. Every panel the app has today stays. This is a **layout and styling change only**: no new backend endpoints, and all Alpine state and methods (`search`, `openDetail`, `startDownload`, `toggleMonitor`, `selectMissing`, `cancelTask`, `retryFailure`, `checkNow`, `bottomTab`, …) are reused.

- **3a Home / Discover**: three-column glass window. Left: monitored shows + NAS downloads. Center: featured hero + trending poster row. Right: active tasks, failed files, logs/history.
- **3b Series detail**: same shell. The center column becomes the series header + season/episode picker.

## About the design files
`Zapflix Directions.dc.html` is a **design reference built in HTML**, not production code. Open it in a browser (keep `support.js` next to it); it's a pan/zoom canvas. Artboards **3a** and **3b** sit at the top; turns 1–2 are earlier explorations, ignore them.
Recreate 3a/3b inside the existing `templates/index.html`, keeping Alpine.js and Tailwind. Use Tailwind classes where they map cleanly, and put the glass and background recipes in the existing `<style>` block. Don't copy the HTML in wholesale; it uses static sample data.

## Fidelity
**High-fidelity.** Colors, radii, spacing, type sizes and copy are final. Poster/backdrop art in the mock is placeholder gradients; use the real Cinemeta `poster` / `background` URLs (`object-fit: cover`).

## Global shell
- **Page background** (replaces the Vanta.js background; remove the three.js/vanta scripts). It's the 21st.dev "elegant dark pattern", layered on `#000`, all layers `position:absolute; inset:0` (`position:fixed` in the app):
  1. Base: `radial-gradient(100% 100% at 0% 0%, rgb(46,46,46) 0%, rgb(0,0,0) 100%)`, masked by `radial-gradient(125% 100% at 0% 0%, #000 0%, rgba(0,0,0,.224) 88.2883%, transparent 100%)`.
  2. Inside it, 5 skewed streak layers (`opacity:.2`, `transform:skewX(45deg)`), each with background `linear-gradient(rgb(0,207,255) 0%, rgba(0,207,255,0) 100%)` and a different horizontal mask (copy the 5 `mask` strings verbatim from the source file).
  3. Grain: `https://cdn.21st.dev/assets/mirror/f5/f55dfc553c100e6da0ad95258a042b4100f0ff4bb03a5313d1f541984275e262.png`, repeat, `background-size:149.76px`, `opacity:.05`. Self-host it for offline NAS use.
  4. Dots: `radial-gradient(circle at 1px 1px, rgba(255,255,255,.5) 1px, transparent 0)`, `background-size:20px 20px`, `opacity:.2`.
- **App window**: inset 44px top/bottom, 56px left/right; `border-radius:36px`; `background:rgba(32,32,36,.55)`; `backdrop-filter:blur(40px) saturate(140%)`; `border:1px solid rgba(255,255,255,.12)`; `box-shadow:0 40px 120px rgba(0,0,0,.5)`; `padding:24px`; flex column, `gap:22px`.
- **Grid** (top bar and body use the same columns): `grid-template-columns: 520px minmax(0,1fr) 560px; gap:24px`.
- **Font**: Plus Jakarta Sans 400–800 (Google Fonts); logs use JetBrains Mono. Base 15px, color `#f4f4f5`.

## Top bar (height 56px)
- **Left (520px)**, space-between:
  - Logo "⚡ Zapflix": 22px/800, gradient text `linear-gradient(90deg,#60a5fa,#a855f7)`.
  - Under the logo, "NAS Edition · Real-Debrid · v1.11.0": 11px, `rgba(255,255,255,.55)`; version from `status.version`.
  - Tabs: "Discover", "🔥 Movies", "📺 Series", 14px, `padding:9px 12–16px`, radius 20. Active tab: `background:rgba(255,255,255,.14)`, weight 600. Inactive: `rgba(255,255,255,.65)`.
- **Center**: search, centered, `max-width:820px`, height 52, radius 26, `background:rgba(255,255,255,.09)`, `border:1px solid rgba(255,255,255,.14)`, `box-shadow:0 8px 30px rgba(0,0,0,.25)`, padding `0 8px 0 22px`.
  - Contents: "⌕" glyph, then the input (placeholder "Search movies or series (e.g. 'Teen Wolf')...", placeholder color `rgba(255,255,255,.55)`), then a "Search" button.
  - Search button: inside the field, height 38, radius 19, `#2563eb`, 14px/600. Label "Searching…" while `searching`.
- **Right**: 4 status chips + settings.
  - Chips: 12.5px, `padding:8px 12px`, radius 20, `background:rgba(255,255,255,.07)`, text `rgba(255,255,255,.8)`, 7px dot `#22c55e`. Use a grey dot `#6b7280` when inactive.
  - Chip labels come from the existing `status` bindings: Config Loaded / RD Active / aria2 → NAS / Jellyfin sync.
  - Settings: 44px circle, `rgba(255,255,255,.08)`, "⚙️", calls `openSettings()`.

## Panels (shared recipe)
- Default panel: radius 26, `background:rgba(255,255,255,.05)`, `border:1px solid rgba(255,255,255,.08)`, padding 20, flex column, gap 14–16.
- Panel title: 16px/600; the count after it is weight 500, `rgba(255,255,255,.5)`.
- Small pill buttons: 12.5px, `padding:6px 12px`, radius 14, `rgba(255,255,255,.08)`.
- Variants:
  - **Active tasks**: `background:rgba(59,130,246,.1)`, border `rgba(59,130,246,.3)`, title `#bfdbfe`.
  - **Failed files**: `background:rgba(239,68,68,.09)`, border `rgba(239,68,68,.3)`, title `#fecaca`, reason `#fca5a5`.
  - **Logs**: `background:rgba(0,0,0,.25)`.

## 3a Home: columns
**Left column (520px)**, flex column, gap 20:
1. **🔔 Monitored shows · N**, with a "Check now" pill (`checkNow()`).
   - Rows (gap 14): 56×84 poster (radius 8), name 15px/600.
   - Below the name: "last grab S06E20 · 12 Jul 20:29" (12.5px, .6 alpha), then "checked …" (12px, .42 alpha).
   - Row ends in a 34px round ✕ (`unmonitor(m)`).
   - Show the panel only if `monitored.length`.
2. **NAS downloads · aria2** (flex:1, overflow auto). Header counts, 12.5px: active `#60a5fa`, queued `#facc15`, done `#22c55e`, failed `#f87171`.
   - Rows: 40×60 poster thumb (radius 6), then filename (14px/500, ellipsis) with meta on the right (12.5px).
   - Meta by status: active "12.4 MB/s · 2m 10s" (`#9ca3af`), waiting "queued" (`#facc15`), complete "✓ 1.4 GB" (`#22c55e`), error "✗ failed" (`#f87171`).
   - Under each row, a 4px bar (radius 2, track `rgba(255,255,255,.1)`). Fill: `#3b82f6` active, `#22c55e` complete, `#ef4444` error; width `max(progress,2)%`.
   - The thumb is the poster of the matching title, if available; otherwise a neutral `#27272a` block.

**Center column**, flex column, gap 20:
1. **Hero** (height 640, radius 28, overflow hidden): backdrop image cover, plus overlay `linear-gradient(90deg, rgba(10,10,12,.92) 0%, rgba(10,10,12,.55) 45%, rgba(10,10,12,.1) 75%)`.
   - Poster on the right: `right:120px; top:60px; height:520px; aspect-ratio:2/3`, radius 14, `box-shadow:0 30px 80px rgba(0,0,0,.6)`.
   - Text block at `left:48px; bottom:48px; width:900px`, gap 16:
     - Chips (13px, `padding:6px 12px`, radius 14, `rgba(255,255,255,.12)`): "🔥 Trending", type chip (MOVIE `rgba(22,163,74,.85)` / SERIES `rgba(147,51,234,.85)`, 700), genre.
     - Title 60px/700, line-height 1.05, letter-spacing −.015em.
     - Meta 18px, .75 alpha.
     - Plot 17px/1.55, .68 alpha, max-width 640.
   - Actions:
     - Primary "⬇ Download Movie": height 56, radius 28, `#16a34a`, 17px/700.
     - Folder select "📁 movies ▼": height 56, radius 28, `border:1.5px solid rgba(255,255,255,.4)`, bound to `destFolder`.
     - For a series, the primary is "Open" → `openDetail(item)`.
   - Carousel ‹ › (48px circles, `rgba(255,255,255,.1)` / `.22`, bottom-right 40px): cycles the featured title through `discover.movies` (+ series). Suggested: first 5 items, auto-advance optional.
   - Clicking the hero poster or title opens `openDetail(item)`.
2. **Row header**: "🔥 Trending Movies" (22px/600) + Movies/Series segmented toggle (13px, track `rgba(255,255,255,.07)`, active `rgba(255,255,255,.14)`). Switches between `discover.movies` and `discover.series`; the top-bar tabs do the same.
3. **Poster cards**: `grid-template-columns: repeat(6, minmax(0,1fr)); gap:18px`. Each card is `aspect-ratio:2/3`, radius 20, overflow hidden, with the poster image and the overlay `linear-gradient(0deg, rgba(10,10,12,.95) 0%, rgba(10,10,12,.6) 26%, transparent 50%)`.
   - Type chip top-left (11px/700, letter-spacing .05em, `padding:4px 9px`, radius 12).
   - Bottom 16px inset: title 16px/600 (ellipsis), year 13px .6 alpha.
   - Right: when `inLibrary(item)`, a 40px circle `#22c55e` with "✓" (`#052e16`, 800).
   - Hover: keep the existing `.poster-card` lift (translateY(-4px), shadow). Click: `openDetail(item)`.
   - Search results (`results.length > 0`) replace the hero + row with the same cards in a wrapping grid.

**Right column (560px)**, flex column, gap 20:
1. **⏳ Active tasks · N** (only if `tasks.length`). Row: pulsing "●" `#60a5fa`, then name 15px/600.
   - Under the name: "series · started 21:10:44" (12.5px, .6 alpha).
   - "🛑 Cancel" button: 13px/600, `padding:9px 16px`, radius 20, `#b91c1c`. While cancelling: "Stopping…", `#374151`.
2. **Failed files · N** (only if `failures.length`). Header pill "🔁 Retry all" (text `#bfdbfe`).
   - Rows: filename 14px/500, reason "RD 34: too many requests" 12.5px `#fca5a5`.
   - Buttons: "Retry" (`#2563eb`, 12.5px/600, `padding:7px 14px`, radius 16) and "Dismiss" (`rgba(255,255,255,.08)`).
3. **Logs / History** (flex:1). Segmented tabs "SYSTEM LOGS" / "📜 HISTORY" (12px, letter-spacing .06em), bound to `bottomTab`; right side "Clear" or "N delivered".
   - Logs: JetBrains Mono 12.5px, line-height 1.9, single-line ellipsis. Color rules unchanged: ❌ → `#f87171`; ✅/🚀/✨ → `#4ade80`; else `#c7cdd4`.
   - History rows: emoji 🎬/📺, filename 13px, then "4 Oct 21:14:02 · 📁 series" (12px, .5 alpha); row divider `rgba(255,255,255,.05)`.

## 3b Series detail
Same shell; the search field shows the current query. The center column is replaced (`view === 'detail'`):
1. **Header card** (height 500, radius 28): backdrop cover, overlay `linear-gradient(90deg, rgba(10,10,12,.94) 0%, rgba(10,10,12,.6) 50%, rgba(10,10,12,.15) 80%)`.
   - Top-left pill "← Back to results" (14px, `padding:8px 16px`, radius 18, `rgba(255,255,255,.12)`) → `closeDetail()`.
   - Bottom row, inset 40, gap 36, align end:
     - **Poster**: 240px wide, 2:3, radius 14.
     - **Info**:
       - Chips: SERIES + one chip per genre.
       - Title 56px/700.
       - Meta 17px: year range, "★ 7.7" (`#facc15`, 700), runtime.
       - Plot 16px/1.55, .68 alpha, max-width 720.
     - **Action stack**, 300px wide, gap 10:
       - "⬇ Download selected": h54, radius 27, `#16a34a`, 700; disabled at 0 selected, "Queued ✓" after.
       - "🔔 Monitoring": h50, `rgba(202,138,4,.9)` when monitored. Otherwise "🔕 Monitor" on `rgba(255,255,255,.08)`.
       - Folder select "📁 series ▼": h50, 1.5px border, .35 alpha.
   - Movies: the action stack is just "⬇ Download Movie" + the folder select, and there is no episode panel.
2. **Episode panel** (default panel recipe, flex:1, scrolls).
   - Toolbar:
     - Pills "Select all" / "Select missing" / "Clear" (13.5px, `padding:8px 16px`, radius 18, `rgba(255,255,255,.08)`). Show "Select missing" only if `seriesInLibrary()`; the last-used one gets `.18` + 600.
     - Right side: "12 episode(s) selected" (14px, .65 alpha).
   - Season rows: radius 18, `rgba(255,255,255,.04)`, border `rgba(255,255,255,.06)`. Header (`padding:11px 18px`, click → `toggleExpand`):
     - Left: 18px checkbox (radius 5, `1.5px solid rgba(255,255,255,.35)`), "Season 3" (15px/600), "· 24 ep" (13px, .5 alpha).
     - Right (13px): "✓ 12/24" (`#4ade80`, delivered count from `epInLibrary`), "12 selected", ▲/▼.
   - Expanded episodes: `grid-template-columns: repeat(4, minmax(0,1fr)); gap:2px 8px; padding:0 12px 12px`. Each row (`padding:6px 10px`, radius 10, 13px):
     - Checkbox: checked = `#22c55e` fill with dark ✓; unchecked = outline.
     - "E01" in JetBrains Mono 12px, .45 alpha.
     - Name, with ellipsis; `#4ade80` if delivered.
     - Trailing green ✓ if delivered.
     - Selected rows get `background:rgba(34,197,94,.08)`.

## Interactions
- All buttons keep their current Alpine handlers. Add hover states: panels/pills +.04 white alpha, primary buttons slightly darker (`#15803d` green, `#1d4ed8` blue).
- Keep `@keyframes fade` for view switches; pulse dot: opacity 1→.35→1 over 1.6s.
- Hide empty panels as today (`x-show="... .length > 0"`).
- Below about 2000px wide, drop the right column under the center column; below about 1200px, stack all columns.

## Design tokens
- Text: `#f4f4f5`, secondary `rgba(255,255,255,.6–.68)`, tertiary `.42–.5`.
- Accents:
  - Blue: `#2563eb` (button), `#3b82f6` (bar), `#60a5fa` (text)
  - Green: `#16a34a` (button), `#22c55e` (✓/done), `#4ade80` (text)
  - Purple: `#9333ea` / `#a855f7` (series)
  - Red: `#b91c1c` (cancel), `#ef4444` (bar), `#f87171` / `#fca5a5` (text)
  - Yellow: `#facc15` (rating/queued), `#ca8a04` (monitoring)
- Radii: window 36, hero 28, panels 26, cards 20, season rows 18, pills 14–28 (fully rounded), thumbs 6–8.
- Spacing: 24 grid gaps, 20 panel padding/gaps, 14 row gaps.

## Files
- `Zapflix Directions.dc.html`: the design canvas (artboards `#3a`, `#3b`). Open it with `support.js` next to it.
- Target: `templates/index.html` in the Zapflix repo.
