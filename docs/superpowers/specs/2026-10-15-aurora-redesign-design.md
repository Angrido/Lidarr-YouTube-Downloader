# Aurora redesign — design spec

**Status:** approved in conversation (direction, scope, approach A), 2026-10-15.

## Goal

Give the whole web UI a premium 2026 look with dynamic effects and rich
colour, driven by the music itself: a slow "aurora" background that takes
its colours from the album cover in focus, glass surfaces, a vivid accent
and spring-like motion. Behaviour, URLs, routes and data stay exactly as
they are.

## Decisions

| Topic | Decision |
|---|---|
| Direction | **Aurora from covers**: dark-first, light theme kept and refined |
| Scope | **Design system + key moments**: tokens/components/motion in `static/components.css` + `static/app.js` reach every page; targeted touch-ups on the most visible screens. No new layouts or navigation. |
| Aurora implementation | **A — CSS aurora + palette extracted in the browser.** No WebGL, no server-side palette, no new dependency. |
| Fonts | System stack kept (no downloaded fonts: the app runs offline in Docker). |
| Default theme | Dark when the user never chose one. A stored `light`/`dark`/`auto` preference is honoured unchanged. |

The design-system rules in `CLAUDE.md` still hold: no per-page colour
variables, no theme toggles, navigation or icon fonts outside the shared
files. Dark values stay declared twice (media query guarded by
`:root:not([data-theme="light"])`, and `:root[data-theme="dark"]`).

## 1. Colour and materials (`static/components.css`)

**Surfaces.** Dark base `#07070c` (blue-black), elevated surfaces as
translucent layers over the aurora; light base a milky white with a faint
lilac cast. Existing token names are kept so the ~3,300 lines of page CSS
that consume them keep working; only values change, plus new tokens:

- `--accent: #7c5cff` (electric violet), `--accent-hover`, `--accent-text`,
  `--accent-soft*`, `--focus-ring` retuned for both themes.
- `--accent-gradient: linear-gradient(120deg, #7c5cff, #4f8bff 55%, #e04fd8)`
  — the signature, used only on the primary button, progress bars and the
  active nav indicator.
- Semantic colours (`--success/warning/danger/info` and `-mark/-soft`)
  more saturated; all text pairs stay **WCAG AA** in both themes.
- `--glass-bg`, `--glass-border` (1px top highlight), `--glass-blur`
  (`blur(24px) saturate(160%)`), `--glow-accent` (coloured shadow).
- Shadows deeper and slightly tinted; radii up a step (cards 20px,
  controls 12px).

**Glass** applies to containers: sidebar, phone tab bar, top bar, cards,
stat tiles, modals/sheets, mini player, bulk bar, toasts. **Not** to every
album card in a grid (a 5,000-album Library must not stack thousands of
backdrop filters): grid items get an opaque elevated surface.

**Type.** Same font stacks. Display/title sizes up one step, tighter
tracking (`-0.02em` on titles), `font-variant-numeric: tabular-nums` on
stats, counters and durations.

## 2. Aurora layer

- `app.js` prepends one `<div class="app-aura" aria-hidden="true">` to
  `<body>` on start (no template edits). It is `position: fixed`, behind
  everything (`z-index: -1`, `pointer-events: none`), and holds 3 large
  blurred radial blobs coloured by `--aura-1`, `--aura-2`, `--aura-3`.
- Blobs drift in a slow 30–40 s loop using `transform` only.
- `UI.setAura(url)`:
  1. loads the image with `crossOrigin = "anonymous"`;
  2. draws it into a 24×24 canvas, reads pixels;
  3. `UI.auraPalette(pixels)` (pure function) picks 3 dominant, vivid
     colours (skips near-black/white/grey, clamps lightness and boosts
     saturation so text on glass stays legible);
  4. sets `--aura-1..3` on `:root`; CSS transitions them over ~1.2 s
     (`@property`-registered colours so they animate).
