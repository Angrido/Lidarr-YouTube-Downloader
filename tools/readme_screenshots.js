// Regenerates the README screenshots in docs/screenshots/.
//
//   python tools/explore_preview.py --demo        # in another terminal
//   npx -y -p playwright node tools/readme_screenshots.js
//
// Each page is captured in both themes, then framed in a browser window (or
// three phones) with rounded corners and a soft shadow on a transparent
// background, so the images sit cleanly on GitHub's light and dark themes.
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const BASE = process.env.PREVIEW_URL || "http://127.0.0.1:5099";
const OUT = path.join(__dirname, "..", "docs", "screenshots");
const ALBUM = "/explore?album=MPREb_statusMiss1";

const DESKTOP = {
  explore: "/explore",
  library: "/",
  album: ALBUM,
  artist: "/explore?artist=UCUDVBtnOQi4c7E8jebpjc9Q",
  downloads: "/downloads",
  insights: "/insights",
  settings: "/settings",
};
const PHONES = ["/explore", ALBUM, "/downloads"];

const THEMES = {
  light: { bar: "#f2f2f4", line: "rgba(0,0,0,.10)", pill: "#ffffff", text: "#6e6e73", phone: "#1d1d1f" },
  dark: { bar: "#1f1f22", line: "rgba(255,255,255,.12)", pill: "#2c2c30", text: "#9a9aa0", phone: "#2a2a2e" },
};

async function capture(browser, theme, url, viewport) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, colorScheme: theme });
  await ctx.addInitScript(t => localStorage.setItem("theme", t), theme);
  const page = await ctx.newPage();
  await page.goto(BASE + url, { waitUntil: "networkidle" });
  await page.addStyleTag({ content: "::-webkit-scrollbar{display:none} *{caret-color:transparent}" });
  await page.waitForTimeout(1200);
  // On a phone the album hero sits below the page header: start the shot at the breadcrumb.
  if (viewport.width < 600 && url.includes("album=")) {
    await page.evaluate(() => window.scrollTo(0, document.querySelector(".ex-crumbs").getBoundingClientRect().top + scrollY - 8));
    await page.waitForTimeout(300);
  }
  const png = await page.screenshot();
  await ctx.close();
  return "data:image/png;base64," + png.toString("base64");
}

const windowHtml = (shot, t) => `
  <div class="win">
    <div class="bar"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i>
      <span class="url">lidarr-yt.local</span></div>
    <img src="${shot}">
  </div>
  <style>
    .win{width:1280px;border-radius:14px;overflow:hidden;background:${t.bar};
      box-shadow:0 0 0 1px ${t.line},0 30px 60px -18px rgba(0,0,0,.40),0 12px 24px -12px rgba(0,0,0,.20)}
    .bar{height:38px;display:flex;align-items:center;gap:8px;padding:0 14px;position:relative;border-bottom:1px solid ${t.line}}
    .bar i{width:12px;height:12px;border-radius:50%}
    .url{position:absolute;left:50%;transform:translateX(-50%);padding:4px 64px;border-radius:7px;
      background:${t.pill};color:${t.text};font:500 12px -apple-system,system-ui,sans-serif}
    img{display:block;width:1280px}
  </style>`;

const phonesHtml = (shots, t) => `
  <div class="row">${shots.map((s, i) => `<div class="phone${i === 1 ? " mid" : ""}"><img src="${s}"></div>`).join("")}</div>
  <style>
    .row{display:flex;gap:56px;align-items:flex-start;padding-top:40px}
    .phone{width:390px;padding:11px;border-radius:56px;background:${t.phone};
      box-shadow:inset 0 0 0 1.5px rgba(255,255,255,.10),0 0 0 1px ${t.line},0 34px 60px -20px rgba(0,0,0,.45)}
    .phone.mid{transform:translateY(-40px)}
    img{display:block;width:390px;border-radius:45px}
  </style>`;

async function frame(browser, html, file) {
  const ctx = await browser.newContext({ viewport: { width: 800, height: 600 }, deviceScaleFactor: 1.5 });
  const page = await ctx.newPage();
  await page.setContent(`<body style="margin:0;background:transparent"><div id="stage" style="display:inline-block;padding:56px">${html}</div></body>`);
  await page.waitForFunction(() => [...document.images].every(i => i.complete));
  const png = await page.locator("#stage").screenshot({ omitBackground: true });
  // Chromium encodes WebP with alpha; Playwright itself only writes PNG/JPEG.
  const webp = await page.evaluate(async src => {
    const img = new Image();
    img.src = src;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = img.naturalWidth;
    c.height = img.naturalHeight;
    c.getContext("2d").drawImage(img, 0, 0);
    return c.toDataURL("image/webp", 0.86).split(",")[1];
  }, "data:image/png;base64," + png.toString("base64"));
  fs.writeFileSync(path.join(OUT, file), Buffer.from(webp, "base64"));
  await ctx.close();
  console.log("wrote", file);
}

(async () => {
  const browser = await chromium.launch();
  for (const theme of Object.keys(THEMES)) {
    const t = THEMES[theme];
    for (const [name, url] of Object.entries(DESKTOP)) {
      const shot = await capture(browser, theme, url, { width: 1280, height: 800 });
      await frame(browser, windowHtml(shot, t), `${name}-${theme}.webp`);
    }
    const shots = [];
    for (const url of PHONES) shots.push(await capture(browser, theme, url, { width: 390, height: 844 }));
    await frame(browser, phonesHtml(shots, t), `mobile-${theme}.webp`);
  }
  await browser.close();
})().catch(e => {
  console.error(e);
  process.exit(1);
});
