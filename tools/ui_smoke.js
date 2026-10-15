// UI smoke check for the design system: every page, every theme state.
//
//   python tools/explore_preview.py --demo        # in another terminal
//   npx -y -p playwright node tools/ui_smoke.js
//
// Fails (exit 1) on a JavaScript error, a missing aurora layer, a theme that
// is not dark when nothing is stored, text tokens below WCAG AA, a backdrop
// filter on album grid cards, or an aurora that still moves under reduced
// motion.
const { chromium } = require("playwright");

const BASE = process.env.PREVIEW_URL || "http://127.0.0.1:5099";
const PAGES = [
  "/", "/explore", "/explore?album=MPREb_statusMiss1",
  "/explore?artist=UCUDVBtnOQi4c7E8jebpjc9Q", "/downloads", "/insights",
  "/logs", "/settings", "/youtube", "/setup",
];
// null = nothing stored (first visit).
const STATES = [
  { theme: null }, { theme: "light" }, { theme: "dark" },
  { theme: "dark", reducedMotion: "reduce" },
];
const TEXT_TOKENS = ["--text-1", "--text-2", "--text-3", "--accent-text"];

const failures = [];
const fail = (where, msg) => failures.push(`${where}: ${msg}`);

async function check(browser, path, state) {
  const label = `${path} [${state.theme || "unset"}${state.reducedMotion ? ", reduced motion" : ""}]`;
  const ctx = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    reducedMotion: state.reducedMotion || "no-preference",
  });
  await ctx.addInitScript(t => {
    try {
      if (t) localStorage.setItem("theme", t);
      else localStorage.removeItem("theme");
    } catch (e) {}
  }, state.theme);
  const page = await ctx.newPage();
  page.on("pageerror", e => fail(label, `page error: ${e.message}`));
  page.on("console", m => {
    if (m.type() === "error") fail(label, `console error: ${m.text()}`);
  });
  await page.goto(BASE + path, { waitUntil: "networkidle" });
  await page.waitForTimeout(400);

  const r = await page.evaluate(tokens => {
    const rootStyle = getComputedStyle(document.documentElement);
    const lum = rgb => {
      const [r, g, b] = rgb.map(v => {
        v /= 255;
        return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    // Resolve any CSS colour (hex, rgb, color-mix…) to rgb via a probe.
    const probe = document.createElement("i");
    probe.style.transition = "none"; // an inherited colour transition would report the old value
    document.body.appendChild(probe);
    const toRgb = value => {
      probe.style.color = "";
      probe.style.color = value;
      const m = getComputedStyle(probe).color.match(/[\d.]+/g) || [];
      return m.slice(0, 3).map(Number);
    };
    const bg = toRgb(rootStyle.getPropertyValue("--bg-elevated").trim());
    const contrast = {};
    for (const t of tokens) {
      const fg = toRgb(rootStyle.getPropertyValue(t).trim());
      const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
      contrast[t] = (a + 0.05) / (b + 0.05);
    }
    probe.remove();
    const aura = document.querySelector(".app-aura");
    const blobs = aura ? [...aura.children] : [];
    return {
      body: getComputedStyle(document.body).backgroundColor,
      aura: !!aura,
      blobsMoving: blobs.some(b => {
        const s = getComputedStyle(b);
        return s.animationName !== "none" && s.animationPlayState !== "paused";
      }),
      albumBlur: [...document.querySelectorAll(".ui-album")].filter(a => {
        const f = getComputedStyle(a).backdropFilter;
        return f && f !== "none";
      }).length,
      contrast,
    };
  }, TEXT_TOKENS);

  if (!r.aura) fail(label, "no .app-aura layer");
  if (!state.theme && r.body !== "rgb(7, 7, 12)") {
    fail(label, `nothing stored but body is ${r.body}, not the dark base rgb(7, 7, 12)`);
  }
  for (const [t, ratio] of Object.entries(r.contrast)) {
    if (!(ratio >= 4.5)) fail(label, `${t} contrast ${ratio.toFixed(2)} < 4.5 on --bg-elevated`);
  }
  if (r.albumBlur) fail(label, `${r.albumBlur} album cards have a backdrop filter`);
  if (state.reducedMotion && r.blobsMoving) fail(label, "aurora still animates under reduced motion");
  await ctx.close();
}

(async () => {
  const browser = await chromium.launch();
  for (const state of STATES) {
    for (const path of PAGES) await check(browser, path, state);
  }
  await browser.close();
  if (failures.length) {
    console.log([...new Set(failures)].join("\n"));
    console.log(`\n${failures.length} check(s) failed`);
    process.exit(1);
  }
  console.log(`UI smoke: ${PAGES.length * STATES.length} page loads, all checks passed`);
})().catch(e => {
  console.error(e);
  process.exit(1);
});
