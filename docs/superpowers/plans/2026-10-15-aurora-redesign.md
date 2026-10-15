# Aurora Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the web UI a premium 2026 "aurora" look — dark-first, glass surfaces, a vivid violet accent, motion, and a slow background that takes its colours from the album cover in focus — without changing behaviour.

**Architecture:** Values and components live in the existing design system (`static/components.css`). Two new small scripts hold the new behaviour: `static/aura.js` (palette extraction + the aurora layer, `UI.setAura`) and `static/motion.js` (stagger, tilt, count-up). Their pure functions run under plain `node` for tests. Templates only gain a few `UI.setAura` / `UI.countTo` calls and class tweaks.

**Tech Stack:** CSS custom properties + `@property`, vanilla JS (ES5-style IIFEs like `app.js`), Jinja templates, pytest, node (tests of the pure JS functions), Playwright via `npx` for the UI smoke check and screenshots.

**Spec:** `docs/superpowers/specs/2026-10-15-aurora-redesign-design.md`

## Global Constraints

- No Python behaviour, route, URL or data change. `.venv/bin/python -m pytest tests/ -q` stays green after every task.
- Design-system rules from `CLAUDE.md` hold: no per-page colour variables, no theme toggles/navigation/icon fonts outside the shared files; dark values declared in **both** the `@media (prefers-color-scheme: dark) :root:not([data-theme="light"])` block and `:root[data-theme="dark"]`.
- Existing token names keep working (page CSS consumes them); add tokens, change values.
- Exact values: dark base `#07070c`; `--accent: #7c5cff`; `--accent-gradient: linear-gradient(120deg, #7c5cff, #4f8bff 55%, #e04fd8)`; default aurora palette `#7c5cff`, `#4f8bff`, `#e04fd8`; glass blur `blur(24px) saturate(160%)`; card radius 20px, control radius 12px; aurora cycle 30–40 s; aurora colour transition ~1.2 s; stagger ~30 ms capped at 12 items; tilt ≤ 4°; button press `scale(.97)`.
- Backdrop blur only on containers (sidebar, tab bar, top bar, cards, stat tiles, modals/sheets, mini player, bulk bar, toasts) — never on `.ui-album` grid items.
- All new animation uses `transform`/`opacity`, stops under `prefers-reduced-motion: reduce`, and the aurora pauses while `document.hidden`.
- Text tokens (`--text-1/2/3`, `--accent-text`, semantic text colours) ≥ WCAG AA (4.5:1) on `--bg-elevated` in both themes.
- No downloaded fonts, no WebGL, no new pip/npm dependency in the app.
- Commits: author from the repo git config (Angrido); end messages with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; no Angrido co-author line.

## Review Focus

1. A user who previously chose **automatic** theme had no stored key (`setTheme("auto")` removed it) → after this change they see dark; re-choosing automatic must stick (now stored as `"auto"`). Pinned in Task 2's smoke assertion.
2. A cover whose host sends no CORS header (Lidarr remote URLs) → `UI.setAura` falls back to the default palette with no console error. Pinned in Task 3 (node test for the null path; smoke check that pages with remote covers log no error).
3. A grey/black-and-white or near-black cover → never an invisible or muddy aurora: the palette ignores unsaturated pixels and falls back to defaults. Pinned in Task 3 tests.
4. Library with thousands of albums → no per-card backdrop filter, tilt listeners attach only on fine pointers. Pinned in Task 4 (CSS rule check in smoke: `.ui-album` computed `backdrop-filter` is `none`).
5. Reduced motion → no aurora drift, no stagger, no shimmer. Pinned in Task 4's smoke run with `reducedMotion: "reduce"`.

---

### Task 1: UI smoke check

**Files:**
- Create: `tools/ui_smoke.js`
- Modify: `CLAUDE.md` (Utility Tools list: one line for `ui_smoke.js`)

**Interfaces:**
- Produces: `npx -y -p playwright node tools/ui_smoke.js` (env `PREVIEW_URL`, default `http://127.0.0.1:5099`), exit code 0/1, one line per failed check. Later tasks run it against `tools/explore_preview.py --demo`.

