# Frontend: design system, static JS, templates

<!-- Moved verbatim from CLAUDE.md; keep it in sync with the code. -->

## Templates

Every page is built on one design system; **don't add per-page colour
variables, theme toggles, navigation or Font Awesome**.

- `static/components.css` — the design system. Tokens on `:root` (4px
  spacing scale `--sp-*`, radii `--r-*`, type scale `--text-*`, layered
  surfaces `--bg-base/-elevated/-elevated-2/-sunken/-overlay`, text
  `--text-1/2/3`, one accent `--accent*`, semantic `--success/warning/
  danger/info` with `-mark` and `-soft` variants, shadows `--shadow-1..3`,
  motion `--ease` = `cubic-bezier(0.32, 0.72, 0, 1)`, `--ease-spring` and
  `--dur-*`; the "Aurora" look adds `--accent-gradient` (accent → hue −45°
  → hue +55°, used only on the primary button, progress bars and the
  nav indicator). **Every accent colour derives from `--accent-base`** (the
  user's `ui_accent`) with `color-mix()` / `oklch(from …)`: hover, soft
  fills, focus ring, glows, `--accent-text` (lightness capped so any colour
  stays AA as text) and `--text-on-accent` (flips to near-black on light
  accents) — never hard-code the violet again. Also `--aura-1..3` (registered with `@property` so they
  cross-fade), `--aura-opacity`, and glass `--glass-bg/-border/-blur`,
  `--glow-accent`). Glass (backdrop blur) is for containers only — sidebar,
  tab bar, cards, stat tiles, now-downloading card, player, bulk bar —
  never `.ui-album` grid items (a 5,000-album Library). Dark
  values are declared twice: under `@media (prefers-color-scheme: dark)`
  guarded by `:root:not([data-theme="light"])`, and under
  `:root[data-theme="dark"]` (manual override) — change both. Components
  are `ui-*` (buttons, badges, fields, `.ui-switch`, `.ui-segmented`,
  `.ui-check`, chips, cards, iOS grouped lists `.ui-group`, stat tiles,
  responsive tables that become cards on phones, lists with a coloured
  status edge, album cards, per-track progress `.ui-track`, now-playing
  card, empty states, skeletons, callouts, modals that become bottom
  sheets on phones, stacked toasts, bulk bar, drop zone, steps, tabs,
  tooltip, horizontal shelves `.ui-shelf` with `-wide`/`-rows`/`-round`
  variants, and the fixed mini player `.ui-player` — `body.has-player`
  pads `.app-main` so it never covers content). `prefers-reduced-motion`
  collapses all durations. Contrast
  targets WCAG AA in both themes. Under `(max-width: 720px), (pointer:
  coarse)` controls grow to touch-sized hit targets (36–42px); keep new
  controls on the shared classes so they inherit this. Frosted surfaces use
  `--bg-overlay` (high opacity on purpose, so text behind never competes).
- `static/app.js` — `window.UI`: theme (`auto`/`light`/`dark`, stored in
  `localStorage.theme`; **dark when nothing is stored**, and `auto` is
  stored explicitly — the pre-paint script in `_head.html` mirrors this;
  `themechange` event, `theme-color` meta kept in sync), `UI.icon(name)`, `UI.escape`, `UI.toast(msg, {type, action})`,
  `UI.openModal/closeModal` (Esc, backdrop, focus trap/restore),
  `UI.confirm({...})` → Promise, `UI.guard(btn, fn)` (in-flight guard +
  spinner), `UI.fetchJSON`, `UI.poll(fn, ms)` (never overlaps, pauses in
  hidden tabs), nav badges from `/api/stats` (`queued`, `active`), and the
  `/` shortcut that focuses the page's `input[type="search"]`.
- `static/aura.js` — the aurora background: prepends `.app-aura` (three
  blurred blobs drifting 30–40 s, paused in hidden tabs, still under
  reduced motion) to `<body>` and adds `UI.setAura(url|null)`, which
  reduces the cover to 24×24 in a canvas, takes `UI.auraPalette()`'s
  dominant vivid colours (grey/near-black/near-white ignored, clamped for
  legibility) and sets `--aura-1..3`; an unreadable cover (no CORS, 404)
  or `null` → the defaults derived from the accent (inline `--aura-*`
  removed). `<html data-aura>` (`ui_aura_mode`): only `cover` follows
  covers, `accent` keeps the defaults, `off` hides the layer. Callers: Library/Downloads
  now-downloading card, Explore album/artist pages, the mini player;
  other Explore views reset it.
- `static/motion.js` — cover tilt (≤ 4°, fine pointers only, one delegated
  frame-throttled listener, sheen at `--mx/--my`), `UI.stagger(el)` (first
  12 children rise in once; `[data-stagger]` on load) and
  `UI.countTo(el, value, ms, format)`. `auraPalette` and `tiltAngles` are
  pure and tested under node by `tests/test_frontend_js.py`
  (`tests/js/*.test.js`).
- `static/queue-eta.js` — `QueueEta.describe(data)` turns `/api/queue/eta`
  into headline / detail / hints / explanation (pure, tested under node);
  loaded only by Downloads and Library.
- Page motion in `components.css`: cross-document View Transitions
  (`@view-transition`, only without reduced motion) cross-fade the content
  while `.app-sidebar` / `.app-tabbar` stay put (named, no old snapshot), and
  `.app-content` cards / groups / shelves rise in on scroll with a `view()`
  timeline (`ui-reveal`, under `@supports`; anything visible on load is
  already at rest). It skips `.ui-card-interactive` (its hover transform)
  and `.is-entering > *` (the stagger). Never put it on `.ui-album`. Explore
  home shelves, the album tracklist and the Insights tiles are staggered.
- `static/icons.svg` — SVG symbol sprite (`#i-<name>`), used as
  `<svg class="ico"><use href="/static/icons.svg#i-name"></use></svg>`.
- `templates/_head.html` (meta, manifest, CSS, pre-paint theme script,
  app.js), `templates/_nav.html` (sidebar + phone tab bar; set
  `active_page` before including), `templates/_topbar.html` (phone header).
- `templates/index.html` — Library: status tiles, missing-albums grid /
  list / table, search and sort. Per-album checkboxes drive the floating
  **bulk-action bar** (`/api/download/queue/bulk`); `selectedAlbums` Set
  survives view re-renders.
- `templates/downloads.html` — now-downloading card with per-track states,
  reorderable queue, history filtered by outcome and audio quality.
- `templates/insights.html` — analytics (`/insights`); fetches
  `/api/insights?days=N` and draws dependency-free inline-SVG charts
  rendered at the measured container width (fixed-size labels, tooltips,
  legend, table view). Aggregation is `models.get_insights()`.
- `templates/logs.html` — download log entries with type filters and
  inline retry/dismiss/unban.
- `templates/settings.html` — configuration in anchored sections, incl.
  **Appearance** first (accent swatches + native colour picker, aurora
  Covers / My colour / Off; instant saves via `saveAppearance()`, live
  preview by setting `--accent-base` / `data-aura` on `<html>`, rolled back
  on error) and
  **Backup & Restore** (`/api/backup/export` streams a `sqlite3`-consistent
  copy of the DB; `/api/backup/import` validates the upload, atomically
  replaces the DB, and restarts — refused while any download is active).
- `templates/import.html` — **Import** (`/import`): paste a YouTube, YouTube Music, Spotify, Deezer, Apple Music or ListenBrainz link (pasting a URL fetches it; `?url=` deep-links and is kept in the address bar), pick tracks, import; a finished import stays on screen with *Import another link*. The API stays under `/api/youtube/*`.
- The Library accepts `/?q=` to pre-fill its search; its header and empty
  state link to Explore.
- `templates/explore.html` — **Explore** (`/explore`), a client-routed
  page: `?album=`, `?artist=`, `?playlist=`, `?mood=&title=`, `?q=` are deep
  links (history API, `a[data-route]` links). Home shelves (`.ui-shelf`):
  new-releases hero, top songs with a country `<select>` (stored in
  `localStorage.exploreCountry`), trending, new albums, top artists
  (`.ui-shelf-round`), charts, videos, mood chips. Album pages render the
  match as a badge plus the contextual CTA and the candidate picker; artist
  pages the *Add artist* flow (a picker modal when ambiguous); playlists
  selection + bulk import. Lidarr add options reuse
  `localStorage.addMusicPrefs` (editable from the sliders button). The
  `.ui-player` mini player streams previews from `/api/youtube/stream`
  (volume in `localStorage.explorePlayerVolume`, Media Session handlers) and
  lives as long as the page. Search suggests as you type (combobox, arrow
  keys, `/` focuses it). On phones Explore takes YouTube's tab-bar slot;
  `_nav.html` skips `import` there and highlights Explore on `/import`.
- `templates/setup.html` — first-run setup wizard (`/setup`, no nav); the
  dashboard redirects unconfigured instances here (client-side, skippable).
- `static/favicon.svg` + PNG icons (`apple-touch-icon.png`, `icon-192.png`,
  `icon-512.png`, `icon-maskable-512.png`).
- PWA: `/manifest.webmanifest` and `/sw.js` are served from the app root;
  `_head.html` links the manifest and `app.js` registers the (no-op-fetch)
  service worker so the UI is installable.