- Any failure (tainted canvas, 404, no URL) → `UI.setAura(null)` → the
  default palette (violet / blue / magenta). Same-origin `/api/thumbnail`
  covers (Explore) always work; Lidarr/Cover Art Archive covers work when
  their host sends CORS headers.
- Intensity: strong in dark, much softer in light (lower blob opacity).
- Paused while `document.hidden`; with `prefers-reduced-motion` the blobs
  do not move and colour changes apply without transition.

**Callers** (the only template changes for the aurora):

| Where | Cover |
|---|---|
| Explore album page, artist page | the album / artist art |
| Library + Downloads "now downloading" card | the album being downloaded |
| Mini player (Explore) | the playing track's art, on track change |
| Leaving those views | `UI.setAura(null)` |

## 3. Motion (`components.css` + `app.js`, ~120 lines of JS)

- **Page enter:** content fades up 8px; cards/tiles stagger ~30 ms apart,
  capped at 12 items (CSS `--i` index set by a tiny helper).
- **Album cards:** hover tilt ≤ 4° with a moving sheen, only on
  `(hover: hover) and (pointer: fine)`; a soft cover-coloured glow under
  the card (from the already-loaded image via the same palette helper,
  only on hover, cached per card).
- **Progress bars:** accent gradient + travelling shimmer while active;
  glow on the active bar.
- **Buttons:** spring press (`scale(.97)` with an overshooting easing).
- **Stats:** numbers count up briefly when their value changes.
- **Live elements:** "live" dot and downloading states pulse with a halo.
- **Nav:** the active item gets an indicator that slides between items.

Everything uses `transform`/`opacity`, and `prefers-reduced-motion`
collapses all durations (existing rule extended to the new animations).

## 4. Key moments (targeted template touch-ups)

Class and markup tweaks only; no JS behaviour, endpoint or data changes.

- **Library (`index.html`):** glass hero header; richer status tiles; the
  "now downloading" card full-width with large cover, aurora from that
  cover, glowing progress bar.
- **Explore (`explore.html`):** new-releases hero with cover glow; album
  page with a large reflective cover and the aurora taking its colours;
  mini player with stronger glass and gradient seek bar.
- **Downloads (`downloads.html`):** now-downloading card styled like the
  Library one; animated per-track states.
- **Navigation (`_nav.html` via CSS):** sliding active indicator; glass
  phone tab bar.
- Other pages (settings, logs, insights, youtube, setup) change through
  the design system only. Insights chart colours move to the new palette
  through existing tokens.

## 5. Accessibility, performance, verification

- Text on glass ≥ AA in both themes; the aurora sits under surfaces with
  enough opacity; extracted colours are lightness-clamped.
- Visible focus ring in both themes; touch target sizes unchanged.
- Backdrop blur only on containers; aurora animates `transform`/`opacity`
  only and pauses in hidden tabs.
- `theme-color` meta and the pre-paint script in `_head.html` follow the
  new dark default and base colours; `app.js` `effectiveTheme()` agrees
  with the pre-paint script.

**Verification**
- Full pytest suite stays green (no Python behaviour changes).
- New test for `UI.auraPalette` (pure function) run with `node` from the
  test suite (skipped when node is absent) or as a CI step.
- Playwright screenshots from `tools/explore_preview.py --demo` before and
  after: Library, Explore home, album, Downloads, Settings; light + dark;
  desktop + phone. Checked by eye for contrast and layout regressions.
- README screenshots regenerated with `tools/readme_screenshots.js`.
- `CLAUDE.md` Templates section updated (new tokens, `.app-aura`,
  `UI.setAura`, glass rule).

## Out of scope

New layouts or navigation, downloaded fonts, WebGL, server-side palette
extraction, extending `/api/thumbnail` to Lidarr cover hosts (possible
follow-up if the default aurora shows too often in Library/Downloads).