- [ ] **Step 1: Write `tools/ui_smoke.js`** (CommonJS, `require("playwright")`, same style as `tools/readme_screenshots.js`). Pages: `/`, `/explore`, `/explore?album=MPREb_statusMiss1`, `/explore?artist=UCUDVBtnOQi4c7E8jebpjc9Q`, `/downloads`, `/insights`, `/logs`, `/settings`, `/youtube`, `/setup`. For each page in three theme states — no stored key, `"light"`, `"dark"` — and once with `reducedMotion: "reduce"` (dark):
  - fail on any `pageerror` or `console` message of type `error`;
  - assert `document.querySelector(".app-aura")` exists;
  - with no stored key: `getComputedStyle(document.body).backgroundColor` is `rgb(7, 7, 12)` (dark default);
  - assert WCAG contrast ≥ 4.5 for `--text-1`, `--text-2`, `--text-3`, `--accent-text` against `--bg-elevated` (read with `getComputedStyle(document.documentElement).getPropertyValue`, compute relative luminance in the page);
  - on `/`: every `.ui-album` has computed `backdrop-filter` `none`;
  - under reduced motion: `.app-aura` blobs report `animation-name` `none` (or `animation-play-state` `paused`).
- [ ] **Step 2: Run it against the current UI**

Run (preview running: `.venv/bin/python tools/explore_preview.py --demo`): `npx -y -p playwright node tools/ui_smoke.js`
Expected: FAIL — `.app-aura` missing, dark default not `rgb(7, 7, 12)`. Record the list; no page errors are expected (if there are, note them: they are existing bugs, not this plan's).

- [ ] **Step 3: Commit** `tools/ui_smoke.js`, `CLAUDE.md` — "Add a Playwright UI smoke check for the redesign".

### Task 2: Dark-first theme, colour, materials, type

**Files:**
- Modify: `static/components.css` (tokens in `:root`, `:root[data-theme="dark"]`, the dark media block; components `.ui-card`, `.ui-stat`, `.app-sidebar`, `.app-tab*`/tab bar, `.app-topbar`, `.ui-modal*`, `.ui-player`, `.ui-bulkbar`, `.ui-toast*`, `.ui-btn-primary`, `.ui-progress-bar`, `.app-nav-indicator`, headings/`.t-*` title classes)
- Modify: `static/app.js:8-60` (theme), `templates/_head.html` (pre-paint script, `theme-color`), `app.py:282-283` (manifest colours)

**Interfaces:**
- Produces tokens: `--accent-gradient`, `--glass-bg`, `--glass-border`, `--glass-blur`, `--glow-accent`, `--aura-1`, `--aura-2`, `--aura-3` (defaults = default palette), plus retuned existing tokens.
- Produces theme API (unchanged names): `UI.getTheme()` returns `"dark"` when nothing is stored; `UI.setTheme("auto")` now stores `"auto"`.

- [ ] **Step 1: Theme default.** `readTheme()` returns the stored `"light"|"dark"|"auto"`, and `"dark"` when nothing is stored. `setTheme(pref)` stores every value, including `"auto"`. Cycle order stays `auto → light → dark`. The `_head.html` pre-paint script mirrors it: no key → `data-theme="dark"`; `"auto"` → follow `prefers-color-scheme`. `theme-color` dark `#07070c`, light the new light base. Manifest `background_color`/`theme_color` `#07070c`.
- [ ] **Step 2: Tokens.** New values per spec §1 in all three token blocks (light `:root`, dark media block, `[data-theme="dark"]`). Light base: milky white with a faint lilac cast; pick values that keep the Global Constraints' AA rule.
- [ ] **Step 3: Materials and type.** Glass (`background: var(--glass-bg); backdrop-filter: var(--glass-blur); box-shadow: inset 0 1px 0 var(--glass-border), var(--shadow-2)`) on the container list above; opaque `--bg-elevated` on `.ui-album`. Radii up (cards 20px, controls 12px). Titles `letter-spacing: -0.02em`, one size step up for display/title tokens; `font-variant-numeric: tabular-nums` on `.ui-stat-value`, durations and counters. `--accent-gradient` on `.ui-btn-primary`, `.ui-progress-bar`, `.app-nav-indicator`.
- [ ] **Step 4: Verify**

Run: `.venv/bin/python -m pytest tests/ -q` → all pass. Restart the demo preview, then `npx -y -p playwright node tools/ui_smoke.js` → the dark-default and contrast checks pass; only `.app-aura` checks still fail.
Run: `npx -y -p playwright node tools/readme_screenshots.js` into a scratch copy (or revert `docs/screenshots` after looking) and inspect Library, album, Downloads, Settings in both themes for broken layout.

- [ ] **Step 5: Commit** — "Redesign: dark-first theme, aurora palette tokens, glass materials".

### Task 3: Aurora layer (`static/aura.js`)

**Files:**
- Create: `static/aura.js`, `tests/js/aura.test.js`, `tests/test_frontend_js.py`
- Modify: `templates/_head.html` (load `/static/aura.js?v={{ APP_VERSION }}` with `defer`, after `app.js`), `static/components.css` (`.app-aura` + blobs, `@property --aura-1/2/3` as `<color>`, light-theme intensity, reduced-motion)

**Interfaces:**
- Consumes: `window.UI` from `app.js`; tokens `--aura-1..3` from Task 2.
- Produces: `UI.auraPalette(rgba: Uint8ClampedArray|number[], count = 3) -> string[]` (exactly `count` `#rrggbb`), `UI.setAura(url: string|null) -> Promise<void>`, `UI.AURA_DEFAULTS = ["#7c5cff", "#4f8bff", "#e04fd8"]`. In node, `require("../../static/aura.js")` returns `{ auraPalette, AURA_DEFAULTS }` (file ends with `if (typeof module !== "undefined") module.exports = …` and skips all DOM work when `document` is undefined).

- [ ] **Step 1: Write `tests/js/aura.test.js`** (plain `node`, `assert`, no framework; exit non-zero on failure):
  - all-grey 24×24 image → `auraPalette(px)` deep-equals `AURA_DEFAULTS`;
  - near-black vivid pixels (HSL l < 0.12) → defaults;
  - fully transparent pixels ignored;
  - solid pure red → `[0]` has hue within 15° of 0, lightness in [0.45, 0.62], saturation ≥ 0.55; `[1]`, `[2]` = `AURA_DEFAULTS[1]`, `[2]`;
  - half red / half blue → first two colours are one red-ish (hue ≤ 15° or ≥ 345°) and one blue-ish (hue 200°–250°), most-frequent first; third = `AURA_DEFAULTS[2]`;
  - returns `count` items for `count` = 1 and 3.
- [ ] **Step 2: Write `tests/test_frontend_js.py`**: `test_aura_palette` runs `node tests/js/aura.test.js` with `subprocess.run(..., check=False)`, asserts `returncode == 0` (print stdout/stderr on failure); `pytest.skip` when `shutil.which("node")` is None.
- [ ] **Step 3: Run** `.venv/bin/python -m pytest tests/test_frontend_js.py -q` → FAIL (`static/aura.js` missing).
- [ ] **Step 4: Implement `auraPalette`.** For each pixel with alpha ≥ 128: to HSL; skip s < 0.25, l < 0.12, l > 0.92; bin by hue into 12 bins weighted by s; take bins by total weight, highest first; colour = weighted mean RGB of the bin, then s = max(s, 0.55), l clamped to [0.45, 0.62]; fill missing slots with `AURA_DEFAULTS` at the same index.
- [ ] **Step 5: Implement the layer.** On ready, prepend `<div class="app-aura" aria-hidden="true">` with three `<span>` blobs to `<body>`. `setAura(url)`: falsy → set defaults; else `new Image()`, `crossOrigin = "anonymous"`, on load draw into a 24×24 canvas, `getImageData` inside `try` (tainted → defaults), set `--aura-1..3` on `document.documentElement.style`; `onerror` → defaults; ignore results of superseded calls (keep a request counter). Toggle `.is-paused` on `.app-aura` on `visibilitychange`. CSS: blobs `position: fixed`, `z-index: -1`, `pointer-events: none`, radial gradients from `--aura-*`, `filter: blur(…)`, drift keyframes 30–40 s on `transform`; `transition` on the registered colours ~1.2 s; lower opacity in light theme; under reduced motion no animation and no colour transition; `.is-paused` sets `animation-play-state: paused`.
- [ ] **Step 6: Verify** — `pytest tests/test_frontend_js.py -q` PASS; full suite green; smoke check fully green.
- [ ] **Step 7: Commit** — "Redesign: aurora background that takes its colours from the cover".

### Task 4: Motion (`static/motion.js`)

**Files:**
- Create: `static/motion.js`, `tests/js/motion.test.js`
- Modify: `tests/test_frontend_js.py` (add `test_motion_helpers`), `templates/_head.html` (load `motion.js` like `aura.js`), `static/components.css` (enter/stagger, tilt + sheen, button press, progress shimmer + glow, live pulse, reduced-motion block)

**Interfaces:**
- Produces: `UI.tiltAngles(px, py, width, height, max = 4) -> {rx, ry}` (degrees, each clamped to ±max, 0 at centre; `px/py` relative to the element), `UI.countTo(el: Element, value: number, ms = 600)` (sets final text immediately under reduced motion), `UI.stagger(container: Element)` (sets `--i` = 0..11 on the first 12 children, adds `.is-entering`). In node, `require("../../static/motion.js")` returns `{ tiltAngles }`.

- [ ] **Step 1: Write `tests/js/motion.test.js`**: `tiltAngles(50, 50, 100, 100)` → `{rx: 0, ry: 0}`; corner `(0, 0, 100, 100)` → both magnitudes = 4; `(1000, -1000, 100, 100)` clamped to ±4; `max = 2` respected; signs: pointer right of centre → `ry > 0`, pointer above centre → `rx > 0`.
- [ ] **Step 2: Add `test_motion_helpers`** to `tests/test_frontend_js.py` (same pattern as Task 3); run → FAIL.
- [ ] **Step 3: Implement `static/motion.js`.** Tilt listeners attach to `.ui-album` only when `matchMedia("(hover: hover) and (pointer: fine)")` matches and reduced motion is off; delegated `pointermove`/`pointerleave` on the grid container, `requestAnimationFrame`-throttled, writing `--rx`, `--ry`, `--mx`, `--my` (sheen position) on the card. `UI.stagger` runs on ready for `[data-stagger]` containers. CSS per spec §3 (enter: fade + 8px rise, `animation-delay: calc(var(--i) * 30ms)`; press `scale(.97)` with an overshoot easing; shimmer on `.ui-progress-bar` while active; halo pulse on live dots / downloading states; glow under `.ui-album` on hover: a `box-shadow` of `var(--glow, var(--glow-accent))` — `--glow` is unset in this plan, so every card glows in the accent colour).
- [ ] **Step 4: Verify** — node tests pass via pytest; full suite green; smoke check green (including reduced-motion and `.ui-album` backdrop checks).
- [ ] **Step 5: Commit** — "Redesign: page-enter, tilt, shimmer and count-up motion".

### Task 5: Key moments

**Files:**
- Modify: `templates/index.html` (hero header, status tiles, now-downloading card; `setNowPlayingCover` at ~2022 calls `UI.setAura(url)`; tile values via `UI.countTo`; `data-stagger` on the album grid container), `templates/downloads.html` (now-downloading card; `buildCurrentView` at ~741 calls `UI.setAura(safeCover)`; `UI.setAura(null)` when idle), `templates/explore.html` (`render()` at ~1085 calls `UI.setAura(null)` before dispatch; `renderAlbum` ~1182 → `UI.setAura(album.thumbnail)`; `renderArtist` ~1333 → `UI.setAura(a.banner || a.thumbnail)`; `loadTrack` ~1774 → `UI.setAura(art)`; hero glow; large reflective album cover; player glass + gradient seek bar), `static/components.css` (`.ui-nowplaying*`, `.ui-player`, nav/tab bar polish).

**Interfaces:**
- Consumes: `UI.setAura`, `UI.countTo`, `UI.stagger` / `data-stagger` (Tasks 3–4).

- [ ] **Step 1: Library + Downloads** — markup/class changes and the calls above. No change to data flow, polling or SSE handling.
- [ ] **Step 2: Explore + player** — calls above; class/markup changes only.
- [ ] **Step 3: Verify** — full suite green; smoke check green; with the demo preview run `npx -y -p playwright node tools/readme_screenshots.js` and inspect every screenshot (both themes, phones): aurora coloured by the demo covers on album/Downloads, nothing overlapping, text readable.
- [ ] **Step 4: Commit** — "Redesign: Library, Explore, Downloads and player key moments".

### Task 6: Docs and screenshots

**Files:**
- Modify: `CLAUDE.md` (Templates: new tokens, glass rule, `.app-aura`, `static/aura.js`/`motion.js` and their `UI.*` functions, dark-first theme storage, `tests/js/` + `tests/test_frontend_js.py`), `docs/screenshots/*.webp` (regenerated), `README.md` only if a caption no longer matches.

- [ ] **Step 1: Update `CLAUDE.md`.**
- [ ] **Step 2: Regenerate screenshots** — demo preview + `npx -y -p playwright node tools/readme_screenshots.js`; inspect.
- [ ] **Step 3: Verify** — full pytest suite green; smoke check green.
- [ ] **Step 4: Commit** — "Redesign: docs and README screenshots".
