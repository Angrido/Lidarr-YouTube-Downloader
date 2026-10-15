// Run with: node tests/js/aura.test.js (tests/test_frontend_js.py runs it).
const assert = require("assert");
const { auraPalette, AURA_DEFAULTS } = require("../../static/aura.js");

const SIZE = 24 * 24;

function image(pixelAt) {
  const px = new Uint8ClampedArray(SIZE * 4);
  for (let i = 0; i < SIZE; i++) px.set(pixelAt(i), i * 4);
  return px;
}

function hsl(hex) {
  const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  const d = max - min;
  const s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
  let h = 0;
  if (d) {
    if (max === r) h = 60 * (((g - b) / d) % 6);
    else if (max === g) h = 60 * ((b - r) / d + 2);
    else h = 60 * ((r - g) / d + 4);
  }
  return { h: (h + 360) % 360, s, l };
}

const tests = {
  "grey picture falls back to the defaults"() {
    assert.deepStrictEqual(auraPalette(image(() => [128, 128, 128, 255])), AURA_DEFAULTS);
  },
  "near-black vivid picture falls back to the defaults"() {
    assert.deepStrictEqual(auraPalette(image(() => [40, 0, 0, 255])), AURA_DEFAULTS);
  },
  "transparent pixels are ignored"() {
    assert.deepStrictEqual(auraPalette(image(() => [255, 0, 0, 0])), AURA_DEFAULTS);
  },
  "solid red gives one clamped red, then defaults"() {
    const out = auraPalette(image(() => [255, 0, 0, 255]));
    const c = hsl(out[0]);
    assert.ok(c.h <= 15 || c.h >= 345, `hue ${c.h}`);
    assert.ok(c.l >= 0.45 - 0.01 && c.l <= 0.62 + 0.01, `lightness ${c.l}`);
    assert.ok(c.s >= 0.55 - 0.01, `saturation ${c.s}`);
    assert.deepStrictEqual(out.slice(1), AURA_DEFAULTS.slice(1));
  },
  "red and blue: most frequent first, third is a default"() {
    // 60% red, 40% blue.
    const out = auraPalette(image(i => (i < SIZE * 0.6 ? [230, 20, 30, 255] : [30, 60, 230, 255])));
    const red = hsl(out[0]), blue = hsl(out[1]);
    assert.ok(red.h <= 15 || red.h >= 345, `first hue ${red.h}`);
    assert.ok(blue.h >= 200 && blue.h <= 250, `second hue ${blue.h}`);
    assert.strictEqual(out[2], AURA_DEFAULTS[2]);
  },
  "returns exactly count colours"() {
    assert.strictEqual(auraPalette(image(() => [255, 0, 0, 255]), 1).length, 1);
    assert.strictEqual(auraPalette(image(() => [128, 128, 128, 255]), 3).length, 3);
    assert.ok(auraPalette(image(() => [255, 0, 0, 255])).every(c => /^#[0-9a-f]{6}$/.test(c)));
  },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failed++;
    console.log(`FAIL ${name}: ${e.message}`);
  }
}
process.exit(failed ? 1 : 0);
